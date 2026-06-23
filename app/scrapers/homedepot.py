"""Home Depot careers scraper — thin subclass of :class:`BaseEmployerScraper`.

Home Depot publishes ``schema.org/JobPosting`` JSON-LD on every job-detail
page (including a full structured address) so the default
``_extract_posting`` from the base class is everything we need. The only
site-specific bits we configure here are the URL templates, the result-card
CSS selectors, the well-known "false-positive h1" rejects, and the fixture
list.

Akamai still gates the live path, so the live-with-fallback orchestrator
inherited from the base will quietly serve the fixture HTML when the real
site is mad at us. See ``base_employer.py`` for the resilience features
(robots.txt caching, retry-on-Timeout, per-keyword failure budget, etc.).
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx  # noqa: F401  -- imported so test patches at app.scrapers.homedepot.httpx land

log = logging.getLogger(__name__)

from app.scrapers.base_employer import BaseEmployerScraper
from app.scrapers.registry import register

# Re-export fixture directory + filename so existing tests + tooling that
# imported them at module scope keep working.
FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILE = "homedepot_lot_associate_sample.html"


@register("Home Depot")
class HomeDepotScraper(BaseEmployerScraper):
    name = "Home Depot"
    robots_url = "https://careers.homedepot.com/robots.txt"
    robots_target_path = "/job-search-results/"
    search_url_template = (
        "https://careers.homedepot.com/job-search-results/?keyword={kw}&country=US"
    )

    def search_url_for(self, keyword, location=None):
        kw = keyword.replace(" ", "+")
        url = f"https://careers.homedepot.com/job-search-results/?keyword={kw}&country=US"
        if location:
            city, state = location
            url += f"&city={city.replace(' ', '+')}&state={state}"
        return url
    # Try selectors in order of specificity. If none match we'll raise from
    # ``_scrape_live`` and the orchestrator will fall through to fixtures.
    result_link_selectors = [
        "a.job-search-result-card",
        "a[href*='/job/']",
        "a.job-link",
        "article a[href*='/job/']",
    ]
    link_href_must_contain = ("/job/", "/job-detail")
    # ``main h1`` is the actual job title; the page also has a ``header h1``
    # that reads "CHECK APPLICATION STATUS" which must NOT win the title
    # selection. Reject it (and a couple of other well-known false positives)
    # by exact uppercase match.
    title_rejects = frozenset({
        "CHECK APPLICATION STATUS",
        "WORK LOCATION",
        "DISABILITY ASSISTANCE",
    })
    fixture_file = FIXTURE_FILE
    fixture_dir = FIXTURE_DIR
    # Each fixture entry yields one ScrapedPosting in fallback mode. Fields
    # below match what we would have parsed from the real search-results card
    # (LLM does the wage extraction from raw_html downstream).
    fixture_postings = [
        {
            "raw_title": "Lot Associate",
            "location_city": "Atlanta",
            "location_state": "GA",
            "street_address": "",
            "zip_code": "",
            "source_url": "https://careers.homedepot.com/job/atlanta/lot-associate/sample-0001/",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Lot Associate",
            "location_city": "Dallas",
            "location_state": "TX",
            "street_address": "",
            "zip_code": "",
            "source_url": "https://careers.homedepot.com/job/dallas/lot-associate/sample-0002/",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Freight Associate",
            "location_city": "Phoenix",
            "location_state": "AZ",
            "street_address": "",
            "zip_code": "",
            "source_url": "https://careers.homedepot.com/job/phoenix/freight-associate/sample-0003/",
            "fixture": FIXTURE_FILE,
        },
    ]
