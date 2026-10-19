# Architecture

## Overview

The platform is split into three independently deployable services that share a single PostgreSQL (or MySQL) database.

```
┌────────────────────────────────────────────────────────────┐
│                        Docker Network                      │
│                                                            │
│  ┌──────────────────┐   queue    ┌──────────────────────┐  │
│  │  URL Discovery   │──────────▶│  Detail Scraper      │  │
│  │  Worker          │           │  Worker              │  │
│  │                  │           │                      │  │
│  │  pincali_optimized│          │  pincali_scraper_v6.3│  │
│  │  logic (reused)  │           │  logic (reused)      │  │
│  └────────┬─────────┘           └──────────┬───────────┘  │
│           │                                │               │
│           ▼                                ▼               │
│  ┌──────────────────────────────────────────────────────┐  │
│  │            PostgreSQL / MySQL Database               │  │
│  │  listing_urls  listings  listing_photos  scrape_runs │  │
│  └────────────────────────┬─────────────────────────────┘  │
│                           │                                │
│                    ┌──────▼──────┐                         │
│                    │  FastAPI    │                         │
│                    │  + Admin    │                         │
│                    │  Dashboard  │                         │
│                    └─────────────┘                         │
└────────────────────────────────────────────────────────────┘
```

## Services

### URL Discovery Worker (`app/workers/url_discovery_worker.py`)

Wraps the `pincali_optimized.py` logic in an async service loop.

- Generates 200+ filter combinations (property type × bedroom count × price tier × sort method).
- Paginates each filter URL, extracting `/en/home/*` listing hrefs.
- Deduplicates by URL before inserting into `listing_urls`.
- Records each discovery run in `scrape_runs`.
- Sleeps for `DISCOVERY_INTERVAL_SECONDS` between full passes.

### Detail Scraper Worker (`app/workers/detail_scraper_worker.py`)

Wraps the `pincali_scraper_v6.3.py` logic in an async service loop.

- Polls `listing_urls` for `status = 'pending'` rows in batches.
- For each URL: tries HTTP extraction first (fast); falls back to Playwright.
- Passes extracted data through the normaliser (field mapping + MXN→USD conversion).
- Upserts into `listings` (dedup by `external_id + source`); records field changes in `listing_changes`.
- Increments `scrape_attempts`; marks URLs `done` or `failed`.

### API (`app/main.py` + `app/api/`)

FastAPI app with:
- `GET /health`
- `GET /api/v1/listings` (paginated, filterable, sortable)
- `GET /api/v1/listings/{id}`
- `GET /api/v1/listings/search?q=`
- `GET /api/v1/listings/statistics`
- `GET /api/v1/scraper/status|runs|errors`
- `/admin` dashboard (Jinja2 templates)
- `/docs` — Swagger UI

## Database Schema

Eight tables (see `alembic/versions/001_initial.py`):

| Table | Purpose |
|---|---|
| `listing_urls` | URL discovery queue |
| `listings` | Normalised property data |
| `listing_photos` | Photo URLs + ordering |
| `listing_videos` | Video iframes |
| `virtual_tours` | Virtual tour links |
| `scrape_runs` | Per-run audit log |
| `listing_changes` | Field-level change history |
| `scraper_errors` | Error log with full traceback |

## Scraper Reuse

The existing scrapers are ported verbatim into `app/scraper/`:

- `app/scraper/core.py` — HTTPExtractor, DomainThrottler, CircuitBreaker, Playwright extraction, lat/lon helpers, DynamicKeywordLearner
- `app/scraper/discovery.py` — DiscoveryService, filter generation, pagination, URL extraction

## Configuration

All configuration flows through `app/core/config.py` (pydantic-settings). No hardcoded values anywhere in the codebase. See `.env.example`.

## Scaling

- Run multiple `detail_scraper_worker` containers to increase throughput (each is independent; DB-level row locking prevents double-processing).
- Increase `PINCALI_CONCURRENCY` per worker to add more async browser sessions.
- The `listing_urls` table acts as a distributed work queue — any number of workers can safely poll it simultaneously.
- For very high volume, place Redis in front (`PINCALI_REDIS_URL`) for queue distribution.
