# Real Estate Scraping & ETL Platform

![Python](https://img.shields.io/badge/Python-3.11-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688) ![PostgreSQL](https://img.shields.io/badge/PostgreSQL-MySQL-336791) ![Playwright](https://img.shields.io/badge/Playwright-scraping-2EAD33) ![Docker](https://img.shields.io/badge/Docker-Compose-2496ED) ![Tests](https://github.com/salmantariq596/real-estate-scraping-platform/actions/workflows/tests.yml/badge.svg)

**At a glance:** two async workers (URL discovery → detail scraping) feed a normalized PostgreSQL/MySQL store, served by a FastAPI REST API and an admin dashboard. HTTP-first scraping with Playwright fallback, rotating residential proxies (Webshare gateway) with quota handling, circuit breaker, MXN→USD normalization and change tracking.


A production-ready scraping and data pipeline platform for [Pincali.com](https://www.pincali.com). Discovers, scrapes, normalises, and serves Mexican real estate listings through a FastAPI REST API and admin dashboard.

---

## Features

- **URL Discovery Service** — Crawls Pincali with 200+ filter combinations to discover all listing URLs; fully resumable with checkpoint support.
- **Detail Scraper Service** — Consumes the URL queue; HTTP-first with Playwright fallback, circuit breaker, anti-bot handling, and dynamic keyword learning.
- **Normalisation Pipeline** — Cleans, validates, and converts raw scraped data (MXN→USD exchange rate, field mapping, change tracking).
- **PostgreSQL / MySQL** — Configurable via `.env`; full SQLAlchemy ORM + Alembic migrations.
- **FastAPI REST API** — Paginated listing endpoints with filtering, search, statistics, and scraper status.
- **Jinja2 Admin Dashboard** — Internal dashboard at `/admin` for real-time monitoring.
- **Docker-first** — Single `docker-compose up --build` starts everything.

---

## Quick Start (Docker)

```bash
# 1. Clone / copy the project
cp .env.example .env          # edit credentials as needed

# 2. Start all services
docker-compose up --build

# 3. Explore
open http://localhost:8000/docs    # Swagger UI
open http://localhost:8000/admin   # Admin dashboard
```

### MySQL variant

```bash
docker-compose --profile mysql up --build
# and set DATABASE_TYPE=mysql in .env
```

---

## Local Development

```bash
./scripts/setup_dev.sh            # creates venv, installs deps, Playwright
source .venv/bin/activate

# Start PostgreSQL (Docker)
docker-compose up postgres -d

# Apply migrations
./scripts/migrate.sh

# Start the API
uvicorn app.main:app --reload

# Run URL discovery (once)
./scripts/run_discovery.sh

# Run detail scraper
./scripts/run_scraper.sh
```

---

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_TYPE` | `postgresql` | `postgresql` or `mysql` |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `MXN_TO_USD_RATE` | `20.5` | Fallback exchange rate |
| `PINCALI_CONCURRENCY` | `5` | Scraper worker count |
| `PINCALI_HTTP_FIRST` | `true` | Try plain HTTP before Playwright |
| `DISCOVERY_INTERVAL_SECONDS` | `3600` | How often discovery re-runs |

---

## Project Layout

```
app/
  api/v1/           REST API routes
  core/             Config + logging
  dashboard/        Jinja2 admin dashboard
  db/               SQLAlchemy session / engine
  models/           ORM models
  repositories/     DB access layer
  schemas/          Pydantic schemas
  scraper/          Reused v6.3 scraper + discovery logic
  services/         Exchange rate, normaliser
  utils/            Shared helpers
  workers/          Discovery + detail scraper workers
alembic/            DB migrations
docs/               Architecture, API, deployment docs
scripts/            Helper shell scripts
tests/              pytest test suite
```

---

## Testing

```bash
pytest -v                    # all tests
pytest tests/test_api.py     # API tests only
pytest tests/test_db.py      # repository tests
pytest tests/test_scraper.py # scraper unit tests
```

---

## Documentation

| Doc | Description |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and service overview |
| [API.md](docs/API.md) | REST API reference |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production deployment guide |
| [SCRAPER_FLOW.md](docs/SCRAPER_FLOW.md) | URL discovery and scraping pipeline |

---

## Proxy check (`test_webshare_proxies.py`)

Before a long run, checks every proxy in the list: OK/FAIL, latency and exit IP, and confirms the IPs really rotate.

```bash
python test_webshare_proxies.py
```

## Standalone resumable scraper (`tools/resumable_scraper.py`)

The single-file predecessor of the platform, kept as a reference for crash-safe scraping:
atomic writes (temp file + rename), an append-only `progress.log` for recovery after a crash,
`resume` / `retry-failed` / `migrate` modes, browser restarts every N URLs to cap memory, and
JSON → CSV + JSONL export.

```bash
python tools/resumable_scraper.py resume
```

## Author

**Salman Tariq** — Backend & Python Data Engineer · [GitHub](https://github.com/salmantariq596)

