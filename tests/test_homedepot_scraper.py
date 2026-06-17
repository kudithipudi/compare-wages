"""Tests for HomeDepotScraper that DO NOT hit the real network.

We exercise:
  - registry wiring (the scraper registers itself on import)
  - the type contract of is_available() (bool — value depends on network)
  - the FIXTURE_MODE fallback path (the live Playwright call is never made)

Live Playwright behavior is intentionally out of scope here. Akamai's
behavioral signals make a deterministic CI assertion against the real site
impossible. The fixture path is what proves the rest of the pipeline can
keep moving when the live run is blocked.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_registry_has_home_depot() -> None:
    # Importing the package side-effect-registers the scraper via @register.
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import get_scraper, has_scraper

    assert has_scraper("Home Depot"), "Home Depot scraper should be registered"
    scraper = get_scraper("Home Depot")
    assert scraper is not None
    assert scraper.name == "Home Depot"
    # Rate limit floor of 1 req/sec per the Scraper contract.
    assert scraper.rate_limit_hz >= 1.0


def test_is_available_returns_bool() -> None:
    """is_available() must return a bool. We don't assert True/False because
    that depends on whether the test environment can reach the real internet.
    We patch httpx so the test stays deterministic + offline."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Home Depot")
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

    with patch("app.scrapers.homedepot.httpx.Client", _FakeClient):
        result = scraper.is_available()

    assert isinstance(result, bool)


def test_is_available_respects_robots_disallow() -> None:
    """If robots.txt explicitly disallows our target path, return False even
    if the rest of the world looks healthy. Fixture mode must not paper this
    over (per the spec)."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Home Depot")
    assert scraper is not None

    class _DisallowResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /job-search-results/\n"

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

    with patch("app.scrapers.homedepot.httpx.Client", _FakeClient):
        assert scraper.is_available() is False


def test_fixture_mode_yields_postings(monkeypatch: pytest.MonkeyPatch) -> None:
    """With FIXTURE_MODE on, scrape() yields postings entirely from disk and
    never touches Playwright/network. We assert the shape the downstream
    pipeline depends on."""
    from app.scrapers.base import ScrapedPosting
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Home Depot")
    assert scraper is not None

    postings = list(scraper.scrape(keywords=["Lot Associate", "Freight Associate"], max_postings=3))
    assert len(postings) >= 1, "fixture mode should yield at least one posting"
    assert len(postings) <= 3, "max_postings cap should be honored"

    for p in postings:
        assert isinstance(p, ScrapedPosting)
        assert p.competitor_name == "Home Depot"
        assert p.raw_title, "raw_title must be non-empty"
        assert p.location_city, "location_city must be non-empty"
        assert len(p.location_state) == 2 and p.location_state.isupper()
        assert p.raw_html, "raw_html must be non-empty"
        assert p.source_url.startswith("https://"), "source_url must look real"
        # The LLM extractor scans for currency and a 'Pay' label — both must
        # appear in the fixture so the downstream pipeline has something to
        # parse.
        assert ("$" in p.raw_html) and ("Pay" in p.raw_html)


def test_fixture_mode_max_postings_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_postings=0 should yield nothing — guards against runaway loops."""
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Home Depot")
    assert scraper is not None
    assert list(scraper.scrape(keywords=["Lot Associate"], max_postings=0)) == []


def test_live_failure_falls_back_to_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the live path raises (Akamai block, no chromium, etc.) and FIXTURE_MODE
    is not set, scrape() should still yield fixture postings so the pipeline
    keeps moving."""
    from app.scrapers.registry import get_scraper

    # Make sure FIXTURE_MODE is OFF so we exercise the exception-fallback path.
    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    scraper = get_scraper("Home Depot")
    assert scraper is not None

    def _boom(self, keywords, max_postings):  # noqa: ARG001
        raise RuntimeError("simulated Akamai 403")
        yield  # pragma: no cover  - make this a generator

    monkeypatch.setattr(
        "app.scrapers.homedepot.HomeDepotScraper._scrape_live", _boom
    )

    postings = list(scraper.scrape(keywords=["Lot Associate"], max_postings=2))
    assert len(postings) >= 1
    assert all(p.competitor_name == "Home Depot" for p in postings)


def test_fixture_file_present_and_realistic() -> None:
    """The fixture file must exist and contain the markers our extractor (and
    a future LLM prompt) keys off of."""
    from app.scrapers.homedepot import FIXTURE_DIR, FIXTURE_FILE

    path = FIXTURE_DIR / FIXTURE_FILE
    assert path.exists(), f"fixture HTML missing at {path}"
    html = path.read_text(encoding="utf-8")
    assert "<h1" in html.lower()
    assert "Position Purpose" in html
    assert "Pay" in html
    assert "$" in html
    assert "per hour" in html.lower()
