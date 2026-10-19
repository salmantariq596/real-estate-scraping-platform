# Deployment Guide

## Docker (Recommended)

### Prerequisites

- Docker Engine ≥ 24
- Docker Compose ≥ 2.20
- 4 GB RAM minimum (Chromium is memory-hungry)
- Persistent volume for PostgreSQL data

### Steps

```bash
# 1. Clone the repo
git clone <repo-url>
cd pincali_platform

# 2. Configure environment
cp .env.example .env
# Edit .env — at minimum set:
#   POSTGRES_PASSWORD=<strong-password>
#   SECRET_KEY=<random-64-char-string>
#   MXN_TO_USD_RATE=<current-rate>

# 3. Build and start
docker-compose up --build -d

# 4. Run DB migrations (first time only)
docker-compose run --rm migrator

# 5. Verify
curl http://localhost:8000/health
```

### Useful commands

```bash
# Tail logs
docker-compose logs -f api
docker-compose logs -f detail_scraper_worker
docker-compose logs -f url_discovery_worker

# Restart a service
docker-compose restart detail_scraper_worker

# Scale scraper workers
docker-compose up -d --scale detail_scraper_worker=3

# Stop everything
docker-compose down

# Stop and wipe volumes (DESTRUCTIVE)
docker-compose down -v
```

---

## Production Considerations

### Security

- Set `SECRET_KEY` to a cryptographically random 64-character string.
- Never expose the PostgreSQL port (`5432`) publicly — only the API port (`8000`) should be reachable.
- Place the API behind an Nginx reverse proxy with TLS.
- The admin dashboard (`/admin`) has no auth in this build — add HTTP Basic Auth or an OAuth2 middleware before exposing publicly.

### Nginx reverse proxy (minimal)

```nginx
server {
    listen 443 ssl;
    server_name api.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

### Database

- Use a managed PostgreSQL service (RDS, Cloud SQL, Supabase) for production.
- Set `POSTGRES_HOST` to the managed DB endpoint.
- Enable automated backups and point-in-time recovery.
- Create a dedicated DB user with only `SELECT, INSERT, UPDATE, DELETE` on the `pincali` schema.

### Scaling workers

Scraper workers are stateless. Scale horizontally:

```bash
docker-compose up -d --scale detail_scraper_worker=5
```

Each worker independently polls `listing_urls` for pending rows using `SELECT ... FOR UPDATE SKIP LOCKED` — no coordination needed.

### Exchange rate

Set `EXCHANGE_RATE_MODE=live` and provide an `EXCHANGE_RATE_API_KEY` (openexchangerates.org) to get the live MXN/USD rate automatically. The fallback `MXN_TO_USD_RATE` is used when live mode is unavailable.

### Monitoring

- The platform exposes Prometheus metrics when `PINCALI_METRICS_PORT` is set.
- Scrape the metrics endpoint with Prometheus and visualise in Grafana.
- Key metrics: `scrape_duration_seconds`, `active_workers`, `queue_depth`, `browser_crashes_total`.

### Log rotation

Logs are written to `/app/logs/` inside each container. Mount a host volume and configure logrotate:

```
/var/lib/docker/volumes/pincali_logs/_data/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
```

---

## MySQL Variant

```bash
# Start with MySQL profile
docker-compose --profile mysql up -d

# Set in .env:
DATABASE_TYPE=mysql
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DB=pincali
MYSQL_USER=pincali
MYSQL_PASSWORD=<password>
```

---

## Adding a New Scraping Source

1. Implement a new discovery service in `app/scraper/` following `DiscoveryService`.
2. Add a new worker in `app/workers/` following `URLDiscoveryWorker`.
3. Set `source` to the new source name when inserting into `listing_urls`.
4. The normaliser and API are source-agnostic — no changes needed there.
