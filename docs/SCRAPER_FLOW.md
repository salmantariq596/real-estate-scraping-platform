# Scraper Flow

## Overview

The scraping pipeline has two distinct phases that run as independent services:

```
Phase 1: URL Discovery          Phase 2: Detail Scraping
─────────────────────           ────────────────────────
Pincali listing pages    →     listing_urls table   →    Listing detail pages
(filter/pagination)            (pending queue)            (full property data)
```

---

## Phase 1 — URL Discovery

**Service:** `URLDiscoveryWorker` (`app/workers/url_discovery_worker.py`)  
**Core logic:** `DiscoveryService` (`app/scraper/discovery.py`) — ported from `pincali_optimized.py`

### Filter Generation

`generate_discovery_filters()` produces 200+ filter combinations covering:

- **Operations:** buy, rent
- **Property types:** apartments, houses, villas, land, commercial, etc.
- **Bedroom ranges:** studio, 1, 2, 3, 4+
- **Price tiers:** 8 buy tiers (under 500k → over 50m MXN), 6 rent tiers
- **Area tiers:** floor area and lot size ranges
- **Age tiers:** year built ranges
- **Cross-filters:** bedroom × price, bedroom × bathroom combinations

Each filter maps to a Pincali search URL, e.g.:

```
https://www.pincali.com/en/properties/apartments-for-sale?min_price=500000&max_price=1000000&min_bedroom=2&max_bedroom=2
```

### Pagination

For each filter URL:

1. Fetch the page (HTTP with cloudscraper fallback).
2. Parse the `<div class="pagination-summary">Page N of M</div>` element to detect total pages.
3. Extract all `/en/home/<id>-<slug>` href links from the page.
4. Follow `<span class="next page pagination-button"><a>` to the next page.
5. Stop when no next link is found, or after `MAX_PAGES_PER_FILTER` (101) pages.

### Deduplication

Before inserting to `listing_urls`, the worker checks:
- In-memory set of already-discovered URLs this run.
- `listing_urls.url` unique constraint in the DB (upsert ignores duplicates).

### CAPTCHA Handling

When a CAPTCHA is detected (Cloudflare challenge, hcaptcha, etc.):
- The filter is paused and queued in `captcha_queue.json`.
- Other filters continue.
- Run with `--captcha` flag to retry queued pages with a visible browser.

### Checkpoint / Resume

Every 10 pages, the worker writes a checkpoint:
- `completed_filters`, `failed_filters`, `seen_urls`
- Resume picks up from the last completed filter.

---

## Phase 2 — Detail Scraping

**Service:** `DetailScraperWorker` (`app/workers/detail_scraper_worker.py`)  
**Core logic:** `scrape_url()` + `HTTPExtractor` + `extract_property_data_playwright()` — ported from `pincali_scraper_v6.3.py`

### HTTP-First Strategy

```
For each pending URL:
  1. Try plain HTTP (httpx, with browser-like headers)
     → Parse HTML with HTTPExtractor (regex-based, fast, no browser)
     → If property_id + title/price extracted: SUCCESS → skip Playwright

  2. If HTTP fails or content incomplete:
     → Launch Playwright (Chromium, headless)
     → Block images/fonts/stylesheets (faster)
     → Navigate to URL
     → Wait for .listing selector
     → Check for CAPTCHA
     → Extract with page.query_selector() calls
```

### Data Extraction

`HTTPExtractor` and `extract_property_data_playwright()` extract:

- **Identity:** `property_id`, `product_url`, listing status
- **Price:** `price_digits`, `currency`, `operation_type`
- **Location:** `title_h1`, `location_subtitle`, `address`, lat/lon
- **Description:** full text from `span[data-translation]`
- **Agent:** name, agency, phone, WhatsApp, logo
- **Features:** bedroom/bathroom/parking/lot/area/year built (from `.feature-icon` elements)
- **Amenities:** grouped lists from `.amenities-group` elements
- **Media:** all `assets.easybroker.com` image URLs, video iframes, virtual tour links
- **Metadata:** JSON-LD, meta description, og:image

### Dynamic Keyword Learning

`DynamicKeywordLearner` runs alongside extraction:
- Learns city names, amenity terms, view types, property types from descriptions.
- Persists learned keywords to a JSON cache.
- Uses learned keywords to enhance extracted records with `detected_city`, `detected_view`, `detected_amenities`.

### Normalisation

`normalize_property_record()` (`app/services/normalizer.py`) maps raw scraper output to DB schema:

- Converts `price_digits` + `currency` to `price_usd` (MXN→USD via exchange rate service).
- Maps `listing_status`/`status_message` to `mls_status` (`Active`, `Sold`, `Pending`, `Cancelled`).
- Maps `listing_type` to `property_type` enum.
- Parses `location_subtitle` into `city`, `state`, `community`.
- Parses `features` dict into typed numeric fields (`beds_total`, `baths_full`, `lot_size_m2`, etc.).
- Splits amenities into `interior_features` and `exterior_features` lists.

### Upsert and Change Tracking

`ListingRepository.upsert()`:
1. Queries `listings` by `(external_id, source)`.
2. If new → INSERT.
3. If exists → compare all fields; for each changed field, INSERT a row into `listing_changes` with `old_value` and `new_value`.
4. UPDATE the listing row; set `last_seen_at = NOW()`.

### Circuit Breaker

`DomainThrottler` tracks failures per domain:
- After 5 failures within 5 minutes → domain is "circuit open" for 600 seconds.
- All queued URLs for that domain are skipped until the circuit resets.

### Browser Restart

Each Playwright browser instance is restarted every 100 URLs to prevent memory leaks and stale state.

---

## Error Handling

All errors are:
1. Logged to `app.scraper` logger (rotating file + console).
2. Inserted into `scraper_errors` table with full traceback and context JSON.
3. The `listing_urls` row has `scrape_attempts` incremented.
4. After `MAX_RETRIES_PER_URL` attempts the row is marked `failed`.

---

## Adding a New Source

1. Create a new discovery service in `app/scraper/` implementing `DiscoveryService`-like interface.
2. Add a new worker class inheriting patterns from `URLDiscoveryWorker`.
3. The detail scraper, normaliser, and API are source-agnostic — no changes needed.
4. Add source-specific normalisation logic in `normalize_property_record()` if the raw field names differ.
