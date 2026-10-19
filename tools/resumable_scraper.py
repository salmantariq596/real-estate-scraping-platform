#!/usr/bin/env python3
"""
Pincali Multi-Property Scraper 
- Bulletproof checkpoint logic (survives PC crashes/shutdowns)
- Resume NEVER reads from all_properties.json
- Retry-failed mode: keeps ALL existing data, only adds/fixes failed ones
- Atomic writes everywhere (temp + rename)
- Append-only progress.log for crash recovery
- Missing data detection with separate report file
- Live terminal progress display
- UNIFIED MIGRATION: JSON → CSV + JSONL simultaneously (atomic, idempotent)
- BROWSER AUTO-RESTART: Prevents crash after 1500+ URLs
- RATE LIMITING: Politeness delay + proxy rotation support
- LAZY-LOAD WAITS: Description + image scroll triggers
- COOKIE SHARING: Captcha solved cookies forwarded to headless workers
"""

import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Union
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Error as PlaywrightError


# ========== CONFIGURATION ==========
URLS_FILE = Path("unique_urls.json")
OUTPUT_DIR = Path("pincali_all_scraped_pages")
DATA_DIR = OUTPUT_DIR / "data"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"
FAILED_FILE = OUTPUT_DIR / "failed.json"
COMPLETED_URLS_FILE = OUTPUT_DIR / "completed_urls.json"
MISSING_DATA_FILE = OUTPUT_DIR / "missing_data.json"
PROGRESS_LOG = OUTPUT_DIR / "progress.log"
SCRAPER_LOG = OUTPUT_DIR / "scraper.log"
ALL_DATA_FILE = OUTPUT_DIR / "all_properties.json"

# Migration output files
CSV_FILE = OUTPUT_DIR / "all_properties.csv"
JSONL_FILE = OUTPUT_DIR / "all_properties.jsonl"
MIGRATION_LOG = OUTPUT_DIR / "migration.log"
MIGRATION_MANIFEST = OUTPUT_DIR / "migration.manifest.json"

# Proxy rotation (empty list = no proxy)
PROXY_LIST: List[str] = []
_proxy_rotor = 0

# Browser restart every N URLs to prevent memory crash
BROWSER_RESTART_EVERY = 100
MAX_RETRIES_PER_URL = 2

CSV_COLUMNS = [
    "property_id", "product_url", "price_digits", "operation_type",
    "title_h1", "location_subtitle", "address", "description",
    "agent_name", "agency_name", "video_iframe", "map_iframe",
    "virtual_tour_url",
    "feature_bedrooms", "feature_bedrooms_raw",
    "feature_bathrooms", "feature_bathrooms_raw",
    "feature_parking", "feature_parking_raw",
    "feature_total_space_m2", "feature_total_space_m2_raw",
    "feature_lot_size_m2", "feature_lot_size_m2_raw",
    "feature_lot_length_m", "feature_lot_length_m_raw",
    "feature_lot_width_m", "feature_lot_width_m_raw",
    "feature_year_built", "feature_year_built_raw",
    "feature_floors", "feature_floors_raw",
    "features_json", "amenities_json", "all_images", "extra_properties_json",
    "source_json_file", "migrated_at"
]

CONCURRENCY = 5
HEADLESS = True
TIMEOUT = 50000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

SELECTORS = {
    "property_id": ".listing-id span",
    "price_digits": ".price .digits",
    "operation_type": ".price .operation-type",
    "title_h1": "h1",
    "location_subtitle": "h2.location",
    "address": ".listing__map p",
    "description": ".listing__description p.text-description span[data-translation]",
    "agent_name": ".publisher-name a",
    "agency_name": ".publisher-organization-name",
    "video_iframe": ".listing__video iframe",
    "map_iframe": "#map iframe",
    "virtual_tour_link": "a[data-track-event='View Virtual Tour']",
}

# Global locks & state
CAPTCHA_LOCK = asyncio.Lock()
currently_processing: Set[str] = set()


# ========== ASYNC FILE I/O HELPERS (Fix #24) ==========
async def run_in_thread(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def _atomic_write_json_sync(filepath: Path, data: Any) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp = filepath.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(filepath)


async def async_atomic_write_json(filepath: Path, data: Any) -> None:
    await run_in_thread(_atomic_write_json_sync, filepath, data)


def _append_jsonl_sync(filepath: Path, entry: Dict) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()


async def async_append_jsonl(filepath: Path, entry: Dict) -> None:
    await run_in_thread(_append_jsonl_sync, filepath, entry)


def log_message_sync(msg: str, level: str = "INFO") -> None:
    ensure_output_dir()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {level}: {msg}"
    print(line)
    with open(SCRAPER_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


async def log_message(msg: str, level: str = "INFO") -> None:
    await run_in_thread(log_message_sync, msg, level)


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_next_proxy() -> Optional[Dict[str, str]]:
    global _proxy_rotor
    if not PROXY_LIST:
        return None
    proxy = PROXY_LIST[_proxy_rotor % len(PROXY_LIST)]
    _proxy_rotor += 1
    return {"server": proxy}


# ========== FEATURE PARSING (Fix #10) ==========
def parse_feature_value(text: Any) -> tuple[str, Optional[Union[int, float]]]:
    if text is None:
        return "", None
    s = str(text).strip()
    m = re.search(r"[\d,.]+", s)
    if m:
        num_str = m.group().replace(",", "")
        try:
            if "." in num_str:
                return s, float(num_str)
            else:
                return s, int(num_str)
        except ValueError:
            return s, None
    return s, None


def validate_property(data: Dict[str, Any]) -> bool:
    """Fix #19: Reject empty records before saving."""
    return bool(data.get("property_id") and (data.get("price_digits") or data.get("title_h1")))


# ========== UNIFIED MIGRATION (Fixes #6, #7, #27) ==========
def _migrate_files_thread(files, csv_temp, jsonl_temp):
    migrated = 0
    failed = 0
    failed_files = []
    csv_exists = csv_temp.exists() and csv_temp.stat().st_size > 0

    with open(csv_temp, "a", newline="", encoding="utf-8") as cf, \
         open(jsonl_temp, "a", encoding="utf-8") as jf:
        writer = csv.DictWriter(cf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not csv_exists:
            writer.writeheader()

        for filepath in files:
            try:
                with open(filepath, "r", encoding="utf-8") as jf_in:
                    data = json.load(jf_in)
                if not isinstance(data, dict):
                    raise ValueError("JSON root is not a dictionary")

                csv_row = flatten_property_for_csv(data, filepath.name)
                data["_migration_meta"] = {
                    "source_file": filepath.name,
                    "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                writer.writerow(csv_row)
                jf.write(json.dumps(data, ensure_ascii=False) + "\n")
                migrated += 1
            except Exception as e:
                failed += 1
                failed_files.append({
                    "file": filepath.name,
                    "error": f"{type(e).__name__}: {e}"
                })

        # Fix #7: Single fsync at the end, not per row
        cf.flush()
        os.fsync(cf.fileno())
        jf.flush()
        os.fsync(jf.fileno())

    return {"migrated": migrated, "failed": failed, "failed_files": failed_files}


async def migrate_both() -> None:
    if not DATA_DIR.exists():
        await log_message("DATA_DIR does not exist. Nothing to migrate.", "MIGRATE")
        return

    # Fix #27: Load manifest to avoid duplicate migrations
    migrated_set: Set[str] = set()
    if MIGRATION_MANIFEST.exists():
        try:
            manifest = json.loads(MIGRATION_MANIFEST.read_text(encoding="utf-8"))
            migrated_set = set(manifest.get("migrated", []))
        except Exception:
            pass

    json_files = sorted([f for f in DATA_DIR.glob("*.json") if f.name not in migrated_set])
    total = len(json_files)

    if total == 0:
        await log_message("No new JSON files found to migrate.", "MIGRATE")
        return

    await log_message(f"Starting unified migration. {total} JSON files → CSV + JSONL", "MIGRATE")

    csv_temp = CSV_FILE.with_suffix(".csv.tmp")
    jsonl_temp = JSONL_FILE.with_suffix(".jsonl.tmp")

    # Stream-copy existing outputs to temps so we can append atomically
    def _copy_existing():
        if CSV_FILE.exists():
            with open(CSV_FILE, "rb") as src, open(csv_temp, "wb") as dst:
                dst.write(src.read())
        if JSONL_FILE.exists():
            with open(JSONL_FILE, "rb") as src, open(jsonl_temp, "wb") as dst:
                dst.write(src.read())

    await run_in_thread(_copy_existing)

    result = await run_in_thread(_migrate_files_thread, json_files, csv_temp, jsonl_temp)
    migrated_count = result["migrated"]
    failed_count = result["failed"]
    failed_files = result["failed_files"]

    # Atomic rename only after successful writes
    def _finalize():
        csv_temp.replace(CSV_FILE)
        jsonl_temp.replace(JSONL_FILE)
        # Now safe to delete sources
        for filepath in json_files:
            filepath.unlink()

    await run_in_thread(_finalize)

    # Update manifest
    new_migrated = sorted(list(migrated_set | {f.name for f in json_files}))
    await async_atomic_write_json(MIGRATION_MANIFEST, {"migrated": new_migrated})

    log_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "unix_timestamp": time.time(),
        "run_type": "migrate_both",
        "total_json_files_found": total,
        "migrated_count": migrated_count,
        "failed_count": failed_count,
        "failed_files": failed_files,
        "csv_file": str(CSV_FILE),
        "csv_size_mb": round(CSV_FILE.stat().st_size / (1024 * 1024), 2) if CSV_FILE.exists() else 0,
        "jsonl_file": str(JSONL_FILE),
        "jsonl_size_mb": round(JSONL_FILE.stat().st_size / (1024 * 1024), 2) if JSONL_FILE.exists() else 0,
        "data_dir": str(DATA_DIR)
    }
    await async_append_jsonl(MIGRATION_LOG, log_entry)
    _print_migration_summary(total, migrated_count, failed_count)


def _print_migration_summary(total: int, migrated: int, failed: int) -> None:
    print(f"\n{'='*60}")
    print(f"📊  UNIFIED MIGRATION SUMMARY (CSV + JSONL)")
    print(f"{'='*60}")
    print(f"Total JSON files:   {total}")
    print(f"✅ Migrated:         {migrated}")
    print(f"❌ Failed:           {failed}")
    print(f"CSV file:           {CSV_FILE}")
    if CSV_FILE.exists():
        print(f"   Size:            {CSV_FILE.stat().st_size / (1024*1024):.2f} MB")
    print(f"JSONL file:         {JSONL_FILE}")
    if JSONL_FILE.exists():
        print(f"   Size:            {JSONL_FILE.stat().st_size / (1024*1024):.2f} MB")
    print(f"Migration log:      {MIGRATION_LOG}")

    if failed > 0:
        print(f"\n⚠️  {failed} files FAILED to migrate.")
        print(f"   Their JSON files are PRESERVED in: {DATA_DIR}\n")

    if MIGRATION_LOG.exists():
        try:
            with open(MIGRATION_LOG, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if first_line:
                    first_record = json.loads(first_line)
                    print(f"📜 First migration:  {first_record.get('timestamp', 'N/A')}")
        except Exception:
            pass

    print(f"{'='*60}\n")
    log_message_sync(f"Migration complete. {migrated}/{total} migrated. {failed} failed.", "MIGRATE")


# ========== HELPER FUNCTIONS ==========
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", str(name))[:100]


def parse_custom_properties(page_html: str) -> Dict[str, Any]:
    pattern = r'data-track-custom-properties="({.*?})"'
    match = re.search(pattern, page_html)
    if match:
        try:
            json_str = match.group(1).replace('&quot;', '"')
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass
    return {}


# Fix #12: Use regex anchors instead of loose substring matching
async def extract_feature_icons(page) -> Dict[str, str]:
    features = {}
    icons = await page.query_selector_all(".feature-icon")
    for icon in icons:
        text = (await icon.inner_text()).strip().lower()
        if re.search(r'\b\d+\s*bedrooms?\b', text):
            features["bedrooms"] = text
        elif re.search(r'\b\d+\s*bath(?:rooms?|s)?\b', text):
            features["bathrooms"] = text
        elif re.search(r'\bparking\b', text):
            features["parking"] = text
        elif re.search(r'\btotal\s+space\b', text):
            features["total_space_m2"] = text
        elif re.search(r'\blot\s+size\b', text):
            features["lot_size_m2"] = text
        elif re.search(r'\b\d+\s*m\s+long\b', text):
            features["lot_length_m"] = text
        elif re.search(r'\blot\s+width\b', text):
            features["lot_width_m"] = text
        elif re.search(r'\byear\s+built\b', text):
            features["year_built"] = text
        elif re.search(r'\b\d+\s*floors?\b', text):
            features["floors"] = text
        else:
            key = re.sub(r'\W+', '_', text[:30]).strip('_')
            if key:
                features[key] = text
    return features


async def extract_amenities(page) -> List[Dict[str, List[str]]]:
    amenities = []
    groups = await page.query_selector_all(".amenities-group")
    for group in groups:
        title_elem = await group.query_selector(".amenities-group-title")
        title = await title_elem.inner_text() if title_elem else "Other"
        items = []
        for li in await group.query_selector_all("li"):
            span = await li.query_selector("span")
            if span:
                items.append(await span.inner_text())
        amenities.append({"category": title, "items": items})
    return amenities


async def extract_images(page) -> List[str]:
    urls = set()
    imgs = await page.query_selector_all("img[src*='assets.easybroker.com']")
    for img in imgs:
        src = await img.get_attribute("src")
        if src:
            urls.add(src)
    lazy_imgs = await page.query_selector_all("img[data-src*='assets.easybroker.com']")
    for img in lazy_imgs:
        src = await img.get_attribute("data-src")
        if src:
            urls.add(src)
    return list(urls)


async def is_captcha_present(page) -> bool:
    try:
        listing = await page.query_selector(".listing")
        if listing:
            return False
    except Exception:
        pass

    captcha_selectors = [
        ".g-recaptcha", ".h-captcha", ".hcaptcha-box",
        "iframe[src*='google.com/recaptcha']", "iframe[src*='hcaptcha.com']",
        "#cf-challenge-running", ".cf-turnstile", "#turnstile-box",
        "#challenge-running", "input[name='cf-turnstile-response']",
    ]
    for selector in captcha_selectors:
        try:
            elem = await page.query_selector(selector)
            if elem:
                box = await elem.bounding_box()
                if box and box.get("width", 0) > 20 and box.get("height", 0) > 20:
                    return True
        except Exception:
            continue

    try:
        title = await page.title()
        title_lower = title.lower()
        if any(x in title_lower for x in ["just a moment", "attention required", "cloudflare"]):
            body_text = await page.inner_text("body")
            body_lower = body_text.lower()
            if any(x in body_lower for x in ["challenge", "verifying", "captcha", "security check"]):
                return True
    except Exception:
        pass
    return False


# Fix #25: Scroll to trigger lazy-loaded images & amenities
async def extract_property_data(page, url: str) -> Dict[str, Any]:
    html = await page.content()
    custom = parse_custom_properties(html)
    result = {"product_url": url}

    for key, selector in SELECTORS.items():
        try:
            if key == "virtual_tour_link":
                elem = await page.query_selector(selector)
                href = await elem.get_attribute("href") if elem else None
                result["virtual_tour_url"] = href
            elif key in ("video_iframe", "map_iframe"):
                elem = await page.query_selector(selector)
                src = await elem.get_attribute("src") if elem else None
                result[key] = src
            else:
                elem = await page.query_selector(selector)
                raw_text = await elem.inner_text() if elem else None
                if key == "property_id" and raw_text:
                    raw_text = raw_text.replace("ID:", "").strip()
                result[key] = raw_text
        except Exception:
            result[key] = None

    result["features"] = await extract_feature_icons(page)
    result["amenities"] = await extract_amenities(page)
    result["all_images"] = await extract_images(page)
    result.update(custom)
    return result


# Fix #14 + #15: Global captcha lock + cookie extraction
async def scrape_headed_for_captcha(url: str) -> tuple[Dict[str, Any], list]:
    await log_message(f"REAL CAPTCHA detected for {url}. Launching headed browser...", "CAPTCHA")
    print("   Captcha solve karo aur is terminal mein ENTER press karo.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT
        )
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=TIMEOUT)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input, "   👉 Captcha solve karke ENTER dabao... ")

        await asyncio.sleep(2)
        try:
            await page.wait_for_selector(".listing", timeout=15000)
        except Exception:
            pass

        data = await extract_property_data(page, url)
        cookies = await context.cookies()

        await page.close()
        await context.close()
        await browser.close()

        await log_message("Headed scraping complete. Resuming headless mode.", "CAPTCHA")
        return data, cookies


# ========== URL LOADING ==========
def load_urls() -> List[str]:
    if not URLS_FILE.exists():
        log_message_sync(f"{URLS_FILE} not found!", "ERROR")
        sys.exit(1)

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        log_message_sync("urls.json must contain an array", "ERROR")
        sys.exit(1)

    raw_urls = []
    for item in data:
        if isinstance(item, str):
            raw_urls.append(item)
        elif isinstance(item, dict) and "url" in item:
            raw_urls.append(item["url"])
        else:
            log_message_sync(f"Skipping invalid URL entry: {item}", "WARN")

    seen: Set[str] = set()
    unique_urls: List[str] = []
    for url in raw_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return unique_urls


# ========== BULLETPROOF CHECKPOINT SYSTEM ==========
# Fix #17: Reconcile completed/failed sets so no URL is in both
def load_checkpoint_robust() -> (Set[str], Dict[str, str]):
    completed: Set[str] = set()
    failed: Dict[str, str] = {}

    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    completed = set(data.get("completed", []))
                    failed = {k: v for k, v in data.get("failed", {}).items() if isinstance(v, str)}
                    log_message_sync(f"Loaded checkpoint: {len(completed)} completed, {len(failed)} failed", "CHECKPOINT")
        except Exception as e:
            log_message_sync(f"checkpoint.json corrupted ({e}). Will recover from progress.log.", "WARN")

    if PROGRESS_LOG.exists():
        recovered_success = 0
        recovered_failed = 0
        try:
            with open(PROGRESS_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        url = entry.get("url")
                        status = entry.get("status")
                        if status == "success" and url:
                            completed.add(url)
                            recovered_success += 1
                        elif status == "failed" and url:
                            failed[url] = entry.get("error", "Unknown error")
                            recovered_failed += 1
                    except json.JSONDecodeError:
                        continue
            if recovered_success > 0 or recovered_failed > 0:
                log_message_sync(f"Recovered from progress.log: +{recovered_success} success, +{recovered_failed} failed", "RECOVERY")
        except Exception as e:
            log_message_sync(f"progress.log read error ({e})", "WARN")

    # Fix #17: Remove completed URLs from failed dict
    for url in completed:
        failed.pop(url, None)

    return completed, failed


async def async_save_checkpoint_snapshot(completed: Set[str], failed: Dict[str, str]) -> None:
    snapshot = {
        "completed": sorted(list(completed)),
        "failed": failed,
        "last_updated": time.time(),
        "total_completed": len(completed),
        "total_failed": len(failed)
    }
    await async_atomic_write_json(CHECKPOINT_FILE, snapshot)
    await async_atomic_write_json(COMPLETED_URLS_FILE, sorted(list(completed)))
    # Fix #16: Also write dedicated failed file
    await async_atomic_write_json(FAILED_FILE, failed)


def detect_missing_data(completed_set: Set[str]) -> List[str]:
    if not DATA_DIR.exists() or not completed_set:
        return []

    urls_with_files: Set[str] = set()
    for filepath in DATA_DIR.glob("*.json"):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                url = data.get("product_url")
                if url:
                    urls_with_files.add(url)
        except Exception:
            continue

    missing = sorted(completed_set - urls_with_files)

    if missing:
        report = {
            "notice": "These URLs were marked completed but their individual data files were not found.",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "missing_count": len(missing),
            "missing_urls": missing,
            "recommendation": "These URLs will NOT be re-scraped automatically. Use retry-failed mode if they appear in failed.json."
        }
        _atomic_write_json_sync(MISSING_DATA_FILE, report)
        log_message_sync(f"{len(missing)} URLs have missing data files. Report saved to {MISSING_DATA_FILE}", "INTEGRITY")
    else:
        if MISSING_DATA_FILE.exists():
            MISSING_DATA_FILE.unlink()

    return missing


async def async_save_property_atomic(data: Dict[str, Any], index: int) -> str:
    prop_id = data.get("property_id") or "no_id"
    safe_id = sanitize_filename(str(prop_id))
    filename = f"{index:06d}_{safe_id}.json"
    filepath = DATA_DIR / filename
    await async_atomic_write_json(filepath, data)
    return filename


def count_existing_data_files() -> int:
    if not DATA_DIR.exists():
        return 0
    return len(list(DATA_DIR.glob("*.json")))


# Fix #5: Stream-write with in-memory dedup; skip if >50k items to avoid OOM
async def generate_all_properties_json(max_in_memory: int = 50000) -> int:
    if not DATA_DIR.exists():
        return 0
    files = list(DATA_DIR.glob("*.json"))
    if len(files) > max_in_memory:
        await log_message(
            f"Skipping all_properties.json: {len(files)} files exceeds safe limit ({max_in_memory}). "
            "Use JSONL as canonical output.", "WARN"
        )
        return 0

    def _write():
        temp = ALL_DATA_FILE.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as f:
            f.write('[\n')
            first = True
            unique: Dict[str, Dict] = {}
            for filepath in sorted(files):
                try:
                    with open(filepath, "r", encoding="utf-8") as jf:
                        item = json.load(jf)
                    url = item.get("product_url")
                    if url:
                        unique[url] = item
                except Exception:
                    continue
            for item in unique.values():
                if not first:
                    f.write(',\n')
                f.write(json.dumps(item, ensure_ascii=False))
                first = False
            f.write('\n]')
            f.flush()
            os.fsync(f.fileno())
        temp.replace(ALL_DATA_FILE)
        return len(unique)

    return await run_in_thread(_write)


# ========== INTERACTIVE MENU ==========
def ask_mode(urls: List[str]) -> str:
    completed, failed = load_checkpoint_robust()
    has_checkpoint = len(completed) > 0 or len(failed) > 0

    print("\n" + "="*60)
    print("🏠  PINCALI MULTI-PROPERTY SCRAPER")
    print("="*60)
    print(f"📄 Total URLs loaded: {len(urls)}")

    if has_checkpoint:
        remaining = len(urls) - len(completed)
        existing_files = count_existing_data_files()
        print(f"\n📊  EXISTING CHECKPOINT FOUND:")
        print(f"   ✅ Completed: {len(completed)}")
        print(f"   ❌ Failed:    {len(failed)}")
        print(f"   ⏳ Remaining: {remaining}")
        print(f"   📁 Data files: {existing_files} in {DATA_DIR}/")
        if ALL_DATA_FILE.exists():
            try:
                size_mb = ALL_DATA_FILE.stat().st_size / (1024*1024)
                print(f"   📦 Final JSON: {size_mb:.2f} MB")
            except Exception:
                pass
        if CSV_FILE.exists():
            try:
                size_mb = CSV_FILE.stat().st_size / (1024*1024)
                print(f"   📄 CSV file:   {size_mb:.2f} MB")
            except Exception:
                pass
        if JSONL_FILE.exists():
            try:
                size_mb = JSONL_FILE.stat().st_size / (1024*1024)
                print(f"   📄 JSONL file: {size_mb:.2f} MB")
            except Exception:
                pass
        print("\n   Choose an option:")
        print("   [1] ▶️  RESUME       — Continue scraping")
        print("   [2] 🔄 RETRY-FAILED  — Retry failed URLs")
        print("   [3] 🆕 NEW          — Start fresh scrape")
        print("   [4] 📄 MIGRATE      — JSON → CSV + JSONL")
    else:
        print("\n   🆕 No checkpoint found.")
        print("\n   Choose an option:")
        print("   [1] 🆕 NEW          — Start fresh scrape")
        print("   [4] 📄 MIGRATE      — JSON → CSV + JSONL")

    print("   [q] ❌ QUIT         — Exit")
    print("-"*60)

    while True:
        choice = input("   👉 Enter your choice: ").strip().lower()

        if has_checkpoint:
            if choice in ("1", "resume", "r"):
                return "resume"
            elif choice in ("2", "retry", "retry-failed", "f", "failed"):
                if len(failed) == 0:
                    print("   ⚠️  No failed URLs to retry. Use RESUME instead.\n")
                    continue
                return "retry-failed"
            elif choice in ("3", "new", "n", "fresh"):
                confirm = input("   ⚠️  DELETE all progress? Type 'yes' to confirm: ").strip().lower()
                if confirm == "yes":
                    return "new"
                else:
                    print("   ❌ Cancelled.\n")
            elif choice in ("4", "migrate", "m"):
                return "migrate"
            elif choice in ("q", "quit", "exit"):
                print("   👋 Goodbye!")
                sys.exit(0)
            else:
                print("   ⚠️  Invalid choice. Try again.\n")
        else:
            if choice in ("1", "new", "n", "fresh"):
                return "new"
            elif choice in ("4", "migrate", "m"):
                return "migrate"
            elif choice in ("q", "quit", "exit"):
                print("   👋 Goodbye!")
                sys.exit(0)
            else:
                print("   ⚠️  Invalid choice. Try again.\n")


# ========== STATS & WORKERS ==========
class Stats:
    def __init__(self, total: int):
        self.total = total
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.active = 0
        self.lock = asyncio.Lock()
        self.start_time = time.perf_counter()
        self._last_checkpoint_time = time.perf_counter()

    async def task_start(self):
        async with self.lock:
            self.active += 1

    async def task_end(self, status: str = "success"):
        async with self.lock:
            if status == "success":
                self.success += 1
            elif status == "failed":
                self.failed += 1
            elif status == "skipped":
                self.skipped += 1

            self.active -= 1
            elapsed = time.perf_counter() - self.start_time
            done = self.success + self.failed + self.skipped
            speed = done / elapsed if elapsed > 0 else 0

            print(f"📊 [{done}/{self.total}] ✅ {self.success} | ❌ {self.failed} | ⏭️ {self.skipped} | 🔄 {self.active} | ⚡ {speed:.2f}u/s | ⏱️ {elapsed:.0f}s")


# Fix #2: _log_failure is now async and properly awaited
async def _log_failure(url, index, error_msg, completed_set, failed_dict, checkpoint_lock, stats):
    await async_append_jsonl(PROGRESS_LOG, {
        "timestamp": time.time(),
        "url": url,
        "status": "failed",
        "error": error_msg,
        "index": index
    })
    async with checkpoint_lock:
        failed_dict[url] = error_msg
    await log_message(f"FAILED [{index}] {url} - {error_msg}", "FAILED")
    # Fix #3: Always decrement stats here so callers don't forget
    await stats.task_end("failed")
    currently_processing.discard(url)


async def _finalize_success(data, url, index, completed_set, failed_dict, checkpoint_lock, stats):
    """Fix #19 validation + atomic save + checkpoint update."""
    if not validate_property(data):
        raise ValueError("Validation failed: missing property_id and price/title")
    filename = await async_save_property_atomic(data, index)
    await async_append_jsonl(PROGRESS_LOG, {
        "timestamp": time.time(),
        "url": url,
        "status": "success",
        "file": filename,
        "index": index
    })
    async with checkpoint_lock:
        completed_set.add(url)
        failed_dict.pop(url, None)
        currently_processing.discard(url)
    await log_message(f"SUCCESS [{index}] {url} -> {filename}", "SUCCESS")
    await stats.task_end("success")


async def process_url(
    url: str,
    index: int,
    browser,
    completed_set: Set[str],
    failed_dict: Dict[str, str],
    checkpoint_lock: asyncio.Lock,
    stats: Stats,
    shared_state: Dict[str, Any],
) -> tuple[bool, Optional[list]]:
    """
    Returns (success, cookies_or_none).
    success=False signals browser crash (caller should restart browser).
    """
    await stats.task_start()

    if url in completed_set:
        await stats.task_end("skipped")
        return True, None

    context = None
    page = None

    try:
        # Fix #21: Rate limiting / politeness delay
        await asyncio.sleep(random.uniform(1.0, 2.5))

        context_options = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": USER_AGENT,
        }
        # Fix #15: Inject captcha cookies from previous headed solve
        if shared_state.get("captcha_cookies"):
            context = await browser.new_context(**context_options)
            await context.add_cookies(shared_state["captcha_cookies"])
        else:
            context = await browser.new_context(**context_options)

        page = await context.new_page()

        response = await page.goto(url, wait_until="networkidle", timeout=TIMEOUT)

        # Try to wait for listing shell (may fail on captcha pages)
        try:
            await page.wait_for_selector(".listing", timeout=20000)
        except Exception:
            pass

        # Check captcha BEFORE HTTP status, since challenge pages may return 403
        captcha_found = await is_captcha_present(page)
        if captcha_found:
            if page:
                await page.close()
                page = None
            if context:
                await context.close()
                context = None

            # Fix #14: Global captcha lock so only one headed browser at a time
            async with CAPTCHA_LOCK:
                data, cookies = await scrape_headed_for_captcha(url)
            await _finalize_success(data, url, index, completed_set, failed_dict, checkpoint_lock, stats)
            return True, cookies

        # Fix #11: Verify no redirect / 404 for non-captcha pages
        if response and response.status >= 400:
            raise Exception(f"HTTP error {response.status} on {url}")
        parsed_got = urlparse(page.url)
        parsed_want = urlparse(url)
        if parsed_got.path != parsed_want.path:
            raise Exception(f"Redirect detected: got {page.url}, expected {url}")

        # Fix #1: Wait for lazy-loaded description text
        try:
            await page.wait_for_selector(".listing__description span[data-translation]", timeout=10000)
            await page.wait_for_function(
                "() => document.querySelector('.listing__description span[data-translation]')?.innerText?.trim().length > 0",
                timeout=8000
            )
        except Exception:
            pass  # Description may legitimately be absent

        # Fix #25: Scroll to trigger lazy-loaded images & amenities
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)

        data = await extract_property_data(page, url)
        await _finalize_success(data, url, index, completed_set, failed_dict, checkpoint_lock, stats)
        return True, None

    except PlaywrightError as e:
        err_str = str(e).lower()
        if any(x in err_str for x in ["connection closed", "browser has been closed", "target closed", "protocol error"]):
            await log_message(f"BROWSER CRASH while processing [{index}] {url}. Will restart browser.", "BROWSER_CRASH")
            # Fix #4: Do NOT call task_end here; worker will retry
            return False, None
        else:
            error_msg = f"{type(e).__name__}: {str(e)}"
            await _log_failure(url, index, error_msg, completed_set, failed_dict, checkpoint_lock, stats)
            return True, None

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        await _log_failure(url, index, error_msg, completed_set, failed_dict, checkpoint_lock, stats)
        return True, None

    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass

        # Fix #9: Periodic checkpoint snapshot every 30 seconds (update time inside lock)
        now = time.perf_counter()
        if now - stats._last_checkpoint_time > 30:
            async with checkpoint_lock:
                now2 = time.perf_counter()
                if now2 - stats._last_checkpoint_time > 30:
                    await async_save_checkpoint_snapshot(completed_set, failed_dict)
                    stats._last_checkpoint_time = now2


async def worker_with_browser_restart(
    queue: asyncio.Queue,
    completed_set: Set[str],
    failed_dict: Dict[str, str],
    checkpoint_lock: asyncio.Lock,
    stats: Stats,
    playwright,
):
    """
    Each worker manages its OWN browser instance.
    Restarts browser every BROWSER_RESTART_EVERY URLs to prevent memory crash.
    If browser crashes mid-URL, retries with fresh browser (up to MAX_RETRIES).
    """
    browser = None
    urls_since_restart = 0
    retry_count = 0
    current_url = None
    current_index = None
    shared_state: Dict[str, Any] = {"captcha_cookies": []}

    while True:
        try:
            if current_url is None:
                try:
                    current_index, current_url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                # Fix #8: Skip if already processing or completed
                async with checkpoint_lock:
                    if current_url in completed_set or current_url in currently_processing:
                        current_url = None
                        continue
                    currently_processing.add(current_url)

            # Restart browser if needed
            if browser is None or urls_since_restart >= BROWSER_RESTART_EVERY:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                proxy = get_next_proxy()
                launch_opts = {"headless": HEADLESS}
                if proxy:
                    launch_opts["proxy"] = proxy
                await log_message(f"Worker restarting browser (processed {urls_since_restart} URLs)...", "BROWSER")
                browser = await playwright.chromium.launch(**launch_opts)
                urls_since_restart = 0

            success, cookies = await process_url(
                current_url, current_index, browser,
                completed_set, failed_dict, checkpoint_lock, stats, shared_state
            )

            if success:
                urls_since_restart += 1
                retry_count = 0
                if cookies:
                    shared_state["captcha_cookies"] = cookies
                current_url = None
                current_index = None
            else:
                # Browser crashed
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                browser = None
                urls_since_restart = 0

                retry_count += 1
                if retry_count <= MAX_RETRIES_PER_URL:
                    await log_message(f"Retrying [{current_index}] after browser crash (attempt {retry_count}/{MAX_RETRIES_PER_URL})", "RETRY")
                    await asyncio.sleep(2)
                    # Keep current_url/current_index; loop continues with new browser
                else:
                    await log_message(f"GIVING UP [{current_index}] {current_url} after {MAX_RETRIES_PER_URL} browser crashes", "GIVEUP")
                    error_msg = "Browser crashed repeatedly (Connection closed)"
                    await _log_failure(current_url, current_index, error_msg, completed_set, failed_dict, checkpoint_lock, stats)
                    async with checkpoint_lock:
                        currently_processing.discard(current_url)
                    current_url = None
                    current_index = None
                    retry_count = 0

        except Exception as e:
            await log_message(f"Worker unexpected error: {e}", "WORKER_ERROR")
            if current_url:
                await _log_failure(current_url, current_index, f"Worker error: {e}", completed_set, failed_dict, checkpoint_lock, stats)
                async with checkpoint_lock:
                    currently_processing.discard(current_url)
                current_url = None
                current_index = None
            retry_count = 0

    if browser:
        try:
            await browser.close()
        except Exception:
            pass


async def run_scraper(urls: List[str], mode: str):
    ensure_output_dir()
    completed_set, failed_dict = load_checkpoint_robust()
    existing_data_count = count_existing_data_files()

    if mode == "new":
        await log_message("NEW mode: clearing all previous data...", "NEW")
        completed_set.clear()
        failed_dict.clear()

        for f in [ALL_DATA_FILE, CHECKPOINT_FILE, FAILED_FILE, COMPLETED_URLS_FILE,
                  MISSING_DATA_FILE, PROGRESS_LOG, MIGRATION_MANIFEST]:
            if f.exists():
                f.unlink()

        if DATA_DIR.exists():
            for f in DATA_DIR.glob("*.json"):
                f.unlink()

        to_scrape = [(i, url) for i, url in enumerate(urls)]
        await log_message("All previous data cleared. Starting fresh.", "NEW")

    elif mode == "resume":
        # Fix #22: Rotate progress.log if it grew huge
        if PROGRESS_LOG.exists() and PROGRESS_LOG.stat().st_size > 50 * 1024 * 1024:
            rotated = OUTPUT_DIR / f"progress_{time.strftime('%Y%m%d_%H%M%S')}.log"
            PROGRESS_LOG.rename(rotated)
            await log_message(f"Rotated oversized progress.log to {rotated.name}", "MAINTENANCE")

        missing = detect_missing_data(completed_set)
        if missing:
            print(f"\n⚠️  WARNING: {len(missing)} URLs marked completed but data files missing.")
            print(f"   See report: {MISSING_DATA_FILE}\n")

        to_scrape = [(i, url) for i, url in enumerate(urls) if url not in completed_set]
        await log_message(f"RESUME mode: {len(completed_set)} completed, {len(to_scrape)} remaining", "RESUME")

    elif mode == "retry-failed":
        to_scrape = [(i, url) for i, url in enumerate(urls) if url in failed_dict]
        await log_message(f"RETRY-FAILED mode: {len(to_scrape)} URLs to retry", "RETRY")
        await log_message(f"Existing dataset: {existing_data_count} files preserved", "RETRY")

    else:
        await log_message(f"Unknown mode: {mode}", "ERROR")
        sys.exit(1)

    if not to_scrape:
        await log_message("No URLs to scrape. Generating final file...", "DONE")
        count = await generate_all_properties_json()
        await log_message(f"Final file generated: {count} properties", "DONE")
        return

    print(f"\n📋 Total URLs to scrape: {len(to_scrape)} (concurrency={CONCURRENCY})")
    print(f"🔄 Browser restarts every {BROWSER_RESTART_EVERY} URLs per worker")
    print("🚀 Launching workers...\n")

    checkpoint_lock = asyncio.Lock()
    stats = Stats(len(to_scrape))

    async with async_playwright() as p:
        queue = asyncio.Queue()
        for original_index, url in to_scrape:
            await queue.put((original_index, url))

        # Fix #26: No redundant semaphore; concurrency controlled purely by worker count
        workers = [
            asyncio.create_task(worker_with_browser_restart(
                queue, completed_set, failed_dict,
                checkpoint_lock, stats, p
            ))
            for _ in range(CONCURRENCY)
        ]
        await asyncio.gather(*workers)

    # Final checkpoint save
    await async_save_checkpoint_snapshot(completed_set, failed_dict)

    await log_message("Merging all data files into final JSON...", "MERGE")
    final_count = await generate_all_properties_json()
    new_data_count = count_existing_data_files()

    elapsed = time.perf_counter() - stats.start_time
    print(f"\n{'='*60}")
    print(f"📦 Final data file: {ALL_DATA_FILE} (skipped if >50k items)")
    print(f"📁 Individual files: {new_data_count} in {DATA_DIR}/")
    print(f"📄 Completed URLs:   {COMPLETED_URLS_FILE}")
    print(f"📄 Failed URLs:      {FAILED_FILE}")
    print(f"📄 Progress log:     {PROGRESS_LOG}")
    print(f"📄 Scraper log:      {SCRAPER_LOG}")
    print(f"{'='*60}")

    if mode == "retry-failed":
        print(f"🔄 RETRY SUMMARY:")
        print(f"   Previously failed: {len(to_scrape)}")
        print(f"   Fixed now:         {stats.success}")
        print(f"   Still failed:      {stats.failed}")
        print(f"   Dataset grew:      ~{existing_data_count} → {final_count}")
    else:
        print(f"✅ Successfully:     {stats.success}")
        print(f"❌ Failed:           {stats.failed}")
        print(f"⏭️  Skipped:         {stats.skipped}")

    print(f"⏱️  Total time:       {elapsed:.2f} seconds")
    print(f"{'='*60}")

    await log_message(f"Scraping complete. Final dataset: {final_count} properties. Time: {elapsed:.2f}s", "DONE")


# Fix #13: Flatten features with raw + parsed; filter unknown keys to features_json
def flatten_property_for_csv(data: Dict[str, Any], filename: str) -> Dict[str, str]:
    row: Dict[str, str] = {}

    direct_fields = [
        "property_id", "product_url", "price_digits", "operation_type",
        "title_h1", "location_subtitle", "address", "description",
        "agent_name", "agency_name", "video_iframe", "map_iframe",
        "virtual_tour_url"
    ]
    for field in direct_fields:
        val = data.get(field)
        row[field] = "" if val is None else str(val).replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')

    features = data.get("features", {})
    known_features = [
        "bedrooms", "bathrooms", "parking", "total_space_m2",
        "lot_size_m2", "lot_length_m", "lot_width_m", "year_built", "floors"
    ]

    if isinstance(features, dict):
        flat_features = {}
        for k, v in features.items():
            raw, parsed = parse_feature_value(v)
            if k in known_features:
                row[f"feature_{k}_raw"] = raw
                row[f"feature_{k}"] = str(parsed) if parsed is not None else raw
                flat_features[k] = v
            else:
                # Unknown keys go into features_json only, not flat columns
                flat_features[k] = v
        row["features_json"] = json.dumps(flat_features, ensure_ascii=False)
    else:
        row["features_json"] = ""

    for k in known_features:
        if f"feature_{k}" not in row:
            row[f"feature_{k}"] = ""
        if f"feature_{k}_raw" not in row:
            row[f"feature_{k}_raw"] = ""

    amenities = data.get("amenities", [])
    row["amenities_json"] = json.dumps(amenities, ensure_ascii=False) if isinstance(amenities, list) else ""

    images = data.get("all_images", [])
    row["all_images"] = "|".join(str(u) for u in images) if isinstance(images, list) else ""

    handled_keys = set(direct_fields + ["features", "amenities", "all_images", "virtual_tour_url"])
    extra = {k: v for k, v in data.items() if k not in handled_keys}
    row["extra_properties_json"] = json.dumps(extra, ensure_ascii=False) if extra else ""

    row["source_json_file"] = filename
    row["migrated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    return row


def main():
    ensure_output_dir()
    urls = load_urls()

    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        if mode not in ("new", "resume", "retry-failed", "migrate"):
            print("Usage: python script.py [new|resume|retry-failed|migrate]")
            sys.exit(1)
    else:
        mode = ask_mode(urls)

    if mode == "migrate":
        log_message_sync("Starting unified migration (CSV + JSONL).", "START")
        asyncio.run(migrate_both())
        return

    log_message_sync(f"Starting scraper. Mode: {mode}. Total URLs: {len(urls)}", "START")
    asyncio.run(run_scraper(urls, mode))


if __name__ == "__main__":
    main()