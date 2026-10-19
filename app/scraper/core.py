"""
app/scraper/core.py — Core scraping engine, ported from Pincali v6.3.

PATCHED 2026-06-04:
- Rotating user-agent pool (10 real browsers)
- Full stealth headers (sec-fetch-*, accept-encoding, referer)
- Playwright stealth args (disable AutomationControlled, realistic context)
- Circuit breaker uses dedicated threshold (15) not concurrency (3)
- Circuit ban reduced from 600s to 120s
- Proxy rotation in both HTTP and Playwright paths
- Human-like delays in Playwright
- 405 errors logged and trigger proxy rotation instead of circuit open
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.core.logging import get_scraper_logger
from app.core.redis_client import get_redis
import socket
from app.scraper.proxy_manager import (
    ProxyEndpoint,
    ProxyPoolExhausted,
    ProxyQuotaExceededError,
    get_proxy_manager,
)

settings = get_settings()
logger = get_scraper_logger()

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright, Error as PlaywrightError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightError = Exception

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# ── PATCH: Rotating User-Agent pool ──────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)

# ── HTML helpers (unchanged from v6.3) ──────────────────────────────────────

def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    text = (
        text.replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()

def _sanitize_for_extraction(text: Optional[str]) -> Optional[str]:
    import unicodedata as _ud
    if not text:
        return None
    cleaned = []
    for char in text:
        cp = ord(char)
        cat = _ud.category(char)
        if cat == "So":
            continue
        if cat == "Cc" and char not in ("\t", "\n", "\r"):
            continue
        if 0xFE00 <= cp <= 0xFE0F:
            continue
        if 0x200B <= cp <= 0x200F:
            continue
        if cp == 0xFEFF:
            continue
        cleaned.append(char)
    result = "".join(cleaned)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"^ +", "", result, flags=re.M)
    result = re.sub(r" +$", "", result, flags=re.M)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip() or None


def _extract_meta_content(html: str, attr_name: str, attr_val: str) -> Optional[str]:
    escaped = re.escape(attr_val)
    m = re.search(
        rf'<meta[^>]+{attr_name}\s*=\s*["\'][^"\']*{escaped}[^"\']*["\'][^>]+content\s*=\s*["\']([^"\']*)["\']',
        html, re.I,
    )
    if m:
        return m.group(1).strip() or None
    m = re.search(
        rf'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]+{attr_name}\s*=\s*["\'][^"\']*{escaped}[^"\']*["\']',
        html, re.I,
    )
    return m.group(1).strip() or None if m else None

_EXCLUDE_PATTERNS = [
    r"<(?:section|div)[^>]*\b(?:similar-properties|related-listings|similar_listings|you-might-also-like|more-properties)\b[^>]*>.*?</(?:section|div)>",
    r"<(?:nav|ol|ul|div)[^>]*\b(?:breadcrumb|breadcrumbs|crumbs)\b[^>]*>.*?</(?:nav|ol|ul|div)>",
    # Exclude agent/agency image containers so their photos never reach the image extractor
    r"<(?:div|figure|span|a)[^>]*\b(?:publisher-logo|publisher-photo|agent-photo|agency-logo|company-logo|profile-image|publisher-avatar|contact-photo)\b[^>]*>.*?</(?:div|figure|span|a)>",
]
_EXCLUDE_RE = re.compile("|".join(_EXCLUDE_PATTERNS), re.I | re.S)

def _sanitize_html(html: str) -> str:
    return _EXCLUDE_RE.sub("", html)

def _scope_tag(html: str, open_pattern: str) -> Optional[Tuple[str, str]]:
    m = re.search(open_pattern, html, re.I | re.S)
    if not m:
        return None
    tag_m = re.match(r"<(\w+)", m.group(0))
    if not tag_m:
        return None
    tag = tag_m.group(1).lower()
    pos = m.end()
    depth = 1
    open_re = re.compile(rf"<{tag}(?:\s[^>]*)?>", re.I)
    close_re = re.compile(rf"</{tag}\s*>", re.I)
    inner_start = pos
    while depth > 0 and pos < len(html):
        om = open_re.search(html, pos)
        cm = close_re.search(html, pos)
        if cm is None:
            return tag, html[inner_start:]
        if om is not None and om.start() < cm.start():
            depth += 1
            pos = om.end()
        else:
            depth -= 1
            if depth == 0:
                return tag, html[inner_start : cm.start()]
            pos = cm.end()
    return tag, html[inner_start:pos]

def _first_tag_text_scoped(html: str, container_pattern: str, inner_tag: str) -> Optional[str]:
    result = _scope_tag(html, container_pattern)
    if not result:
        return None
    _, inner = result
    child = re.search(
        r"<" + re.escape(inner_tag) + r"(?:\s[^>]*)?>(.+?)</" + re.escape(inner_tag) + r">",
        inner, re.I | re.S,
    )
    if not child:
        return None
    return _strip_tags(child.group(1)) or None

# ── URL helpers (unchanged) ─────────────────────────────────────────────────

def extract_property_id_from_url(url: str) -> str:
    path = urlparse(url).path
    last_segment = path.rstrip("/").split("/")[-1]
    if not last_segment:
        return hashlib.sha256(url.encode()).hexdigest()[:16]
    parts = last_segment.split("-")
    if parts and re.match(r"^\d+$", parts[0]):
        return parts[0]
    safe_id = re.sub(r"[^\w\-]", "_", last_segment).strip("_")
    if len(safe_id) > 60:
        safe_id = safe_id[:60] + "_" + hashlib.sha256(last_segment.encode()).hexdigest()[:8]
    return safe_id or hashlib.sha256(url.encode()).hexdigest()[:16]

def extract_lat_lon_from_html(html: str) -> Tuple[Optional[float], Optional[float]]:
    match = re.search(
        r'<a\b[^>]*?\bdata-clipboard-text\s*=\s*["\']([^"\']*?google\.com/maps[^"\']*?)["\']',
        html, re.I | re.S,
    )
    if match:
        url = match.group(1).replace("&amp;", "&").replace("&quot;", '"')
        q_match = re.search(r"[?&]q=([^&]+)", url)
        if q_match:
            parts = re.split(r"[\s,+]+", q_match.group(1).strip())
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                try:
                    lat, lng = float(parts[0]), float(parts[1])
                    if -90 <= lat <= 90 and -180 <= lng <= 180:
                        return lat, lng
                except ValueError:
                    pass
    ld_m = re.search(
        r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    )
    if ld_m:
        try:
            data = json.loads(ld_m.group(1).strip())
            if isinstance(data, dict):
                geo = data.get("geo") or {}
                lat, lng = geo.get("latitude"), geo.get("longitude")
                if lat is not None and lng is not None:
                    return float(lat), float(lng)
        except Exception:
            pass
    return None, None

def parse_custom_properties(page_html: str) -> Dict[str, Any]:
    match = re.search(r'data-track-custom-properties="({.*?})"', page_html)
    if match:
        try:
            return json.loads(match.group(1).replace("&quot;", '"'))
        except json.JSONDecodeError:
            pass
    return {}

# ── Circuit Breaker / Domain Throttler (PATCHED) ────────────────────────────

# PATCH: cross-process visibility. Same issue as ProxyManager (see
# app/scraper/proxy_manager.py) -- DomainThrottler is an in-memory
# singleton per process, so the dashboard (running in the `api` container,
# which never scrapes) always saw its own empty copy. Mirror snapshots to
# Redis under a per-process key so the dashboard can read real state.
THROTTLE_STATS_REDIS_PREFIX = "pincali:throttle_stats:"
THROTTLE_STATS_REDIS_TTL_SECONDS = 45


def _throttle_process_label() -> str:
    name = os.environ.get("PINCALI_PROCESS_NAME", "")
    host = socket.gethostname()
    return f"{name}@{host}" if name else host


class CircuitOpen(Exception):
    pass

class DomainThrottler:
    def __init__(self):
        self.failures: Dict[str, List[float]] = defaultdict(list)
        self.banned_until: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._process_label = _throttle_process_label()

    def _domain(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    async def acquire(self, url: str):
        domain = self._domain(url)
        async with self._lock:
            if domain in self.banned_until:
                if time.time() < self.banned_until[domain]:
                    wait = self.banned_until[domain] - time.time()
                    raise CircuitOpen(f"Domain {domain} throttled for {wait:.0f}s")
        await asyncio.sleep(
            random.uniform(settings.pincali_rate_limit_min, settings.pincali_rate_limit_max)
        )

    async def record_failure(self, url: str, error: Optional[str] = None):
        domain = self._domain(url)
        async with self._lock:
            now = time.time()
            window = self.failures[domain]
            window[:] = [t for t in window if now - t < 300]
            window.append(now)
            # PATCH: use dedicated threshold instead of concurrency
            if len(window) >= settings.pincali_circuit_failure_threshold:
                ban_until = now + settings.pincali_circuit_ban_seconds
                self.banned_until[domain] = ban_until
                logger.warning(
                    f"Circuit OPEN for {domain} ({len(window)} failures in 5min, "
                    f"banning for {settings.pincali_circuit_ban_seconds}s)"
                )
        await self._publish_stats_snapshot()

    async def record_success(self, url: str):
        domain = self._domain(url)
        async with self._lock:
            if domain in self.banned_until and time.time() < self.banned_until[domain]:
                del self.banned_until[domain]
            self.failures[domain].clear()
        await self._publish_stats_snapshot()

    async def _publish_stats_snapshot(self) -> None:
        """PATCH: best-effort mirror of this process's throttle state into
        Redis so the dashboard can read real, live state. Never raises --
        a Redis hiccup must not affect actual scraping/throttling."""
        try:
            payload = await self.stats()
            payload["process"] = self._process_label
            payload["updated_at"] = time.time()
            redis = get_redis()
            await redis.set(
                f"{THROTTLE_STATS_REDIS_PREFIX}{self._process_label}",
                json.dumps(payload),
                ex=THROTTLE_STATS_REDIS_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DomainThrottler] stats publish to Redis skipped: {exc}")

    async def stats(self) -> Dict[str, object]:
        """Snapshot for the dashboard's throttling/rate-limit panel."""
        now = time.time()
        async with self._lock:
            domains = []
            all_domains = set(self.failures.keys()) | set(self.banned_until.keys())
            for domain in all_domains:
                recent_failures = len(
                    [t for t in self.failures.get(domain, []) if now - t < 300]
                )
                ban_until = self.banned_until.get(domain, 0.0)
                if recent_failures == 0 and ban_until <= now:
                    continue  # nothing interesting to show for this domain
                domains.append(
                    {
                        "domain": domain,
                        "recent_failures_5min": recent_failures,
                        "banned": ban_until > now,
                        "ban_remaining_s": max(0, round(ban_until - now)),
                    }
                )
            domains.sort(key=lambda d: (-d["banned"], -d["recent_failures_5min"]))
        return {
            "rate_limit_delay_min_s": settings.pincali_rate_limit_min,
            "rate_limit_delay_max_s": settings.pincali_rate_limit_max,
            "circuit_failure_threshold": settings.pincali_circuit_failure_threshold,
            "circuit_ban_seconds": settings.pincali_circuit_ban_seconds,
            "domains": domains,
        }

# Shared singleton
domain_throttler = DomainThrottler()


async def get_cluster_throttle_snapshots() -> List[Dict[str, object]]:
    """PATCH: read every worker process's published throttle-stats snapshot
    from Redis. The dashboard should read from here, not
    `await domain_throttler.stats()` directly, which only reflects
    whichever process rendered the page (usually the idle `api` container).
    """
    try:
        redis = get_redis()
        keys = [
            k async for k in redis.scan_iter(match=f"{THROTTLE_STATS_REDIS_PREFIX}*")
        ]
        if not keys:
            return []
        raw_values = await redis.mget(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[DomainThrottler] could not read cluster throttle stats from Redis: {exc}"
        )
        return []

    snapshots: List[Dict[str, object]] = []
    for raw in raw_values:
        if not raw:
            continue
        try:
            snapshots.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    snapshots.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return snapshots

# ── HTTP Extractor (PATCHED headers + status logging) ───────────────────────

_LISTING_CLASS_RE = re.compile(r"""class\s*=\s*["\'][^"\']*listing[^"\']*["\']""")

async def fetch_static_page(url: str, proxy: Optional[str] = None) -> Optional[str]:
    # PATCH: full stealth headers with rotating UA
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es-MX;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "Referer": "https://www.pincali.com/en/properties/properties-for-sale",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=30.0,
            proxy=proxy,
            http2=False,
        ) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            # PATCH: log exact status code
            logger.warning(f"HTTP fetch failed: {url} -> HTTP {response.status_code}")
            return None
        html = response.text
        if not html or len(html) < 500:
            return None
        if not (_LISTING_CLASS_RE.search(html) or "listing-id" in html):
            return None
        html_lower = html.lower()
        captcha_indicators = [
            "g-recaptcha", "h-captcha", "cf-challenge", "just a moment",
            "cf-turnstile", "challenge-platform", "access denied",
        ]
        if any(x in html_lower for x in captcha_indicators):
            return None
        return html
    except ProxyQuotaExceededError:
        raise  # let the retry loop handle this -- don't swallow it below
    except httpx.ProxyError as e:
        msg = str(e)
        if "402" in msg:
            # PATCH: this is the vendor (e.g. Webshare) telling us the
            # account itself is out of bandwidth/credit -- confirmed via
            # curl reproduction: httpx.ProxyError('402 Payment Required').
            # This is account-wide; rotating to a different proxy endpoint
            # in the same pool will never help, so surface it distinctly
            # instead of letting it look like a normal per-request block.
            raise ProxyQuotaExceededError(
                f"Proxy vendor returned 402 Payment Required for {url}: {msg}",
                reason="402_payment_required",
            ) from e
        logger.debug(f"HTTP fetch failed for {url} (proxy error): {e}")
        return None
    except Exception as e:
        logger.debug(f"HTTP fetch failed for {url}: {e}")
        return None

class HTTPExtractor:
    """Unchanged from v6.3 — pure HTML extraction without Playwright."""

    def __init__(self, html: str, url: str):
        self.html = _sanitize_html(html)
        self.raw_html = html
        self.url = url

    def _extract_digits_from_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        num_match = re.search(r"([\d\s,\.]+)", text)
        if num_match:
            cleaned = num_match.group(1).replace(",", "").replace(" ", "").replace(".", "")
            if cleaned and cleaned.isdigit() and int(cleaned) > 0:
                return cleaned
        return None

    def _detect_currency_from_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        upper = text.upper()
        if "MXN" in upper or "PESO" in upper:
            return "MXN"
        if "USD" in upper or "DOLLAR" in upper:
            return "USD"
        if "$" in text and "USD" not in upper:
            return "MXN"
        return None

    def extract(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"product_url": self.url}

        pid = _first_tag_text_scoped(self.html, r"<[^>]*\blisting-id\b[^>]*>", "span")
        if pid:
            pid = re.sub(r"(?i)^id\s*[:\-]?\s*", "", pid).strip()
        result["property_id"] = (pid and pid.strip()) or extract_property_id_from_url(self.url)

        # ── Price digits (multi-strategy) ──
        price_digits = None
        price_raw_text = None

        digits_m = re.search(r"<[^>]*\bdigits\b[^>]*>(.*?)</(?:span|div|p)>", self.html, re.I | re.S)
        if digits_m:
            raw = _strip_tags(digits_m.group(1)).strip()
            price_raw_text = raw
            price_digits = self._extract_digits_from_text(raw)

        if not price_digits:
            for price_pattern in [
                r'<[^>]*\bclass\s*=\s*["\'][^"\']*price[^"\']*["\'][^>]*>(.*?)</(?:span|div|p)>',
                r'<[^>]*\bid\s*=\s*["\']price["\'][^>]*>(.*?)</(?:span|div|p)>',
                r'<[^>]*\bclass\s*=\s*["\'][^"\']*listing-price[^"\']*["\'][^>]*>(.*?)</(?:span|div|p)>',
                r'<[^>]*\bclass\s*=\s*["\'][^"\']*property-price[^"\']*["\'][^>]*>(.*?)</(?:span|div|p)>',
            ]:
                price_m = re.search(price_pattern, self.html, re.I | re.S)
                if price_m:
                    raw = _strip_tags(price_m.group(1)).strip()
                    price_raw_text = raw
                    price_digits = self._extract_digits_from_text(raw)
                    if price_digits:
                        break

        if not price_digits:
            dp = re.search(r'data-price\s*=\s*["\']([\d\.]+)["\']', self.html, re.I)
            if dp:
                price_digits = dp.group(1).replace(".", "")
                price_raw_text = dp.group(0)

        if not price_digits:
            ld = self._extract_json_ld()
            if isinstance(ld, dict):
                direct_price = ld.get("price")
                if direct_price:
                    price_digits = str(direct_price).replace(",", "").replace(" ", "")
                if not price_digits:
                    offers = ld.get("offers") or ld.get("makesOffer") or {}
                    if isinstance(offers, dict):
                        p = offers.get("price") or offers.get("highPrice")
                        if p:
                            price_digits = str(p).replace(",", "").replace(" ", "")
                    elif isinstance(offers, list) and offers:
                        p = offers[0].get("price") if isinstance(offers[0], dict) else None
                        if p:
                            price_digits = str(p).replace(",", "").replace(" ", "")

        if not price_digits:
            meta_price = _extract_meta_content(self.raw_html, "property", "og:price:amount")
            if meta_price:
                price_digits = meta_price.replace(",", "").replace(" ", "")

        if not price_digits:
            broad = re.search(r"(?:\$|USD|MXN)\s*([\d\s,]+(?:\.\d+)?)", self.raw_html[:5000], re.I)
            if broad:
                cleaned = broad.group(1).replace(",", "").replace(" ", "")
                if cleaned.isdigit() and int(cleaned) > 0:
                    price_digits = cleaned

        result["price_digits"] = price_digits

        op_m = re.search(r"<[^>]*\boperation-type\b[^>]*>(.*?)</(?:span|div|p)>", self.html, re.I | re.S)
        result["operation_type"] = _strip_tags(op_m.group(1)).strip() or None if op_m else None

        # ── Currency detection (4-step priority, highest first) ────────────────
        #
        # STEP 1 — Read currency label directly from the same price text the
        #           scraper already extracted.  This fires BEFORE any DOM selector
        #           so a page-level currency element (e.g. a nav filter or sidebar
        #           "Mostrar precios en: MXN" dropdown) cannot shadow the real label
        #           that appears right next to the number.
        #           e.g. "$60 USD per m²" → "USD"
        result["currency"] = None
        if price_raw_text:
            detected = self._detect_currency_from_text(price_raw_text)
            if detected:
                result["currency"] = detected

        # STEP 2 — og:price:currency meta tag — most authoritative machine-readable
        #           signal on EasyBroker pages.  Overrides Step 1 only when present
        #           (rare, but more reliable than any text heuristic).
        if not result.get("currency"):
            og_cur = _extract_meta_content(self.raw_html, "property", "og:price:currency")
            if og_cur and og_cur.strip():
                result["currency"] = og_cur.strip().upper()

        # STEP 3 — DOM currency element SCOPED to the price block.
        #           We first try to find it inside the price container so we never
        #           accidentally match a currency selector elsewhere on the page.
        #           Fall back to unscoped search ONLY when result is a bare ISO code
        #           (3 capital letters) to reject human-readable labels from navbars.
        if not result.get("currency"):
            # Scoped: look inside the price/digits container first
            price_block_result = _scope_tag(self.html, r"<[^>]*\bprice\b[^>]*>")
            search_html = price_block_result[1] if price_block_result else self.html
            currency_m = re.search(
                r"<[^>]*\bcurrency\b[^>]*>(.*?)</(?:span|div|p)>", search_html, re.I | re.S
            )
            if currency_m:
                raw_cur = _strip_tags(currency_m.group(1)).strip() or None
                if raw_cur and raw_cur.upper() != "$":
                    result["currency"] = raw_cur
            else:
                # Unscoped fallback — only trust a clean 3-letter ISO code to avoid
                # picking up "MXN" from a page-level currency filter/dropdown.
                cur_fb = re.search(
                    r'<[^>]*class\s*=\s*["\'][^"\']*(?:currency|price-currency)[^"\']*["\'][^>]*>([^<]+)',
                    self.html, re.I,
                )
                if cur_fb:
                    raw_fb = _strip_tags(cur_fb.group(1)).strip()
                    if raw_fb and re.match(r'^[A-Z]{3}$', raw_fb.upper()):
                        result["currency"] = raw_fb.upper()

        # STEP 4 — Full price-block text scan as final heuristic.
        if not result.get("currency"):
            price_block_m = re.search(
                r'<[^>]*\bprice\b[^>]*>(.*?)</(?:div|section|article)>',
                self.html, re.I | re.S,
            )
            if price_block_m:
                block_text = _strip_tags(price_block_m.group(1))
                detected = self._detect_currency_from_text(block_text)
                if detected:
                    result["currency"] = detected

        # Default: pincali.com is a Mexican property site — MXN if nothing found.
        if not result.get("currency"):
            result["currency"] = "MXN"

        h1_m = re.search(r"<h1(?:\s[^>]*)?>(.+?)</h1>", self.html, re.I | re.S)
        result["title_h1"] = _strip_tags(h1_m.group(1)) if h1_m else ""

        loc_m = re.search(r"<h2[^>]*\blocation\b[^>]*>(.+?)</h2>", self.html, re.I | re.S)
        result["location_subtitle"] = _strip_tags(loc_m.group(1)) if loc_m else None

        if result["location_subtitle"]:
            first_word = result["location_subtitle"].strip().split()[0] if result["location_subtitle"].strip() else ""
            result["listing_type"] = first_word if first_word else None
        else:
            result["listing_type"] = None

        result["address"] = _first_tag_text_scoped(self.html, r"<[^>]*\blisting__map\b[^>]*>", "p")

        # Fallback: read all text from the map container when the first <p> is missing
        if not result["address"]:
            map_result = _scope_tag(self.html, r"<[^>]*\blisting__map\b[^>]*>")
            if map_result:
                _, map_inner = map_result
                all_text = _strip_tags(map_inner).strip()
                if all_text:
                    result["address"] = all_text

        # Capture the agency logo URL so the normalizer can exclude it from photos
        agency_logo_m = re.search(
            r'<[^>]*\b(?:publisher-logo|agency-logo|company-logo)\b[^>]*>[^<]*'
            r'<img[^>]+\bsrc\s*=\s*["\']([^"\']+)["\']',
            self.raw_html, re.I | re.S,
        )
        result["agency_logo_url"] = agency_logo_m.group(1).strip() if agency_logo_m else None

        desc_result = _scope_tag(self.html, r"<[^>]*\blisting__description\b[^>]*>")
        description = None
        if desc_result:
            _, desc_inner = desc_result
            spans = re.findall(
                r"<span[^>]*\bdata-translation\b[^>]*>(.*?)</span>", desc_inner, re.I | re.S
            )
            candidates = [_sanitize_for_extraction(_strip_tags(s)) for s in spans]
            candidates = [c for c in candidates if c]
            if candidates:
                description = max(candidates, key=len)
        result["description"] = description

        result["agent_name"] = _first_tag_text_scoped(
            self.html, r"<[^>]*\bpublisher-name\b[^>]*>", "a"
        )
        agency_m = re.search(
            r"<[^>]*\bpublisher-organization-name\b[^>]*>(.*?)</(?:span|div|p|li)>",
            self.html, re.I | re.S,
        )
        result["agency_name"] = _strip_tags(agency_m.group(1)).strip() or None if agency_m else None

        phone_m = re.search(r'href\s*=\s*["\']tel:([^"\']+)["\']', self.html, re.I)
        result["agent_phone"] = phone_m.group(1).strip() if phone_m else None

        wa_m = re.search(r'href\s*=\s*["\']https://wa\.me/([^"\'?&]+)', self.html, re.I)
        result["agent_whatsapp"] = wa_m.group(1).strip() if wa_m else None

        video_result = _scope_tag(self.html, r"<[^>]*\blisting__video\b[^>]*>")
        if video_result:
            _, video_inner = video_result
            iframe_m = re.search(r'<iframe[^>]+\bsrc\s*=\s*["\']([^"\']+)["\']', video_inner, re.I)
            result["video_iframe"] = iframe_m.group(1) if iframe_m else None
        else:
            result["video_iframe"] = None

        map_result = _scope_tag(self.html, r'<[^>]*\bid\s*=\s*["\']map["\'][^>]*>')
        if map_result:
            _, map_inner = map_result
            iframe_m = re.search(r'<iframe[^>]+\bsrc\s*=\s*["\']([^"\']+)["\']', map_inner, re.I)
            result["map_iframe"] = iframe_m.group(1) if iframe_m else None
        else:
            result["map_iframe"] = None

        vt_m = re.search(
            r'<a[^>]+data-track-event\s*=\s*["\']View Virtual Tour["\'][^>]+href\s*=\s*["\']([^"\']+)["\']',
            self.html, re.I,
        )
        result["virtual_tour_url"] = vt_m.group(1) if vt_m else None

        result["listing_status"] = _first_tag_text_scoped(
            self.html, r"<[^>]*\blisting-status\b[^>]*>", "span"
        )
        result["status_message"] = self._extract_status_message()

        result["meta_description"] = _extract_meta_content(self.raw_html, "name", "description")
        result["meta_keywords"] = _extract_meta_content(self.raw_html, "name", "keywords")
        result["og_image"] = _extract_meta_content(self.raw_html, "property", "og:image")
        result["json_ld"] = self._extract_json_ld()
        result["features"] = self._extract_features()
        result["amenities"] = self._extract_amenities()
        result["all_images"] = self._extract_images()

        lat, lon = extract_lat_lon_from_html(self.raw_html)
        result["latitude"] = lat
        result["longitude"] = lon

        result.update(parse_custom_properties(self.raw_html))
        return result

    def _extract_status_message(self) -> Optional[str]:
        keywords = [
            "unpublished", "no longer available", "off market",
            "sold", "rented", "reserved", "was removed",
        ]
        intro_result = _scope_tag(self.html, r"<[^>]*\blisting__intro\b[^>]*>")
        if intro_result:
            _, intro_inner = intro_result
            for div_m in re.finditer(r"<div[^>]*>(.*?)</div>", intro_inner, re.I | re.S):
                text = _strip_tags(div_m.group(1))
                if text and any(kw in text.lower() for kw in keywords):
                    return text.strip()
        return None

    def _extract_features(self) -> Dict[str, str]:
        features: Dict[str, str] = {}
        for m in re.finditer(r"<(\w+)[^>]*\bfeature-icon\b[^>]*>", self.html, re.I):
            result = _scope_tag(self.html[m.start():], r"<[^>]*\bfeature-icon\b[^>]*>")
            if not result:
                continue
            _, content = result
            text = _strip_tags(content).strip()
            if not text:
                continue
            tl = text.lower()
            if re.search(r"\b\d+\s*bedrooms?\b", tl):
                features.setdefault("bedrooms", text)
            elif re.search(r"\b\d+\s*bath(?:rooms?|s)?\b", tl):
                features.setdefault("bathrooms", text)
            elif re.search(r"\bparking\b", tl):
                features.setdefault("parking", text)
            elif re.search(r"\btotal\s+space\b", tl):
                features.setdefault("total_space_m2", text)
            elif re.search(r"\blot\s+size\b", tl):
                features.setdefault("lot_size_m2", text)
            elif re.search(r"\byear\s+built\b", tl):
                features.setdefault("year_built", text)
            elif re.search(r"\bfloors?\b", tl):
                features.setdefault("floors", text)
        return features

    def _extract_amenities(self) -> List[Dict[str, List[str]]]:
        amenities: List[Dict[str, List[str]]] = []
        group_patterns = [
            r"<[^>]*\bamenities-group\b[^>]*>",
            r"<[^>]*\bamenity-group\b[^>]*>",
            r'<[^>]*class\s*=\s*["\'][^"\']*amenities[^"\']*["\'][^>]*>',
        ]
        seen_positions: Set[int] = set()
        for group_pattern in group_patterns:
            search_start = 0
            while True:
                m = re.search(group_pattern, self.html[search_start:], re.I | re.S)
                if not m:
                    break
                abs_pos = search_start + m.start()
                if abs_pos in seen_positions:
                    search_start += m.end()
                    continue
                seen_positions.add(abs_pos)
                group_result = _scope_tag(self.html[abs_pos:], group_pattern)
                if not group_result:
                    search_start = abs_pos + 1
                    continue
                _, group_html = group_result
                title = "Other"
                for tp in [
                    r"<(?:h[1-6]|strong|b)[^>]*>(.*?)</(?:h[1-6]|strong|b)>",
                    r"<[^>]*\bamenities-group-title\b[^>]*>(.*?)</[^>]+>",
                ]:
                    tm = re.search(tp, group_html, re.I | re.S)
                    if tm:
                        candidate = _strip_tags(tm.group(1)).strip()
                        if candidate:
                            title = candidate
                            break
                items: List[str] = []
                for li_m in re.finditer(r"<li[^>]*>(.*?)</li>", group_html, re.I | re.S):
                    li_text = _strip_tags(li_m.group(1)).strip()
                    if li_text:
                        items.append(li_text)
                if items:
                    amenities.append({"category": title, "items": items})
                search_start = abs_pos + len(group_html)
            if amenities:
                break
        return amenities

    def _extract_images(self) -> List[str]:
        # PRIMARY: Scope to the media-modal gallery grid — this is where all
        # property photos live on Pincali/EasyBroker detail pages.
        # Fallback patterns cover other possible gallery container names.
        GALLERY_PATTERNS = [
            r'<[^>]*\bmedia-modal__grid\b[^>]*>',
            r'<[^>]*\bmedia-modal__gallery-images\b[^>]*>',
            r'<[^>]*\blisting[_-]?gallery\b[^>]*>',
            r'<[^>]*\blisting[_-]?carousel\b[^>]*>',
            r'<[^>]*\blisting[_-]?photos\b[^>]*>',
            r'<[^>]*\bproperty[_-]?gallery\b[^>]*>',
            r'<[^>]*\bslider[_-]?wrapper\b[^>]*>',
            r'<[^>]*\bgallery[_-]?container\b[^>]*>',
        ]
        gallery_html = None
        for pattern in GALLERY_PATTERNS:
            result = _scope_tag(self.html, pattern)
            if result:
                _, gallery_html = result
                break
        # If no gallery container found, fall back to full sanitized HTML
        search_html = gallery_html if gallery_html else self.html

        urls: Set[str] = set()
        for m in re.finditer(
            r'<img[^>]+\bsrc\s*=\s*["\']([^"\']*assets\.easybroker\.com[^"\']*)["\']',
            search_html, re.I,
        ):
            urls.add(m.group(1))
        for m in re.finditer(
            r'<img[^>]+\bdata-src\s*=\s*["\']([^"\']*assets\.easybroker\.com[^"\']*)["\']',
            search_html, re.I,
        ):
            urls.add(m.group(1))
        for m in re.finditer(r'\bsrcset\s*=\s*["\']([^"\']+)["\']', search_html, re.I):
            for part in m.group(1).split(","):
                token = part.strip().split()[0]
                if token and "easybroker" in token:
                    urls.add(token)

        # DEFENSE: Statistical filtering — discard images from "Similar Properties" section.
        # EasyBroker URLs contain the property ID: property_images/{ID}/...
        # The most frequent ID is the current listing; everything else is from other listings.
        id_counts: Dict[str, int] = {}
        url_to_id: Dict[str, Optional[str]] = {}
        for url in urls:
            m = re.search(r'property_images/(\d+)', url)
            if m:
                img_id = m.group(1)
                id_counts[img_id] = id_counts.get(img_id, 0) + 1
                url_to_id[url] = img_id
            else:
                url_to_id[url] = None

        dominant_id: Optional[str] = None
        if id_counts:
            dominant_id = max(id_counts, key=id_counts.get)
            total_with_id = sum(id_counts.values())
            sorted_counts = sorted(id_counts.values(), reverse=True)
            runner_up = sorted_counts[1] if len(sorted_counts) > 1 else 0
            if not (
                id_counts[dominant_id] >= total_with_id * 0.30
                or id_counts[dominant_id] >= runner_up * 2
            ):
                dominant_id = None  # too ambiguous — keep all

        filtered: Set[str] = set()
        for url in urls:
            img_id = url_to_id[url]
            if img_id is None or dominant_id is None or img_id == dominant_id:
                filtered.add(url)

        result_urls = sorted(filtered, key=len, reverse=True)
        if len(result_urls) > 50:
            result_urls = result_urls[:50]
        return result_urls

    def _extract_json_ld(self) -> Optional[Dict]:
        ld_m = re.search(
            r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            self.raw_html, re.I | re.S,
        )
        if not ld_m:
            return None
        try:
            return json.loads(ld_m.group(1).strip())
        except Exception:
            return None

# ── Playwright extraction helpers (unchanged) ───────────────────────────────

# PATCH: resource_type alone misses lazy-loaded assets. A JS-driven photo
# gallery or an embedded map (Google Maps/Mapbox -- both common on real
# estate listing pages) fetches images/tiles via fetch()/XHR, which
# Playwright reports as resource_type "fetch"/"xhr", NOT "image" -- so
# they sailed straight through the old filter untouched. This is a very
# plausible major contributor to burning through the Webshare bandwidth
# allowance so fast. We can't just block all fetch/xhr though, since the
# property data itself is very often hydrated via exactly that kind of
# call -- so this blocks by URL pattern (file extension + known heavy
# third-party domains) instead, regardless of resource_type.
_HEAVY_ASSET_URL_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|avif|bmp|svg|ico|mp4|webm|mov|avi|"
    r"woff2?|ttf|eot|otf)(\?|$)"
    r"|maps\.googleapis\.com|maps\.gstatic\.com|maps\.google\.com|mapbox\.com"
    r"|googletagmanager\.com|google-analytics\.com|doubleclick\.net"
    r"|connect\.facebook\.net|hotjar\.com|intercom\.io|drift\.com"
    r"|zdassets\.com|youtube\.com|ytimg\.com|cloudinary\.com|imgix\.net",
    re.IGNORECASE,
)


def _should_block_request(resource_type: str, url: str) -> bool:
    """Pure decision logic, split out from _route_handler so it's testable
    without spinning up a real Playwright browser/route object."""
    if resource_type in ("image", "stylesheet", "font", "media", "other"):
        return True
    if _HEAVY_ASSET_URL_RE.search(url):
        return True
    if "analytics" in url or "tracking" in url:
        return True
    return False


async def _route_handler(route):
    if not settings.pincali_block_assets:
        await route.continue_()
        return
    req = route.request
    if _should_block_request(req.resource_type, req.url):
        await route.abort()
    else:
        await route.continue_()

async def _is_captcha_present(page) -> bool:
    try:
        if await page.query_selector(".listing"):
            return False
    except Exception:
        pass
    captcha_selectors = [
        ".g-recaptcha", ".h-captcha", "iframe[src*='google.com/recaptcha']",
        "#cf-challenge-running", ".cf-turnstile", "#challenge-running",
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
    return False

async def extract_property_data_playwright(page, url: str) -> Dict[str, Any]:
    """Full Playwright extraction — unchanged from v6.3."""
    html = await page.content()
    result: Dict[str, Any] = {"product_url": url}

    async def _get(selector: str, attr: Optional[str] = None) -> Optional[str]:
        try:
            elem = await page.query_selector(selector)
            if elem:
                val = (
                    await elem.get_attribute(attr)
                    if attr
                    else await elem.inner_text()
                )
                if val and str(val).strip():
                    return str(val).strip()
        except Exception:
            pass
        return None

    def _extract_digits_from_text(text: str) -> Optional[str]:
        if not text:
            return None
        num_match = re.search(r"([\d\s,\.]+)", text)
        if num_match:
            cleaned = num_match.group(1).replace(",", "").replace(" ", "").replace(".", "")
            if cleaned and cleaned.isdigit() and int(cleaned) > 0:
                return cleaned
        return None

    def _detect_currency_from_text(text: str) -> Optional[str]:
        if not text:
            return None
        upper = text.upper()
        if "MXN" in upper or "PESO" in upper:
            return "MXN"
        if "USD" in upper or "DOLLAR" in upper:
            return "USD"
        if "$" in text and "USD" not in upper:
            return "MXN"
        return None

    selectors_map = {
        "property_id": (".listing-id span", None),
        "price_digits": (".price .digits", None),
        "price_raw": (".price", None),
        "price_alt": (".listing-price", None),
        "price_meta": ("meta[property='og:price:amount']", "content"),
        "operation_type": (".price .operation-type", None),
        "currency": (".price .currency", None),
        "currency_meta": ("meta[property='og:price:currency']", "content"),
        "title_h1": ("h1", None),
        "location_subtitle": ("h2.location", None),
        "address": (".listing__map p", None),
        "agent_name": (".publisher-name a", None),
        "agency_name": (".publisher-organization-name", None),
        "listing_status": (".listing-status", None),
    }
    for key, (sel, attr) in selectors_map.items():
        result[key] = await _get(sel, attr)

    # Fallback: read all text from map container when the first <p> alone is incomplete
    if not result.get("address"):
        try:
            map_el = await page.query_selector(".listing__map")
            if map_el:
                full_text = (await map_el.inner_text()).strip()
                if full_text:
                    result["address"] = full_text
        except Exception:
            pass

    if result.get("location_subtitle"):
        first_word = result["location_subtitle"].strip().split()[0] if result["location_subtitle"].strip() else ""
        result["listing_type"] = first_word if first_word else None
    else:
        result["listing_type"] = None

    # BUG FIX: .price .digits inner_text() returns the full label text such as
    # "$60 USD per m²", not just the bare number.  Capture that as price_raw_text
    # for currency detection BEFORE entering the fallback block (which is skipped
    # when price_digits is already truthy), and clean price_digits to a pure number.
    price_raw_text = None
    if result.get("price_digits"):
        price_raw_text = result["price_digits"]          # e.g. "$60 USD per m²"
        cleaned = _extract_digits_from_text(price_raw_text)
        if cleaned:
            result["price_digits"] = cleaned             # store "60", not the raw label

    if not result.get("price_digits"):
        if result.get("price_alt"):
            raw = result.pop("price_alt", "")
            price_raw_text = raw
            digits = _extract_digits_from_text(raw)
            if digits:
                result["price_digits"] = digits
        if not result.get("price_digits") and result.get("price_meta"):
            meta_val = result.pop("price_meta", "")
            price_raw_text = meta_val
            digits = _extract_digits_from_text(meta_val)
            if digits:
                result["price_digits"] = digits
            else:
                cleaned = meta_val.replace(",", "").replace(" ", "")
                if cleaned.isdigit() and int(cleaned) > 0:
                    result["price_digits"] = cleaned
        if not result.get("price_digits") and result.get("price_raw"):
            raw = result.pop("price_raw", "")
            price_raw_text = raw
            digits = _extract_digits_from_text(raw)
            if digits:
                result["price_digits"] = digits

    # ── Currency detection (priority order) ────────────────────────────────
    #
    # STEP 1 — Read directly from the price label text we just captured.
    #           This fires BEFORE any DOM selector so a page-level currency
    #           element (nav filter, sidebar dropdown showing "MXN") cannot
    #           shadow the real label next to the number (e.g. "$60 USD per m²").
    result["currency"] = None
    if price_raw_text:
        detected = _detect_currency_from_text(price_raw_text)
        if detected:
            result["currency"] = detected

    # STEP 2 — og:price:currency meta tag (most reliable machine-readable signal).
    if not result.get("currency") and result.get("currency_meta"):
        result["currency"] = result.pop("currency_meta", None)
    else:
        result.pop("currency_meta", None)

    # STEP 3 — .price .currency DOM element (already in result, scoped to .price).
    #           "$" is ambiguous — clear it so the default kicks in below.
    if result.get("currency") == "$":
        result["currency"] = None

    # STEP 4 — Full .price inner_text scan as final heuristic.
    if not result.get("currency"):
        try:
            price_el = await page.query_selector(".price")
            if price_el:
                price_text = (await price_el.inner_text()).strip()
                detected = _detect_currency_from_text(price_text)
                if detected:
                    result["currency"] = detected
        except Exception:
            pass

    # pincali.com is a Mexican property site — default to MXN, not USD
    if not result.get("currency"):
        result["currency"] = "MXN"

    if not result.get("property_id"):
        result["property_id"] = extract_property_id_from_url(url)

    desc_elems = await page.query_selector_all(
        ".listing__description span[data-translation]"
    )
    texts = []
    for el in desc_elems:
        raw_t = (await el.inner_text()).strip()
        clean_t = _sanitize_for_extraction(raw_t)
        if clean_t:
            texts.append(clean_t)
    result["description"] = max(texts, key=len) if texts else None

    iframe_src = await _get(".listing__video iframe", "src")
    result["video_iframe"] = iframe_src

    map_src = await _get("#map iframe", "src")
    result["map_iframe"] = map_src

    vt_href = await _get("a[data-track-event='View Virtual Tour']", "href")
    result["virtual_tour_url"] = vt_href

    features: Dict[str, str] = {}
    icons = await page.query_selector_all(".feature-icon")
    for icon in icons:
        text = (await icon.inner_text()).strip()
        tl = text.lower()
        if re.search(r"\b\d+\s*bedrooms?\b", tl):
            features.setdefault("bedrooms", text)
        elif re.search(r"\b\d+\s*bath(?:rooms?|s)?\b", tl):
            features.setdefault("bathrooms", text)
        elif re.search(r"\bparking\b", tl):
            features.setdefault("parking", text)
        elif re.search(r"\btotal\s+space\b", tl):
            features.setdefault("total_space_m2", text)
        elif re.search(r"\blot\s+size\b", tl):
            features.setdefault("lot_size_m2", text)
        elif re.search(r"\byear\s+built\b", tl):
            features.setdefault("year_built", text)
        elif re.search(r"\bfloors?\b", tl):
            features.setdefault("floors", text)
    result["features"] = features

    amenities: List[Dict] = []
    groups = await page.query_selector_all(".amenities-group, .amenity-group")
    for group in groups:
        title = "Other"
        for ts in [".amenities-group-title", "h3", "h4", "strong"]:
            te = await group.query_selector(ts)
            if te:
                candidate = (await te.inner_text()).strip()
                if candidate:
                    title = candidate
                    break
        items = []
        for li in await group.query_selector_all("li"):
            li_text = (await li.inner_text()).strip()
            if li_text:
                items.append(li_text)
        if items:
            amenities.append({"category": title, "items": items})
    result["amenities"] = amenities

    img_urls: Set[str] = set()
    # PRIMARY: Scope to the media-modal gallery grid where all property photos live.
    # Fallback selectors cover other possible gallery container names.
    gallery_container = await page.query_selector(
        ".media-modal__grid, .media-modal__gallery-images, "
        ".listing__gallery, .listing-gallery, .listing-carousel, "
        ".listing-photos, .property-gallery, .slider-wrapper, .gallery-container"
    )
    search_scope = gallery_container if gallery_container else page
    for img in await search_scope.query_selector_all("img[src*='assets.easybroker.com']"):
        src = await img.get_attribute("src")
        if src:
            img_urls.add(src)

    # DEFENSE: Statistical filtering — discard images from "Similar Properties" section.
    # EasyBroker URLs contain the property ID: property_images/{ID}/...
    # The most frequent ID is the current listing; everything else is from other listings.
    id_counts: Dict[str, int] = {}
    url_to_id: Dict[str, Optional[str]] = {}
    for url in img_urls:
        m = re.search(r'property_images/(\d+)', url)
        if m:
            img_id = m.group(1)
            id_counts[img_id] = id_counts.get(img_id, 0) + 1
            url_to_id[url] = img_id
        else:
            url_to_id[url] = None

    dominant_id: Optional[str] = None
    if id_counts:
        dominant_id = max(id_counts, key=id_counts.get)
        total_with_id = sum(id_counts.values())
        sorted_counts = sorted(id_counts.values(), reverse=True)
        runner_up = sorted_counts[1] if len(sorted_counts) > 1 else 0
        if not (
            id_counts[dominant_id] >= total_with_id * 0.30
            or id_counts[dominant_id] >= runner_up * 2
        ):
            dominant_id = None  # too ambiguous — keep all

    filtered: Set[str] = set()
    for url in img_urls:
        img_id = url_to_id[url]
        if img_id is None or dominant_id is None or img_id == dominant_id:
            filtered.add(url)

    result_urls = sorted(filtered, key=len, reverse=True)
    if len(result_urls) > 50:
        result_urls = result_urls[:50]
    result["all_images"] = result_urls

    # Capture agency logo URL so normalizer can exclude it from the photo array
    agency_logo_el = await page.query_selector(
        ".publisher-logo img, .agency-logo img, .company-logo img"
    )
    result["agency_logo_url"] = (
        await agency_logo_el.get_attribute("src") if agency_logo_el else None
    )

    result["meta_description"] = _extract_meta_content(html, "name", "description")
    result["meta_keywords"] = _extract_meta_content(html, "name", "keywords")
    result["og_image"] = _extract_meta_content(html, "property", "og:image")
    ld_m = re.search(
        r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    )
    if ld_m:
        try:
            result["json_ld"] = json.loads(ld_m.group(1).strip())
        except Exception:
            result["json_ld"] = None
    else:
        result["json_ld"] = None

    phone_m = re.search(r'href\s*=\s*["\']tel:([^"\']+)["\']', html, re.I)
    result["agent_phone"] = phone_m.group(1).strip() if phone_m else None
    wa_m = re.search(r'href\s*=\s*["\']https://wa\.me/([^"\'?&]+)', html, re.I)
    result["agent_whatsapp"] = wa_m.group(1).strip() if wa_m else None

    lat, lon = extract_lat_lon_from_html(html)
    result["latitude"] = lat
    result["longitude"] = lon

    result.update(parse_custom_properties(html))
    return result

# ── Proxy rotation ───────────────────────────────────────────────────────────
# PATCH: now backed by the provider-agnostic ProxyManager
# (app/scraper/proxy_manager.py) instead of a flat round robin. Signature is
# unchanged so existing call sites (discovery.py, detail_scraper_worker.py)
# keep working without modification. Default provider is "static", which
# reproduces the old behavior exactly until PROXY_PROVIDER is switched.

def get_next_proxy() -> Optional[Dict[str, str]]:
    endpoint = get_proxy_manager().get_proxy_sync()
    return endpoint.as_playwright_proxy() if endpoint else None


async def _fetch_static_page_with_retry(
    url: str,
) -> Tuple[Optional[str], Optional[ProxyEndpoint]]:
    """HTTP-first fetch with proxy-aware retry/failover.

    On a failed/blocked attempt (fetch_static_page returns None -- covers
    connection errors, timeouts, non-2xx status, CAPTCHA pages, and
    too-short/invalid responses) this reports the failure to the
    ProxyManager, rotates to a fresh healthy proxy, waits an exponential
    backoff, and retries -- up to PROXY_MAX_RETRIES_PER_REQUEST attempts.
    """
    manager = get_proxy_manager()
    last_endpoint: Optional[ProxyEndpoint] = None
    max_attempts = max(1, settings.proxy_max_retries_per_request)

    for attempt in range(1, max_attempts + 1):
        try:
            endpoint = await manager.get_proxy()
        except ProxyPoolExhausted:
            logger.warning(
                f"[Scraper] Proxy pool exhausted fetching {url} "
                f"(attempt {attempt}/{max_attempts})"
            )
            return None, last_endpoint

        last_endpoint = endpoint
        proxy_str = endpoint.url if endpoint else None
        semaphore = manager.semaphore_for(endpoint)

        await manager.mark_request_start(endpoint, url)
        start = time.time()
        html = None
        quota_error: Optional[ProxyQuotaExceededError] = None
        try:
            if semaphore:
                async with semaphore:
                    html = await fetch_static_page(url, proxy=proxy_str)
            else:
                html = await fetch_static_page(url, proxy=proxy_str)
        except ProxyQuotaExceededError as e:
            # PATCH: account-level hard stop (e.g. Webshare 402/bandwidth
            # limit) -- rotating to another proxy in the same pool cannot
            # help this, so don't waste the remaining retry attempts
            # hitting the same wall on gw-1, gw-2, etc.
            quota_error = e
        finally:
            latency_ms = round((time.time() - start) * 1000)
            await manager.mark_request_end(
                endpoint, "success" if html else "failed", latency_ms
            )

        if quota_error is not None:
            await manager.mark_quota_exceeded(quota_error.reason)
            logger.error(
                f"[Scraper] Aborting retries for {url} -- proxy account quota "
                f"exceeded ({quota_error.reason}). This is account-wide; "
                f"check the vendor dashboard/billing, rotating proxies will "
                f"not fix it."
            )
            return None, endpoint

        proxy_id = endpoint.proxy_id if endpoint else "none"
        provider = endpoint.provider_name if endpoint else "none"

        if html:
            await manager.report_success(endpoint)
            logger.debug(
                f"[Scraper] fetch OK url={url} proxy={proxy_id} provider={provider} "
                f"attempt={attempt}/{max_attempts} latency_ms={latency_ms}"
            )
            return html, endpoint

        await manager.report_failure(endpoint, reason="fetch_failed_or_blocked")
        logger.warning(
            f"[Scraper] fetch BLOCKED url={url} proxy={proxy_id} provider={provider} "
            f"attempt={attempt}/{max_attempts} latency_ms={latency_ms}"
        )

        if attempt < max_attempts:
            backoff = settings.proxy_retry_backoff_base_seconds * (2 ** (attempt - 1))
            backoff += random.uniform(0, backoff * 0.25)
            await asyncio.sleep(backoff)

    return None, last_endpoint

# ── Scrape single URL — high-level function (PATCHED) ───────────────────────

async def scrape_url(
    url: str,
    browser,
    shared_state: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    try:
        await domain_throttler.acquire(url)
    except CircuitOpen as e:
        return None, "circuit", str(e)

    # HTTP-first path (PATCH: now goes through proxy-aware retry/failover --
    # see _fetch_static_page_with_retry() / ProxyManager)
    if settings.pincali_http_first:
        html, _endpoint = await _fetch_static_page_with_retry(url)
        if html:
            try:
                extractor = HTTPExtractor(html, url)
                data = extractor.extract()
                if data.get("property_id") and data.get("title_h1"):
                    await domain_throttler.record_success(url)
                    return data, "http", None
            except Exception as e:
                logger.debug(f"HTTP extraction failed for {url}, falling back: {e}")

    # Playwright fallback
    if get_proxy_manager().is_quota_exceeded():
        # PATCH: the HTTP path already confirmed the proxy account itself
        # is out of bandwidth/credit (account-wide). Launching a browser
        # here would just hit net::ERR_TUNNEL_CONNECTION_FAILED on the
        # exact same wall -- skip it and fail fast instead.
        return None, "proxy_quota", "Proxy account quota/bandwidth exceeded — see logs/dashboard"

    if not PLAYWRIGHT_AVAILABLE or browser is None:
        logger.warning(
            f"[Scraper] Playwright unavailable and HTTP extraction insufficient for {url}. "
            "Install playwright (pip install playwright && playwright install chromium) "
            "to enable JS-rendered page scraping."
        )
        return None, "browser", "Playwright not available — install playwright for JS pages"

    page = None
    context = None
    try:
        await asyncio.sleep(random.uniform(1.0, 2.5))
        # PATCH: realistic browser context
        context_opts = {
            "viewport": {"width": 1366, "height": 768},
            "user_agent": get_random_user_agent(),
            "locale": "es-MX",
            "timezone_id": "America/Mexico_City",
            "color_scheme": "light",
            "java_script_enabled": True,
            "bypass_csp": True,
        }
        context = await browser.new_context(**context_opts)
        if shared_state.get("captcha_cookies"):
            await context.add_cookies(shared_state["captcha_cookies"])
        page = await context.new_page()
        if settings.pincali_block_assets:
            await page.route("**/*", _route_handler)

        # PATCH: human-like delay before navigation
        await asyncio.sleep(random.uniform(2.0, 5.0))

        response = await page.goto(
            url, wait_until="networkidle", timeout=settings.pincali_timeout
        )
        try:
            await page.wait_for_selector(".listing", timeout=20000)
        except Exception:
            pass

        # PATCH: random scroll to simulate human
        await page.evaluate("window.scrollTo(0, Math.random() * 300)")
        await asyncio.sleep(random.uniform(0.5, 1.5))

        if await _is_captcha_present(page):
            return None, "browser", "CAPTCHA detected"

        if response and response.status >= 400:
            # PATCH: 405 = hard block -> rotate proxy, don't open circuit
            if response.status == 405:
                logger.warning(f"[Scraper] HTTP 405 (bot block) for {url} — rotating proxy")
                return None, "browser", f"HTTP 405 — bot block, rotate proxy"
            return None, "browser", f"HTTP {response.status}"

        try:
            await page.wait_for_selector(
                ".listing__description span[data-translation]", timeout=10000
            )
        except Exception:
            pass

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)

        data = await extract_property_data_playwright(page, url)
        await domain_throttler.record_success(url)
        return data, "browser", None

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        await domain_throttler.record_failure(url, err)
        return None, "browser", err
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