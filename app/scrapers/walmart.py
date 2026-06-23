"""Walmart careers scraper — thin subclass of :class:`BaseEmployerScraper`.

Walmart detail pages ship ``schema.org/JobPosting`` JSON-LD that includes a
full structured address, so the default ``_extract_posting`` from the base
class is everything we need.

Walmart is the hardest of the four scrapers to keep running live:
``careers.walmart.com`` is fronted by Akamai + PerimeterX behavioral
defenses, and datacenter IPs (the kind our hosting provider gives us)
routinely get a 403 before any real navigation happens. The
live-with-fallback orchestrator inherited from ``BaseEmployerScraper``
quietly serves fixture HTML when that happens.

Walmart has shipped two URL shapes for the public search endpoint over the
years — ``/results?q=...`` and ``/search?q=...``. The base class only takes
a single ``search_url_template`` so we override ``_scrape_live`` *just
enough* to try the second template if the first yields zero links; all the
retry, telemetry, and fallback logic still comes from the base.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import httpx  # noqa: F401  -- imported so test patches at app.scrapers.walmart.httpx land

from app.scrapers.base import ScrapedPosting
from app.scrapers.base_employer import BaseEmployerScraper
from app.scrapers.registry import register

log = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILE = "walmart_cashier_sample.html"

# Walmart has shipped both /results and /search over the years; we try them
# in order and accept the first that returns at least one card.
_SEARCH_URL_TEMPLATES = [
    "https://careers.walmart.com/results?q={kw}",
    "https://careers.walmart.com/search?q={kw}",
]

# Substrings (case-insensitive) that uniquely identify an Akamai / PerimeterX /
# Imperva / DataDome edge-challenge page. We use substring matching because the
# vendors rotate the surrounding markup but keep these recognizable user-facing
# strings (or script identifiers) stable across deploys. If any one of these
# fires after a search-page navigation we raise WalmartBlocked so the
# orchestrator's fixture-fallback engages AND the operator sees a precise
# "edge challenge" reason in telemetry instead of "search returned no results".
_CHALLENGE_PAGE_MARKERS = (
    "Pardon Our Interruption",
    "_Incapsula_Resource",
    "px-captcha",
    "DataDome",
    "Access Denied",
    "Sorry, we just need to make sure you're not a robot",
    "captcha-delivery.com",
)


class WalmartBlocked(RuntimeError):
    """Raised when an Akamai/PerimeterX/DataDome interstitial is detected.

    The orchestrator (``BaseEmployerScraper.scrape``) catches ``Exception``
    from ``_scrape_live`` and falls back to fixtures, so subclassing
    ``RuntimeError`` keeps that contract intact while letting the operator
    see ``why: WalmartBlocked: …`` in ``last_run_telemetry["reasons"]``.
    """


@register("Walmart")
class WalmartScraper(BaseEmployerScraper):
    name = "Walmart"
    robots_url = "https://careers.walmart.com/robots.txt"
    robots_target_path = "/results"
    # Primary template — the override in ``_scrape_live`` walks the rest of
    # the fallback list if this one yields nothing.
    search_url_template = _SEARCH_URL_TEMPLATES[0]
    result_link_selectors = [
        "a[data-automation-id='job-card-link']",
        "a.job-card-link",
        "a[href*='/job/']",
        "article a[href*='/job/']",
    ]
    link_href_must_contain = ("/job/", "/jobs/")
    title_rejects = frozenset({
        "JOB SEARCH",
        "SAVED JOBS",
        "MY APPLICATIONS",
        "WALMART CAREERS",
    })
    detail_title_selectors = [
        "main h1",
        "[data-automation-id='job-title']",
        "[data-testid='job-title']",
        ".job-title",
        "h1",
    ]
    fixture_file = FIXTURE_FILE
    fixture_dir = FIXTURE_DIR
    fixture_postings = [
        {
            "raw_title": "Cashier / Front End Associate",
            "location_city": "Bentonville",
            "location_state": "AR",
            "street_address": "406 S Walton Blvd",
            "zip_code": "72712",
            "source_url": "https://careers.walmart.com/us/jobs/WD-bentonville-cashier-0001",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Cashier / Front End Associate",
            "location_city": "Sacramento",
            "location_state": "CA",
            "street_address": "8915 Gerber Rd",
            "zip_code": "95829",
            "source_url": "https://careers.walmart.com/us/jobs/WD-sacramento-cashier-0002",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Stocker / Backroom Associate",
            "location_city": "Denver",
            "location_state": "CO",
            "street_address": "7800 E Smith Rd",
            "zip_code": "80207",
            "source_url": "https://careers.walmart.com/us/jobs/WD-denver-stocker-0003",
            "fixture": FIXTURE_FILE,
        },
    ]

    # ------------------------------------------------------------------ #
    # Live path override — Walmart needs the URL-template fallback        #
    # ------------------------------------------------------------------ #
    def _scrape_live(
        self,
        keywords: list[str],
        max_postings: int,
        *,
        locations: list[tuple[str, str]] | None = None,
    ) -> Iterator[ScrapedPosting]:
        """Walmart-specific live path.

        Identical to the base class's implementation EXCEPT that the search-
        URL step walks ``_SEARCH_URL_TEMPLATES`` in order and accepts the
        first template that yields links. Walmart has alternated between
        ``/results`` and ``/search`` over the years and the inactive one
        typically 200s with an empty grid.

        ``locations`` is accepted for signature parity with the base class but
        unused — Walmart's live path is reliably Akamai-blocked from datacenter
        IPs, so a location filter wouldn't change the outcome (fixture fallback
        handles the demoable path). When live runs become reachable in the
        future, this is where ``search_url_for`` would be wired in.
        """
        from playwright.sync_api import sync_playwright  # type: ignore
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        except Exception:  # noqa: BLE001
            PlaywrightTimeoutError = Exception  # type: ignore[assignment]

        pause = 1.0 / max(self.rate_limit_hz, 0.001)

        # Residential proxy config from env. All three vars are optional; when
        # the URL is unset we behave exactly like the pre-proxy code (no
        # ``proxy=`` kwarg passed to ``new_context``). When set, this is what
        # makes a live Walmart scrape actually work from a datacenter box —
        # Akamai/PerimeterX rate-limit known DC ASNs hard.
        proxy_url = os.environ.get("WALMART_PROXY_URL", "").strip()
        proxy_username = os.environ.get("WALMART_PROXY_USERNAME", "").strip()
        proxy_password = os.environ.get("WALMART_PROXY_PASSWORD", "").strip()
        context_kwargs: dict = dict(
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        if proxy_url:
            proxy_cfg: dict = {"server": proxy_url}
            if proxy_username:
                proxy_cfg["username"] = proxy_username
            if proxy_password:
                proxy_cfg["password"] = proxy_password
            context_kwargs["proxy"] = proxy_cfg
            # Mask credentials in telemetry — operators read this from
            # /admin/scrape-runs and we don't want auth leaking onto the page.
            try:
                host = urlparse(proxy_url).hostname or proxy_url
            except Exception:  # noqa: BLE001
                host = proxy_url
            self.last_run_telemetry["reasons"].append(
                f"proxy_configured=*****@{host}"
            )

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(**context_kwargs)
                page = context.new_page()

                # Apply playwright-stealth fingerprint patches BEFORE the first
                # navigation. Stealth patches navigator.webdriver, chrome.runtime,
                # plugins, language, WebGL vendor/renderer etc. — these are the
                # standard fingerprints Akamai/PerimeterX score against. Lazy
                # import so test envs without the dep installed still import
                # this module fine.
                try:
                    from playwright_stealth import stealth_sync  # type: ignore
                    stealth_sync(page)
                    self.last_run_telemetry["reasons"].append("stealth_applied")
                except Exception as exc:  # noqa: BLE001
                    self._note(
                        f"playwright-stealth not applied ({type(exc).__name__}: {exc})"
                    )

                seen: set[str] = set()
                all_links: list[tuple[str, str]] = []
                for kw in keywords:
                    self.last_run_telemetry["keywords_tried"].append(kw)
                    self.last_run_telemetry["per_keyword_yielded"].setdefault(kw, 0)
                    self.last_run_telemetry["per_keyword_errors"].setdefault(kw, 0)
                    if len(all_links) >= max_postings:
                        break
                    links = self._search_for_links(
                        page, kw, max_postings - len(all_links), pause
                    )
                    for href in links:
                        if href in seen:
                            continue
                        seen.add(href)
                        all_links.append((kw, href))
                        if len(all_links) >= max_postings:
                            break

                self.last_run_telemetry["links_seen"] = len(all_links)
                if not all_links:
                    raise RuntimeError(
                        "Walmart search returned no result links — likely Akamai/PerimeterX "
                        "block or selector drift (no card matched any candidate selector)."
                    )

                consecutive_failures: dict[str, int] = {kw: 0 for kw, _ in all_links}
                skipped_keywords: set[str] = set()
                for kw, href in all_links[:max_postings]:
                    if kw in skipped_keywords:
                        continue
                    detail_url = self._absolutize(href)
                    posting = self._load_detail_with_retry(
                        page, detail_url, PlaywrightTimeoutError, pause
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
            finally:
                try:
                    browser.close()
                except Exception as exc:  # noqa: BLE001
                    self._note(f"browser close failed ({type(exc).__name__}: {exc})")

    def _search_for_links(self, page, keyword: str, want: int, pause: float) -> list[str]:
        """Try each URL template in order; return the first non-empty result list.

        After each successful navigation we sniff the rendered HTML for known
        Akamai / PerimeterX / DataDome challenge-page markers. If any match,
        we raise :class:`WalmartBlocked` so the orchestrator's fixture-
        fallback engages with a precise reason instead of the vague
        "search returned no result links". This is the operator-visible win:
        telemetry now distinguishes "edge defense fired" from "selector drift".
        """
        kw_enc = keyword.replace(" ", "+")
        for tpl in _SEARCH_URL_TEMPLATES:
            search_url = tpl.format(kw=kw_enc)
            log.info("Navigating to %s", search_url)
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                self._note(
                    f"search nav failed for '{keyword}' at {search_url} "
                    f"({type(exc).__name__}: {exc})"
                )
                continue
            time.sleep(pause)
            # Challenge-page sniff. Substring match is case-insensitive so a
            # vendor capitalization change doesn't silently disable detection.
            try:
                content_lower = (page.content() or "").lower()
            except Exception as exc:  # noqa: BLE001
                self._note(
                    f"page.content() failed at {search_url} "
                    f"({type(exc).__name__}: {exc})"
                )
                content_lower = ""
            for marker in _CHALLENGE_PAGE_MARKERS:
                if marker.lower() in content_lower:
                    raise WalmartBlocked(
                        f"Walmart edge challenge detected ({marker!r}) at {search_url}"
                    )
            links = self._extract_result_links(page, want)
            if links:
                return links
        return []
