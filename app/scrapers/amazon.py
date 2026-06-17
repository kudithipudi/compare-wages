"""Amazon scraper — split across two subdomains.

Why split?
----------
Amazon publishes job postings under **two different careers properties**:

* ``hiring.amazon.com`` — the warehouse / fulfillment / delivery hiring
  portal. Detail pages ship ``schema.org/JobPosting`` JSON-LD with a full
  structured address (``streetAddress``, ``addressLocality``,
  ``addressRegion``, ``postalCode``). Warehouse roles are the ones we care
  about for the wage comparison dashboard — high street-level precision
  here is the whole point of the dashboard.

* ``www.amazon.jobs`` — the corporate careers site (white-collar / SDE /
  AWS / PM roles). Detail pages do NOT ship JSON-LD; the location is
  rendered as body text ``USA, CO, Gypsum`` or ``US, Colorado, Gypsum``.
  We keep the legacy body-text parser so corp roles still ingest with
  city + state granularity (no street, no zip — which is fine, they're
  not what feeds the wage comparison).

Public contract is unchanged: ``scrape()`` accepts a single ``keywords``
list and yields ``ScrapedPosting`` objects. Internally we partition that
list by a heuristic match against :data:`WAREHOUSE_KEYWORDS` and dispatch
each subset to the right subdomain. Telemetry tracks how many postings
each subdomain produced under
``last_run_telemetry["per_subdomain_yielded"]``.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterator

import httpx  # noqa: F401  -- imported so test patches at app.scrapers.amazon.httpx land

from app.scrapers.base import ScrapedPosting
from app.scrapers.base_employer import (
    BaseEmployerScraper,
    USER_AGENT,  # noqa: F401  -- legacy re-export
    US_STATE_ABBR,
    parse_jobposting_jsonld as _parse_jobposting_jsonld,  # noqa: F401  -- legacy re-export
)
from app.scrapers.registry import register

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILE = "amazon_warehouse_sample.html"
HIRING_FIXTURE_FILE = "amazon_hiring_sample.html"

# Lower-cased state-name lookup for amazon.jobs' "US, Colorado, Gypsum" body
# text. We keep our own lowercase index here (the base ships an uppercase one
# for the JSON-LD addressRegion full-name shape).
_STATE_NAMES_LOWER = {k.lower(): v for k, v in US_STATE_ABBR.items()}


# --------------------------------------------------------------------------- #
# Keyword classification                                                      #
# --------------------------------------------------------------------------- #
# A keyword is "warehouse-ish" if any of these substrings appear in it (case-
# insensitive). The list is intentionally conservative — we want false-
# negatives (a warehouse keyword routed to amazon.jobs by mistake) to be
# rarer than false-positives (a corp keyword routed to hiring.amazon.com,
# which would produce zero results because hiring.amazon.com doesn't list
# software roles). The set is class-frozen so subclasses can extend by
# inheriting and overriding if a new hourly role comes online.
WAREHOUSE_KEYWORDS: frozenset[str] = frozenset({
    "warehouse associate", "warehouse worker", "fulfillment associate",
    "sortation associate", "material handler", "loader", "stocker",
    "picker", "packer", "order filler", "receiver", "warehouse operator",
    "amazon delivery", "delivery associate", "amazon flex",
})


def _is_warehouse_keyword(kw: str) -> bool:
    """Return True iff ``kw`` matches any :data:`WAREHOUSE_KEYWORDS` substring
    case-insensitively.

    Substring (not exact) match so ``"Seasonal Warehouse Associate"`` and
    ``"Warehouse Associate - Night Shift"`` both classify as warehouse.
    """
    kw_lower = kw.lower()
    return any(wk in kw_lower for wk in WAREHOUSE_KEYWORDS)


@register("Amazon")
class AmazonScraper(BaseEmployerScraper):
    name = "Amazon"
    # robots.txt is checked against the corporate site since that's still
    # where ``is_available()``'s HEAD probe lands; ``hiring.amazon.com`` has
    # its own robots.txt but its rules are well-aligned with the corp one
    # (both allow ``/search``-style paths). Keeping a single ``robots_url``
    # also keeps the legacy ``is_available()`` test deterministic.
    robots_url = "https://www.amazon.jobs/robots.txt"
    robots_target_path = "/en/search"
    search_url_template = (
        "https://www.amazon.jobs/en/search?base_query={kw}&country=USA"
    )

    # Class-level expose so tests and operators can introspect.
    WAREHOUSE_KEYWORDS = WAREHOUSE_KEYWORDS

    def search_url_for(self, keyword, location=None):
        """Build a corporate-amazon.jobs search URL. Used for ``corp`` keywords
        and for the base class's HEAD probe in ``is_available()``."""
        kw = keyword.replace(" ", "+")
        url = f"https://www.amazon.jobs/en/search?base_query={kw}&country=USA"
        if location:
            city, state = location
            url += f"&loc_query={city.replace(' ', '+')}%2C+{state}"
        return url

    # ----- corporate (amazon.jobs) search-result + detail selectors -----
    result_link_selectors = [
        "a.job-link",
        "div.job-tile a.read-more",
        "a[href*='/en/jobs/']",
        "div.job a[href*='/jobs/']",
    ]
    link_href_must_contain = ("/jobs/",)
    title_rejects = frozenset({"AMAZON JOBS", "SIGN IN"})
    detail_title_selectors = [
        "h1.title",
        "[data-test='job-title']",
        "main h1",
        "h1",
    ]

    # ----- hiring.amazon.com search-result + detail selectors -----
    # The hiring portal's result cards have changed shape a few times.
    # We try the most-specific selectors first and fall back to a broad
    # ``a[href*='/job/']`` so a re-skin doesn't immediately break us.
    hiring_result_link_selectors: list[str] = [
        "a[data-test-component='StencilReactCard']",
        "a[data-test-id='job-card']",
        "a[href*='/app/jobDetail/']",
        "a[href*='/jobs/']",
        "a[href*='/job/']",
    ]
    # Whitelist substrings for hiring.amazon.com detail-page hrefs. We accept
    # both the current ``/app/jobDetail/<id>`` shape and the older ``/jobs/<id>``
    # shape so a redesign mid-flight doesn't strand us.
    hiring_link_href_must_contain: tuple[str, ...] = (
        "/app/jobDetail/", "/jobs/", "/job/",
    )

    # ----- fixtures -----
    fixture_file = FIXTURE_FILE
    fixture_dir = FIXTURE_DIR
    fixture_postings = [
        {
            "raw_title": "Warehouse Associate",
            "location_city": "Atlanta",
            "location_state": "GA",
            "street_address": "4200 N Commerce Dr",
            "zip_code": "30344",
            "source_url": "https://hiring.amazon.com/app/jobDetail/JOB-US-0000000001",
            "fixture": HIRING_FIXTURE_FILE,
        },
        {
            "raw_title": "Warehouse Associate",
            "location_city": "Dallas",
            "location_state": "TX",
            "street_address": "940 W Bethel Rd",
            "zip_code": "75019",
            "source_url": "https://hiring.amazon.com/app/jobDetail/JOB-US-0000000002",
            "fixture": HIRING_FIXTURE_FILE,
        },
        {
            "raw_title": "Sortation Associate",
            "location_city": "Phoenix",
            "location_state": "AZ",
            "street_address": "800 N 75th Ave",
            "zip_code": "85043",
            "source_url": "https://hiring.amazon.com/app/jobDetail/JOB-US-0000000003",
            "fixture": HIRING_FIXTURE_FILE,
        },
    ]

    # ------------------------------------------------------------------ #
    # Hiring-subdomain helpers                                            #
    # ------------------------------------------------------------------ #
    def _search_url_for_hiring(
        self, keyword: str, location: tuple[str, str] | None = None
    ) -> str:
        """Build a ``hiring.amazon.com`` warehouse-search URL.

        We use the ``searchQuery=`` parameter which is the shape the hiring
        portal SPA reads on load. If the portal renames it again, swap here
        — everything else flows through.

        Location filter: hiring.amazon.com supports a ``city=<City>`` +
        ``state=<ST>`` filter; we pass both. If the portal stops honoring
        them they'll be ignored (the SPA falls back to a "near you"
        geolocation prompt which Playwright headless ignores) and we'll
        still get nationwide results.
        """
        kw = keyword.replace(" ", "+")
        url = f"https://hiring.amazon.com/search/warehouse-jobs?searchQuery={kw}"
        if location:
            city, state = location
            url += f"&city={city.replace(' ', '+')}&state={state}"
        return url

    def _absolutize_hiring(self, href: str) -> str:
        """Resolve a hiring.amazon.com result-card href to an absolute URL."""
        from urllib.parse import urljoin
        return urljoin("https://hiring.amazon.com/", href)

    # ------------------------------------------------------------------ #
    # Public override: split scrape across the two subdomains            #
    # ------------------------------------------------------------------ #
    def _scrape_live(
        self,
        keywords: list[str],
        max_postings: int,
        *,
        locations: list[tuple[str, str]] | None = None,
    ) -> Iterator[ScrapedPosting]:
        """Override that partitions ``keywords`` by warehouse-vs-corp and
        dispatches each subset to the right subdomain.

        Warehouse keywords hit ``hiring.amazon.com`` (full JSON-LD addresses).
        Everything else goes through the base-class ``_scrape_live`` against
        ``amazon.jobs`` (city+state only — preserving legacy behavior).
        """
        self.last_run_telemetry.setdefault(
            "per_subdomain_yielded",
            {"hiring.amazon.com": 0, "amazon.jobs": 0},
        )

        warehouse_kws = [k for k in keywords if _is_warehouse_keyword(k)]
        corp_kws = [k for k in keywords if not _is_warehouse_keyword(k)]

        yielded = 0
        if warehouse_kws and yielded < max_postings:
            for posting in self._scrape_hiring(
                warehouse_kws, max_postings - yielded, locations=locations
            ):
                self.last_run_telemetry["per_subdomain_yielded"]["hiring.amazon.com"] += 1
                yielded += 1
                yield posting
                if yielded >= max_postings:
                    return
        if corp_kws and yielded < max_postings:
            # Inherit base behavior for amazon.jobs (the legacy code path).
            for posting in super()._scrape_live(
                corp_kws, max_postings - yielded, locations=locations
            ):
                self.last_run_telemetry["per_subdomain_yielded"]["amazon.jobs"] += 1
                yielded += 1
                yield posting
                if yielded >= max_postings:
                    return

    # ------------------------------------------------------------------ #
    # Hiring-subdomain scrape loop                                        #
    # ------------------------------------------------------------------ #
    def _scrape_hiring(
        self,
        keywords: list[str],
        max_postings: int,
        *,
        locations: list[tuple[str, str]] | None = None,
    ) -> Iterator[ScrapedPosting]:
        """Live-scrape the ``hiring.amazon.com`` warehouse portal.

        Mostly mirrors :meth:`BaseEmployerScraper._scrape_live` but with:

        * ``_search_url_for_hiring`` instead of ``search_url_for`` (different
          host + query-param name).
        * ``hiring_result_link_selectors`` + ``hiring_link_href_must_contain``
          instead of the corp selectors.
        * ``_absolutize_hiring`` for relative-href resolution.
        * The default ``_extract_posting`` does the heavy lifting because
          hiring.amazon.com pages ship ``schema.org/JobPosting`` JSON-LD.

        Resilience features (retry, per-keyword failure budget, telemetry
        notes) are reused via ``_load_detail_with_retry`` and
        ``_walk_details_concurrent``.
        """
        if max_postings <= 0:
            return
        # Lazy import — keep the import-time cost off the fixture path.
        from playwright.sync_api import sync_playwright  # type: ignore
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        except Exception:  # noqa: BLE001
            PlaywrightTimeoutError = Exception  # type: ignore[assignment]

        pause = 1.0 / max(self.rate_limit_hz, 0.001)

        # Plan the (location, keyword) work. None location = "nationwide".
        loc_list: list[tuple[str, str] | None] = list(locations) if locations else [None]
        # Don't clobber the corp path's locations_tried list — append.
        for entry in loc_list:
            if entry is None:
                continue
            c, st = entry
            if c:
                token = f"{c},{st}"
                if token not in self.last_run_telemetry["locations_tried"]:
                    self.last_run_telemetry["locations_tried"].append(token)
        planned: list[tuple[tuple[str, str] | None, str]] = [
            (loc, kw) for loc in loc_list for kw in keywords
        ]
        cap = self.max_location_keyword_pairs
        if len(planned) > cap:
            import random as _r
            _r.shuffle(planned)
            planned = planned[:cap]
            self._note(
                f"hiring: planned {len(loc_list) * len(keywords)} (loc×kw) pairs, sampling {cap}"
            )
        self.last_run_telemetry["total_pairs_planned"] += len(planned)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                )
                page = context.new_page()

                # 1. Walk search-result pages, collecting deduped detail hrefs.
                seen: set[str] = set()
                all_links: list[tuple[str, str]] = []  # (keyword, href)
                for loc, kw in planned:
                    if kw not in self.last_run_telemetry["keywords_tried"]:
                        self.last_run_telemetry["keywords_tried"].append(kw)
                    self.last_run_telemetry["per_keyword_yielded"].setdefault(kw, 0)
                    self.last_run_telemetry["per_keyword_errors"].setdefault(kw, 0)
                    loc_key = f"{loc[0]},{loc[1]}" if loc else "(global)"
                    self.last_run_telemetry["per_location_yielded"].setdefault(loc_key, 0)
                    self.last_run_telemetry["per_location_errors"].setdefault(loc_key, 0)

                    if len(all_links) >= max_postings:
                        break
                    search_url = self._search_url_for_hiring(kw, loc)
                    try:
                        page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                    except Exception as exc:  # noqa: BLE001
                        self._note(
                            f"hiring search nav failed for '{kw}' @ {loc_key} "
                            f"({type(exc).__name__}: {exc})"
                        )
                        self.last_run_telemetry["per_location_errors"][loc_key] += 1
                        continue
                    time.sleep(pause)
                    new_links = self._extract_hiring_result_links(
                        page, max_postings - len(all_links)
                    )
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

                self.last_run_telemetry["links_seen"] += len(all_links)

                if not all_links:
                    raise RuntimeError(
                        "Amazon hiring.amazon.com search returned no result links — "
                        "likely an SPA-rendered list (JS not yet rehydrated) or selector drift."
                    )

                # 2. Walk detail pages. We rebind result-link-resolution to the
                #    hiring host by temporarily swapping ``_absolutize`` for the
                #    duration of the walk.
                original_absolutize = self._absolutize
                self._absolutize = self._absolutize_hiring  # type: ignore[method-assign]
                try:
                    if self.detail_concurrency <= 1:
                        yield from self._walk_details_sequential(
                            page, all_links[:max_postings], pause, PlaywrightTimeoutError
                        )
                    else:
                        yield from self._walk_details_concurrent(
                            context, all_links[:max_postings], pause, PlaywrightTimeoutError
                        )
                finally:
                    self._absolutize = original_absolutize  # type: ignore[method-assign]
            finally:
                try:
                    browser.close()
                except Exception as exc:  # noqa: BLE001
                    self._note(f"browser close failed ({type(exc).__name__}: {exc})")

    def _extract_hiring_result_links(self, page, remaining_budget: int) -> list[str]:
        """Like ``_extract_result_links`` but reads the hiring-subdomain
        selectors. We don't permanently swap class attrs because the corp path
        runs in the same scrape and needs its own selectors intact.
        """
        seen: set[str] = set()
        out: list[str] = []
        whitelist = self.hiring_link_href_must_contain
        for sel in self.hiring_result_link_selectors:
            try:
                hrefs = page.eval_on_selector_all(
                    sel, "els => els.map(e => e.getAttribute('href'))"
                )
            except Exception as exc:  # noqa: BLE001
                self._note(f"hiring selector '{sel}' raised ({type(exc).__name__}: {exc})")
                continue
            for href in hrefs or []:
                if not href:
                    continue
                if whitelist and not any(s in href for s in whitelist):
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

    # ------------------------------------------------------------------ #
    # Corporate-amazon.jobs absolutize + extract (unchanged from legacy)  #
    # ------------------------------------------------------------------ #
    def _absolutize(self, href: str) -> str:
        # Detail links can be relative to either the robots.txt host or the
        # ``www.amazon.jobs`` root; ``urljoin`` against the latter is correct
        # in either case.
        from urllib.parse import urljoin
        return urljoin("https://www.amazon.jobs/", href)

    def _extract_posting(self, page, html, source_url) -> ScrapedPosting | None:
        """Site-specific extractor.

        Routes by URL: if we're on ``hiring.amazon.com`` we use the base
        class's default JSON-LD-first extractor (full address). Otherwise
        we're on corporate ``amazon.jobs`` and fall back to the body-text
        regex (``USA, CO, Gypsum``) -> page-title parse chain.
        """
        if "hiring.amazon.com" in (source_url or ""):
            return super()._extract_posting(page, html, source_url)

        ld = _parse_jobposting_jsonld(html)
        title = ""
        city = state = street = zip_code = ""
        if ld:
            title = (ld.get("title") or "").strip()
            addr = self._address_from_jsonld(ld)
            city = (addr.get("addressLocality") or "").strip()
            state = (addr.get("addressRegion") or "").strip().upper()
            street = (addr.get("streetAddress") or "").strip()
            zip_code = str(addr.get("postalCode") or "").strip()

        if not title:
            title = self._first_text(page, self.detail_title_selectors) or "Unknown Role"
            # Strip the "- Job ID: ..." suffix the corporate site adds to <title>.
            title = re.sub(r"\s*-\s*Job ID:.*$", "", title).strip()

        if not city or not state:
            # amazon.jobs corporate renders body text "USA, CO, Gypsum".
            body_text = ""
            try:
                body_text = page.evaluate("document.body.innerText") or ""
            except Exception as exc:  # noqa: BLE001
                self._note(f"body innerText failed ({type(exc).__name__}: {exc})")
            ct_city, ct_state = self._city_state_from_body_text(body_text)
            if not city:
                city = ct_city or ""
            if not state:
                state = (ct_state or "").upper()

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
    def _city_state_from_body_text(body_text: str) -> tuple[str | None, str | None]:
        """Parse ``USA, CO, Gypsum`` or ``US, Colorado, Gypsum`` into (city, ST).

        Handles both the two-letter abbrev and full state-name forms that
        amazon.jobs corporate has shipped over the years.
        """
        if not body_text:
            return None, None
        m = re.search(
            r"\bUSA?,\s*([A-Z]{2}),\s*([A-Za-z][A-Za-z .'-]+?)(?:\s*[-\n,]|$)",
            body_text,
        )
        if m:
            return m.group(2).strip(), m.group(1).strip().upper()
        m = re.search(
            r"\bUSA?,\s*([A-Za-z][A-Za-z .'-]+?),\s*([A-Za-z][A-Za-z .'-]+?)(?:\s*[-\n,]|$)",
            body_text,
        )
        if m:
            code = _STATE_NAMES_LOWER.get(m.group(1).strip().lower())
            if code:
                return m.group(2).strip(), code
        return None, None
