# API Reference

Base URL: `http://localhost:8000`

Interactive docs: `GET /docs` (Swagger UI)

---

## Health

### `GET /health`

```json
{"status": "ok", "version": "1.0.0"}
```

---

## Listings

### `GET /api/v1/listings`

Returns a paginated list of property listings.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `page` | int (default: 1) | Page number |
| `page_size` | int (default: 20, max: 100) | Results per page |
| `city` | string | Filter by city (case-insensitive, partial match) |
| `min_price` | float | Minimum price (USD) |
| `max_price` | float | Maximum price (USD) |
| `beds` | int | Minimum bedrooms |
| `baths` | int | Minimum bathrooms |
| `property_type` | string | `A` (apartment), `B` (house), `E` (land), `F` (commercial), `H` (fractional), `I` (multi-family) |
| `mls_status` | string | `Active`, `Sold`, `Pending`, `Cancelled` |
| `sort` | string | `price_asc`, `price_desc`, `created_desc` (default), `updated_desc` |

**Response:**

```json
{
  "total": 1543,
  "page": 1,
  "page_size": 20,
  "pages": 78,
  "items": [
    {
      "id": 1,
      "external_id": "0099123",
      "source": "pincali",
      "name": "beautiful villa in cabo",
      "price_usd": 450000.0,
      "original_price": 9000000.0,
      "original_currency": "MXN",
      "city": "cabo san lucas",
      "state": "baja california sur",
      "beds_total": 3,
      "baths_full": 2,
      "property_type": "B",
      "mls_status": "Active",
      "latitude": 22.9,
      "longitude": -109.9,
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-03-01T08:00:00Z"
    }
  ]
}
```

---

### `GET /api/v1/listings/{id}`

Returns full detail for a single listing including photos, videos, and virtual tours.

**Response:**

```json
{
  "id": 1,
  "external_id": "0099123",
  "source": "pincali",
  "origin_url": "https://www.pincali.com/en/home/0099123-...",
  "name": "beautiful villa in cabo",
  "description": "Stunning ocean-view villa...",
  "price_usd": 450000.0,
  "original_price": 9000000.0,
  "original_currency": "MXN",
  "mls_status": "Active",
  "property_type": "B",
  "city": "cabo san lucas",
  "state": "baja california sur",
  "country": "Mexico",
  "latitude": 22.9,
  "longitude": -109.9,
  "beds_total": 3,
  "baths_full": 2,
  "baths_half": 0,
  "year_built": 2018,
  "lot_size_m2": 500.0,
  "construction_m2": 280.0,
  "parking_spaces": 2,
  "pool": true,
  "furnished": "fully",
  "view": "ocean",
  "interior_features": ["AC", "Fireplace"],
  "exterior_features": ["Pool", "Garden"],
  "list_agent_name": "Maria Lopez",
  "list_office_name": "Cabo Realty",
  "photos": [
    {"id": 1, "url": "https://assets.easybroker.com/...", "position": 0, "is_primary": true}
  ],
  "videos": [],
  "virtual_tours": [],
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-03-01T08:00:00Z",
  "last_seen_at": "2024-03-01T08:00:00Z"
}
```

Returns `404` if not found.

---

### `GET /api/v1/listings/search`

Full-text search across name, description, city, and community.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `q` | string (required) | Search term |
| `page` | int | Page number |
| `page_size` | int | Results per page |

**Response:** Same structure as `/api/v1/listings`.

---

### `GET /api/v1/listings/statistics`

Aggregate statistics across all listings.

```json
{
  "total_listings": 12543,
  "active_listings": 11200,
  "sold_listings": 800,
  "pending_listings": 300,
  "avg_price_usd": 385000.0,
  "median_price_usd": 250000.0,
  "min_price_usd": 45000.0,
  "max_price_usd": 8500000.0,
  "listings_by_city": {"cabo san lucas": 3200, "cancun": 2100},
  "listings_by_type": {"B": 6500, "A": 4000},
  "listings_added_last_7_days": 142,
  "listings_added_last_30_days": 580
}
```

---

## Scraper

### `GET /api/v1/scraper/status`

```json
{
  "pending_urls": 4821,
  "processing_urls": 5,
  "done_urls": 198402,
  "failed_urls": 234,
  "last_run": {
    "id": 47,
    "type": "detail",
    "started_at": "2024-03-01T07:00:00Z",
    "finished_at": "2024-03-01T08:30:00Z",
    "urls_found": 48,
    "urls_scraped": 45,
    "urls_failed": 3,
    "status": "completed"
  }
}
```

### `GET /api/v1/scraper/runs`

Returns recent scrape run history (last 50).

### `GET /api/v1/scraper/errors`

Returns recent scraper errors (last 100).

---

## Admin Dashboard

| Path | Description |
|---|---|
| `/admin` | Overview: stats cards + recent activity |
| `/admin/listings` | Paginated listing table with search |
| `/admin/runs` | Scrape run history |
| `/admin/errors` | Error log |
| `/admin/stats` | Full statistics breakdown |
