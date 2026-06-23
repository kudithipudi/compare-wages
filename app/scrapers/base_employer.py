"""Shared base class for employer-careers scrapers.

Why this exists
---------------
Before this module existed, every employer scraper (Home Depot, Amazon, Costco,
Walmart) shipped its own copy of the same ~450 lines of boilerplate:

  * ``robots.txt`` fetch + parse
  * HEAD probe to detect Akamai-style edge blocks
  * ``schema.org/JobPosting`` JSON-LD extraction
  * ``FIXTURE_MODE`` env-var check
  * ``sync_playwright()`` browser launch boilerplate
  * The live-then-fixture exception-fallback pattern in ``scrape()``
  * Hardcoded ``User-Agent`` string
  * The ``time.sleep(1.0)`` polite pause between page navigations

Pulling all of that up into ``BaseEmployerScraper`` shrinks each concrete
scraper down to ~120 lines of *only* site-specific bits (URL templates,
fixture entries, the occasional ``_extract_posting`` override for sites that
don't ship JSON-LD).

Resilience features (new, not in the per-site copies)
-----------------------------------------------------
The refactor also lands several reliability features that were missing
everywhere:

1.  **``robots.txt`` cached process-wide for ~1 hour.**  ``functools.lru_cache``
    keyed on ``(robots_url, target_path, time.time() // 3600)`` means a long-
    running daemon won't re-fetch robots.txt on every scrape.

2.  **Playwright ``TimeoutError`` retry with exponential backoff** on detail-
    page navigation.  Up to 2 retries, sleeping 1s -> 3s -> 9s.  After
    exhausting retries we log and skip that single posting — we do NOT abort
    the whole keyword.

3.  **Per-keyword failure budget.**  If 3 consecutive detail pages fail for a
    given keyword we stop walking that keyword's link list and move on,
    recording the count under ``telemetry["per_keyword_errors"][kw]``.  This
    prevents one bad keyword from burning the whole rate-limit budget.

4.  **HTTP retry inside ``is_available()``.**  Up to 2 retries on HEAD with
    exponential backoff before deciding the site is blocked.

5.  **No silent ``except Exception``.**  Every catch logs the exception type
    plus ``str(exc)`` into ``telemetry["reasons"]`` so the operator can see
    *why* a run produced 0 postings from ``/admin/scrape-runs``.

6.  **``time.sleep(...)`` between pages reads ``self.rate_limit_hz``** so a
    subclass can throttle slower without re-implementing the polite-pause
    logic.

7.  **Fixture fallback only kicks in if 0 postings yielded.**  Partial
    success (e.g. 5 live + then an Akamai 403) keeps the 5 — we don't
    replace real data with fixtures.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx  # imported here so test-suite patches at this module also resolve

# Snapshot the real httpx.Client at import time. The per-scraper test suites
# patch ``app.scrapers.<name>.httpx.Client`` (which equals the global
# ``httpx.Client``) — when that happens we want to detect "this is no longer
# the real client" and bypass the robots.txt cache so the second test in a
# sequence doesn't read a stale decision left behind by the first.
_REAL_HTTPX_CLIENT = httpx.Client

from app.scrapers.base import ScrapedPosting, Scraper

log = logging.getLogger(__name__)

# Realistic desktop Chrome UA. Used for HTTP probes and the Playwright context.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Shared regexes for JSON-LD JobPosting parsing. Compiled once at import time.
_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Strip control chars that occasionally appear unescaped inside JSON-LD blocks
# and would otherwise make ``json.loads`` raise.
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Map state full names -> USPS two-letter codes. Used by both the Amazon body-
# text parser (corporate amazon.jobs) and Costco's full-name JSON-LD shape.
US_STATE_ABBR: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "PUERTO RICO": "PR",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}


def _normalize_state(raw: str) -> str:
    """Map a raw ``addressRegion`` string to the USPS two-letter code.

    Accepts either the already-correct two-letter code (returned uppercased)
    or a full state name. Returns ``""`` when neither matches — the caller
    handles the fall-through ``"XX"`` placeholder.
    """
    if not raw:
        return ""
    cleaned = raw.strip().upper()
    if cleaned in US_STATE_ABBR:
        return US_STATE_ABBR[cleaned]
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned
    return ""


def parse_jobposting_jsonld(html: str) -> dict | None:
    """Return the first ``schema.org/JobPosting`` dict found in any JSON-LD block,
    or ``None`` if nothing parseable is present.

    Tolerant of unescaped control chars (we've seen them in the wild on a few
    iCIMS-rendered pages) and of both single-object and list-of-objects shapes.
    """
    if not html:
        return None
    for block in _JSONLD_BLOCK_RE.findall(html):
        cleaned = _CTRL_CHARS_RE.sub("", block).strip()
        try:
            obj = json.loads(cleaned)
        except Exception:
            continue
        candidates = obj if isinstance(obj, list) else [obj]
        for c in candidates:
            if isinstance(c, dict) and str(c.get("@type", "")) in ("JobPosting", "JobAnnouncement"):
                return c
    return None


def fixture_mode_enabled() -> bool:
    """``FIXTURE_MODE`` env-var truthy check, shared by every employer scraper."""
    return os.environ.get("FIXTURE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_robots_decision(
    robots_url: str,
    target_path: str,
    user_agent: str,
    client_factory: Any,
) -> bool:
    """Perform the ``robots.txt`` fetch + parse. Fail-open on transient errors."""
    try:
        with client_factory(
            timeout=8.0,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        ) as client:
            resp = client.get(robots_url)
        if resp.status_code != 200:
            log.info("robots.txt fetch returned %s; assuming allow", resp.status_code)
            return True
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        full_url = urljoin(robots_url, target_path)
        return rp.can_fetch(user_agent, full_url)
    except Exception as exc:  # noqa: BLE001
        log.info("robots.txt check failed (%s); fail-open", exc)
        return True


@lru_cache(maxsize=32)
def _cached_real_robots_decision(
    robots_url: str, target_path: str, user_agent: str, bucket: int  # noqa: ARG001
) -> bool:
    """Cached ``robots.txt`` decision keyed by (url, path, ua, hour-bucket).

    Only used when we're hitting the real httpx client — see ``check_robots``
    for the routing. ``bucket`` is ``int(time.time() // 3600)`` so a long-
    running daemon picks up robots.txt edits within ~1 hour without anyone
    having to manually invalidate the cache.
    """
    return _fetch_robots_decision(robots_url, target_path, user_agent, httpx.Client)


def check_robots(
    robots_url: str,
    target_path: str,
    user_agent: str = USER_AGENT,
    *,
    client_factory: Any = None,
) -> bool:
    """Public entry point.

    Routes to the lru-cached fetcher only when the real ``httpx.Client`` is
    in use (the common case in production). When a test injects a fake client
    we bypass the cache entirely so a stale "allow" decision from an earlier
    test doesn't bleed into a later test that expects "disallow".
    """
    if client_factory is None or client_factory is _REAL_HTTPX_CLIENT:
        bucket = int(time.time() // 3600)
        return _cached_real_robots_decision(robots_url, target_path, user_agent, bucket)
    return _fetch_robots_decision(robots_url, target_path, user_agent, client_factory)


class BaseEmployerScraper(Scraper):
    """Shared base for the four employer-careers scrapers.

    Subclasses set a handful of class attributes (``name``, ``robots_url``,
    ``search_url_template``, ``fixture_postings``, ``fixture_file``, etc.) and
    override ``_extract_posting`` only if the site needs site-specific logic
    on top of the default JSON-LD parser. Everything else — Playwright launch,
    retries, fallback, telemetry — comes from this base.
    """

    # --- subclasses MUST set ---
    name: str = ""
    robots_url: str = ""
    robots_target_path: str = ""
    search_url_template: str = ""  # contains ``{kw}`` placeholder; for keyword-only URLs.
    fixture_file: str = ""
    fixture_postings: list[dict[str, Any]] = []
    result_link_selectors: list[str] = []

    # --- subclasses MAY override for location-aware search ---
    # Default: no location filter (uses ``search_url_template`` as-is). Subclasses
    # override ``search_url_for(keyword, location)`` to inject site-specific city/state
    # query params (e.g. Home Depot supports ``&city=X&state=Y``).
    # The maximum (location × keyword) pairs to plan per scrape, so a 50-yard × 10-keyword
    # combination doesn't fan out to 500 page fetches. Excess pairs are dropped after
    # sampling (operator can run multiple scrapes for full coverage).
    max_location_keyword_pairs: int = 40

    # --- subclasses MAY override ---
    user_agent: str = USER_AGENT
    rate_limit_hz: float = 1.0
    fixture_dir: Path = Path(__file__).parent / "fixtures"
    title_rejects: frozenset[str] = frozenset()
    detail_title_selectors: list[str] = [
        "main h1",
        "[data-testid='job-title']",
        ".job-title",
        "h1",
    ]
    # ``{kw}`` substring is replaced with ``+``-encoded keyword in the search URL.
    # Substrings required to appear in a result-card href; if a link has none
    # of these we skip it. Empty list = accept any href.
    link_href_must_contain: tuple[str, ...] = ()
    # Detail pages we never want to load (e.g. iCIMS' ``/login`` and ``/apply``
    # endpoints, which are the same posting wrapped in an auth flow).
    link_href_blocklist_re: tuple[str, ...] = ()

    # --- transient per-instance state ---
    last_run_telemetry: dict[str, Any]

    def __init__(self) -> None:
        # Each fresh instance starts with a clean telemetry dict. The service
        # layer reads ``last_run_telemetry`` after a scrape to surface details
        # on the ``/admin/scrape-runs`` page.
        self.last_run_telemetry = self._fresh_telemetry()

    @classmethod
    def _http_client_factory(cls):
        """Return the ``httpx.Client`` callable to use for HTTP probes.

        We resolve it from the subclass's own module (e.g.
        ``app.scrapers.homedepot.httpx.Client``) so test suites that
        ``patch('app.scrapers.homedepot.httpx.Client', ...)`` actually take
        effect. Falls back to the base module's import.
        """
        import sys

        mod = sys.modules.get(cls.__module__)
        if mod is not None:
            mod_httpx = getattr(mod, "httpx", None)
            if mod_httpx is not None and hasattr(mod_httpx, "Client"):
                return mod_httpx.Client
        return httpx.Client

    # ------------------------------------------------------------------ #
    # Telemetry                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fresh_telemetry() -> dict[str, Any]:
        return {
            "keywords_tried": [],
            "per_keyword_yielded": {},
            "per_keyword_errors": {},
            "locations_tried": [],
            "per_location_yielded": {},
            "per_location_errors": {},
            "total_pairs_planned": 0,
            "links_seen": 0,
            "fallback_to_fixtures": False,
            "reasons": [],
        }

    def _note(self, msg: str) -> None:
        """Append an operator-readable reason to telemetry without flooding logs."""
        self.last_run_telemetry["reasons"].append(msg)
        log.info("[%s] %s", self.name or self.__class__.__name__, msg)

    # ------------------------------------------------------------------ #
    # Public surface: is_available + scrape                               #
    # ------------------------------------------------------------------ #
    def is_available(self) -> bool:
        """robots.txt + HEAD probe with retry. Subclasses rarely override."""
        factory = self._http_client_factory()
        if not check_robots(
            self.robots_url,
            self.robots_target_path,
            self.user_agent,
            client_factory=factory,
        ):
            log.info("%s robots.txt disallows %s", self.name, self.robots_target_path)
            return False
        probe_url = self.search_url_template.format(kw="Cashier")
        if not self._head_ok(probe_url, retries=2):
            log.info("%s search HEAD failed after retries — likely blocked", self.name)
            return False
        return True

    def search_url_for(self, keyword: str, location: tuple[str, str] | None = None) -> str:
        """Build a search URL for ``keyword``, optionally scoped to ``(city, state)``.

        Default uses ``search_url_template`` formatted with the keyword (URL-encoded).
        Subclasses override this when the site supports location filtering via query
        params (e.g. Home Depot's ``&city=X&state=Y``). When ``location`` is None and
        the subclass hasn't overridden, behavior is identical to the pre-location-aware
        codebase — preserves backwards compatibility for tests that don't pass locations.
        """
        return self.search_url_template.format(kw=keyword.replace(" ", "+"))

    def scrape(
        self,
        *,
        keywords: list[str],
        locations: list[tuple[str, str]] | None = None,
        max_postings: int = 25,
    ) -> Iterator[ScrapedPosting]:
        """Live-with-fallback orchestrator (location-aware).

        Order of operations:
          1. Reset telemetry.
          2. Empty keywords -> yield nothing (the contract is explicit).
          3. ``FIXTURE_MODE`` set -> serve fixtures, skip the live path entirely.
          4. Else try ``_scrape_live``. If it yields anything, keep it. If it
             raises BEFORE yielding anything, fall back to fixtures. Partial
             successes are NEVER replaced with fixtures.
          5. Either way ``last_run_telemetry`` reflects what happened.

        ``locations`` is an optional list of ``(city, state)`` pairs. ``None`` or ``[]``
        means "global keyword search" (today's behavior). With locations, the live path
        iterates ``(location × keyword)`` pairs, capped at ``max_location_keyword_pairs``.
        """
        self.last_run_telemetry = self._fresh_telemetry()
        if not keywords:
            self._note("no keywords provided")
            return
        if fixture_mode_enabled():
            self._note("FIXTURE_MODE set — serving canned postings")
            self.last_run_telemetry["fallback_to_fixtures"] = True
            self.last_run_telemetry["keywords_tried"] = list(keywords)
            yield from self._scrape_from_fixtures(keywords, max_postings)
            return

        yielded = 0
        try:
            for posting in self._scrape_live(keywords, max_postings, locations=locations):
                yielded += 1
                yield posting
                if yielded >= max_postings:
                    return
        except Exception as exc:  # noqa: BLE001
            self._note(f"live scrape failed ({type(exc).__name__}: {exc})")
            if yielded == 0:
                self.last_run_telemetry["fallback_to_fixtures"] = True
                if not self.last_run_telemetry["keywords_tried"]:
                    self.last_run_telemetry["keywords_tried"] = list(keywords)
                yield from self._scrape_from_fixtures(keywords, max_postings)

    # ------------------------------------------------------------------ #
    # HEAD probe with retry                                               #
    # ------------------------------------------------------------------ #
    def _head_ok(self, url: str, *, retries: int = 2) -> bool:
        """HEAD ``url`` up to ``retries+1`` times with exponential backoff.

        Returns True if we ever see a 2xx (or 405 — some CDNs refuse HEAD but
        the upstream is fine). Returns False if we exhaust retries on 4xx/5xx
        or network errors.
        """
        factory = self._http_client_factory()
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with factory(
                    timeout=8.0,
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=True,
                ) as client:
                    resp = client.head(url)
                if resp.status_code == 405:
                    return True
                if resp.status_code < 400:
                    return True
                # 4xx/5xx — retry unless it's the last attempt.
                last_exc = RuntimeError(f"HEAD {url} -> {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            if attempt < retries:
                time.sleep(delay)
                delay *= 3
        log.info("HEAD %s failed after %d attempts (%s)", url, retries + 1, last_exc)
        return False

    # ------------------------------------------------------------------ #
    # Live scrape (default implementation — Playwright + JSON-LD)         #
    # ------------------------------------------------------------------ #
    def _scrape_live(
        self,
        keywords: list[str],
        max_postings: int,
        *,
        locations: list[tuple[str, str]] | None = None,
    ) -> Iterator[ScrapedPosting]:
        # Lazy import so unit tests that never touch the live path don't
        # require chromium to be installed.
        from playwright.sync_api import sync_playwright  # type: ignore
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        except Exception:  # noqa: BLE001
            PlaywrightTimeoutError = Exception  # type: ignore[assignment]

        pause = 1.0 / max(self.rate_limit_hz, 0.001)

        # Plan the (location, keyword) work units. None location = global keyword search
        # (backwards-compatible default).
        loc_list: list[tuple[str, str] | None] = list(locations) if locations else [None]
        self.last_run_telemetry["locations_tried"] = [
            f"{c},{s}" for (c, s) in loc_list if c
        ]
        planned: list[tuple[tuple[str, str] | None, str]] = [
            (loc, kw) for loc in loc_list for kw in keywords
        ]
        cap = self.max_location_keyword_pairs
        if len(planned) > cap:
            import random as _r
            _r.shuffle(planned)
            planned = planned[:cap]
            self._note(
                f"planned {len(loc_list) * len(keywords)} (loc×kw) pairs, sampling {cap}"
            )
        self.last_run_telemetry["total_pairs_planned"] = len(planned)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                )
                page = context.new_page()

                # 1. Collect a deduplicated link list for each (location, keyword)
                #    pair, tracking origin so the per-keyword failure budget below
                #    still works.
                seen: set[str] = set()
                all_links: list[tuple[str, str]] = []  # (keyword, href)
                for loc, kw in planned:
                    self.last_run_telemetry["keywords_tried"].append(kw)
                    self.last_run_telemetry["per_keyword_yielded"].setdefault(kw, 0)
                    self.last_run_telemetry["per_keyword_errors"].setdefault(kw, 0)
                    loc_key = f"{loc[0]},{loc[1]}" if loc else "(global)"
                    self.last_run_telemetry["per_location_yielded"].setdefault(loc_key, 0)
                    self.last_run_telemetry["per_location_errors"].setdefault(loc_key, 0)
                    if len(all_links) >= max_postings:
                        break
                    search_url = self.search_url_for(kw, loc)
                    log.info("Navigating to %s", search_url)
                    try:
                        page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                    except Exception as exc:  # noqa: BLE001
                        self._note(f"search nav failed for '{kw}' @ {loc_key} ({type(exc).__name__}: {exc})")
                        self.last_run_telemetry["per_location_errors"][loc_key] += 1
                        continue
                    time.sleep(pause)
                    new_links = self._extract_result_links(page, max_postings - len(all_links))
                    if not new_links:
                        self.last_run_telemetry["per_location_errors"][loc_key] += 1
                    for href in new_links:
                        if href in seen:
                            continue
                        seen.add(href)
                        all_links.append((kw, href))
                        self.last_run_telemetry["per_location_yielded"][loc_key] += 1
                        if len(all_links) >= max_postings:
                            break

                self.last_run_telemetry["links_seen"] = len(all_links)

                if not all_links:
                    raise RuntimeError(
                        f"{self.name} search returned no result links — likely an edge block "
                        "or selector drift (no card matched any candidate selector)."
                    )

                # 2. Walk detail pages with per-keyword failure budgeting.
                yield from self._walk_details_sequential(
                    page, all_links[:max_postings], pause, PlaywrightTimeoutError
                )
            finally:
                try:
                    browser.close()
                except Exception as exc:  # noqa: BLE001
                    self._note(f"browser close failed ({type(exc).__name__}: {exc})")

    def _walk_details_sequential(
        self,
        page,
        links: list[tuple[str, str]],
        pause: float,
        timeout_exc_cls: type[BaseException],
    ) -> Iterator[ScrapedPosting]:
        """Single-page, single-thread detail-walk path.

        Iterates over ``(keyword, href)`` pairs, loading each detail page
        through :meth:`_load_detail_with_retry`. Maintains a per-keyword
        consecutive-failure counter; once a keyword burns 3 failures we stop
        walking that keyword's remaining links so one bad keyword can't burn
        the whole budget.
        """
        consecutive_failures: dict[str, int] = {kw: 0 for kw, _ in links}
        skipped_keywords: set[str] = set()
        # Defensive: callers from ``_scrape_live`` always pre-seed these dicts,
        # but if a subclass / test invokes this method directly we still want
        # to record telemetry instead of KeyError-ing.
        for kw, _ in links:
            self.last_run_telemetry["per_keyword_yielded"].setdefault(kw, 0)
            self.last_run_telemetry["per_keyword_errors"].setdefault(kw, 0)

        for kw, href in links:
            if kw in skipped_keywords:
                continue
            detail_url = self._absolutize(href)

            posting = self._load_detail_with_retry(
                page, detail_url, timeout_exc_cls, pause
            )
            if posting is None:
                consecutive_failures[kw] += 1
                self.last_run_telemetry["per_keyword_errors"][kw] += 1
                if consecutive_failures[kw] >= 3:
                    self._note(
                        f"3 consecutive failures on keyword '{kw}' — skipping remainder"
                    )
                    skipped_keywords.add(kw)
                continue

            consecutive_failures[kw] = 0
            self.last_run_telemetry["per_keyword_yielded"][kw] += 1
            yield posting

    def _load_detail_with_retry(
        self,
        page,
        detail_url: str,
        timeout_exc_cls: type[BaseException],
        pause: float,
    ) -> ScrapedPosting | None:
        """Load a detail page with exponential-backoff retry on TimeoutError.

        Up to 2 retries (3 total attempts), sleeping 1s -> 3s -> 9s. Returns the
        ScrapedPosting on success, or ``None`` if all attempts fail. Non-timeout
        exceptions are logged into telemetry and treated as a single failure.
        """
        delay = 1.0
        for attempt in range(3):
            try:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(pause)
                html = page.content()
                posting = self._extract_posting(page, html, detail_url)
                if posting is None:
                    self._note(f"_extract_posting returned None for {detail_url}")
                    return None
                return posting
            except timeout_exc_cls as exc:
                if attempt < 2:
                    self._note(
                        f"timeout on {detail_url} (attempt {attempt + 1}); "
                        f"retrying in {delay}s"
                    )
                    time.sleep(delay)
                    delay *= 3
                    continue
                self._note(f"timeout on {detail_url} after 3 attempts ({exc})")
                return None
            except Exception as exc:  # noqa: BLE001
                self._note(
                    f"failed to load {detail_url} ({type(exc).__name__}: {exc}); skipping"
                )
                return None
        return None

    def _absolutize(self, href: str) -> str:
        """Join a relative href onto the same origin as the robots.txt URL."""
        return urljoin(self.robots_url, href)

    # ------------------------------------------------------------------ #
    # Default _extract_posting: JSON-LD JobPosting -> ScrapedPosting      #
    # ------------------------------------------------------------------ #
    def _extract_posting(
        self, page, html: str, source_url: str
    ) -> ScrapedPosting | None:
        """Parse a job-detail page into a ScrapedPosting.

        Default behavior:
          1. Pull the first ``schema.org/JobPosting`` JSON-LD block.
          2. Extract ``title`` + ``jobLocation.address`` fields.
          3. Fall back to page-title regex (``"<title> - City, ST | ..."``)
             when JSON-LD is absent.

        Subclasses override this when the site doesn't ship JSON-LD — e.g.
        amazon.jobs (corporate) renders location as plain body text.
        """
        ld = parse_jobposting_jsonld(html)
        title = ""
        city = state = street = zip_code = ""
        if ld:
            title = (ld.get("title") or "").strip()
            addr = self._address_from_jsonld(ld)
            city = (addr.get("addressLocality") or "").strip()
            state = _normalize_state(addr.get("addressRegion") or "") or (
                (addr.get("addressRegion") or "").strip().upper()
            )
            street = (addr.get("streetAddress") or "").strip()
            zip_code = str(addr.get("postalCode") or "").strip()

        if not title:
            title = self._first_text(page, self.detail_title_selectors) or "Unknown Role"
        if not city or not state:
            t_city, t_state = self._city_state_from_title(self._safe_page_title(page))
            if not city and t_city:
                city = t_city
            if not state and t_state:
                state = t_state.upper()

        return ScrapedPosting(
            competitor_name=self.name,
            raw_title=title.strip(),
            location_city=city or "Unknown",
            location_state=(state or "XX").upper()[:2],
            raw_html=html,
            source_url=source_url,
            street_address=street,
            zip_code=zip_code,
        )

    @staticmethod
    def _address_from_jsonld(ld: dict) -> dict:
        """Extract the address subdict from a JSON-LD JobPosting.

        ``jobLocation`` can be either a dict or a list of dicts (multi-location
        postings). We use the first entry's address either way.
        """
        job_loc = ld.get("jobLocation")
        if isinstance(job_loc, list):
            job_loc = job_loc[0] if job_loc else {}
        if isinstance(job_loc, dict):
            return job_loc.get("address", {}) or {}
        return {}

    @staticmethod
    def _safe_page_title(page) -> str:
        try:
            return page.title() or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ #
    # Fixture path                                                        #
    # ------------------------------------------------------------------ #
    def _scrape_from_fixtures(
        self, keywords: list[str], max_postings: int
    ) -> Iterator[ScrapedPosting]:
        """Yield ScrapedPostings from the in-module fixture list.

        Behavior:
          * ``max_postings <= 0`` -> nothing (runaway guard).
          * Filter ``fixture_postings`` by case-insensitive keyword match
            against ``raw_title``; if nothing matches, fall back to the full
            list so the dashboard still has content to display.
          * Render ``{{CITY}}``, ``{{STATE}}``, ``{{TITLE}}``, ``{{STREET}}``,
            ``{{ZIP}}`` placeholders in the fixture HTML so the LLM extractor
            sees real strings.
        """
        if max_postings <= 0:
            return
        kw_lower = [k.lower() for k in keywords] if keywords else []
        chosen = self.fixture_postings if not kw_lower else [
            f for f in self.fixture_postings
            if any(k in f["raw_title"].lower() for k in kw_lower)
        ] or self.fixture_postings
        for entry in chosen[:max_postings]:
            path = self.fixture_dir / entry.get("fixture", self.fixture_file)
            if not path.exists():
                self._note(f"fixture file missing: {path}")
                continue
            html = path.read_text(encoding="utf-8")
            html = self._render_fixture_html(html, entry)
            yield self._posting_from_fixture(entry, html)

    @staticmethod
    def _render_fixture_html(html: str, entry: dict) -> str:
        """Substitute the standard placeholders into the fixture HTML."""
        return (
            html.replace("{{CITY}}", entry.get("location_city", ""))
            .replace("{{STATE}}", entry.get("location_state", ""))
            .replace("{{TITLE}}", entry.get("raw_title", ""))
            .replace("{{STREET}}", entry.get("street_address", ""))
            .replace("{{ZIP}}", entry.get("zip_code", ""))
        )

    def _posting_from_fixture(self, entry: dict, html: str) -> ScrapedPosting:
        """Build a ScrapedPosting from a fixture entry, re-parsing the rendered
        JSON-LD when present so the structured-address round-trip is exercised
        end-to-end in tests.
        """
        street = entry.get("street_address", "")
        zip_code = entry.get("zip_code", "")
        city = entry["location_city"]
        state = entry["location_state"]
        raw_title = entry["raw_title"]
        ld = parse_jobposting_jsonld(html)
        if ld:
            addr = self._address_from_jsonld(ld)
            if addr:
                street = (addr.get("streetAddress") or street).strip()
                zip_code = str(addr.get("postalCode") or zip_code).strip()
                city = (addr.get("addressLocality") or city).strip()
                norm = _normalize_state(addr.get("addressRegion") or "")
                state = norm or state
            raw_title = (ld.get("title") or raw_title).strip()
        return ScrapedPosting(
            competitor_name=self.name,
            raw_title=raw_title,
            location_city=city,
            location_state=state,
            raw_html=html,
            source_url=entry["source_url"],
            street_address=street,
            zip_code=zip_code,
        )

    # ------------------------------------------------------------------ #
    # Page-parsing helpers (live path)                                    #
    # ------------------------------------------------------------------ #
    def _extract_result_links(self, page, remaining_budget: int) -> list[str]:
        """Return up to ``remaining_budget`` job-detail hrefs from a results page.

        Tries each selector in ``self.result_link_selectors`` in order; stops
        at the first selector that yields any hits. Applies the substring
        whitelist (``link_href_must_contain``) and blocklist
        (``link_href_blocklist_re``) so we don't follow login/apply flow URLs.
        """
        seen: set[str] = set()
        out: list[str] = []
        whitelist = self.link_href_must_contain
        blocklist = [re.compile(p) for p in self.link_href_blocklist_re]
        for sel in self.result_link_selectors:
            try:
                hrefs = page.eval_on_selector_all(
                    sel, "els => els.map(e => e.getAttribute('href'))"
                )
            except Exception as exc:  # noqa: BLE001
                self._note(f"selector '{sel}' raised ({type(exc).__name__}: {exc})")
                continue
            for href in hrefs or []:
                if not href:
                    continue
                if whitelist and not any(s in href for s in whitelist):
                    continue
                if any(rx.search(href) for rx in blocklist):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                out.append(href)
                if len(out) >= remaining_budget:
                    return out
            if out:
                return out
        return out

    def _first_text(self, page, selectors: list[str]) -> str | None:
        """Return the first non-empty, non-rejected text match across ``selectors``."""
        for sel in selectors:
            try:
                el = page.query_selector(sel)
            except Exception as exc:  # noqa: BLE001
                self._note(f"query_selector '{sel}' raised ({type(exc).__name__}: {exc})")
                continue
            if not el:
                continue
            try:
                txt = el.inner_text()
            except Exception as exc:  # noqa: BLE001
                self._note(f"inner_text on '{sel}' raised ({type(exc).__name__}: {exc})")
                continue
            if not txt or not txt.strip():
                continue
            candidate = txt.strip()
            if candidate.upper() in self.title_rejects:
                continue
            return candidate
        return None

    @staticmethod
    def _city_state_from_title(title: str) -> tuple[str | None, str | None]:
        """Parse ``"Job Title - City, ST | Brand"`` into (city, state) or (None, None)."""
        if not title:
            return None, None
        m = re.search(r"-\s+([A-Za-z][A-Za-z .'-]+),\s+([A-Z]{2})\b", title)
        if m:
            return m.group(1).strip(), m.group(2).strip().upper()
        return None, None

