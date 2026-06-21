# ACME Competitive Wage Intelligence

Internal dashboard that compares ACME's entry-level wages to local labor-market competitors at each yard. Built to be shown to the C-suite and to demonstrate how to assemble an AI-powered web application using LLMs as **functional components in a deterministic pipeline** (not autonomous agents).

- **Audience:** internal execs, "are we paying enough to attract talent?"
- **Data philosophy:** free sources only — employer career sites (real Playwright scrapers; today Home Depot) + BEA RPP + BLS OEWS state-level baseline. Aggregators (Indeed etc.) are clearly labeled lower-trust fallback. No paid datasets.
- **AI surface:** wage extraction, title classification, executive narrative — each step picks its own model.
- **Locations covered (seeded):** 154 real acme yards across 49 states.
- **Competitors (seeded):** Walmart, Amazon, Home Depot, Costco, Starbucks.

## Quick start (development)

```bash
cd /var/www/compare-wages
./run.sh
# → http://localhost:8000
```

`run.sh` creates the venv on first run, installs `requirements.txt`, re-seeds the SQLite DB from `app/seed_data.py`, and starts `uvicorn --reload`.

After the app is up, click **Run Now** in the admin top bar (or `POST /admin/run-now`) to trigger ingestion: it walks every seeded job posting, runs LLM extraction + classification, and writes a fresh national narrative.

The default `.env` settings use the mock LLM (regex + keyword) so the demo runs without a key. To use real OpenRouter calls, copy `.env.example` → `.env` and set:

```
OPENROUTER_API_KEY=sk-or-...
USE_MOCK_LLM=false
```

## Project layout

```
app/
  main.py              FastAPI app + lifespan (init_db, apply_config)
  config.py            Pydantic settings: API key, model fallbacks, distance cutoff
  db.py                SQLAlchemy engine, session_scope, get_db
  models.py            ORM models (locations, postings, llm_calls, model config, …)
  templating.py        Jinja env + filters (money, signed_money, pressure_color) + `prefix` global
  security.py          Form-auth: verify_credentials + require_admin (session → 303 to /admin/login)
  seed.py              Idempotent seed orchestrator (BEA RPP, yards, competitors, postings)
  seed_data.py         Hand-curated 154 acme yards + state-base wage table
  scheduler.py         APScheduler — reads ScheduleConfig row + cron expression
  routers/
    dashboard.py       Exec surface: /, /location/{code}, /methodology
    admin.py           Operator surface: /admin/* — defines `auth_router` (login/logout, public)
                       and `router` (everything else, gated by require_admin)
  services/
    geo.py             Haversine
    market.py          Inverse-distance weighted blended wage, RPP, quartiles, rollups
    ingestion.py       Orchestrates: postings → extract → classify → narrative.
                       Also exposes extract_postings_by_ids() for the scrape→extract handoff.
    llm.py             OpenRouter client wrapper + mock fallback + per-purpose model lookup.
                       Tolerant JSON parsing (handles markdown fences / extra prose) +
                       split wire-schema-vs-server-validation so OpenAI strict mode and
                       Anthropic's loose mode both work.
    bls.py             BLS OEWS state-level lookups (baseline_for, baseline_blended_p50)
    scraping.py        Async orchestrator for scraper runs (parallel to services/ingestion.py).
                       Auto-runs extract_postings_by_ids() on the just-saved postings at the
                       end of every scrape so the operator never needs a second click.
    geocoding.py       Census Geocoder API wrapper. Free, no key. 1 req/sec throttled.
                       Used by services/scraping.py when creating a new CompetitorLocation.
  scrapers/            Real employer-site scrapers.
    base.py            Scraper ABC + ScrapedPosting (frozen contract — never modify).
    base_employer.py   `BaseEmployerScraper(Scraper)` — shared concrete base. Owns
                       robots.txt caching (1-hour TTL, process-wide), Playwright launch,
                       JSON-LD parse, retry-on-TimeoutError (1s→3s→9s backoff), per-keyword
                       failure budget (3 strikes → skip kw), partial-success preservation
                       (5 live then exception keeps the 5), and the `last_run_telemetry`
                       dict scrapers populate for the admin UI to surface.
    registry.py        @register decorator + get_scraper / has_scraper.
    homedepot.py       (~100 lines) selectors + fixtures + location-aware search URL.
    amazon.py          (~330 lines) dual-subdomain dispatch: hiring.amazon.com for
                       warehouse keywords (JSON-LD full address), amazon.jobs for corp
                       (body-text city/state).
    costco.py          (~110 lines) selectors + fixtures; uses default extraction.
    walmart.py         (~280 lines) overrides _scrape_live with playwright-stealth +
                       optional residential proxy + Akamai/PerimeterX/DataDome challenge
                       detection (WalmartBlocked typed exception).
    starbucks.py       (~95 lines) JSON-LD path; light anti-bot.
    fixtures/          Per-employer realistic facsimile HTML for FIXTURE_MODE + tests.
  templates/
    base.html          Tailwind/Inter/Fraunces + Leaflet + Alpine + HTMX
    exec/              overview, location_detail, methodology (editorial design)
    admin/             11 admin pages + login.html + _shell.html macro (sticky nav + Run Now + Sign out)
  static/css/app.css   Custom editorial tokens (paper bg, terracotta accent, dropcap, etc.)
data/
  sample_postings/     Per-employer HTML templates (Walmart/Amazon/HD/Costco/Starbucks)
  raw_html/            Rendered postings written by the seed; read by ingestion
  bea_rpp.csv          Regional Price Parity by state (BEA 2023)
  bls_oews_2023.csv    BLS OEWS state-level wage data (5 occupations × 51 jurisdictions).
                       Regenerate via `scripts/generate_bls_oews.py` after editing
                       `data/bls_oews_source.py`. Refresh from bls.gov annually.
  zip_to_cbsa.csv      HUD USPS ZIP→CBSA crosswalk (32k ZIPs, collapsed to one row per
                       ZIP — highest bus_ratio wins). Refresh via
                       `scripts/download_zip_cbsa.py` with HUD_API_TOKEN set in .env.
  cbsa_names.csv       Census Bureau CBSA code → title (935 rows).
                       Refresh by downloading the Census delineation XLSX and re-running
                       the `openpyxl` parsing step (see git history of seed work).
  golden_postings.json 10 fixture postings with known wages for the eval harness
  wages.db             SQLite (gitignored)
tests/
  conftest.py             USE_MOCK_LLM=true, isolated test DB, seeded_session fixture
  test_geo.py             Haversine correctness + symmetry
  test_market.py          blended_wage, quartile, RPP, rollups
  test_extraction.py      Mock extractor on a simple range + LlmCall logging
  test_ingestion.py       End-to-end: seed → ingest → assert wages/buckets/narrative populated
  test_bls.py             BLS baseline lookups
  test_scraping.py        Service: keyword derivation, no-scraper fast-path, geocoding
                          regression guard, scraped-posting-appears-in-yard-observations
                          end-to-end
  test_homedepot_scraper.py   Registry, robots, fixture mode, JSON-LD path
  test_amazon_scraper.py     Same shape
  test_costco_scraper.py     Same shape
  test_walmart_scraper.py    Same shape
  test_eval_harness.py    Smoke + accuracy threshold on the golden set
  eval_harness.py         run_extraction_evals() — imported by /admin/eval
gunicorn_config.py     Production worker config (mirrors sat-prep style)
deploy/
  compare-wages.service  Reference systemd unit (install to /etc/systemd/system)
alembic.ini            Alembic config — script_location=alembic; reads DATABASE_URL from env.py
alembic/
  env.py                 Wires target_metadata=app.db.Base.metadata; imports app.models so all tables register
  script.py.mako         Template for new revisions
  versions/              Generated revision files (`<timestamp>_<rev>_<slug>.py`)
run.sh                 Dev convenience: venv + deps + seed + uvicorn --reload
pytest.ini             pythonpath=., testpaths=tests
```

## Data flow on a single Scrape now click

1. `POST /admin/scrape/{competitor_id}` → `services.scraping.run_scrape(async_mode=True)`. Returns ScraperRun id immediately; the actual work runs in a daemon thread.
2. Service derives keywords by querying `RoleMapping WHERE competitor_id IN (id, NULL) AND confidence ≥ 0.7`. Empty keyword list = strict failed run.
3. Calls `scraper.scrape(keywords=…, max_postings=25)`. Each ScrapedPosting includes `raw_title`, `location_city`, `location_state`, and (when the source allows) `street_address` + `zip_code`.
4. For each yielded posting:
   - Match or create a `CompetitorLocation` for `(competitor_id, city, state)`. On create, geocode via Census Geocoder API (street+city+state+zip when available, falls back to city+state).
   - Write `raw_html` to `data/raw_html/scraped_<competitor>_<run_id>_<n>.html`.
   - Insert `JobPosting` with `source_tier="employer_owned"`, `wage_low=None`.
5. **Auto-extraction:** `extract_postings_by_ids(saved_ids)` runs at the end of the scrape over **just the saved postings** (ignores active-yard filters — if you bothered to scrape, you want wages). Each posting gets `extract_wage` + `classify_role` LLM calls. Postings whose HTML legitimately contains no wage (HD outside CA/CO/NY/WA/IL etc.) are correctly left with `wage_low=None`.
6. `ScraperRun` finalizes with `candidates_found`, `postings_saved`, and a `extracted=N/M` count in `notes`.

## Data flow on a single Run Now click

1. `POST /admin/run-now` → `services.ingestion.run_ingestion(triggered_by="manual")`. A scoped variant exists too — see below.
2. Creates a `ScrapeRun` row (`status="running"`, `scope_yard_codes=""` for full runs).
3. Selects `JobPosting` rows to process:
   - **Full run** (default): every posting (or only those without `wage_low` if `refresh_all=False`).
   - **Yard-scoped run** (`yard_ids=[…]`): only postings whose competitor location is within `DISTANCE_CUTOFF_MILES` (Haversine) of any selected yard. Empty `yard_ids` list = strict failed no-op (won't silently expand to a full run).
4. For each:
   - Reads the cached HTML from `data/raw_html/...`.
   - `llm.extract_wage(html, raw_title)` → structured `{wage_low, wage_high, wage_unit, confidence, …}` and logs an `LlmCall` row.
   - `llm.classify_role(raw_title)` → `{normalized_role, bucket: "outdoor"|"indoor", confidence}` and logs another `LlmCall`.
   - Persists wage + bucket back onto the `JobPosting`.
5. Recomputes `national_facts` from the metric store and calls `llm.generate_narrative(facts)` → writes a fresh `Narrative` row. The narrative reads the full DB state, so it stays nationally coherent even after a yard-scoped run.
6. Updates `ScrapeRun` with counts, `scope_yard_codes`, and `status="success"`.

Read-side (the exec dashboard) pulls postings, distances, quartiles, RPP, and the latest national narrative via `services.market.*`.

## Configuration

### Environment (`.env`)

| Var | Purpose | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter auth | empty |
| `OPENROUTER_MODEL` | Global fallback model | `anthropic/claude-3.5-haiku` |
| `EXTRACTION_MODEL` | Per-purpose override | (uses DB/global) |
| `CLASSIFICATION_MODEL` | Per-purpose override | (uses DB/global) |
| `NARRATIVE_MODEL` | Per-purpose override | (uses DB/global) |
| `USE_MOCK_LLM` | Force deterministic mocks (no network) | `true` |
| `DATABASE_URL` | SQLAlchemy URL | `sqlite:///./data/wages.db` |
| `DISTANCE_CUTOFF_MILES` | Competition radius | `25.0` |
| `ROOT_PATH` | Sub-path prefix when reverse-proxied (e.g. `/compare-wages`) | empty |
| `HUD_API_TOKEN` | Long-lived JWT for the HUD USPS ZIP→CBSA API (see `scripts/download_zip_cbsa.py`) | empty |
| `PLAYWRIGHT_BROWSERS_PATH` | Where Playwright stores Chromium. Must be readable by `www-data` in prod. | `/var/www/compare-wages/.playwright` |
| `FIXTURE_MODE` | Set to `1` to force scrapers to yield fixture HTML instead of hitting the live site | unset |
| `ALLOW_PROD_SEED` | Set to `1` to override the prod-DB safety guard in `app/seed.py` | unset (guard on) |
| `ADMIN_USERNAME` | Login form username for `/admin/*` | empty (fail-closed) |
| `ADMIN_PASSWORD` | Login form password for `/admin/*` | empty (fail-closed) |
| `SESSION_SECRET` | Signs the admin session cookie (set this in prod so sessions survive restart) | ephemeral random per boot |
| `SESSION_MAX_AGE_SECONDS` | Session lifetime | `43200` (12h) |

### Per-purpose LLM models

Admin UI: `/admin/llm-models`. Resolution order on every call:

1. `LlmModelConfig` DB row for that purpose (Admin UI writes here) — wins if present. Read live, no restart needed when you change it.
2. Per-purpose env var (e.g. `EXTRACTION_MODEL`, `CLASSIFICATION_MODEL`, `NARRATIVE_MODEL`) — only used when there is no DB row. Service restart picks up changes.
3. Global `OPENROUTER_MODEL` — final fallback.

The admin UI shows all three sources for every purpose: an **Effective on next call** chip with the source (`DB override`, `From env`, or `Global default`), the current `.env` value (read-only, labeled "shadowed by DB override" when applicable), and the DB-override form. Each card with a DB row has a **Reset to env value** button — clicking it deletes the DB row so the env value (or global default) takes over.

**Gotcha you'll hit at least once:** if you edit `.env` and the page doesn't reflect it, you have a DB override from a previous Save. Use Reset to env to remove it. The seed populates DB rows on first boot, so freshly-seeded deployments need a one-time reset per purpose if you want `.env` to drive everything.

Defaults (seeded): extraction → `anthropic/claude-3.5-haiku`, classification → `openai/gpt-4o-mini`, narrative → `anthropic/claude-3.5-haiku`. Change via UI without restart — pipeline reads it fresh per call.

**The OpenAI-strict vs Anthropic-loose problem (solved).** OpenAI with `strict: true` requires every property in `properties` to be listed in `required`. Anthropic via OpenRouter ignores enforcement entirely AND occasionally wraps the JSON in markdown fences or adds explanatory prose before/after the object. `app/services/llm.py` handles both:
- **Wire schemas (`WAGE_SCHEMA`, `CLASSIFY_SCHEMA`)** list every property in `required` so OpenAI strict mode is happy.
- **Server-side validation (`*_REQUIRED_KEYS`)** only enforces load-bearing keys (`wage_low`, `wage_high` for extraction; `bucket` for classification). Anthropic returning JSON missing `confidence` or `reasoning` is fine.
- **`_tolerant_json_parse(raw)`** strips ` ```json ``` ` markdown fences and falls back to a greedy `\{.*\}` match to recover JSON from "prose then JSON" or "JSON then trailing notes" patterns.
- **Raw API responses are preserved on parse failure** in the `llm_calls.response` column — debugging is impossible without this, so don't go back to logging `str(e)` instead.

**Picking a working OpenRouter model identifier.** Some bare names (e.g. `anthropic/claude-3.5-sonnet`) return 404 on certain accounts; `openai/gpt-5.4-mini` and similar typos return 400 with `"Invalid s..."`. If you see 4xx in `/admin/ai-ops`, the actual OpenRouter body is now in the `response` column — read it. Common fixes: dated variant (`anthropic/claude-3.5-sonnet-20241022`), `:beta` suffix, or fall back to the known-working `anthropic/claude-3.5-haiku`.

**"No wage in posting" is not a bug.** Home Depot and Walmart only disclose wages in states with pay-transparency mandates (CA, CO, NY, WA, IL, MD, etc.). Postings outside those states have nothing for the LLM to extract — Haiku correctly responds with `wage_low: null` + a note like "I cannot find a clear wage range." `services/ingestion.py` then skips the posting (no wage to persist). This shows up in the run summary as `extraction_failed` but is structurally different from a real LLM/transport failure. The /admin/ai-ops page shows both populated — use the `response` body to tell them apart.

## Admin authentication

Every route under `/admin/*` (except the login/logout pages themselves) is gated by a session-cookie check. The exec dashboard (`/`, `/location/{code}`, `/methodology`) stays public.

Set credentials in `.env`:

```
ADMIN_USERNAME=admin
ADMIN_PASSWORD=use-a-strong-passphrase-here
# Recommended in prod so sessions survive restarts:
SESSION_SECRET=<output of `python -c "import secrets;print(secrets.token_urlsafe(32))"`>
```

Then `sudo systemctl restart compare-wages`. Navigating to `/admin` shows a styled login form. After sign-in, users are redirected back to the page they were trying to reach (open-redirect-guarded — only `/admin/*` targets accepted).

**Logout** is a button in the admin top bar (`POST /admin/logout`).

**Session cookie behavior:**
- Name: `admin_session`. Signed with `SESSION_SECRET` (HMAC).
- Lifetime: `SESSION_MAX_AGE_SECONDS` seconds (default 12h).
- `HttpOnly`, `SameSite=Lax`. `Secure` is auto-on when `ROOT_PATH` is set (the production heuristic) and off otherwise so local dev over plain HTTP still works.
- If `SESSION_SECRET` is empty, a fresh random key is generated each boot — all sessions get invalidated on restart, which is safe but annoying for prod. Set it.

**Fail-closed defaults.** If either `ADMIN_USERNAME` or `ADMIN_PASSWORD` is empty, the login form shows an explicit "Admin auth not configured" error and refuses every sign-in attempt. The system never silently allows open admin access.

**Mechanics.** `app/security.py` exposes `verify_credentials` (timing-safe via `secrets.compare_digest`) for the login POST handler and `require_admin` (session check; raises a 303 redirect to `/admin/login?next=…` if not authed) for every gated route. `app/routers/admin.py` splits into `auth_router` (public — login, logout) and `router` (gated — everything else). New admin routes added to `router` inherit auth for free.

**TLS in production.** The session cookie is `HttpOnly` and `SameSite=Lax`, but it's still a bearer credential — the deployment **must** be behind TLS (your nginx → `https://lab.kudithipudi.org/compare-wages` already is). Don't expose `/admin/*` over plain HTTP in production.

For a multi-user or role-based setup, replace `verify_credentials` with a real user store and `require_admin` with a richer session payload (e.g. role/permissions) — the rest of the codebase doesn't care.

## How geocoding works

When `services/scraping._match_or_create_location` writes a new `CompetitorLocation`, it calls `app/services/geocoding.py:geocode()` against the free Census Geocoder API (`geocoding.geo.census.gov`). No API key needed. ~10 req/sec rate limit on Census's side; the wrapper throttles to 1 req/sec to be polite.

Lookup priority within the call: `street, city, state` first, then `city, state` if street is missing. Returns `(lat, lng)` on a match or `None` on any failure (network, no match, junk address). On `None`, the row is still inserted with `lat=0.0, lng=0.0` so the operator can find it later for a backfill, but the geographic filter on the dashboard will exclude it from any yard's observations — that's the regression guard that broke Home Depot pre-fix.

US only. ~4% miss rate on tiny-town addresses. See "Known gaps / roadmap" for the fallback proposal.

## Common tasks (extending the system)

### Run ingestion for one (or a few) yards

Yards are **inactive by default** — both freshly seeded ones and net-new ones added via the admin UI. You opt yards into ingestion by flipping their "active" toggle on `/admin/locations`. A "Run Now" with no explicit scope walks postings near every **currently active** yard. This is the operator's primary switch for "is this yard in production yet?".

Three entry points for an actual run:

- **Admin UI · Run Now (top bar).** No explicit scope → `POST /admin/run-now` → processes postings near every currently-active yard. With zero active yards it's a clean no-op (status=success, processed=0). Use this after toggling a few yards active.
- **Admin UI · per-yard button.** `/admin/locations` has a "Run yard" button on every row → `POST /admin/run-now/yard/{code}`. **Bypasses the active flag** — useful for a one-off test of an inactive yard.
- **Admin UI · multi-select.** `POST /admin/run-now/yards` accepts repeated `yard_codes` form fields. Wire it into a form when you need multi-select. Also bypasses active flag.

**Activating yards in bulk by state.** `/admin/locations` has a state-chip filter row. Pick a state and a contextual action bar appears — `Activate all in XX` / `Deactivate all in XX` (`POST /admin/locations/bulk-toggle-state`, form fields `state` + `active`). Use this to onboard or pause a region in one click instead of toggling 20 cards individually.
- **Python (tests, scripts):**
  ```python
  from app.services.ingestion import run_ingestion
  run_ingestion(yard_ids=[<id1>, <id2>, …], triggered_by="manual")  # explicit scope, active flag ignored
  run_ingestion(triggered_by="manual")                              # implicit: all active yards
  ```

**How scoping is defined.** Every posting whose competitor location is within `DISTANCE_CUTOFF_MILES` (25 mi default, Haversine) of any in-scope yard. Cross-state borders are honored — geography rules, not state membership.

**Active flag semantics.**
- Implicit run (`yard_ids=None`) → respects active flag. Only postings near active yards are processed.
- Explicit run (`yard_ids=[…]`) → operator override. Even inactive yards in the list are processed. The admin UI's per-yard and multi-select buttons use this path so you can test before flipping active.
- Empty list (`yard_ids=[]`) → strict failed no-op. Will not silently fall through.

**What's always full-scope vs scoped.**
- **Full scope:** the regenerated `Narrative` row (grounds in the entire metric store, so the exec dashboard stays coherent regardless of which subset you re-extracted).
- **Scoped:** the LLM extract+classify calls (cost saved here).

The `ScrapeRun` row records what was targeted via `scope_yard_codes` (comma-separated codes for explicit runs, empty string for implicit "all active"), shown on `/admin/runs` as a Scope column ("All active" or yard codes).

**Async by default.** Admin "Run Now" + per-yard buttons return immediately (303 to the new run's detail page) and the actual extraction happens in a daemon thread. The run-detail page auto-refreshes every 2.5 seconds while `status="running"` and shows the live progress (`postings_collected` / `extraction_success` / `extraction_failed`) committed by the background thread every few postings. Once the run finishes, the refresh stops. If the server is restarted mid-run, the orphaned `running` row is auto-marked `failed` on next startup (`mark_orphaned_runs_failed` in `app/services/ingestion.py`).

Tests and scripts can still run synchronously by omitting `async_mode`:
```python
run_ingestion(yard_ids=[…])                  # synchronous (default — used by tests)
run_ingestion(yard_ids=[…], async_mode=True) # detaches into a daemon thread
```

### Add a acme yard
Two options.
- **Admin UI:** `/admin/locations` → "Add location" form.
- **Code (preferred for permanent additions):** append a tuple to `acme_YARDS` in `app/seed_data.py`, then re-run `python -m app.seed`. Code format: `("XX-AAA", "Display Name", "Street", "City", "STATE", "ZIP", lat, lng)` — the seed assigns a wage from `STATE_BASE_WAGE`.

### Add a competitor
- Add to `COMPETITORS` in `app/seed.py` (name, source_priority, source_tier, careers_url).
- Create `data/sample_postings/{name_lowercase}.html` with the same format placeholders as the existing templates (`{role}`, `{city}`, `{state}`, `{wage_low}`, `{wage_high}`, `{store_id}`, `{role_blurb}`).
- Add wage ranges to `WAGE_TABLE` (via the `base` dict inside `_populate_wage_table` in `seed.py`).
- Add a `ROLE_POOL[(name, "outdoor")]` and `[(name, "indoor")]` list of role titles.
- Re-seed.

### Add an LLM step
1. Define a JSON schema constant in `services/llm.py` (see `WAGE_SCHEMA`).
2. Write a mock implementation (`_mock_<step>(...)`) so the demo runs without a key.
3. Add a public function calling `_run(purpose="<name>", ...)`. Logging + per-purpose model lookup are automatic.
4. Seed a `LlmModelConfig(purpose="<name>", model="…")` row.
5. Surface it on `/admin/ai-ops` (already automatic — it groups by `purpose`).
6. Add eval coverage if it produces structured output.

### Add an admin page
1. Add a route in `app/routers/admin.py`. Attach it to `router` (the gated one) so it inherits `require_admin` — **don't** put it on `auth_router`, which is reserved for the public login/logout pages.
2. Add a template under `app/templates/admin/` using `{% from "admin/_shell.html" import admin_shell %}` and `{% call admin_shell("<your_key>") %}…{% endcall %}` — that wraps it in the auth'd shell with Run Now + Sign out in the top bar.
3. Add the nav entry tuple to `nav_items` in `app/templates/admin/_shell.html`.

### Run a real scrape for a competitor

Scrapers are registered via `app/scrapers/registry.py` and discovered at app boot through `app/scrapers/__init__.py`. **Five scrapers** ship today, all using Playwright headless Chromium:

| Competitor | Status (datacenter IP) | Address fidelity | Notes |
|---|---|---|---|
| **Home Depot** | Live | full street + city + state + zip via JSON-LD | reference implementation; `&city=X&state=Y` URL params only influence ranking, not strict filter (see post-fetch filter below) |
| **Amazon** | Live (dual-subdomain) | **warehouse: full address** via `hiring.amazon.com` JSON-LD; corp: city + state via `amazon.jobs` body text | Keyword-based dispatch: warehouse-y keywords (Warehouse Associate, Fulfillment Associate, Sortation Associate, etc.) route to `hiring.amazon.com`; corp keywords (Software Engineer, etc.) route to `amazon.jobs`. Telemetry's `per_subdomain_yielded` shows the split. |
| **Costco** | Live | full street + city + state + zip via JSON-LD | iCIMS SPA — `addressRegion` ships as full state name, scraper normalizes to USPS code |
| **Walmart** | Live with stealth + optional proxy | full address via JSON-LD | `playwright-stealth` patches `navigator.webdriver` and friends; Akamai/PerimeterX/DataDome challenge-page detection (`Pardon Our Interruption`, `_Incapsula_Resource`, `px-captcha`, etc.) raises typed `WalmartBlocked` so the operator sees the exact reason in `ScraperRun.notes`. **Residential proxy required** for live datacenter use — see env vars below. Without proxy, falls back to fixtures with `WalmartBlocked` reason logged. |
| **Starbucks** | Live | full street + city + state + zip via JSON-LD | light anti-bot; uses default base-class extraction |

### Walmart residential proxy (optional)

Three optional env vars enable a residential proxy for the Walmart scraper specifically. Without them, the live path still attempts (with stealth) but typically gets blocked from a datacenter IP — fixture fallback engages automatically.

```bash
# Bright Data / Oxylabs / Smartproxy supply residential pools with sticky-session URLs.
WALMART_PROXY_URL=http://gate.smartproxy.com:7000
WALMART_PROXY_USERNAME=user-sp_session-xxxxxx
WALMART_PROXY_PASSWORD=<password>
```

When `WALMART_PROXY_URL` is unset, the scraper still applies stealth but skips the proxy kwarg on `browser.new_context` — that's how the fixture-fallback demo path stays functional without any proxy account. Telemetry annotates `proxy_configured=*****@gate.smartproxy.com` (credentials masked) when active.

### Amazon dual-subdomain dispatch

`AmazonScraper._scrape_live` partitions the keywords list and dispatches each subset to the right subdomain:

| Keyword pattern | Subdomain | Address fidelity |
|---|---|---|
| Warehouse Associate, Fulfillment Associate, Sortation Associate, Material Handler, Loader, Stocker, Picker, Packer, Order Filler, Receiver, Warehouse Operator, Amazon Delivery, Delivery Associate, Amazon Flex | `hiring.amazon.com` (JSON-LD) | full street + city + state + zip |
| Software Engineer, Product Manager, anything else | `amazon.jobs` (corp, body text) | city + state only |

Classification is case-insensitive substring match against `app.scrapers.amazon.WAREHOUSE_KEYWORDS`. Telemetry's `per_subdomain_yielded` dict records counts per subdomain so an operator can see `{"hiring.amazon.com": 8, "amazon.jobs": 2}` on `/admin/scrape-runs/{id}`.

### Location-aware + post-fetch geographic filter

Scrapers now target each query at the catchment of **active** Copart yards instead of relying on the employer's global ranking. Two layers:

1. **Per-yard search URLs.** `services/scraping.active_yard_locations(s)` builds a `[(city, state), …]` list from active yards. The service passes it to `scraper.scrape(keywords=…, locations=…, max_postings=…)`. The scraper's `search_url_for(keyword, location)` method (override per subclass) injects each site's location query params — Home Depot `&city=X&state=Y`, Costco `&location=City+ST`, etc. The `BaseEmployerScraper._scrape_live` loop iterates `(location × keyword)` pairs, capped at `max_location_keyword_pairs = 40` per scrape (random-sampled when exceeded so a 50-yard × 10-keyword setup doesn't fan out to 500 page fetches).

2. **Post-fetch geographic filter (the real fix).** Empirically, most employer search URL location params are advisory only — they influence ranking, not strict filtering. So after a `Cashier @ Hueytown, AL` query we still get hits from Newark NJ. After each `ScrapedPosting` is geocoded into a `CompetitorLocation`, `_do_scrape` checks whether its `(lat, lng)` is within `DISTANCE_CUTOFF_MILES` of **any** active yard. If not, it's dropped from persistence — the operator sees the count as `out-of-catchment=N` in the `ScraperRun.notes` line on `/admin/scrape-runs/{id}`. Guarantees every saved JobPosting is visible to at least one active yard's dashboard.

**Edge cases preserved:**
- Zero active yards → keep everything (operator can pre-scrape before activating yards)
- `(lat, lng) = (0, 0)` from a geocoder miss → keep (the dashboard's own geo filter excludes it visually but the row survives for a future re-geocode backfill)

### `seed` vs `employer_owned` source tier
Synthetic seed-generated postings carry `source_tier="seed"`; real scraped postings carry `source_tier="employer_owned"`. The dashboard's `observations_for_yard` excludes seed data by default — pass `include_seed=True` to fall back to seed when nothing's been scraped yet (useful for first-boot demos). Live scrapes never reintroduce the seed tier.

Operator surface:
- `/admin/competitors` shows a per-row "Scrape now" button for any competitor whose name has a registered scraper. The button POSTs to `/admin/scrape/{competitor_id}`.
- The scrape runs **async** (daemon thread) — page redirects immediately to `/admin/scrape-runs/{id}` which auto-refreshes every 2.5s.
- A `ScraperRun` row records `candidates_found` and `postings_saved`. Each saved posting is a new `JobPosting` row with `wage_low=None`, `source_tier="employer_owned"`, and `raw_html_path` pointing at `data/raw_html/scraped_<slug>_<run_id>_<n>.html`.
- The next **Run Now** in the admin top bar picks those new postings up and runs LLM extraction on them.

Three states a `ScraperRun` can land in:
| Status | Meaning |
|---|---|
| `success` | Got `candidates_found > 0` and saved at least some |
| `blocked` | `scraper.is_available()` returned False (robots.txt disallow, or pre-check 4xx). No requests made. |
| `failed` | Exception inside the thread, or unrecoverable scraper error. `notes` carries the error message. |

**FIXTURE_MODE.** Set `FIXTURE_MODE=1` in the env to force scrapers to yield from `app/scrapers/fixtures/*.html` instead of hitting the live site. Useful for demos when datacenter IPs get challenged.

**Playwright + www-data gotcha.** The systemd service runs as `www-data`. Playwright's default `chromium install` lands in `$HOME/.cache/ms-playwright/`, which for `www-data` resolves to `/var/www/.cache/` — not writable. Install browsers into a project-local path readable by the service:

```bash
sudo -u www-data PLAYWRIGHT_BROWSERS_PATH=/var/www/compare-wages/.playwright \
  /var/www/compare-wages/.venv/bin/playwright install chromium
```

Then add `PLAYWRIGHT_BROWSERS_PATH=/var/www/compare-wages/.playwright` to `/var/www/compare-wages/.env` and `sudo systemctl restart compare-wages`. Without this, the scraper silently falls back to fixtures even though `is_available()` returns True — visible in `/admin/scrape-runs` because the saved JobPosting URLs all end in `/sample-NNNN/` instead of a real Home Depot job ID.

### How scrapers know which job titles to search

Scrapers don't hardcode keywords. Each `Scraper.scrape(*, keywords, locations=None, max_postings=25)` call receives the keyword list at run-time from `app.services.scraping.keywords_for_competitor(s, competitor_id)`, which queries:

```sql
SELECT DISTINCT competitor_role
FROM role_mappings
WHERE (competitor_id = :id OR competitor_id IS NULL)
  AND confidence >= 0.7
```

So the operator's surface for "what should the Home Depot scraper search for?" is `/admin/role-mappings` — adding a row with `competitor_id=<Home Depot>` and `competitor_role="Stocking Associate"` expands the next scrape's coverage immediately, no code change.

**Why nullable `competitor_id`.** Generic mappings (e.g. "Warehouse Associate" applies to Walmart, Costco, Amazon equivalents) live with `competitor_id IS NULL` and are added to every competitor's keyword set. Specific titles like "Lot Associate" (Home Depot vernacular) get scoped to a single competitor so they don't pollute another competitor's search.

**Empty keyword list = strict failed run.** If a competitor has no scoped or global mappings above the confidence floor, `run_scrape` records a `failed` `ScraperRun` with `"no role mappings for <competitor> (add some at /admin/role-mappings)"` in `notes`. The scraper is never invoked with an empty keyword list — the operator must opt into coverage.

### Discovering new role mappings (Role discovery)

`/admin/role-discovery` is the mining-then-review surface that closes the loop with `/admin/role-mappings`. Scrapers always pull adjacent titles incidentally (search-result overspill that wasn't in the keyword list); those titles land in `job_postings.raw_title` and just sit there. Click **Run discovery** (per competitor or all) and `app/services/role_discovery.py:discover_from_existing_postings` walks the unmapped titles, batches them through the LLM (`purpose="role_discovery"`, ~20/batch), and queues each one as a pending `RoleDiscoverySuggestion`. The page reloads with the suggestions — review each (Accept materializes a `RoleMapping` row the next scrape uses; Reject tombstones it). A **Bulk accept ≥ N%** button promotes every high-confidence outdoor/indoor suggestion in one click. The next scrape sees the new keywords automatically. Use this when ingestion runs show titles you didn't expect, or after a competitor's careers site adds new role names.

**Web-search discovery (V2).** The button next to "Run discovery" is **Run web search** — it solves V1's bootstrap problem (a competitor with zero scraped postings has nothing for V1 to mine). `discover_from_web_search` (`app/services/role_discovery.py`) issues a small set of seed queries against a generic web search engine for each competitor, then asks the LLM (`purpose="role_discovery_web_extract"`) to pull distinct job-title strings out of the result snippets. The extracted titles flow through the same `classify_titles_batch` V1 uses, so bucket rules don't fork between the two paths. Results land in the same suggestion queue with `source="web_search"` (visible as a green pill on each card; the Source chip row at the top filters by it). Backend is configurable via `SEARCH_BACKEND` — defaults to `ddg` (zero-config `duckduckgo-search` package, no API key needed). For higher-quality results set `SEARCH_BACKEND=tavily` (or `brave`) + `SEARCH_API_KEY=...` in `.env`. Search results are cached on disk for 1 hour (`data/.search_cache/`, gitignored) so re-running during operator review doesn't re-hammer the backend. Use web search when "existing postings" has run dry — e.g. a freshly-added competitor whose scrapers haven't run yet, or when you suspect a competitor's keyword coverage feels thin.

### Add a competitor scraper

Subclass **`BaseEmployerScraper`** in `app/scrapers/base_employer.py` — don't reimplement the ABC directly. The base handles Playwright launch, robots.txt caching, retry/backoff, JSON-LD parsing, fixture fallback, and telemetry. Your subclass only configures site-specifics:

```python
@register("My Employer")
class MyEmployerScraper(BaseEmployerScraper):
    name = "My Employer"
    robots_url = "https://careers.myemployer.com/robots.txt"
    robots_target_path = "/jobs"
    search_url_template = "https://careers.myemployer.com/jobs?q={kw}"
    result_link_selectors = ["a.job-card", "a[href*='/jobs/']"]
    title_rejects = frozenset({"WORK LOCATION", ...})
    fixture_file = "myemployer_sample.html"
    fixture_postings = [{"raw_title": ..., "location_city": ..., ...}]
```

That's it — under 100 lines for a JSON-LD-publishing employer. Override `_extract_posting(self, page, html, url)` if the site doesn't ship `schema.org/JobPosting`; override `_scrape_live(self, keywords, max_postings)` only if you need a fundamentally different page-walk pattern (e.g. Walmart's two-URL-shape fallback).

**Then** add the module name to the discovery tuple in `app/scrapers/__init__.py`. Import is wrapped in try/except — a broken scraper module won't crash app boot.

**Telemetry your subclass gets for free.** Every `scrape()` call populates `self.last_run_telemetry`:
```python
{
  "keywords_tried": ["Cashier", "Loader"],
  "per_keyword_yielded": {"Cashier": 2, "Loader": 0},
  "per_keyword_errors": {"Cashier": 0, "Loader": 0},
  "links_seen": 2,
  "fallback_to_fixtures": False,
  "reasons": [],   # populated on every catch — e.g. "live scrape failed (RuntimeError: Akamai 403)"
}
```
`services/scraping.py` reads it after the scrape and writes a one-line summary into `ScraperRun.notes`, including the first failure reason when fixture fallback triggered — so an operator looking at `/admin/scrape-runs/{id}` sees exactly why a run produced no live data, without needing logs.
3. Add the module to `app/scrapers/__init__.py` so registration happens at boot (the import is wrapped in try/except, so a broken scraper module doesn't crash the app).
4. Drop fixture HTML into `app/scrapers/fixtures/<competitor>_<role>_sample.html` so FIXTURE_MODE + tests work without the live network.
5. Add tests under `tests/test_<competitor>_scraper.py` — assert registry wiring, `is_available()` type contract, FIXTURE_MODE yields ≥1 posting, exception path falls back. **Mock Playwright** — never hit the live network in CI.

Production-quality scraping is its own discipline. To make Home Depot live-reliable, you'd need: stealth fingerprint patches (`playwright-stealth` or hand-patched `navigator.webdriver`/plugins/WebGL), residential proxy rotation (datacenter IPs get challenged), pagination + dedupe via canonical URL, Akamai challenge-page detection ("Pardon Our Interruption") with a typed exception so the run lands as `blocked` rather than emitting garbage.

### Swap the LLM provider
The OpenRouter client uses an OpenAI-compatible chat-completions schema. Edit `_call_openrouter` in `services/llm.py` to point at your provider. Keep the structured-output `response_format` block — extraction depends on it.

### Change the database schema

Schema lives in `app/models.py`. **Don't** apply schema changes by editing `models.py` alone and relying on `init_db()` — `Base.metadata.create_all` only creates tables that don't exist yet, it doesn't add columns or modify existing ones. Use Alembic:

```bash
# 1. Edit app/models.py (add column, table, index, …).
# 2. Generate the migration:
.venv/bin/alembic revision --autogenerate -m "<short message>"
# 3. Review the generated alembic/versions/<timestamp>_<rev>_<slug>.py file.
# 4. Apply it locally:
.venv/bin/alembic upgrade head
# 5. Commit BOTH app/models.py and the new versions/ file.
# 6. In prod: git pull && .venv/bin/alembic upgrade head && systemctl restart compare-wages.
```

Full workflow + caveats in the **Schema migrations** subsection under Deployment.

## Dev DB vs prod DB — DO NOT mix them

The systemd service writes to `data/wages.db`. When you're iterating locally in the same checkout, **never** run `rm -f data/wages.db && python -m app.seed` — that wipes your prod state (active flags, run history, narrative).

Use the dev path instead:

```bash
./scripts/dev_reseed.sh
# → writes data/wages_dev.db, leaves data/wages.db untouched

# Run the app against the dev DB:
DATABASE_URL=sqlite:///./data/wages_dev.db .venv/bin/uvicorn app.main:app --reload
```

`app/seed.py` enforces this with a guard: if `DATABASE_URL` points at the prod path and the DB already has data, the seed refuses to run unless you set `ALLOW_PROD_SEED=1` or call `run_seed(force=True)` from Python. The guard catches the "I forgot which directory I'm in" mistake.

For ad-hoc Python scripts in the same checkout, always export `DATABASE_URL=sqlite:///./data/wages_dev.db` first or use a sub-shell.

**Schema is now Alembic-managed in prod.** For dev/test the first boot's `init_db()` (a plain `Base.metadata.create_all`) is still convenient and stays — it bootstraps a fresh SQLite file from `app/models.py` with no migration ceremony. **In production the schema is established and evolved by `alembic upgrade head`**, not by `init_db()`. The two are kept in lock-step because both ultimately read `app/models.py`; the day they drift, `.venv/bin/alembic check` will say so. See **Schema migrations** under Deployment for the workflow.

## Testing

```bash
.venv/bin/python -m pytest -q
```

17 tests as of writing. Conventions:
- Tests force `USE_MOCK_LLM=true` and use an isolated `data/test_wages.db`.
- `seeded_session` runs the full seed once per pytest session.
- The eval harness (`tests/eval_harness.py`) is also wired into `/admin/eval` — adding goldens to `data/golden_postings.json` updates both.

## Deployment

`gunicorn_config.py` matches the sat-prep style: one uvicorn-worker, unix socket, file logs. The unit runs as `www-data`, which has two prerequisites the systemd unit assumes — get these wrong and `systemctl start` will fail immediately.

### One-time setup

```bash
# 1. venv + deps + first seed
cd /var/www/compare-wages
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.seed

# 1b. Mark the schema as already at head so Alembic skips the (empty) baseline
# revision. The seed's create_all already materialized every table; this just
# writes the baseline rev id into the `alembic_version` table so future
# `alembic upgrade head` calls only apply real, post-baseline migrations.
# (If you're upgrading an EXISTING deployment that was set up before Alembic
# landed, run this exact same `alembic stamp head` once — same effect.)
.venv/bin/alembic stamp head

# 2. Create .env (REQUIRED — even if empty). EnvironmentFile is declared optional
# in the unit (leading '-'), but the app reads .env via pydantic-settings, and
# keeping it present is the right place for the OpenRouter key and any overrides.
cp .env.example .env
# then edit .env to set OPENROUTER_API_KEY, USE_MOCK_LLM=false, model overrides, etc.

# 3. Give www-data ownership of the whole tree. The unit cannot execute
# .venv/bin/gunicorn, write to logs/, or touch data/wages.db without this.
sudo chown -R www-data:www-data /var/www/compare-wages

# 4. Install + enable + start the systemd unit
sudo cp deploy/compare-wages.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now compare-wages
sudo systemctl status compare-wages
```

### Verifying

```bash
# Direct over the unix socket (bypasses nginx)
curl --unix-socket /var/www/compare-wages/compare-wages.sock http://localhost/ -I
# → HTTP/1.1 200 OK
```

Logs at `logs/access.log` and `logs/error.log`. Point an nginx `proxy_pass` at `unix:/var/www/compare-wages/compare-wages.sock` (same pattern as sat-prep).

### Reverse-proxy at a sub-path (e.g. `/compare-wages`)

If you're serving the app under a path prefix instead of at the host root (the current deployment is `https://lab.kudithipudi.org/compare-wages`), do **two** things:

1. Set the env var so every URL the app emits (templates, redirects, static assets) is prefix-aware:
   ```
   # /var/www/compare-wages/.env
   ROOT_PATH=/compare-wages
   ```
2. Configure nginx to strip the prefix when forwarding (the trailing slash in `proxy_pass` is what strips it):
   ```nginx
   location /compare-wages/ {
       proxy_pass http://unix:/var/www/compare-wages/compare-wages.sock:/;
       proxy_set_header Host              $host;
       proxy_set_header X-Real-IP         $remote_addr;
       proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

> **Gotcha — don't also pass `root_path` to FastAPI.** Because nginx strips the prefix, the app sees bare paths like `/static/css/app.css`. If you also pass `root_path="/compare-wages"` to `FastAPI(...)`, Starlette's `Mount` routing for `/static` expects the prefix to still be present and silently 404s every static asset (regular routes like `/`, `/admin` accidentally still match, so the bug hides until you load CSS/JS). `app/main.py` deliberately omits `root_path` for this reason. The `ROOT_PATH` *env var* is still set — it feeds the `{{ prefix }}` template global and the `SessionMiddleware` https-only heuristic.

To rename the public path tomorrow (e.g. `/compare-acme-wages`) you only change two things — `ROOT_PATH` in `.env` and the nginx `location` block — and `systemctl restart compare-wages`. No code or template edits.

Templates use a Jinja global `{{ prefix }}` (set from `ROOT_PATH`) on every internal URL. Redirects use `_redirect(path)` in `app/routers/admin.py`, which prepends `ROOT_PATH` to the `Location` header. External URLs (CDN, careers-site source links) stay absolute.

### Schema migrations

Schema changes go through **Alembic** — no more hand-rolled `ALTER TABLE` SQL against the prod DB. `app/db.py:init_db()` still runs `Base.metadata.create_all` on first boot so a brand-new dev SQLite file works without any setup, but the **prod path is Alembic** and the two stay in sync because both read `app/models.py`.

**Workflow when you change a model:**

```bash
# 1. Edit app/models.py — add a column, a table, an index, whatever.

# 2. Generate a migration. Autogenerate diffs Base.metadata against the
#    current DB and writes a versions/<timestamp>_<rev>_<slug>.py file.
.venv/bin/alembic revision --autogenerate -m "add foo column to bar"

# 3. REVIEW the generated file. Autogenerate is not perfect — it can miss
#    type changes, server defaults, and check constraints; on SQLite some
#    ALTERs need `op.batch_alter_table` (env.py already enables batch mode,
#    so this is usually free, but eyeball it). Edit the file if needed.

# 4. Apply it locally.
.venv/bin/alembic upgrade head

# 5. Commit BOTH app/models.py and the new versions/ file in one commit.
```

**Production deploy:**

```bash
cd /var/www/compare-wages
git pull
.venv/bin/pip install -r requirements.txt   # if requirements changed
.venv/bin/alembic upgrade head              # apply any new migrations
sudo systemctl restart compare-wages
```

`alembic upgrade head` is idempotent — re-running it after every deploy is fine; it only applies revisions whose id isn't already in the `alembic_version` table.

**Useful one-liners:**

```bash
.venv/bin/alembic current      # which revision is the DB at?
.venv/bin/alembic history      # full revision tree
.venv/bin/alembic heads        # current head(s)
.venv/bin/alembic check        # does Base.metadata match what's on disk? (no drift?)
.venv/bin/alembic downgrade -1 # revert the last migration (dev only — don't do this on prod data you care about)
```

**The baseline revision (`0001_baseline`) has empty `upgrade()` / `downgrade()`.** That's intentional: every existing DB was already materialized by `init_db()`, so re-running `CREATE TABLE` would either no-op or fight subtle DDL differences. `alembic stamp head` (in the one-time setup above) records that revision as applied without running it. From `0002` onward every revision is real and IS run normally.

**Don't edit a migration after it's been applied to prod** — write a NEW migration to fix it. Editing an applied migration leaves the DB and the file out of sync silently.

**SQLite caveat.** `env.py` enables `render_as_batch=True` for SQLite so most ALTER COLUMN operations work via the table-rebuild dance. Postgres / MySQL would handle them natively. If you ever migrate prod to Postgres, the same migration files apply — only the engine changes.

### Day-to-day

```bash
sudo systemctl reload compare-wages   # graceful HUP (after .env / template / static changes)
sudo systemctl restart compare-wages  # full restart (after dependency or Python changes)
sudo journalctl -u compare-wages -n 200 --no-pager
```

The unit hardens via `PrivateTmp=true`, `ProtectSystem=strict`, and `ReadWritePaths=/var/www/compare-wages /tmp`. Add to `ReadWritePaths` if you introduce other writable paths (e.g. a scraper cache outside the project root).

### Troubleshooting `start` failures

Most first-start failures fall into one of these — check `journalctl -u compare-wages -n 100 --no-pager` first.

| Symptom | Likely cause | Fix |
|---|---|---|
| `Failed to load environment files` | `.env` missing AND `EnvironmentFile=` not marked optional | `cp .env.example .env` (the shipped unit already has `EnvironmentFile=-…`, so this stops being fatal; restore the `-` prefix if someone removed it) |
| `Permission denied` on `.venv/bin/gunicorn` or `logs/error.log` | Tree owned by `root` instead of `www-data` | `sudo chown -R www-data:www-data /var/www/compare-wages` |
| `[Errno 98] Address already in use` on the socket | Previous run didn't clean up | `sudo rm /var/www/compare-wages/compare-wages.sock && sudo systemctl restart compare-wages` |
| Gunicorn exits 3 with no traceback | Unit started before deps were installed in the venv | Re-run step 1 of one-time setup, then `sudo systemctl restart compare-wages` |
| `ProtectSystem` errors writing to data/ or logs/ | Both paths must be under `ReadWritePaths` | Already included; check if a path was renamed |

### Security note on credentials

Never put `OPENROUTER_API_KEY` (or any secret) as the default value in `app/config.py` — that file ends up in version control, backups, and logs. The only place a secret should live is `.env`, which is gitignored and loaded by `EnvironmentFile=` at service start. If a key was committed by mistake, rotate it in the OpenRouter console immediately and force-push a remediation commit.

## Known gaps / roadmap

These are intentional day-one cuts — name them when planning the next iteration.

- **Walmart live scraping.** Walmart's careers site fronts an Akamai + PerimeterX stack that 403s vanilla headless Chromium from any datacenter IP. The Walmart scraper currently engages its fixture fallback on every live attempt. Unblocking requires `playwright-stealth` (or Camoufox/patchright) + a residential proxy pool — that's the real production work, not the scraper code.
- **Amazon street/zip address fidelity.** `amazon.jobs` (corporate) doesn't ship `streetAddress`/`postalCode` in its JSON-LD, only `addressLocality` + `addressRegion`. Warehouse roles — which is what we actually want for wage comparisons — live on `hiring.amazon.com` with full structured data. Adding a sibling Amazon scraper pointed at that subdomain would lift address precision to Costco level.
- **Starbucks scraper.** Not yet built; follow the Home Depot / Amazon / Costco template.
- **acme locations scrape.** Yards are hand-seeded — `acme.com/locations` is an AngularJS SPA and not scrape-friendly without a headless browser.
- **Travel time.** Distances are Haversine; a maps API would give drive-time, which is what labor markets actually care about.
- **Historical trends.** Only the current snapshot is stored. Add a `WageSnapshot` model and write a snapshot per scrape run for time-series queries.
- **Embeddings-based entity resolution.** Today the competitor name is the join key. Real-world data would need fuzzy employer matching.
- **BLS OEWS MSA-level.** State-level BLS data is live; ZIP→CBSA crosswalk is now in (`data/zip_to_cbsa.csv`, 32k ZIPs covering 935 CBSAs from the HUD USPS API; CBSA names from Census Bureau in `data/cbsa_names.csv`). Each yard has a `cbsa_code`. To finish the MSA-level wage layer: download `oesm23ma.zip` from BLS, reshape into `BlsOewsWage` keyed on `cbsa_code`, and extend `app/services/bls.py` with `baseline_for_msa(cbsa_code)` falling back to `baseline_for(state)` for rural yards outside any CBSA.
- **Per-purpose eval coverage.** Today only wage extraction has a golden set. Classification + narrative deserve their own evals.
- **Authentication.** No auth — wire in OAuth/SSO before exposing publicly.
