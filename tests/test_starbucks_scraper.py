"""Tests for StarbucksScraper. Offline — no real network calls."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_registry_has_starbucks() -> None:
    import app.scrapers  # noqa: F401  -- ensures the registry side-effect import ran
    from app.scrapers.registry import get_scraper, has_scraper

    assert has_scraper("Starbucks")
    scraper = get_scraper("Starbucks")
    assert scraper is not None
    assert scraper.name == "Starbucks"
    assert scraper.rate_limit_hz >= 1.0


def test_is_available_returns_bool() -> None:
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Starbucks")

    class _Resp:
        status_code = 200
        text = "User-agent: *\nAllow: /\n"

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _Resp()
        def head(self, *a, **kw): return _Resp()

    with patch("app.scrapers.starbucks.httpx.Client", _FakeClient):
        assert isinstance(scraper.is_available(), bool)


def test_is_available_respects_robots_disallow() -> None:
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Starbucks")

    class _DisallowResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /jobs\n"
        def raise_for_status(self): pass

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): return _DisallowResp()
        def head(self, *a, **kw): return _DisallowResp()

    with patch("app.scrapers.starbucks.httpx.Client", _FakeClient):
        assert scraper.is_available() is False


def test_fixture_mode_yields_postings_with_full_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scrapers.base import ScrapedPosting
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Starbucks")

    postings = list(scraper.scrape(keywords=["Barista", "Shift Supervisor"], max_postings=3))
    assert len(postings) >= 1

    for p in postings:
        assert isinstance(p, ScrapedPosting)
        assert p.competitor_name == "Starbucks"
        assert p.raw_title
        assert p.location_city
        assert len(p.location_state) == 2 and p.location_state.isupper()
        assert p.street_address, "regression guard: fixture postings MUST have street_address"
        assert p.zip_code, "regression guard: fixture postings MUST have zip_code"
        assert "$" in p.raw_html
        assert "Pay" in p.raw_html or "pay" in p.raw_html


def test_fixture_mode_max_postings_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Starbucks")
    assert list(scraper.scrape(keywords=["Barista"], max_postings=0)) == []


def test_live_failure_falls_back_to_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)
    scraper = get_scraper("Starbucks")

    def _boom(self, keywords, max_postings, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated edge block")
        yield  # pragma: no cover

    monkeypatch.setattr(
        "app.scrapers.starbucks.StarbucksScraper._scrape_live", _boom
    )

    postings = list(scraper.scrape(keywords=["Barista"], max_postings=2))
    assert len(postings) >= 1
    assert all(p.competitor_name == "Starbucks" for p in postings)


def test_search_url_for_includes_location() -> None:
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Starbucks")
    url_no_loc = scraper.search_url_for("Barista")
    url_with_loc = scraper.search_url_for("Barista", ("Seattle", "WA"))
    assert "location=" not in url_no_loc
    assert "location=Seattle+WA" in url_with_loc
