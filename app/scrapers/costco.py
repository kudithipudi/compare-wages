"""Costco Wholesale careers scraper — thin subclass of :class:`BaseEmployerScraper`.

Costco's careers site runs on the iCIMS recruiting platform. The detail
pages ship ``schema.org/JobPosting`` JSON-LD, but with one wrinkle: the
``addressRegion`` field comes through as a full state name
(``"Connecticut"``), not a USPS two-letter code. The base class already
normalizes that via :func:`base_employer._normalize_state` so we don't
need any override here — every site-specific bit fits in class attributes
below.

The only Costco-specific subtlety in result-link extraction is rejecting
``/jobs/{id}/login`` and ``/jobs/{id}/apply`` — those are the same posting
wrapped in iCIMS' authenticated apply flow, not useful raw HTML. The base
class's ``link_href_blocklist_re`` handles that.
"""
from __future__ import annotations

from pathlib import Path

import httpx  # noqa: F401  -- imported so test patches at app.scrapers.costco.httpx land

from app.scrapers.base_employer import (
    BaseEmployerScraper,
    USER_AGENT,  # noqa: F401
    US_STATE_ABBR,  # noqa: F401  -- legacy re-export for tests
    parse_jobposting_jsonld as _parse_jobposting_jsonld,  # noqa: F401
)
from app.scrapers.registry import register

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILE = "costco_front_end_assistant_sample.html"


@register("Costco")
class CostcoScraper(BaseEmployerScraper):
    name = "Costco"
    robots_url = "https://careers.costco.com/robots.txt"
    robots_target_path = "/jobs"
    # ``/jobs?keywords=...`` is the live endpoint (verified by walking page
    # anchors). The legacy ``/job-search-results/`` path now redirects to a
    # category landing; we stick with the live endpoint exclusively.
    search_url_template = "https://careers.costco.com/jobs?keywords={kw}"

    def search_url_for(self, keyword, location=None):
        kw = keyword.replace(" ", "+")
        url = f"https://careers.costco.com/jobs?keywords={kw}"
        if location:
            city, state = location
            # iCIMS supports `&location=City+State` as a free-text geographic filter.
            url += f"&location={city.replace(' ', '+')}+{state}"
        return url
    result_link_selectors = [
        "a[href*='/jobs/']",
        "a.iCIMS_JobsTableLink",
        "div.iCIMS_JobsTable a[href*='/jobs/']",
        "a.job-search-result-card",
        "article a[href*='/jobs/']",
        "a[href*='/job/']",
    ]
    # Require a real detail-page URL: ``/jobs/{id}`` or ``/job/{slug}/{id}``.
    # The ``link_href_blocklist_re`` rejects ``/login``, ``/apply``, and
    # ``/referral`` — those are the same posting wrapped in iCIMS' auth flow.
    link_href_must_contain = ("/jobs/", "/job/")
    link_href_blocklist_re = (
        r"/jobs/[0-9A-Za-z_-]+/(login|referral|apply)\b",
    )
    title_rejects = frozenset({
        "COSTCO CAREERS",
        "JOB SEARCH",
        "SIGN IN",
    })
    detail_title_selectors = [
        "h1.iCIMS_Header",
        "[data-testid='job-title']",
        "main h1",
        "h1.title",
        "h1",
    ]
    fixture_file = FIXTURE_FILE
    fixture_dir = FIXTURE_DIR
    fixture_postings = [
        {
            "raw_title": "Front End Assistant",
            "location_city": "Issaquah",
            "location_state": "WA",
            "street_address": "1818 NW Boulevard",
            "zip_code": "98027",
            "source_url": "https://careers.costco.com/job/issaquah/front-end-assistant/sample-0001/",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Front End Assistant",
            "location_city": "San Diego",
            "location_state": "CA",
            "street_address": "2345 Fenton Pkwy",
            "zip_code": "92108",
            "source_url": "https://careers.costco.com/job/san-diego/front-end-assistant/sample-0002/",
            "fixture": FIXTURE_FILE,
        },
        {
            "raw_title": "Stocker",
            "location_city": "Plano",
            "location_state": "TX",
            "street_address": "2812 N Dallas Pkwy",
            "zip_code": "75093",
            "source_url": "https://careers.costco.com/job/plano/stocker/sample-0003/",
            "fixture": FIXTURE_FILE,
        },
    ]
