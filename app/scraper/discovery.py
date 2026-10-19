"""
app/scraper/discovery.py — URL Discovery Service.

PATCHED 2026-06-04:
- Rotating user-agent via get_random_user_agent()
- Full stealth headers
- Proxy support via get_proxy_manager().get_proxy_sync() (full authenticated
  URL — httpx needs credentials embedded, unlike Playwright's split fields)
- HTTP status code logged on failure
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.core.logging import get_scraper_logger
from app.scraper.core import get_random_user_agent
from app.scraper.proxy_manager import get_proxy_manager

logger = get_scraper_logger()
settings = get_settings()

BASE_URL = "https://www.pincali.com"

# ── Filter configuration (unchanged from pincali_optimized.py) ───────────────

PROPERTY_TYPE_SLUGS = {
    "Apartment": "apartments",
    "House": "houses",
    "Country House": "country-houses",
    "House in Gated Community": "houses-in-gated-community",
    "Lot": "land",
    "Ranch": "ranches",
    "Villa": "villas",
    "Building": "buildings",
    "Commercial Lot": "commercial-lots",
    "Office": "offices",
    "Retail Space": "retail-spaces",
    "Industrial Building": "industrial-buildings",
    "Industrial Lot": "industrial-lots",
    "Industrial Warehouse": "industrial-warehouses",
}

OPERATION_SLUGS = {"buy": "sale", "rent": "rent"}

BEDROOM_RANGES = {
    "1": {"min_bedroom": 1, "max_bedroom": 1},
    "2": {"min_bedroom": 2, "max_bedroom": 2},
    "3": {"min_bedroom": 3, "max_bedroom": 3},
    "4+": {"min_bedroom": 4},
}

BUY_PRICE_TIERS = [
    {"min_price": 0, "max_price": 500000, "label": "under_500k"},
    {"min_price": 500000, "max_price": 1000000, "label": "500k_to_1m"},
    {"min_price": 1000000, "max_price": 2000000, "label": "1m_to_2m"},
    {"min_price": 2000000, "max_price": 5000000, "label": "2m_to_5m"},
    {"min_price": 5000000, "max_price": 10000000, "label": "5m_to_10m"},
    {"min_price": 10000000, "max_price": 999999999, "label": "over_10m"},
]

# PATCH: full stealth headers with rotating UA
HEADERS = {
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


def build_filter_url(operation: str, property_type: Optional[str] = None, params: Optional[Dict] = None) -> str:
    op_slug = OPERATION_SLUGS.get(operation, "sale")
    if property_type and property_type in PROPERTY_TYPE_SLUGS:
        type_slug = PROPERTY_TYPE_SLUGS[property_type]
        base = f"{BASE_URL}/en/properties/{type_slug}-for-{op_slug}"
    else:
        base = f"{BASE_URL}/en/properties/properties-for-{op_slug}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            return f"{base}?{urlencode(clean)}"
    return base


def generate_discovery_filters() -> List[Dict]:
    """Generate all discovery filters (subset of pincali_optimized filters for efficiency)."""
    filters = []
    fid = 1

    def add(name: str, operation: str, prop_type: Optional[str] = None, params: Optional[Dict] = None):
        nonlocal fid
        url = build_filter_url(operation, prop_type, params)
        filters.append(
            {
                "id": fid,
                "name": name,
                "operation": operation,
                "property_type": prop_type,
                "url": url,
                "params": params or {},
            }
        )
        fid += 1

    # Core broad filters
    add("Buy - All Properties", "buy")
    add("Rent - All Properties", "rent")

    # Per property type
    for pt in list(PROPERTY_TYPE_SLUGS.keys())[:8]:
        add(f"Buy - {pt}", "buy", pt)
    for pt in ["Apartment", "House"]:
        add(f"Rent - {pt}", "rent", pt)

    # Bedroom ranges for top types
    for pt in ["Apartment", "House"]:
        for bed_label, bed_params in BEDROOM_RANGES.items():
            add(f"Buy - {pt} - {bed_label} Bed", "buy", pt, bed_params)

    # Price tiers
    for tier in BUY_PRICE_TIERS[:4]:
        add(f"Buy - Price {tier['label']}", "buy", params={
            "min_price": tier["min_price"],
            "max_price": tier["max_price"],
        })

    return filters


def extract_listing_urls_from_html(html: str) -> List[str]:
    """Extract property listing URLs from a search results page."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    result = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.startswith("/en/home/"):
            full = BASE_URL + href
        elif href.startswith("https://") and "/en/home/" in href:
            full = href
        else:
            continue
        if full not in seen:
            seen.add(full)
            result.append(full)
    return result


def extract_total_pages(html: str) -> Optional[int]:
    """Extract total page count from search results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one("div.pagination-summary")
    if summary:
        text = summary.get_text(strip=True)
        m = re.search(r"\d[\d,\.]*\s+(?:of|de)\s+([\d,\.]+)", text, re.I)
        if m:
            raw = m.group(1).replace(",", "").replace(".", "")
            try:
                total = int(raw)
                if total >= 1:
                    return total
            except ValueError:
                pass
    return None


def find_next_page_url(html: str) -> Optional[str]:
    """Find the URL for the next page."""
    soup = BeautifulSoup(html, "html.parser")
    next_span = soup.select_one(
        "div.paginate nav span.next.page.pagination-button"
    )
    if next_span:
        tag = next_span.find("a", href=True)
        if tag:
            href = tag["href"]
            return href if href.startswith("http") else BASE_URL + href
    tag = soup.find("link", rel="next")
    if tag and tag.get("href"):
        href = tag["href"]
        return href if href.startswith("http") else BASE_URL + href
    return None


def set_page_number(url: str, page: int) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def is_captcha_response(html: str) -> bool:
    if not html:
        return True
    html_lower = html.lower()
    signals = [
        "g-recaptcha", "h-captcha", "just a moment", "cf-challenge",
        "challenge-running", "access denied", "verifying you are human",
    ]
    return any(s in html_lower for s in signals)


# ─────────────────────────────────────────────────────────────────────────────
# DiscoveryService — async HTTP scraper
# ─────────────────────────────────────────────────────────────────────────────

class DiscoveryService:
    """
    Discovers property listing URLs from Pincali search pages
    and returns them as batches for DB insertion.
    """

    def __init__(
        self,
        max_pages_per_filter: int = 101,
        request_delay: float = 0.8,
        existing_urls: Optional[set] = None,
    ):
        self.max_pages = max_pages_per_filter
        self.delay = request_delay
        self.seen_urls: set = existing_urls or set()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        # PATCH: add proxy support
        # NOTE: get_next_proxy() returns the Playwright-shaped dict (server/
        # username/password split apart -- see as_playwright_proxy()'s
        # docstring), which is correct for browser.launch() but wrong here:
        # httpx's `proxy=` kwarg needs credentials embedded in the URL
        # itself (http://user:pass@host:port). Using just the bare
        # "server" value meant every discovery request hit the gateway
        # with NO auth at all, which is why it was returning 407 Proxy
        # Authentication Required on every single fetch.
        endpoint = get_proxy_manager().get_proxy_sync()
        proxy_url = endpoint.url if endpoint else None
        self._client = httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=30.0,
            http2=False,
            proxy=proxy_url,
        )
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _fetch(self, url: str) -> Optional[str]:
        if not self._client:
            raise RuntimeError("Use async context manager")
        try:
            resp = await self._client.get(url)
            if resp.status_code >= 400:
                # PATCH: log exact status code
                logger.warning(
                    f"[Discovery] HTTP {resp.status_code} for {url} "
                    f"(headers: {dict(resp.headers)})"
                )
                return None
            html = resp.text
            if is_captcha_response(html):
                logger.warning(f"[Discovery] CAPTCHA detected at {url}")
                return None
            return html
        except Exception as e:
            logger.warning(f"[Discovery] Fetch error for {url}: {e}")
            return None

    async def discover_filter(
        self, filter_def: Dict
    ) -> AsyncIterator[Dict]:
        """
        Yield URL batches discovered from a single filter.
        Each yield is a dict: {urls, filter_id, filter_name, page}
        """
        fid = filter_def["id"]
        fname = filter_def["name"]
        base_url = filter_def["url"]

        url = base_url
        page_num = 1
        total_pages: Optional[int] = None
        page_limit = self.max_pages

        while page_num <= page_limit:
            logger.info(f"[Discovery] Filter {fid} '{fname}' page {page_num}")
            html = await self._fetch(url)

            if html is None:
                logger.warning(f"[Discovery] Filter {fid} page {page_num} failed")
                break

            if total_pages is None:
                total_pages = extract_total_pages(html)
                if total_pages:
                    page_limit = min(total_pages, self.max_pages)

            links = extract_listing_urls_from_html(html)
            new_urls = [u for u in links if u not in self.seen_urls]
            for u in new_urls:
                self.seen_urls.add(u)

            if new_urls:
                yield {
                    "urls": new_urls,
                    "filter_id": fid,
                    "filter_name": fname,
                    "page": page_num,
                    "total_found": len(links),
                }

            if not links:
                break

            if page_num >= page_limit:
                break

            next_url = find_next_page_url(html)
            if next_url is None:
                break

            url = next_url
            page_num += 1
            await asyncio.sleep(self.delay)

    async def discover_all(
        self, filters: Optional[List[Dict]] = None
    ) -> AsyncIterator[Dict]:
        """Discover URLs from all filters, yielding batches."""
        if filters is None:
            filters = generate_discovery_filters()

        for filter_def in filters:
            async for batch in self.discover_filter(filter_def):
                yield batch
            await asyncio.sleep(self.delay * 2)