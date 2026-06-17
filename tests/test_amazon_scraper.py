"""Tests for AmazonScraper that DO NOT hit the real network.

We exercise:
  - registry wiring (the scraper registers itself on import)
  - the type contract of is_available() (bool — value depends on network)
  - robots.txt disallow short-circuits is_available()
  - the FIXTURE_MODE fallback path yields postings with full structured address
    (regression guard for the lat=0/lng=0 bug that hit Home Depot)
  - max_postings=0 yields nothing
  - a raising live path falls back to fixtures

Live Playwright behavior is intentionally out of scope here — Amazon's bot
defenses make a deterministic CI assertion against the real site impossible.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_registry_has_amazon() -> None:
    # Importing the package side-effect-registers the scraper via @register.
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import get_scraper, has_scraper

    assert has_scraper("Amazon"), "Amazon scraper should be registered"
    scraper = get_scraper("Amazon")
    assert scraper is not None
    assert scraper.name == "Amazon"
    # Rate limit floor of 1 req/sec per the Scraper contract.
    assert scraper.rate_limit_hz >= 1.0


def test_is_available_returns_bool() -> None:
    """is_available() must return a bool. We don't assert True/False because
    that depends on whether the test environment can reach the real internet.
    We patch httpx so the test stays deterministic + offline."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Amazon")
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

    with patch("app.scrapers.amazon.httpx.Client", _FakeClient):
        result = scraper.is_available()

    assert isinstance(result, bool)


def test_is_available_respects_robots_disallow() -> None:
    """If robots.txt explicitly disallows our target path, return False even
    if the rest of the world looks healthy. Fixture mode must not paper this
    over (per the spec)."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Amazon")
    assert scraper is not None

    class _DisallowResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /en/search\n"

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

    with patch("app.scrapers.amazon.httpx.Client", _FakeClient):
        assert scraper.is_available() is False


def test_fixture_mode_yields_postings_with_full_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With FIXTURE_MODE on, scrape() yields postings entirely from disk and
    never touches Playwright/network. We assert the shape the downstream
    pipeline depends on AND that street_address + zip_code are populated —
    this is the regression guard for the geocode-to-(0,0) bug that hit Home
    Depot before JSON-LD parsing landed."""
    from app.scrapers.base import ScrapedPosting
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Amazon")
    assert scraper is not None

    postings = list(
        scraper.scrape(keywords=["Warehouse Associate", "Sortation Associate"], max_postings=3)
    )
    assert len(postings) >= 1, "fixture mode should yield at least one posting"
    assert len(postings) <= 3, "max_postings cap should be honored"

    for p in postings:
        assert isinstance(p, ScrapedPosting)
        assert p.competitor_name == "Amazon"
        assert p.raw_title, "raw_title must be non-empty"
        assert p.location_city, "location_city must be non-empty"
        assert len(p.location_state) == 2 and p.location_state.isupper()
        assert p.raw_html, "raw_html must be non-empty"
        assert p.source_url.startswith("https://"), "source_url must look real"
        # The location-bug regression guard:
        assert p.street_address, "street_address must be populated from JSON-LD"
        assert p.zip_code, "zip_code must be populated from JSON-LD"
        # The LLM extractor scans for currency and a 'Pay' label — both must
        # appear in the fixture so the downstream pipeline has something to
        # parse.
        assert ("$" in p.raw_html) and ("Pay" in p.raw_html)
        # JSON-LD must round-trip through the fixture so the live parser path
        # is exercised end-to-end in tests.
        assert "application/ld+json" in p.raw_html
        assert "JobPosting" in p.raw_html


def test_fixture_mode_max_postings_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_postings=0 should yield nothing — guards against runaway loops."""
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Amazon")
    assert scraper is not None
    assert list(scraper.scrape(keywords=["Warehouse Associate"], max_postings=0)) == []


def test_live_failure_falls_back_to_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the live path raises (bot challenge, no chromium, etc.) and FIXTURE_MODE
    is not set, scrape() should still yield fixture postings so the pipeline
    keeps moving."""
    from app.scrapers.registry import get_scraper

    # Make sure FIXTURE_MODE is OFF so we exercise the exception-fallback path.
    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    scraper = get_scraper("Amazon")
    assert scraper is not None

    def _boom(self, keywords, max_postings):  # noqa: ARG001
        raise RuntimeError("simulated bot challenge")
        yield  # pragma: no cover  - make this a generator

    monkeypatch.setattr("app.scrapers.amazon.AmazonScraper._scrape_live", _boom)

    postings = list(scraper.scrape(keywords=["Warehouse Associate"], max_postings=2))
    assert len(postings) >= 1
    assert all(p.competitor_name == "Amazon" for p in postings)
    # Even on the fallback path, address fields should be populated.
    assert all(p.street_address and p.zip_code for p in postings)


def test_fixture_file_present_and_realistic() -> None:
    """The fixture file must exist and contain the markers our extractor (and
    a future LLM prompt) keys off of, plus the JSON-LD JobPosting block."""
    from app.scrapers.amazon import FIXTURE_DIR, FIXTURE_FILE

    path = FIXTURE_DIR / FIXTURE_FILE
    assert path.exists(), f"fixture HTML missing at {path}"
    html = path.read_text(encoding="utf-8")
    assert "<h1" in html.lower()
    assert "application/ld+json" in html
    assert "JobPosting" in html
    assert "streetAddress" in html
    assert "postalCode" in html
    assert "Pay" in html
    assert "$" in html
    assert "hour" in html.lower()


def test_hiring_fixture_file_present_and_realistic() -> None:
    """The hiring.amazon.com fixture file must also exist and round-trip
    full structured address through JSON-LD."""
    from app.scrapers.amazon import FIXTURE_DIR, HIRING_FIXTURE_FILE

    path = FIXTURE_DIR / HIRING_FIXTURE_FILE
    assert path.exists(), f"hiring fixture HTML missing at {path}"
    html = path.read_text(encoding="utf-8")
    assert "application/ld+json" in html
    assert "JobPosting" in html
    assert "streetAddress" in html
    assert "postalCode" in html
    assert "Pay" in html
    assert "$" in html


def test_warehouse_keyword_classification() -> None:
    """The classifier must split known warehouse strings vs known corporate strings."""
    from app.scrapers.amazon import _is_warehouse_keyword

    assert _is_warehouse_keyword("Warehouse Associate")
    assert _is_warehouse_keyword("Seasonal Warehouse Associate - Night Shift")
    assert _is_warehouse_keyword("Sortation Associate")
    assert _is_warehouse_keyword("Delivery Associate")
    assert _is_warehouse_keyword("Stocker")

    assert not _is_warehouse_keyword("Software Engineer")
    assert not _is_warehouse_keyword("Senior Product Manager")
    assert not _is_warehouse_keyword("Solutions Architect, AWS")


# --------------------------------------------------------------------------- #
# Subdomain dispatch tests                                                    #
# --------------------------------------------------------------------------- #
#
# These stub the two inner scrape helpers (``_scrape_hiring`` and the base
# class's ``_scrape_live``) so we can observe which subset of keywords got
# dispatched where without actually launching Playwright.


def _stub_postings(competitor: str, keywords, max_postings, subdomain: str):
    """Yield one ScrapedPosting per keyword (capped at ``max_postings``)."""
    from app.scrapers.base import ScrapedPosting

    for i, kw in enumerate(keywords):
        if i >= max_postings:
            return
        yield ScrapedPosting(
            competitor_name=competitor,
            raw_title=kw,
            location_city="Atlanta",
            location_state="GA",
            raw_html=f"<html data-subdomain='{subdomain}'></html>",
            source_url=f"https://{subdomain}/job/{i}",
            street_address="100 Test St" if subdomain == "hiring.amazon.com" else "",
            zip_code="30303" if subdomain == "hiring.amazon.com" else "",
        )


def test_warehouse_keyword_routes_to_hiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyword in WAREHOUSE_KEYWORDS must dispatch to ``_scrape_hiring`` and
    NOT to the inherited base-class ``_scrape_live`` (which targets amazon.jobs)."""
    from app.scrapers.amazon import AmazonScraper
    from app.scrapers.base_employer import BaseEmployerScraper
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    hiring_calls: list[tuple[list[str], int]] = []
    corp_calls: list[tuple[list[str], int]] = []

    def fake_hiring(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        hiring_calls.append((list(keywords), max_postings))
        yield from _stub_postings(self.name, keywords, max_postings, "hiring.amazon.com")

    def fake_corp_super(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        corp_calls.append((list(keywords), max_postings))
        yield from _stub_postings(self.name, keywords, max_postings, "www.amazon.jobs")

    monkeypatch.setattr(AmazonScraper, "_scrape_hiring", fake_hiring)
    monkeypatch.setattr(BaseEmployerScraper, "_scrape_live", fake_corp_super)

    scraper = get_scraper("Amazon")
    assert scraper is not None
    postings = list(
        scraper.scrape(keywords=["Warehouse Associate"], max_postings=5)
    )

    assert hiring_calls and hiring_calls[0][0] == ["Warehouse Associate"]
    assert corp_calls == [], "no corp keywords -> base class _scrape_live MUST NOT be called"
    assert all("hiring.amazon.com" in p.source_url for p in postings)
    assert all(p.street_address and p.zip_code for p in postings)


def test_corp_keyword_routes_to_amazon_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyword OUTSIDE WAREHOUSE_KEYWORDS must dispatch to the base class's
    ``_scrape_live`` (which targets amazon.jobs) and NOT to ``_scrape_hiring``."""
    from app.scrapers.amazon import AmazonScraper
    from app.scrapers.base_employer import BaseEmployerScraper
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    hiring_calls: list[tuple[list[str], int]] = []
    corp_calls: list[tuple[list[str], int]] = []

    def fake_hiring(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        hiring_calls.append((list(keywords), max_postings))
        yield from _stub_postings(self.name, keywords, max_postings, "hiring.amazon.com")

    def fake_corp_super(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        corp_calls.append((list(keywords), max_postings))
        yield from _stub_postings(self.name, keywords, max_postings, "www.amazon.jobs")

    monkeypatch.setattr(AmazonScraper, "_scrape_hiring", fake_hiring)
    monkeypatch.setattr(BaseEmployerScraper, "_scrape_live", fake_corp_super)

    scraper = get_scraper("Amazon")
    assert scraper is not None
    postings = list(
        scraper.scrape(keywords=["Software Engineer"], max_postings=5)
    )

    assert corp_calls and corp_calls[0][0] == ["Software Engineer"]
    assert hiring_calls == [], "no warehouse keywords -> _scrape_hiring MUST NOT be called"
    assert all("amazon.jobs" in p.source_url for p in postings)


def test_mixed_keywords_dispatch_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyword list spanning both buckets must hit both subdomains, each with
    only its own subset of keywords."""
    from app.scrapers.amazon import AmazonScraper
    from app.scrapers.base_employer import BaseEmployerScraper
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    hiring_calls: list[tuple[list[str], int]] = []
    corp_calls: list[tuple[list[str], int]] = []

    def fake_hiring(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        hiring_calls.append((list(keywords), max_postings))
        yield from _stub_postings(self.name, keywords, max_postings, "hiring.amazon.com")

    def fake_corp_super(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        corp_calls.append((list(keywords), max_postings))
        yield from _stub_postings(self.name, keywords, max_postings, "www.amazon.jobs")

    monkeypatch.setattr(AmazonScraper, "_scrape_hiring", fake_hiring)
    monkeypatch.setattr(BaseEmployerScraper, "_scrape_live", fake_corp_super)

    scraper = get_scraper("Amazon")
    assert scraper is not None
    postings = list(
        scraper.scrape(
            keywords=["Warehouse Associate", "Software Engineer", "Sortation Associate"],
            max_postings=10,
        )
    )

    assert hiring_calls, "warehouse-side dispatch missing"
    assert corp_calls, "corp-side dispatch missing"
    # Warehouse path got only the warehouse keywords.
    assert set(hiring_calls[0][0]) == {"Warehouse Associate", "Sortation Associate"}
    # Corp path got only the corp keywords.
    assert corp_calls[0][0] == ["Software Engineer"]
    # Postings span both subdomains.
    subdomains = {("hiring" if "hiring.amazon.com" in p.source_url else "corp") for p in postings}
    assert subdomains == {"hiring", "corp"}


def test_telemetry_tracks_per_subdomain(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a scrape that yields from both subdomains, telemetry exposes a
    ``per_subdomain_yielded`` dict the operator can read on /admin/scrape-runs."""
    from app.scrapers.amazon import AmazonScraper
    from app.scrapers.base_employer import BaseEmployerScraper
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    def fake_hiring(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        yield from _stub_postings(self.name, keywords, max_postings, "hiring.amazon.com")

    def fake_corp_super(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        yield from _stub_postings(self.name, keywords, max_postings, "www.amazon.jobs")

    monkeypatch.setattr(AmazonScraper, "_scrape_hiring", fake_hiring)
    monkeypatch.setattr(BaseEmployerScraper, "_scrape_live", fake_corp_super)

    scraper = get_scraper("Amazon")
    assert scraper is not None

    # Two warehouse kws -> 2 hiring postings; one corp kw -> 1 corp posting.
    postings = list(
        scraper.scrape(
            keywords=["Warehouse Associate", "Sortation Associate", "Software Engineer"],
            max_postings=10,
        )
    )
    assert len(postings) == 3

    tel = scraper.last_run_telemetry.get("per_subdomain_yielded")
    assert isinstance(tel, dict)
    assert tel.get("hiring.amazon.com") == 2
    assert tel.get("amazon.jobs") == 1


def test_max_postings_cap_respected_across_subdomains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_postings`` must cap the combined yield across both subdomains. The
    hiring path runs first; once it has consumed the budget the corp path is
    skipped (or yields nothing)."""
    from app.scrapers.amazon import AmazonScraper
    from app.scrapers.base_employer import BaseEmployerScraper
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    def fake_hiring(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        # Emit one posting per warehouse keyword regardless of cap so we can
        # verify the orchestrator clamps the total.
        for i, kw in enumerate(keywords):
            from app.scrapers.base import ScrapedPosting
            yield ScrapedPosting(
                competitor_name=self.name,
                raw_title=kw,
                location_city="Atlanta",
                location_state="GA",
                raw_html="<html/>",
                source_url=f"https://hiring.amazon.com/job/{i}",
                street_address="100 Test St",
                zip_code="30303",
            )

    corp_called = []

    def fake_corp_super(self, keywords, max_postings, *, locations=None):  # noqa: ARG001
        corp_called.append(max_postings)
        return iter(())

    monkeypatch.setattr(AmazonScraper, "_scrape_hiring", fake_hiring)
    monkeypatch.setattr(BaseEmployerScraper, "_scrape_live", fake_corp_super)

    scraper = get_scraper("Amazon")
    assert scraper is not None
    postings = list(
        scraper.scrape(
            keywords=["Warehouse Associate", "Sortation Associate", "Stocker", "Software Engineer"],
            max_postings=2,
        )
    )
    assert len(postings) == 2
    # The corp path either wasn't called (budget exhausted) OR was called with
    # max_postings == 0. Both shapes are acceptable per the orchestrator's
    # early-return on ``yielded >= max_postings``.
    assert corp_called == [] or all(n <= 0 for n in corp_called)
