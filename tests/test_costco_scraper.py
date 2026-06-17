"""Tests for CostcoScraper that DO NOT hit the real network.

We exercise:
  - registry wiring (the scraper registers itself on import)
  - the type contract of is_available() (bool — value depends on network)
  - the robots.txt disallow path (fixture mode must not paper over it)
  - the FIXTURE_MODE fallback path (the live Playwright call is never made)
  - the regression guard for the "every CompetitorLocation at lat=0,lng=0"
    bug — yielded postings must carry a non-empty street_address + zip_code
    so the service layer can geocode against a real address.

Live Playwright behavior is intentionally out of scope here. iCIMS bot
detection makes deterministic CI assertions against the real site
impossible. The fixture path is what proves the rest of the pipeline can
keep moving when the live run is blocked.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_registry_has_costco() -> None:
    # Importing the package side-effect-registers the scraper via @register.
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import get_scraper, has_scraper

    assert has_scraper("Costco"), "Costco scraper should be registered"
    scraper = get_scraper("Costco")
    assert scraper is not None
    assert scraper.name == "Costco"
    # Rate limit floor of 1 req/sec per the Scraper contract.
    assert scraper.rate_limit_hz >= 1.0


def test_is_available_returns_bool() -> None:
    """is_available() must return a bool. We patch httpx so the test stays
    deterministic + offline; the real value depends on whether the test
    environment can reach the real internet."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Costco")
    assert scraper is not None

    class _FakeResp:
        status_code = 200
        text = "User-agent: *\nAllow: /\n"

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _FakeResp()

        def head(self, url):
            return _FakeResp()

    with patch("app.scrapers.costco.httpx.Client", _FakeClient):
        result = scraper.is_available()

    assert isinstance(result, bool)


def test_is_available_respects_robots_disallow() -> None:
    """If robots.txt explicitly disallows our target path, return False even
    if the rest of the world looks healthy. Fixture mode must not paper this
    over (per the spec)."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Costco")
    assert scraper is not None

    class _DisallowResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /jobs\n"

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _DisallowResp()

        def head(self, url):
            return _DisallowResp()

    with patch("app.scrapers.costco.httpx.Client", _FakeClient):
        assert scraper.is_available() is False


def test_fixture_mode_yields_postings_with_full_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With FIXTURE_MODE on, scrape() yields postings entirely from disk and
    never touches Playwright/network.

    Critically, each yielded posting must carry a non-empty street_address +
    zip_code — this is the regression guard for the location bug that hit
    Home Depot, where every CompetitorLocation row landed at lat=0,lng=0
    because the scraper never populated structured-address fields."""
    from app.scrapers.base import ScrapedPosting
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Costco")
    assert scraper is not None

    postings = list(
        scraper.scrape(
            keywords=["Front End Assistant", "Stocker"], max_postings=3
        )
    )
    assert len(postings) >= 1, "fixture mode should yield at least one posting"
    assert len(postings) <= 3, "max_postings cap should be honored"

    for p in postings:
        assert isinstance(p, ScrapedPosting)
        assert p.competitor_name == "Costco"
        assert p.raw_title, "raw_title must be non-empty"
        assert p.location_city, "location_city must be non-empty"
        assert len(p.location_state) == 2 and p.location_state.isupper()
        assert p.raw_html, "raw_html must be non-empty"
        assert p.source_url.startswith("https://"), "source_url must look real"
        # Regression guard: the JSON-LD parser must populate the structured
        # address so the geocoder can hit a precise lat/lng.
        assert p.street_address, "street_address must be populated from JSON-LD"
        assert p.zip_code, "zip_code must be populated from JSON-LD"
        # The downstream LLM extractor scans for currency and a 'Compensation'
        # or 'Pay' label — both must appear so wage extraction has signal.
        assert "$" in p.raw_html
        assert ("Compensation" in p.raw_html) or ("Pay" in p.raw_html)


def test_fixture_mode_max_postings_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_postings=0 should yield nothing — guards against runaway loops."""
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Costco")
    assert scraper is not None
    assert (
        list(scraper.scrape(keywords=["Front End Assistant"], max_postings=0))
        == []
    )


def test_live_failure_falls_back_to_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the live path raises (iCIMS bot challenge, no chromium, etc.) and
    FIXTURE_MODE is not set, scrape() should still yield fixture postings so
    the pipeline keeps moving."""
    from app.scrapers.registry import get_scraper

    # Make sure FIXTURE_MODE is OFF so we exercise the exception-fallback path.
    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    scraper = get_scraper("Costco")
    assert scraper is not None

    def _boom(self, keywords, max_postings):  # noqa: ARG001
        raise RuntimeError("simulated iCIMS 403")
        yield  # pragma: no cover  - make this a generator

    monkeypatch.setattr(
        "app.scrapers.costco.CostcoScraper._scrape_live", _boom
    )

    postings = list(
        scraper.scrape(keywords=["Front End Assistant"], max_postings=2)
    )
    assert len(postings) >= 1
    assert all(p.competitor_name == "Costco" for p in postings)


def test_fixture_file_present_and_realistic() -> None:
    """The fixture file must exist and contain the markers our extractor (and
    a future LLM prompt) keys off of."""
    from app.scrapers.costco import FIXTURE_DIR, FIXTURE_FILE

    path = FIXTURE_DIR / FIXTURE_FILE
    assert path.exists(), f"fixture HTML missing at {path}"
    html = path.read_text(encoding="utf-8")
    assert "<h1" in html.lower()
    assert "application/ld+json" in html
    assert "JobPosting" in html
    assert "Compensation" in html or "Pay" in html
    assert "$" in html
    assert "hr" in html.lower()  # /hr in the wage line
