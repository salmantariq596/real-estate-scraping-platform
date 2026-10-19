# Pincali ETL — Complete Commands Reference

> Auto-generated command reference for deploying and operating the Pincali scraper platform.
> Covers: setup, database, workers, API, monitoring, troubleshooting, and data fixes.

---

## Table of Contents

1. [Project Setup](#1-project-setup)
2. [Environment Configuration](#2-environment-configuration)
3. [Database Operations](#3-database-operations)
4. [Build & Run (Docker)](#4-build--run-docker)
5. [Worker Operations](#5-worker-operations)
6. [API & Dashboard](#6-api--dashboard)
7. [Monitoring & Logs](#7-monitoring--logs)
8. [Data Fixes & Maintenance](#8-data-fixes--maintenance)
9. [Troubleshooting](#9-troubleshooting)
10. [Scaling & Production](#10-scaling--production)

---

## 1. Project Setup

### 1.1 Clone & Initial Setup

```bash
# Clone the repository
git clone <repo-url>
cd pincali_platform

# Create environment file
cp .env.example .env

# Edit .env with your settings
nano .env
```

### 1.2 Required .env Variables

```bash
# Database (PostgreSQL)
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=pincali
POSTGRES_USER=pincali
POSTGRES_PASSWORD=<strong-password>

# Database (MySQL alternative)
# DATABASE_TYPE=mysql
# MYSQL_HOST=mysql
# MYSQL_PORT=3306
# MYSQL_DB=pincali
# MYSQL_USER=pincali
# MYSQL_PASSWORD=<password>

# Security
SECRET_KEY=<random-64-char-string>

# Exchange Rate
MXN_TO_USD_RATE=17.5
# EXCHANGE_RATE_MODE=live
# EXCHANGE_RATE_API_KEY=<your-key>

# Scraper Settings
PINCALI_HEADLESS=true
PINCALI_CONCURRENCY=3
PINCALI_HTTP_FIRST=true
PINCALI_BROWSER_RESTART_EVERY=100
SCRAPER_BATCH_SIZE=50
SCRAPER_POLL_INTERVAL=60

# Discovery Settings
DISCOVERY_INTERVAL_SECONDS=3600
DISCOVERY_MAX_PAGES_PER_FILTER=101

# Proxy (optional)
# PROXY_LIST=http://proxy1:8080,http://proxy2:8080
```

---

## 2. Environment Configuration

### 2.1 Python Virtual Environment (Non-Docker)

```bash
# Create virtual environment
python -m venv venv

# Activate (Linux/Mac)
source venv/bin/activate

# Activate (Windows)
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### 2.2 Pre-commit Hooks (Optional)

```bash
# Install pre-commit
pip install pre-commit

# Install hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

---

## 3. Database Operations

### 3.1 Run Migrations (First Time)

```bash
# Docker
 docker-compose run --rm migrator

# Or manually with Alembic
alembic upgrade head
```

### 3.2 Create Migration

```bash
# Auto-generate from model changes
alembic revision --autogenerate -m "description_of_changes"

# Apply
alembic upgrade head

# Rollback one
alembic downgrade -1

# Rollback to base
alembic downgrade base
```

### 3.3 Database Inspection

```bash
# Connect to PostgreSQL container
 docker-compose exec db psql -U pincali -d pincali

# List tables
\dt

# Count listings
SELECT COUNT(*) FROM listings;

# Count by status
SELECT mls_status, COUNT(*) FROM listings GROUP BY mls_status;

# Count by property type
SELECT property_type, COUNT(*) FROM listings GROUP BY property_type;

# Check URL queue status
SELECT status, COUNT(*) FROM listing_urls GROUP BY status;

# Recent scrape runs
SELECT id, run_type, status, urls_scraped, listings_created, started_at 
FROM scrape_runs 
ORDER BY started_at DESC 
LIMIT 10;

# Exit psql
\q
```

### 3.4 Reset Database (DESTRUCTIVE)

```bash
# Drop all tables and re-migrate
alembic downgrade base
alembic upgrade head

# Or via Docker
 docker-compose down -v
 docker-compose up -d db
 docker-compose run --rm migrator
```

---

## 4. Build & Run (Docker)

### 4.1 Full Stack Startup

```bash
# Build and start all services
 docker-compose up --build -d

# Start without rebuilding
 docker-compose up -d

# Start specific services only
 docker-compose up -d api db

# Start with MySQL profile
 docker-compose --profile mysql up -d
```

### 4.2 Verify Health

```bash
# API health check
curl http://localhost:8000/health

# Expected response:
# {"status": "ok", "version": "1.0.0"}

# Swagger docs
open http://localhost:8000/docs

# Admin dashboard
open http://localhost:8000/admin
```

### 4.3 Stop Services

```bash
# Stop gracefully
 docker-compose down

# Stop and remove volumes (DESTRUCTIVE)
 docker-compose down -v

# Stop specific service
 docker-compose stop detail_scraper_worker

 docker-compose stop url_discovery_worker
```

### 4.4 Rebuild Single Service

```bash
# Rebuild API only
 docker-compose up -d --build api

# Rebuild worker only
 docker-compose up -d --build detail_scraper_worker
```

---

## 5. Worker Operations

### 5.1 Start Workers

```bash
# Start URL discovery worker
 docker-compose up -d url_discovery_worker

# Start detail scraper worker
 docker-compose up -d detail_scraper_worker

# Start multiple scraper workers
 docker-compose up -d --scale detail_scraper_worker=3

# Start all workers
 docker-compose up -d url_discovery_worker detail_scraper_worker
```

### 5.2 Run Workers Manually (One-Off)

```bash
# Discovery worker (single pass)
 docker-compose run --rm url_discovery_worker python -m app.workers.url_discovery_worker

# Detail scraper (single pass)
 docker-compose run --rm detail_scraper_worker python -m app.workers.detail_scraper_worker

# Or with virtual environment
source venv/bin/activate
python -m app.workers.url_discovery_worker
python -m app.workers.detail_scraper_worker
```

### 5.3 Worker Logs

```bash
# Follow discovery worker logs
 docker-compose logs -f url_discovery_worker

# Follow scraper worker logs
 docker-compose logs -f detail_scraper_worker

# Last 100 lines
 docker-compose logs --tail=100 detail_scraper_worker

# All logs with timestamps
 docker-compose logs -t detail_scraper_worker
```

### 5.4 Restart Workers

```bash
# Restart single worker
 docker-compose restart detail_scraper_worker

# Restart all workers
 docker-compose restart url_discovery_worker detail_scraper_worker

# Force recreate (pick up code changes)
 docker-compose up -d --force-recreate detail_scraper_worker
```

---

## 6. API & Dashboard

### 6.1 API Endpoints

```bash
# List listings (paginated)
curl "http://localhost:8000/api/v1/listings?page=1&page_size=20"

# Filter by city
curl "http://localhost:8000/api/v1/listings?city=cabo"

# Filter by price range
curl "http://localhost:8000/api/v1/listings?min_price=100000&max_price=500000"

# Filter by property type
curl "http://localhost:8000/api/v1/listings?property_type=B"

# Filter by status
curl "http://localhost:8000/api/v1/listings?mls_status=Active"

# Search
curl "http://localhost:8000/api/v1/listings/search?q=villa&limit=10"

# Get single listing
curl "http://localhost:8000/api/v1/listings/1"

# Statistics
curl "http://localhost:8000/api/v1/listings/statistics"

# Scraper status
curl "http://localhost:8000/api/v1/scraper/status"

# Scraper runs
curl "http://localhost:8000/api/v1/scraper/runs"

# Scraper errors
curl "http://localhost:8000/api/v1/scraper/errors"

# Hub listings (client format)
curl "http://localhost:8000/api/v1/hub/listings?page=1&page_size=50"
```

### 6.2 Dashboard URLs

```bash
# Main dashboard
open http://localhost:8000/admin

# Listings table
open http://localhost:8000/admin/listings

# Scrape runs
open http://localhost:8000/admin/runs

# Error log
open http://localhost:8000/admin/errors

# Statistics
open http://localhost:8000/admin/stats
```

---

## 7. Monitoring & Logs

### 7.1 Application Logs

```bash
# API logs
 docker-compose logs -f api

# All service logs
 docker-compose logs -f

# Save logs to file
 docker-compose logs > logs_$(date +%Y%m%d_%H%M%S).txt
```

### 7.2 Database Query Monitoring

```bash
# Connect and monitor active queries
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT pid, state, query_start, query 
FROM pg_stat_activity 
WHERE state = 'active';
"

# Long-running queries
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT pid, now() - query_start AS duration, query 
FROM pg_stat_activity 
WHERE state = 'active' AND now() - query_start > interval '5 seconds';
"
```

### 7.3 Worker State Inspection

```bash
# Check pending URLs
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT status, COUNT(*) FROM listing_urls GROUP BY status;
"

# Check recent scrapes
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT id, status, urls_scraped, listings_created, listings_updated, started_at, finished_at 
FROM scrape_runs 
ORDER BY started_at DESC 
LIMIT 5;
"

# Check recent errors
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT id, error_type, error_message, created_at 
FROM scraper_errors 
ORDER BY created_at DESC 
LIMIT 10;
"
```

---

## 8. Data Fixes & Maintenance

### 8.1 Fix 01: Verify Property Type Distribution

```bash
# Check distribution after fix
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT property_type, COUNT(*) 
FROM listings 
GROUP BY property_type 
ORDER BY COUNT(*) DESC;
"

# Check unmapped types in extra field
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT extra->>'listing_type_raw' AS raw_type, COUNT(*) 
FROM listings 
WHERE property_type IS NULL AND extra->>'listing_type_raw' IS NOT NULL 
GROUP BY extra->>'listing_type_raw';
"
```

### 8.2 Fix 02: Verify Updated At Has Time

```bash
# Check API response includes time
curl -s "http://localhost:8000/api/v1/listings?page=1&page_size=1" | python -m json.tool

# Check database has full timestamp
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT id, updated_at, updated_at::time AS time_component 
FROM listings 
ORDER BY updated_at DESC 
LIMIT 5;
"
```

### 8.3 Fix 03: Verify City Field

```bash
# Check city vs title contamination
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT name, city, extra->>'location_subtitle_raw' AS raw_subtitle 
FROM listings 
WHERE LENGTH(city) > 50 OR city = name 
LIMIT 10;
"
```

### 8.4 Fix 04: Clean Logo Photos (One-Time Migration)

```bash
# Delete logo photos from existing data
 docker-compose exec db psql -U pincali -d pincali -c "
DELETE FROM listing_photos 
WHERE url IN (
    SELECT DISTINCT lp.url 
    FROM listing_photos lp 
    JOIN listings l ON lp.listing_id = l.id 
    WHERE l.extra->>'agency_logo_url' = lp.url 
       OR lp.url ~* '/(logo|brand|agency|agent|office|watermark|favicon|icon)[^/]*\.'
);
"

# Re-number positions after deletion
 docker-compose exec db psql -U pincali -d pincali -c "
WITH ranked AS (
    SELECT id, listing_id, 
           ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY position) - 1 AS new_pos
    FROM listing_photos
)
UPDATE listing_photos lp
SET position = r.new_pos,
    is_primary = (r.new_pos = 0)
FROM ranked r
WHERE lp.id = r.id;
"
```

### 8.5 Fix 05: Clean Emoji from Existing Descriptions

```bash
# One-time cleanup of existing descriptions
 docker-compose exec db psql -U pincali -d pincali -c "
UPDATE listings 
SET description = regexp_replace(
    description,
    '[\U0001F300-\U0001FAFF\U00002600-\U000027BF]+',
    '',
    'g'
)
WHERE description ~ '[\U0001F300-\U0001FAFF\U00002600-\U000027BF]';
"

# Verify cleanup
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT id, LEFT(description, 100) 
FROM listings 
WHERE description ~ '[\U0001F300-\U0001FAFF\U00002600-\U000027BF]' 
LIMIT 5;
"
```

### 8.6 Fix 06: Verify Run Counters

```bash
# Check runs have non-zero counters
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT id, run_type, status, urls_scraped, urls_failed, listings_created, listings_updated 
FROM scrape_runs 
WHERE status = 'completed' 
ORDER BY started_at DESC 
LIMIT 5;
"
```

### 8.7 Re-scrape Failed URLs

```bash
# Reset failed URLs for retry
 docker-compose exec db psql -U pincali -d pincali -c "
UPDATE listing_urls 
SET status = 'pending', scrape_attempts = 0, error_message = NULL 
WHERE status = 'failed';
"

# Reset specific URL
 docker-compose exec db psql -U pincali -d pincali -c "
UPDATE listing_urls 
SET status = 'pending', scrape_attempts = 0 
WHERE url = 'https://www.pincali.com/en/home/XXXXX';
"
```

### 8.8 Clear Stuck Scraping URLs

```bash
# Reset URLs stuck in 'scraping' state
 docker-compose exec db psql -U pincali -d pincali -c "
UPDATE listing_urls 
SET status = 'pending', updated_at = NOW() 
WHERE status = 'scraping';
"
```

---

## 9. Troubleshooting

### 9.1 Worker Not Processing URLs

```bash
# Check if URLs are in queue
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT status, COUNT(*) FROM listing_urls GROUP BY status;
"

# Check worker logs for errors
 docker-compose logs --tail=50 detail_scraper_worker

# Restart worker
 docker-compose restart detail_scraper_worker

# Check if worker is running
 docker-compose ps detail_scraper_worker
```

### 9.2 Playwright / Browser Issues

```bash
# Reinstall Playwright browsers
 docker-compose run --rm detail_scraper_worker playwright install chromium

# Check browser is launching
 docker-compose logs -f detail_scraper_worker | grep -i "browser\|playwright\|chromium"

# Test with headless=false (visible browser) for debugging
# Edit .env: PINCALI_HEADLESS=false
 docker-compose up -d --force-recreate detail_scraper_worker
```

### 9.3 CAPTCHA Blocks

```bash
# Check for CAPTCHA errors
 docker-compose logs detail_scraper_worker | grep -i "captcha\|challenge\|cloudflare"

# Add proxy to .env and restart
# PROXY_LIST=http://user:pass@proxy:port
 docker-compose up -d --force-recreate detail_scraper_worker

# Increase rate limits
# PINCALI_RATE_LIMIT_MIN=2.0
# PINCALI_RATE_LIMIT_MAX=5.0
```

### 9.4 Database Connection Issues

```bash
# Check DB is running
 docker-compose ps db

# Check DB logs
 docker-compose logs db

# Test connection
 docker-compose exec db pg_isready -U pincali

# Restart DB
 docker-compose restart db
```

### 9.5 Low Listing Count

```bash
# Check discovery filters
 docker-compose logs url_discovery_worker | grep -i "filter\|discovered"

# Check if discovery is finding URLs
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT COUNT(*) FROM listing_urls WHERE discovered_at > NOW() - INTERVAL '24 hours';
"

# Manually trigger discovery
 docker-compose run --rm url_discovery_worker python -m app.workers.url_discovery_worker

# Check for blocked/filtered URLs
 docker-compose exec db psql -U pincali -d pincali -c "
SELECT filter_name, COUNT(*) 
FROM listing_urls 
GROUP BY filter_name 
ORDER BY COUNT(*) DESC;
"
```

### 9.6 Memory Issues (Playwright)

```bash
# Monitor memory usage
 docker stats

# Reduce concurrency
# PINCALI_CONCURRENCY=1
 docker-compose up -d --force-recreate detail_scraper_worker

# Reduce batch size
# SCRAPER_BATCH_SIZE=25
 docker-compose up -d --force-recreate detail_scraper_worker

# Enable browser restart more frequently
# PINCALI_BROWSER_RESTART_EVERY=50
 docker-compose up -d --force-recreate detail_scraper_worker
```

---

## 10. Scaling & Production

### 10.1 Scale Workers Horizontally

```bash
# Run 5 detail scraper workers
 docker-compose up -d --scale detail_scraper_worker=5

# Verify all are running
 docker-compose ps | grep detail_scraper_worker

# Scale back to 1
 docker-compose up -d --scale detail_scraper_worker=1
```

### 10.2 Production Deployment

```bash
# Production build
 docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# With specific env file
 docker-compose --env-file .env.production up -d

# Update without downtime (rolling update)
 docker-compose up -d --no-deps --build api
 docker-compose up -d --no-deps --build detail_scraper_worker
```

### 10.3 Backup Database

```bash
# Backup PostgreSQL
 docker-compose exec db pg_dump -U pincali pincali > backup_$(date +%Y%m%d_%H%M%S).sql

# Backup with compression
 docker-compose exec db pg_dump -U pincali pincali | gzip > backup_$(date +%Y%m%d_%H%M%S).sql.gz

# Restore from backup
 gunzip < backup_YYYYMMDD_HHMMSS.sql.gz | docker-compose exec -T db psql -U pincali -d pincali
```

### 10.4 Log Rotation

```bash
# Configure logrotate on host
cat << 'EOF' | sudo tee /etc/logrotate.d/pincali
/var/lib/docker/volumes/pincali_logs/_data/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0644 root root
}
EOF

# Test logrotate config
sudo logrotate -d /etc/logrotate.d/pincali
```

---

## Quick Reference Card

| Task | Command |
|---|---|
| Start everything | ` docker-compose up -d` |
| Stop everything | ` docker-compose down` |
| View API logs | ` docker-compose logs -f api` |
| View scraper logs | ` docker-compose logs -f detail_scraper_worker` |
| View discovery logs | ` docker-compose logs -f url_discovery_worker` |
| Restart scraper | ` docker-compose restart detail_scraper_worker` |
| Scale scrapers to 3 | ` docker-compose up -d --scale detail_scraper_worker=3` |
| Run migrations | ` docker-compose run --rm migrator` |
| DB console | ` docker-compose exec db psql -U pincali -d pincali` |
| Health check | `curl http://localhost:8000/health` |
| API docs | `open http://localhost:8000/docs` |
| Dashboard | `open http://localhost:8000/admin` |
| Backup DB | ` docker-compose exec db pg_dump -U pincali pincali > backup.sql` |
| Reset failed URLs | `UPDATE listing_urls SET status='pending' WHERE status='failed';` |

---

*Generated: 2026-06-01*
*Version: Phase 1 Fixes (6 fixes applied)*
