# Pincali Platform 3.0 — Client Feedback: Solution Document

**Prepared for:** Arsalan (developer handoff)
**Based on:** Client feedback email + full codebase review of `pincali_platform_3.0_final_fixing`
**Date:** 2026-06-10

---

## Overview

The client has identified four remaining issues. This document explains each issue in precise technical detail — what the client wants, what is currently happening in the code, why it is happening, and exactly what must be changed (file name, function name, line-level detail) to fix it.

---

## Issue 1 — Emojis in Listing Name / Title

### What the client wants

> "The listing name/title is coming through with emojis, so we would like that sanitized into plain text as well."

The `name` field stored in the database and returned by all API endpoints should contain plain readable text — no emoji characters, no Unicode symbols, no invisible control characters.

### What is currently happening

The listing title is scraped from the `<h1>` tag on the detail page and stored in `raw["title_h1"]`. The normalizer then stores it as-is:

```python
# app/services/normalizer.py — line 469
"name": title_h1.strip() or None,
```

The `description` and `supplement` fields go through a dedicated sanitization function:

```python
"description": _sanitize_description(description_raw),
"supplement":  _sanitize_description(raw.get("status_message")),
```

`_sanitize_description()` correctly strips all emoji and symbol characters using `unicodedata.category(char) == "So"`, removes variation selectors (U+FE00–FE0F), and normalises whitespace. But `title_h1` bypasses this function entirely.

Mexican real estate listings frequently include emojis in their titles, for example:

```
🏡 Casa en Venta en Rinconada Jurica
✅ Hermosa residencia 3 recamaras 🌳
💰 PRECIO: $2,600,000
```

These pass straight through to the `name` column and into every API response.

### Root cause — single line

**File:** `app/services/normalizer.py`
**Function:** `normalize_property_record()`
**Line:** `"name": title_h1.strip() or None,`

`_sanitize_description()` is not called on `title_h1`.

### Fix

**File to change: `app/services/normalizer.py`**

In `normalize_property_record()`, change the `name` field assignment from:

```python
"name": title_h1.strip() or None,
```

to:

```python
"name": _sanitize_description(title_h1) or None,
```

`_sanitize_description()` already handles the `None` / empty string case (returns `None`), handles `.strip()` internally, and removes all the same emoji categories that appear in titles. No new function is needed.

**Note on `hub.py`:** The hub endpoint already lowercases `name` via `(db_listing.name or "").lower()`. This is intentional hub behaviour and does not need to change. The sanitization happens at write time in the normalizer, so the hub will automatically serve clean names.

---

## Issue 2 — Geographic Hierarchy Catalog as Separate Fields / Endpoints

### What the client wants

> "We would like the API to expose the full geographic hierarchy catalog as separate fields/endpoints where available, rather than only the current mapped values."

The client wants to be able to:
1. Retrieve all distinct values for each geographic level (state, city, community, subdivision) so they can build dropdowns, filters, and maps in their own UI.
2. Filter listings by any geographic level — not just city.

The geographic hierarchy the scraper captures is:

```
State → City → Community → Subdivision
e.g.  Querétaro → El Marqués → Ciudad Maderas Residencial → anillo vial III
```

### What is currently happening

**Database model** (`app/models/orm.py`): All four fields are present — `city`, `state`, `community`, `subdivision`. The data is already being stored correctly.

**API filters** (`app/schemas/listings.py`, `ListingFilters`): Only `city` is filterable. `state`, `community`, and `subdivision` are not filter parameters.

```python
class ListingFilters(BaseModel):
    city: Optional[str] = None      # ← only geo filter
    min_price: ...
    ...
    # state, community, subdivision NOT present
```

**Statistics endpoint** (`GET /api/v1/listings/statistics`): Only returns `listings_by_city` (top 10). No breakdown by state, community, or subdivision.

**No catalog endpoint exists**: There is no endpoint that returns the full list of distinct geographic values. A client wanting to know "what states are available?" or "what communities exist in Querétaro?" has no way to find out without fetching and deduplicating raw listings themselves.

### Root cause — missing repository method and missing endpoint

The data exists in the DB. What is missing is:
1. A repository method to query distinct values per geographic level.
2. A new API endpoint (or enhanced existing endpoint) to expose them.
3. Additional filter parameters wired through schema → repository → endpoint.

### Fix

This requires changes to four files.

---

#### File 1: `app/repositories/listings.py`

Add a new method `get_geography_catalog()` that returns all distinct non-null values for each geographic level, grouped hierarchically (states → cities within that state → communities within that city → subdivisions):

```python
async def get_geography_catalog(self) -> Dict:
    """
    Return all distinct geographic values grouped hierarchically.
    Used by GET /api/v1/listings/geography.
    """
    # Distinct states
    state_rows = await self.session.execute(
        select(Listing.state)
        .where(Listing.state.isnot(None))
        .distinct()
        .order_by(Listing.state)
    )
    states = [r[0] for r in state_rows]

    # Distinct cities with their parent state
    city_rows = await self.session.execute(
        select(Listing.state, Listing.city)
        .where(Listing.city.isnot(None))
        .distinct()
        .order_by(Listing.state, Listing.city)
    )
    cities = [{"state": r[0], "city": r[1]} for r in city_rows]

    # Distinct communities with their parent city and state
    community_rows = await self.session.execute(
        select(Listing.state, Listing.city, Listing.community)
        .where(Listing.community.isnot(None))
        .distinct()
        .order_by(Listing.state, Listing.city, Listing.community)
    )
    communities = [
        {"state": r[0], "city": r[1], "community": r[2]}
        for r in community_rows
    ]

    # Distinct subdivisions with their parent community, city, state
    subdivision_rows = await self.session.execute(
        select(Listing.state, Listing.city, Listing.community, Listing.subdivision)
        .where(Listing.subdivision.isnot(None))
        .distinct()
        .order_by(Listing.state, Listing.city, Listing.community, Listing.subdivision)
    )
    subdivisions = [
        {"state": r[0], "city": r[1], "community": r[2], "subdivision": r[3]}
        for r in subdivision_rows
    ]

    return {
        "states": states,
        "cities": cities,
        "communities": communities,
        "subdivisions": subdivisions,
    }
```

Also update `list_paginated()` to accept the new geo filters. Find the existing `if filters.city:` block and add three more filters immediately after it:

```python
# Existing
if filters.city:
    conditions.append(Listing.city.ilike(f"%{filters.city}%"))

# ADD THESE:
if filters.state:
    conditions.append(Listing.state.ilike(f"%{filters.state}%"))
if filters.community:
    conditions.append(Listing.community.ilike(f"%{filters.community}%"))
if filters.subdivision:
    conditions.append(Listing.subdivision.ilike(f"%{filters.subdivision}%"))
```

---

#### File 2: `app/schemas/listings.py`

**Part A** — Add `state`, `community`, `subdivision` to `ListingFilters`:

```python
class ListingFilters(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None          # ADD
    community: Optional[str] = None      # ADD
    subdivision: Optional[str] = None    # ADD
    min_price: Optional[float] = None
    ...
```

**Part B** — Add a new `GeographyCatalogResponse` schema at the bottom of the file:

```python
# ── Geography catalog ─────────────────────────────────────────
class GeoCityEntry(BaseModel):
    state: Optional[str]
    city: str

class GeoCommunityEntry(BaseModel):
    state: Optional[str]
    city: Optional[str]
    community: str

class GeoSubdivisionEntry(BaseModel):
    state: Optional[str]
    city: Optional[str]
    community: Optional[str]
    subdivision: str

class GeographyCatalogResponse(BaseModel):
    states: List[str]
    cities: List[GeoCityEntry]
    communities: List[GeoCommunityEntry]
    subdivisions: List[GeoSubdivisionEntry]
```

---

#### File 3: `app/api/v1/listings.py`

**Part A** — Add the new geography endpoint. Insert this block before the `/{listing_id}` route (it must come before wildcard routes):

```python
@router.get("/geography", response_model=GeographyCatalogResponse)
async def get_geography_catalog(db: AsyncSession = Depends(get_db)):
    """
    Return all distinct geographic values (state, city, community, subdivision)
    as a hierarchical catalog. Use this to populate filter dropdowns.
    Each level includes its parent values for easy grouping.
    """
    repo = ListingRepository(db)
    return await repo.get_geography_catalog()
```

Also add `GeographyCatalogResponse` to the imports from `app.schemas.listings`.

**Part B** — Add `state`, `community`, `subdivision` query parameters to `list_listings()`:

```python
@router.get("", response_model=PaginatedListings)
async def list_listings(
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None, description="Filter by state"),           # ADD
    community: Optional[str] = Query(None, description="Filter by community"),   # ADD
    subdivision: Optional[str] = Query(None, description="Filter by subdivision"), # ADD
    ...
):
    filters = ListingFilters(
        city=city,
        state=state,           # ADD
        community=community,   # ADD
        subdivision=subdivision, # ADD
        ...
    )
```

---

#### File 4: `app/api/v1/hub.py` *(minor)*

The `mls_area_major` field in the hub response is currently mapped as:

```python
mls_area_major=getattr(db_listing, "mls_area_major", None),
```

There is no `mls_area_major` column on the `Listing` model — `getattr` returns `None` silently. This field should be mapped to `state` (the general area within Mexico), since that is the closest semantic equivalent:

```python
mls_area_major=db_listing.state,   # state = general area / region
```

---

## Issue 3 — Scrape Runs Page: Show All Runs, Not Last 50

### What the client wants

> "On the scrape runs page, it would be helpful to show all runs if possible, not only the last 50."

The client wants to see the full history of every scrape run, not just the most recent 50.

### What is currently happening

**File:** `app/dashboard/routes.py`
**Function:** `admin_runs()`

The query is hard-capped at 50:

```python
result = await db.execute(
    select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(50)
)
```

The template also displays "last 50 runs" as a static subtitle:

```html
<!-- app/dashboard/templates/runs.html — line 3 -->
<span class="sub">last 50 runs</span>
```

As the system runs continuously, this cap means the client loses visibility into older runs.

### Fix

**File 1: `app/dashboard/routes.py`**

Add pagination support to `admin_runs()`. Replace the hard-coded `.limit(50)` with a page-based query:

```python
@router.get("/runs", response_class=HTMLResponse)
async def admin_runs(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    # Total count for pagination controls
    total = await db.scalar(select(func.count()).select_from(ScrapeRun)) or 0

    offset = (page - 1) * page_size
    result = await db.execute(
        select(ScrapeRun)
        .order_by(ScrapeRun.started_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    runs = result.scalars().all()
    pages = math.ceil(total / page_size) if page_size else 1

    # Per-run error counts (unchanged)
    run_ids = [r.id for r in runs]
    error_counts: dict = {}
    if run_ids:
        rows = await db.execute(
            select(ScraperError.run_id, func.count().label("cnt"))
            .where(ScraperError.run_id.in_(run_ids))
            .group_by(ScraperError.run_id)
        )
        error_counts = {row.run_id: row.cnt for row in rows}

    return templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "active": "runs",
            "runs": runs,
            "error_counts": error_counts,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        },
    )
```

**File 2: `app/dashboard/templates/runs.html`**

**Change 1** — Update the subtitle to show the real count dynamically:

```html
<!-- BEFORE -->
<span class="sub">last 50 runs</span>

<!-- AFTER -->
<span class="sub">{{ total | format_number }} total runs</span>
```

**Change 2** — Add pagination controls at the bottom of the template, after the closing `</div>` of the table card:

```html
{% if pages > 1 %}
<div style="display:flex;justify-content:center;gap:8px;margin-top:16px;align-items:center;">
    {% if page > 1 %}
    <a href="?page={{ page - 1 }}&page_size={{ page_size }}" class="btn btn-sm">← Prev</a>
    {% endif %}
    <span style="color:var(--text-sub);font-size:13px;">
        Page {{ page }} of {{ pages }} ({{ total | format_number }} runs)
    </span>
    {% if page < pages %}
    <a href="?page={{ page + 1 }}&page_size={{ page_size }}" class="btn btn-sm">Next →</a>
    {% endif %}
</div>
{% endif %}
```

**Change 3** — Update the summary counters at the top of the page. Currently they only count runs in the current page. Add a note making it clear the cards reflect the current page:

```html
<!-- Change the "Total Runs" card -->
<div class="stat-value" style="font-size:28px;">{{ total | format_number }}</div>
<div class="stat-label">Total Runs (all time)</div>
```

---

## Issue 4 — Scrape Runs Page: Counters Showing Only 0s

### What the client wants

> "The counters on the scrape runs page still do not appear to be displaying the correct data. Right now they are showing only 0s."

The columns Discovered, Scraped, Failed URLs, New Listings, and Updated in the runs table should reflect the actual number of URLs processed and listings created/updated during each run.

### What is currently happening — root cause traced in detail

#### Bug A — The worker creates "ghost" runs with zero counts (primary cause)

**File:** `app/workers/detail_scraper_worker.py`
**Function:** `run_loop()`

```python
pending = counts.get("pending", 0)
done    = counts.get("done", 0)
has_work = pending > 0 or done > 0   # ← THE BUG
```

The `or done > 0` condition means the worker triggers `run_once()` whenever there are any completed URLs in the queue — which is true permanently after the very first scrape run. This causes `run_once()` to be called on every poll cycle even when `pending = 0`.

Inside `run_once()`, the first thing that happens is a new `ScrapeRun` row is created in the database:

```python
run = ScrapeRun(
    run_type="detail",
    status="running",
    ...
)
session.add(run)
await session.commit()
```

Then the batch loop checks for pending URLs:

```python
while True:
    pending = await url_repo.get_pending(limit=batch_size)
    if not pending:
        break        # ← exits immediately when nothing to do
```

Because `pending = 0`, it breaks immediately. The `overall` dict stays at all zeros:

```python
overall = {"processed": 0, "created": 0, "updated": 0, "failed": 0}
```

At the end of `run_once()`, these zeros are written back to the `ScrapeRun`:

```python
final_run.urls_scraped    = overall.get("processed", 0)   # → 0
final_run.urls_failed     = overall.get("failed", 0)       # → 0
final_run.listings_created = overall.get("created", 0)    # → 0
final_run.listings_updated = overall.get("updated", 0)    # → 0
final_run.status = "completed"
```

**Result:** The database fills up with "completed" `ScrapeRun` rows where every counter is 0. Since the dashboard query orders by `started_at DESC` and limits to 50, all 50 visible rows are these ghost runs. The rare real runs (which do have counts) are pushed off the page.

#### Bug B — `urls_discovered` is never set by the detail worker (minor, but confusing)

The `ScrapeRun` model has a `urls_discovered` column. This is populated by the discovery worker, not the detail worker. The detail worker never sets it, so it always shows 0 in detail-type runs. This is actually correct behaviour for detail runs, but the column label "Discovered" in the runs table is misleading when it shows 0 for all detail runs. This should be documented or the column should render `—` for detail runs.

### Fix

#### Fix A — `app/workers/detail_scraper_worker.py`

Change the `has_work` condition in `run_loop()` from:

```python
has_work = pending > 0 or done > 0
```

to:

```python
has_work = pending > 0
```

This is the single-line fix that stops ghost runs from being created. The `done > 0` condition was intended to handle re-queueing of stale URLs, but that logic is already handled inside `run_once()` itself (via `reset_stale_scraping()` and `requeue_stale_done()`), so the outer check only needs to know if there is something currently pending to scrape.

Full corrected block:

```python
pending = counts.get("pending", 0)
has_work = pending > 0   # Only trigger when there is actually pending work

if has_work:
    logger.info(f"[Scraper] {pending} pending URLs — starting scrape run")
    stats = await self.run_once()
    logger.info(f"[Scraper] Run complete: {stats}")
else:
    logger.debug("[Scraper] No pending URLs, sleeping...")
```

#### Fix B — `app/dashboard/templates/runs.html`

Make the "Discovered" column render `—` for detail-type runs (since detail runs don't discover URLs):

```html
<!-- BEFORE -->
<td>{{ r.urls_discovered | default(0) | format_number }}</td>

<!-- AFTER -->
<td style="text-align:right;">
    {% if r.run_type == 'discovery' %}
        {{ r.urls_discovered | default(0) | format_number }}
    {% else %}
        <span style="color:var(--text-dim);">—</span>
    {% endif %}
</td>
```

#### Fix C — Clean up historical ghost runs (optional but recommended)

After deploying Fix A, the existing ghost runs remain in the database. Run this SQL to remove them (or archive them to a separate table if history is needed):

```sql
DELETE FROM scrape_runs
WHERE run_type = 'detail'
  AND urls_scraped = 0
  AND listings_created = 0
  AND listings_updated = 0
  AND urls_failed = 0
  AND status = 'completed';
```

---

## Summary Table

| # | Client Feedback | Root Cause | Files to Change |
|---|---|---|---|
| 1 | Emojis in listing name/title | `_sanitize_description()` not applied to `title_h1` in `normalize_property_record()` | `app/services/normalizer.py` |
| 2 | Geographic hierarchy catalog as separate fields/endpoints | No geography catalog endpoint; `ListingFilters` only supports `city`; no `state`/`community`/`subdivision` filter params | `app/repositories/listings.py`, `app/schemas/listings.py`, `app/api/v1/listings.py`, `app/api/v1/hub.py` |
| 3 | Show all runs, not last 50 | Hard-coded `.limit(50)` in `admin_runs()`; no pagination | `app/dashboard/routes.py`, `app/dashboard/templates/runs.html` |
| 4 | Run counters showing 0s | `has_work = pending > 0 or done > 0` in `run_loop()` creates ghost `ScrapeRun` rows with zero counts on every poll cycle | `app/workers/detail_scraper_worker.py`, `app/dashboard/templates/runs.html` |

---

## Files That Will Be Changed After All Work Is Completed

1. `app/services/normalizer.py`
2. `app/repositories/listings.py`
3. `app/schemas/listings.py`
4. `app/api/v1/listings.py`
5. `app/api/v1/hub.py`
6. `app/dashboard/routes.py`
7. `app/dashboard/templates/runs.html`
8. `app/workers/detail_scraper_worker.py`
