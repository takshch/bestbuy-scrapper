# Best Buy Scraper

A distributed scraper for Best Buy search results:
- **API:** FastAPI puts searches on a **Postgres-backed work queue**.
- **Workers:** horizontally scalable processes scrape pages in parallel with **Selenium driving CloakBrowser**,
  a Chromium build with source-level fingerprint patches.
- **Routing:** requests go out on the server's own IP first, with an **Oxylabs US proxy** as a fallback.
- **Data:** products and price history are stored in **PostgreSQL**.
- **Dashboard:** a **Next.js** frontend.

```
 Next.js dashboard ──► FastAPI ──► Postgres queue (scrape_jobs → scrape_tasks, one per results page)
                                        ▲   │  FOR UPDATE SKIP LOCKED
                                        │   ▼
               products, price_history ─┤  worker × N (docker compose --scale worker=N)
                                        │   └─ WORKER_CONCURRENCY slots, each: 1 page → 1 CloakBrowser
                                        │         1. direct IP ─────────────────────────► bestbuy.com
                                        │         2. on block / degraded page:
                                        └──────────  local auth forwarder ─► Oxylabs US ─► bestbuy.com
```

## Layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app. Applies migrations on startup, `/health` |
| `app/routers/` | `scrape` (queue, jobs, per-page tasks, cancel), `products` (+ price history), `stats` |
| `app/queue.py` | Postgres work queue: enqueue, claim, complete, retries, leases, reaper, cooldown |
| `app/worker.py` | Worker process: N browser slots, heartbeats, reaper, graceful shutdown |
| `app/storage.py` | Product upserts and price-history recording |
| `app/scraping/bestbuy.py` | One page per call: routing and fallback, block detection, extraction |
| `app/scraping/proxy.py` | Oxylabs credentials (sticky US sessions) and local authenticating forwarder |
| `app/models.py`, `migrations/` | SQLAlchemy models and Alembic migrations |
| `tests/` | pytest: unit, Postgres integration, worker end-to-end, browser extraction |
| `frontend/` | Next.js 16 dashboard |
| `Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml` | Images and the full stack |

## Run it with Docker

```bash
cp .env.example .env                       # Oxylabs credentials, DB password for production
docker compose up -d --build               # postgres, api, worker, frontend
docker compose up -d --scale worker=4      # more workers (each runs WORKER_CONCURRENCY browsers)
docker compose logs -f worker
```

- **Dashboard:** http://localhost:3000
- **API docs:** http://localhost:8000/docs

The API and workers share one image, which has CloakBrowser and a matching ChromeDriver baked in.
It's built for `linux/amd64`, because Google publishes no Linux ARM ChromeDriver.
Postgres data is kept in the `pgdata` volume.

## How it scales

- **Unit of work:** each results page is one task. `POST /scrape` only inserts a job row plus
  `max_pages` task rows, so the API does no scraping and can be replicated.
- **Claiming:** workers take tasks with `SELECT … FOR UPDATE SKIP LOCKED`. Any number of workers pull
  from the queue at once without taking the same page twice. Claims go in page-first order, which
  round-robins between jobs so one 50-page job can't starve the rest.
- **Concurrency:** each worker runs `WORKER_CONCURRENCY` slots, and each slot uses its own
  CloakBrowser with a fresh fingerprint seed and, on the proxy route, its own sticky Oxylabs session.
  Selenium is blocking, so slots run in threads, while database work stays async (asyncpg).
- **Reliability:**
  - Running tasks hold a lease that heartbeats renew. If a worker dies, a reaper in any other worker
    requeues its tasks. Tasks are retried up to `TASK_MAX_ATTEMPTS` times, then marked failed.
  - A worker that stops (SIGTERM) finishes its in-flight pages, claims nothing new, and hands back
    anything unfinished.
  - If a page returns no results, the job's remaining pages are skipped.
- **Shared state:** the direct-IP cooldown is stored in Postgres (`route_state`), so when one worker
  finds the server IP blocked, every worker switches to proxy-first.
- **Consistency:**
  - Completion locks the job row before its task row, one order everywhere, which prevents deadlocks.
    The concurrency tests found exactly this deadlock before it was fixed.
  - Product upserts are sorted by SKU for the same reason.
  - A result from a worker whose lease expired is discarded.

**Measured locally** (one worker, 3 browsers, direct route from outside the US):
- 5 pages across 2 jobs finished in **52 s**, about 20 s per page, versus about 110 s one at a time.
- The worker ran 3 pages concurrently, with a 4th waiting in the queue.

These numbers haven't been measured at larger scale or through Oxylabs. The credentials given so
far were rejected.

## Anti-bot and routing

- **Stealth browser:** CloakBrowser patches the fingerprint in Chromium's source (canvas, WebGL,
  audio, fonts, `navigator.webdriver`, CDP signals), rather than injecting JavaScript. Each browser
  gets a random `--fingerprint` seed. Timezone and locale are pinned to the US exit
  (`BROWSER_TIMEZONE` / `BROWSER_LOCALE`). WebRTC is limited to proxied traffic, so it can't leak the
  real IP.
- **Fallback triggers** (`PROXY_MODE=fallback`): each page tries the direct IP first and switches
  to Oxylabs on any of these:
  - a hard block such as "Access Denied", which is detected immediately instead of waiting out the 45 s timeout;
  - a challenge page that doesn't clear;
  - a timeout or network error;
  - a **degraded page**, where fewer than 50% of products have a name and price. From non-US IPs,
    Best Buy serves the page but leaves most product cards empty.
- **Fallback behaviour:**
  - Data from a degraded page is kept, and better proxy data overwrites it.
  - Bad proxy credentials are detected with a test CONNECT before the browser starts.
  - After a successful fallback, all workers go proxy-first for `DIRECT_RETRY_AFTER` seconds.
- **Other modes:** `PROXY_MODE=always` or `never`.
- **Proxy auth:** Chrome ignores `user:pass@` in `--proxy-server`, so each browser gets a small local
  forwarder that adds the `Proxy-Authorization` header. It's built with asyncio and handles CONNECT
  tunnels and plain HTTP. Residential usernames target the US (`-cc-US`, optionally `-st-us_new_york`)
  with a sticky session per task. Set `OXYLABS_PROXY_TYPE=datacenter` for `dc.oxylabs.io:8000`.

## Extraction

Best Buy's search page is a React app whose product cards render lazily. Only a few are complete
on load, and cards outside the viewport stay placeholders until scrolled into view. So:

1. **Apollo cache first:** the extractor reads the page's Apollo GraphQL cache,
   `window[Symbol.for('ApolloClientSingleton')].cache.extract()`, resolving `__ref` links and
   argument-keyed fields such as `price({...})`. It gets price, regular price, model number,
   rating, review count, image and URL, even for cards that never rendered.
2. **Rendered cards second:** the DOM fills gaps such as cart availability and covers the legacy
   `li.sku-item` layout.
3. **Search results only:** the order of cards on the page defines which cache entries are actual
   results, so recommendation carousels and open-box variants are ignored.
4. **Scrolling:** the page is scrolled only while that loads more complete products, capped by
   `PAGE_LOAD_BUDGET`.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/scrape` | `{"query": "laptop", "max_pages": 5}`. Returns 202 with the queued job |
| `GET` | `/scrape/jobs` | Recent jobs (`?status=running`) |
| `GET` | `/scrape/jobs/{id}` | Progress: `pages_done`, `last_page`, `products_found`, routes, errors |
| `GET` | `/scrape/jobs/{id}/tasks` | Per page: status, attempts, worker, route, errors |
| `POST` | `/scrape/jobs/{id}/cancel` | Skip queued pages. Running pages finish |
| `GET` | `/products` | `q`, `search_query`, `min_price`, `max_price`, `sort=updated\|price_asc\|price_desc\|rating`, `limit`, `offset` |
| `GET` | `/products/{sku}` and `/products/{sku}/history` | Product and its price changes |
| `GET` | `/stats` | Queue depth, pages and products per hour, success rate per route (24 h), live workers, cooldown |
| `GET` | `/health` | DB, proxy mode, cooldown |

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                   # unit tests (no services needed)

docker compose up -d postgres
docker compose exec postgres createdb -U scraper bestbuy_test
TEST_DATABASE_URL=postgresql://scraper:scraper@localhost:5432/bestbuy_test .venv/bin/pytest   # + DB, worker, API

RUN_BROWSER_TESTS=1 .venv/bin/pytest -m browser    # real extraction JS in CloakBrowser against fixture pages
```

There are 62 tests:
- **Proxy:** credential formats and the forwarder, tested against a mock upstream.
- **Routing:** fallback across direct, blocked, degraded, proxy-down and cooldown cases.
- **Queue:** a concurrent-claim race with 25 claimers and 10 tasks, retries, lease loss, the reaper,
  cancellation, end of results, upsert coalescing and price history.
- **Workers:** parallelism, two workers sharing a queue, crash retry, and graceful stop.
- **API:** the endpoints.
- **Extraction:** the Apollo and DOM merge, the legacy layout, fail-fast on blocks, and stealth checks.

The test database's schema is rebuilt from the Alembic migrations on each run.

## Local development without Docker

```bash
docker compose up -d postgres
.venv/bin/uvicorn app.main:app --reload            # API (applies migrations)
.venv/bin/python -m app.worker                     # one or more workers
cd frontend && cp .env.example .env.local && npm install && npm run dev
```

CloakBrowser downloads its binary to `~/.cloakbrowser` on first use, and Selenium Manager fetches
a matching ChromeDriver.

## Configuration

See `.env.example`. Key settings:

| Setting | Default | Controls |
|---|---|---|
| `WORKER_CONCURRENCY` | `2` | Browsers per worker |
| `TASK_LEASE_SECONDS` | `120` | Time before a silent worker's tasks are requeued |
| `TASK_MAX_ATTEMPTS` | `2` | Attempts per page before it's marked failed |
| `REQUEST_JITTER_MIN` / `REQUEST_JITTER_MAX` | `1` / `3` | Random delay in seconds before each page, so browsers don't hit the site in lockstep |
| `PAGE_LOAD_BUDGET` | `25` | Maximum seconds to wait for a page's product data |
| `PROXY_MODE` | `fallback` | Proxy routing: `fallback`, `always` or `never` |
| `DIRECT_RETRY_AFTER` | `1800` | Seconds all workers stay proxy-first after the direct IP is blocked |
| `OXYLABS_*` | | Proxy credentials and targeting |

## Limitations

- **Region:** from a non-US IP, Best Buy fills in only about 5 of 24 products per page. Deploy in the
  US or use a working US proxy for full pages.
- **CloakBrowser licensing:** the free v146 binary ran 4 concurrent sessions here. Check CloakBrowser's
  `BINARY-LICENSE.md` before running it as a hosted service.
- **Politeness:** jitter spreads requests out, but there is no global rate limit across workers.
  Size `worker × WORKER_CONCURRENCY` to what the target and your proxy plan tolerate.
