"""Generic web-search abstraction for Role Discovery V2.

V1 (``role_discovery.discover_from_existing_postings``) is bootstrap-limited:
a competitor with no scraped postings yet has nothing to mine. V2 closes that
gap by querying a plain web search engine for each competitor and extracting
job titles from the result snippets. This module is the thin search layer the
orchestrator calls into.

Backed by DuckDuckGo via the ``duckduckgo-search`` PyPI package — no API key
required, which keeps the demo-mode story intact (``USE_MOCK_LLM=true`` plus
zero secrets).

Throttle: 1 req/sec inside a single discovery run (module-level last-call
timestamp; no persistent state). Errors return ``[]`` — discovery should
never crash on a rate-limit blip.

Cache: results are written to ``data/.search_cache/<sha256(query)>.json``
with a 1-hour TTL so re-running discovery during operator review doesn't
hammer DDG. The cache directory is gitignored.

Mock mode: when ``USE_MOCK_LLM=true`` the search function returns a
deterministic per-competitor fixture so tests stay offline + stable. This
intentionally piggy-backs on the same flag the rest of the system uses —
the test harness sets it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

# Cache dir + TTL. Module-level so it survives across calls in the same run.
_CACHE_DIR = Path("data/.search_cache")
_CACHE_TTL_SECONDS = 3600  # 1 hour

# Throttle: 1 req/sec between live calls. Process-local — fine for the
# single-threaded synchronous discovery orchestrator.
_LAST_CALL_TS: float = 0.0
_MIN_INTERVAL_SECONDS = 1.0


# ------------------------- mock fixtures -------------------------

# Per-competitor deterministic stubs. Lowercased competitor name → list of
# fake search-result dicts. The mock results reference real role titles so
# the downstream LLM extractor (or its mock fallback) has signal to work
# with. Generic fallback below for unknown competitors.
_MOCK_FIXTURES: dict[str, list[dict[str, str]]] = {
    "home depot": [
        {
            "title": "Lot Associate jobs at The Home Depot",
            "snippet": (
                "Lot Associates work in the parking lot to assemble carts, load "
                "freight, and assist customers. Cashier, Freight Team Associate, "
                "and Customer Service positions are also available."
            ),
            "url": "https://careers.homedepot.com/lot-associate",
        },
        {
            "title": "Freight Team Associate · The Home Depot Careers",
            "snippet": (
                "Freight Team Associate works overnight unloading trucks. "
                "Other roles include Stocker, Forklift Operator, and Receiver."
            ),
            "url": "https://careers.homedepot.com/freight-team",
        },
        {
            "title": "Cashier hourly hiring · Home Depot",
            "snippet": (
                "Cashier and Customer Service Associate openings hire entry-level "
                "applicants. Apply today."
            ),
            "url": "https://careers.homedepot.com/cashier",
        },
    ],
    "walmart": [
        {
            "title": "Walmart hourly jobs: Stocker, Cashier, Cart Attendant",
            "snippet": (
                "Walmart hires Stockers, Cashiers, Cart Attendants, and Order "
                "Fillers for entry-level hourly work."
            ),
            "url": "https://careers.walmart.com/hourly",
        },
        {
            "title": "Forklift Operator · Walmart Distribution",
            "snippet": (
                "Forklift Operator and Freight Handler positions at Walmart "
                "Distribution centers."
            ),
            "url": "https://careers.walmart.com/dc",
        },
    ],
    "amazon": [
        {
            "title": "Amazon Warehouse Associate hiring",
            "snippet": (
                "Warehouse Associate, Sortation Associate, and Delivery Associate "
                "roles at Amazon fulfillment centers."
            ),
            "url": "https://hiring.amazon.com/warehouse",
        },
    ],
    "costco": [
        {
            "title": "Costco careers: Cashier Assistant, Stocker",
            "snippet": (
                "Costco hires Cashier Assistant, Stocker, Front End Assistant, "
                "and Forklift Driver positions."
            ),
            "url": "https://careers.costco.com/hourly",
        },
    ],
    "starbucks": [
        {
            "title": "Starbucks Barista jobs",
            "snippet": (
                "Barista and Shift Supervisor openings at Starbucks. "
                "Customer service experience preferred."
            ),
            "url": "https://careers.starbucks.com/barista",
        },
    ],
}

_GENERIC_FIXTURE: list[dict[str, str]] = [
    {
        "title": "Warehouse Associate · Generic Employer hiring",
        "snippet": (
            "Warehouse Associate, Loader, Cashier, and Customer Service Associate "
            "positions are open."
        ),
        "url": "https://example.test/hourly",
    },
    {
        "title": "Forklift Driver and Stocker jobs",
        "snippet": (
            "Forklift Driver, Stocker, and Material Handler roles are available "
            "for entry-level applicants."
        ),
        "url": "https://example.test/forklift",
    },
]


def _mock_fixture_for(query: str) -> list[dict[str, str]]:
    """Pick a per-competitor mock based on substring match against the query."""
    q = query.lower()
    for name, fixture in _MOCK_FIXTURES.items():
        if name in q:
            return fixture
    return _GENERIC_FIXTURE


# ------------------------- cache helpers -------------------------


def _cache_path(query: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{digest}.json"


def _cache_get(query: str) -> list[dict[str, str]] | None:
    path = _cache_path(query)
    if not path.exists():
        return None
    try:
        # Mtime-based TTL is cheap. The cache value is a JSON list of dicts.
        if (time.time() - path.stat().st_mtime) > _CACHE_TTL_SECONDS:
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        # Corrupted cache file — better to re-fetch than to crash. Caller
        # gets a fresh call; this stale file gets overwritten next write.
        return None
    return None


def _cache_put(query: str, results: list[dict[str, str]]) -> None:
    path = _cache_path(query)
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(results, fh)
    except OSError:
        # Cache writes are best-effort. A read-only data/ shouldn't break
        # discovery — log and move on.
        log.warning("web_search: failed to write cache file %s", path)


# ------------------------- throttle -------------------------


def _throttle() -> None:
    global _LAST_CALL_TS
    elapsed = time.time() - _LAST_CALL_TS
    if elapsed < _MIN_INTERVAL_SECONDS:
        time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
    _LAST_CALL_TS = time.time()


# ------------------------- backend -------------------------


def _search_ddg(query: str, max_results: int) -> list[dict[str, str]]:
    """DuckDuckGo backend via the ``duckduckgo-search`` package. Maps DDG's
    ``{title, body, href}`` keys to our ``{title, snippet, url}`` contract."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        log.warning(
            "web_search: duckduckgo-search not installed; returning empty list"
        )
        return []
    out: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for row in ddgs.text(query, max_results=max_results) or []:
            out.append({
                "title": str(row.get("title", "")),
                "snippet": str(row.get("body", "")),
                "url": str(row.get("href", "")),
            })
    return out


# ------------------------- public API -------------------------


def search(query: str, max_results: int = 15) -> list[dict[str, str]]:
    """Run a single web search and return ``[{title, snippet, url}, ...]``.

    Mock mode (``USE_MOCK_LLM=true``) returns a deterministic per-competitor
    fixture — no network. Live mode hits DuckDuckGo via the
    ``duckduckgo-search`` package. Errors return an empty list so a flaky
    backend doesn't crash the discovery orchestrator. Throttled to 1 request
    per second across all calls in this process; results are cached on disk
    for 1 hour keyed on ``sha256(query)``.
    """
    settings = get_settings()

    # Mock mode short-circuits everything — no cache, no throttle, no network.
    if settings.use_mock_llm:
        return _mock_fixture_for(query)

    # Cache check before anything else.
    cached = _cache_get(query)
    if cached is not None:
        return cached

    _throttle()

    try:
        results = _search_ddg(query, max_results)
    except Exception as e:
        # Never crash the orchestrator on a search failure. Log + return empty.
        log.warning("web_search: query=%r failed: %s", query, e)
        return []

    _cache_put(query, results)
    return results
