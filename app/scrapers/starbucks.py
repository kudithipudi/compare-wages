"""Starbucks careers scraper — thin subclass of :class:`BaseEmployerScraper`.

Starbucks publishes ``schema.org/JobPosting`` JSON-LD on every job-detail page
with a full structured address, so the default ``_extract_posting`` inherited
from the base class handles extraction. We only set the URL templates, the
result-card CSS selectors, and the fixture list.

Wage disclosure: Starbucks discloses starting hourly pay floors broadly (CA,
CO, NY, WA, IL, MD mandate it; their PR + Bean Stock messaging often surfaces
ranges elsewhere too).
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx  # noqa: F401  -- imported so test patches at app.scrapers.starbucks.httpx land

log = logging.getLogger(__name__)

from app.scrapers.base_employer import BaseEmployerScraper
from app.scrapers.registry import register

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILE = "starbucks_barista_sample.html"


@register("Starbucks")
class StarbucksScraper(BaseEmployerScraper):
    name = "Starbucks"
    robots_url = "https://careers.starbucks.com/robots.txt"
    robots_target_path = "/jobs"
    search_url_template = "https://careers.starbucks.com/jobs?keywords={kw}"
    result_link_selectors = [
        "a[href*='/job/']",
        "a[href*='/jobs/']",
        "a.job-card-link",
        "article a[href*='/job/']",
    ]
    link_href_must_contain = ("/job/", "/jobs/")
    title_rejects = frozenset({
        "STARBUCKS CAREERS",
        "SEARCH JOBS",
        "JOB SEARCH",
    })
    fixture_file = FIXTURE_FILE
    fixture_dir = FIXTURE_DIR
    fixture_postings = [
        {
            "raw_title": "Barista",
            "location_city": "Seattle",
            "location_state": "WA",
            "street_address": "1912 Pike Pl",
            "zip_code": "98101",
            "source_url": "https://careers.starbucks.com/jobs/sample-barista-seattle-0001/",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Shift Supervisor",
            "location_city": "Atlanta",
            "location_state": "GA",
            "street_address": "100 Peachtree St NW",
            "zip_code": "30303",
            "source_url": "https://careers.starbucks.com/jobs/sample-shiftsup-atlanta-0002/",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Barista",
            "location_city": "Birmingham",
            "location_state": "AL",
            "street_address": "2200 University Blvd",
            "zip_code": "35233",
            "source_url": "https://careers.starbucks.com/jobs/sample-barista-birmingham-0003/",
            "fixture": FIXTURE_FILE,
        },
    ]

    def search_url_for(self, keyword, location=None):
        kw = keyword.replace(" ", "+")
        url = f"https://careers.starbucks.com/jobs?keywords={kw}"
        if location:
            city, state = location
            # Starbucks uses location-named text searching; pass city + state as a single
            # search-location string. They also support `&country=US` but it's the default.
            url += f"&location={city.replace(' ', '+')}+{state}"
        return url
