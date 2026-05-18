# openclaw

**https://github.com/thinkaloopmedia/openclaw**

Autonomous product data retrieval system. Fetches product pages on a schedule, extracts structured data via CSS selectors, normalises prices and availability, stores results with full price history, and exposes everything through a REST API.

## Architecture

```
config/sources.yaml
       │
       ▼
  Orchestrator  ──── APScheduler ────► run_source()
       │                                    │
       │                          ┌─────────┴─────────┐
       │                       fetch()            cache dedup
       │                          │                    │
       │                       parse()            Redis
       │                          │
       │                      normalize()
       │                          │
       │                      upsert_product()
       │                          │
       │                       SQLite / Postgres
       │
       └──── FastAPI ───► /products  /sources  /jobs
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

# create DB tables
python scripts/migrate.py

# start API + scheduler
python scripts/run.py
```

The API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

## Configuration

### Environment (`.env`)

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/openclaw.db` | SQLAlchemy URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `REQUEST_TIMEOUT` | `30` | Per-request HTTP timeout (seconds) |
| `MAX_CONCURRENT_FETCHES` | `10` | Semaphore limit for batch fetches |
| `USER_AGENT` | `openclaw/1.0` | HTTP User-Agent header |
| `RETRY_ATTEMPTS` | `3` | Max retries on transient errors |
| `RETRY_BACKOFF_BASE` | `2.0` | Exponential backoff base (seconds) |
| `RETRY_STATUS_CODES` | `[429,500,502,503,504]` | Status codes that trigger a retry |

### Sources (`config/sources.yaml`)

Each source defines a site to crawl, the CSS selectors that extract product fields, and a cron schedule.

```yaml
sources:
  - name: my_store
    base_url: https://example.com
    enabled: true
    schedule: "0 */6 * * *"   # every 6 hours
    fetch_ttl: 3600            # skip re-fetching a URL within this window
    parser: product
    headers:
      Accept-Language: "en-US"
    selectors:
      name: "h1.product-title"
      price: "span.price"
      sku: "span[data-sku]"
      availability: "div.stock-status"
      description: "div.product-description"
      image_url: "img.hero-image"
      currency: "meta[itemprop=priceCurrency]"
    urls:
      - https://example.com/products/widget-pro
      - https://example.com/products/gadget-x
```

**Selector notes:**
- Any key not in the standard set (`name price sku availability description image_url currency`) lands in the product's `extras` field.
- For `price`, the parser prefers `data-price`, `data-amount`, and `content` attributes over visible text to avoid formatted display strings.
- For `image_url`, `src` and `data-src` are checked before falling back to text content.

## API

### Products

```
GET  /products                     list products
GET  /products/{id}                single product
GET  /products/{id}/price-history  full price change log
```

Query params for `GET /products`: `source`, `availability`, `limit` (max 500), `offset`.

### Sources

```
GET  /sources                      list configured sources
POST /sources/{name}/submit        add URLs to the crawl list
POST /sources/{name}/trigger       run immediately (returns ok/failed/skipped counts)
POST /sources/{name}/pause         pause scheduled job
POST /sources/{name}/resume        resume scheduled job
```

`POST /sources/{name}/submit` body:
```json
{ "urls": ["https://example.com/products/new-item"] }
```

### System

```
GET  /health                       liveness + source/job counts
GET  /jobs                         scheduler job list with next-run times
```

## Running modes

```bash
# API + embedded scheduler (default)
python scripts/run.py

# Custom port
python scripts/run.py --port=9000

# Custom sources file
python scripts/run.py --sources=config/sources.staging.yaml

# Scheduler only, no HTTP server
python scripts/run.py --worker-only
```

## Data model

### `products` table

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `url` | text unique | natural key |
| `source` | string | source name from config |
| `name` | text | |
| `price` | numeric(14,4) | |
| `currency` | string(3) | ISO 4217 |
| `sku` | string | |
| `availability` | string | `in_stock` `out_of_stock` `limited` `preorder` `unknown` |
| `description` | text | |
| `image_url` | text | |
| `extras` | text (JSON) | any non-standard selector fields |
| `first_seen_at` | datetime | |
| `last_seen_at` | datetime | updated on every run |

### `price_history` table

Append-only. A new row is written whenever `price` or `currency` changes.

| Column | Type |
|---|---|
| `id` | integer PK |
| `product_id` | FK → products |
| `price` | numeric(14,4) |
| `currency` | string(3) |
| `recorded_at` | datetime |

## Development

```bash
# run tests
python -m pytest

# lint
ruff check src tests

# type check
mypy src

# reset DB
python scripts/migrate.py --drop
```

### Project layout

```
openclaw/
├── config/
│   ├── settings.py          env-driven config (pydantic-settings)
│   ├── sources.yaml         source definitions
│   └── logging.yaml         optional logging config
├── src/
│   ├── retrieval/
│   │   ├── fetcher.py       fetch() / fetch_many() with retry + backoff
│   │   └── session.py       shared httpx.AsyncClient
│   ├── parsers/
│   │   ├── product.py       HTML → RawProduct via CSS selectors
│   │   └── normalizer.py    RawProduct → Product (typed, normalised)
│   ├── storage/
│   │   ├── db.py            SQLAlchemy models + CRUD
│   │   └── cache.py         Redis fetch-dedup + product cache
│   ├── pipeline/
│   │   └── runner.py        fetch→parse→normalise→store per URL/source
│   ├── agents/
│   │   ├── scheduler.py     APScheduler wrapper
│   │   └── orchestrator.py  lifecycle, URL registry, signal handling
│   └── api/
│       ├── app.py           FastAPI app factory + lifespan
│       ├── routes.py        all endpoints
│       └── schemas.py       Pydantic request/response models
├── scripts/
│   ├── run.py               main entrypoint
│   └── migrate.py           create / drop DB tables
└── tests/
    └── unit/                113 tests, all using in-memory SQLite + mocked Redis
```
