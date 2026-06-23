"""Tests for WalmartScraper that DO NOT hit the real network.

We exercise:
  - registry wiring (the scraper registers itself on import)
  - the type contract of is_available() (bool — value depends on network)
  - the explicit robots-disallow path (must short-circuit to False)
  - the FIXTURE_MODE fallback (the live Playwright call is never made)
  - the live-failure → fixture-fallback path
  - the regression guard that fixture postings yield street_address + zip_code
    so the downstream geocoder gets a precise address (Walmart is the only
    scraper where the JSON-LD always ships those fields, and a bug that
    dropped them on the floor previously sent every CompetitorLocation row
    to (0, 0) — this test exists to make that bug impossible).

Live Playwright behavior is intentionally out of scope here. Walmart's
Akamai + PerimeterX defenses are behavioral and make a deterministic CI
assertion against the real site impossible. The fixture path is what proves
the rest of the pipeline can keep moving when the live run is blocked.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_registry_has_walmart() -> None:
    # Importing the package side-effect-registers the scraper via @register.
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import get_scraper, has_scraper

    assert has_scraper("Walmart"), "Walmart scraper should be registered"
    scraper = get_scraper("Walmart")
    assert scraper is not None
    assert scraper.name == "Walmart"
    # Rate limit floor of 1 req/sec per the Scraper contract.
    assert scraper.rate_limit_hz >= 1.0


def test_is_available_returns_bool() -> None:
    """is_available() must return a bool. We don't assert True/False because
    that depends on whether the test environment can reach the real internet.
    We patch httpx so the test stays deterministic + offline."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Walmart")
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

    with patch("app.scrapers.walmart.httpx.Client", _FakeClient):
        result = scraper.is_available()

    assert isinstance(result, bool)


def test_is_available_respects_robots_disallow() -> None:
    """If robots.txt explicitly disallows our target path, return False even
    if the rest of the world looks healthy. Fixture mode must not paper this
    over."""
    from app.scrapers.registry import get_scraper

    scraper = get_scraper("Walmart")
    assert scraper is not None

    class _DisallowResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /results\n"

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

    with patch("app.scrapers.walmart.httpx.Client", _FakeClient):
        assert scraper.is_available() is False


def test_fixture_mode_yields_postings_with_full_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION GUARD: With FIXTURE_MODE on, scrape() yields postings entirely
    from disk and never touches Playwright/network. Every yielded posting MUST
    carry a non-empty street_address and zip_code — that's what lets the
    service-layer geocoder hit a precise address instead of city-centroid (or
    worse, lat=0/lng=0)."""
    from app.scrapers.base import ScrapedPosting
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Walmart")
    assert scraper is not None

    postings = list(
        scraper.scrape(keywords=["Cashier", "Stocker"], max_postings=3)
    )
    assert len(postings) >= 1, "fixture mode should yield at least one posting"
    assert len(postings) <= 3, "max_postings cap should be honored"

    for p in postings:
        assert isinstance(p, ScrapedPosting)
        assert p.competitor_name == "Walmart"
        assert p.raw_title, "raw_title must be non-empty"
        assert p.location_city, "location_city must be non-empty"
        assert len(p.location_state) == 2 and p.location_state.isupper()
        assert p.raw_html, "raw_html must be non-empty"
        assert p.source_url.startswith("https://"), "source_url must look real"
        # The whole point of Walmart's JSON-LD is the structured address — if
        # we lose it on the floor the geocoder degrades to a city centroid.
        assert p.street_address, "street_address must be populated from JSON-LD"
        assert p.zip_code, "zip_code must be populated from JSON-LD"
        # The downstream LLM extractor scans for currency and a 'Pay' label;
        # both must appear in the fixture so the wage parser has something to
        # work with on the disclosed-state postings.
        assert ("$" in p.raw_html) and ("Pay" in p.raw_html)
        # And the rendered address values must actually be present in the HTML
        # (i.e. we replaced the placeholders, not left {{STREET}} on the page).
        assert p.street_address in p.raw_html
        assert p.zip_code in p.raw_html
        assert "{{" not in p.raw_html, "fixture placeholders must be rendered"


def test_fixture_mode_max_postings_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_postings=0 should yield nothing — guards against runaway loops."""
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Walmart")
    assert scraper is not None
    assert list(scraper.scrape(keywords=["Cashier"], max_postings=0)) == []


def test_live_failure_falls_back_to_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the live path raises (Akamai/PerimeterX block, no chromium, etc.)
    and FIXTURE_MODE is not set, scrape() should still yield fixture postings
    so the pipeline keeps moving."""
    from app.scrapers.registry import get_scraper

    # Make sure FIXTURE_MODE is OFF so we exercise the exception-fallback path.
    monkeypatch.delenv("FIXTURE_MODE", raising=False)

    scraper = get_scraper("Walmart")
    assert scraper is not None

    def _boom(self, keywords, max_postings):  # noqa: ARG001
        raise RuntimeError("simulated Akamai 403")
        yield  # pragma: no cover  - make this a generator

    monkeypatch.setattr(
        "app.scrapers.walmart.WalmartScraper._scrape_live", _boom
    )

    postings = list(scraper.scrape(keywords=["Cashier"], max_postings=2))
    assert len(postings) >= 1
    assert all(p.competitor_name == "Walmart" for p in postings)
    # Regression guard still applies on the fallback path.
    assert all(p.street_address and p.zip_code for p in postings)


def test_empty_keywords_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Scraper contract is explicit: an empty keywords list MUST yield
    nothing, even in fixture mode. Operators expand coverage by editing role
    mappings, never by relying on a hardcoded default keyword set."""
    from app.scrapers.registry import get_scraper

    monkeypatch.setenv("FIXTURE_MODE", "1")
    scraper = get_scraper("Walmart")
    assert scraper is not None
    assert list(scraper.scrape(keywords=[], max_postings=5)) == []


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, *, page) -> dict:
    """Wire a minimal fake playwright.sync_api into the walmart module.

    Returns the ``captured`` dict that the fakes write into so tests can assert
    on what ``new_context``/``stealth_sync``/etc. were called with.
    """
    import sys
    import types

    captured: dict = {"context_kwargs": None, "stealth_called_with": None}

    class _FakeContext:
        def __init__(self, **kwargs):
            captured["context_kwargs"] = kwargs

        def new_page(self):
            return page

    class _FakeBrowser:
        def new_context(self, **kwargs):
            return _FakeContext(**kwargs)

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, **kwargs):
            return _FakeBrowser()

    class _FakePW:
        chromium = _FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _sync_playwright():
        return _FakePW()

    class _PWTimeout(Exception):
        pass

    # Build a fake ``playwright.sync_api`` module so the lazy ``from
    # playwright.sync_api import sync_playwright`` inside ``_scrape_live``
    # picks up our fakes instead of the real (chromium-requiring) module.
    fake_pw_pkg = types.ModuleType("playwright")
    fake_pw_sync = types.ModuleType("playwright.sync_api")
    fake_pw_sync.sync_playwright = _sync_playwright  # type: ignore[attr-defined]
    fake_pw_sync.TimeoutError = _PWTimeout  # type: ignore[attr-defined]
    fake_pw_pkg.sync_api = fake_pw_sync  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_pw_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_pw_sync)

    return captured


class _FakePage:
    """Minimal page stand-in. Configurable per-test for content + selectors."""

    def __init__(self, *, content: str = "<html><body></body></html>") -> None:
        self._content = content
        self.goto_calls: list[str] = []

    def goto(self, url, **kwargs):  # noqa: ARG002
        self.goto_calls.append(url)

    def content(self) -> str:
        return self._content

    def eval_on_selector_all(self, selector, script):  # noqa: ARG002
        return []

    def query_selector(self, selector):  # noqa: ARG002
        return None

    def title(self) -> str:
        return ""


def test_stealth_applied_when_playwright_stealth_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When playwright_stealth is importable, _scrape_live must call
    stealth_sync on the freshly-opened page and record ``stealth_applied`` in
    telemetry. We install a fake ``playwright_stealth`` module so the test
    doesn't depend on the real dep being pip-installed."""
    import sys
    import types

    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)
    monkeypatch.delenv("WALMART_PROXY_URL", raising=False)

    page = _FakePage()
    captured = _install_fake_playwright(monkeypatch, page=page)

    sentinel = {"called_with": None}

    def _fake_stealth(p):
        sentinel["called_with"] = p
        captured["stealth_called_with"] = p

    fake_stealth_mod = types.ModuleType("playwright_stealth")
    fake_stealth_mod.stealth_sync = _fake_stealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright_stealth", fake_stealth_mod)

    scraper = get_scraper("Walmart")
    assert scraper is not None

    # Drain the generator — _scrape_live will produce 0 links (FakePage returns
    # no selectors) and raise the "no result links" RuntimeError, which
    # ``scrape()`` catches and falls back to fixtures. That's fine for this
    # test — we only care that stealth_sync was called before the failure.
    list(scraper.scrape(keywords=["Cashier"], max_postings=1))

    assert sentinel["called_with"] is page, "stealth_sync should be called with the live page"
    assert "stealth_applied" in scraper.last_run_telemetry["reasons"]


def test_proxy_configuration_passed_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When WALMART_PROXY_URL (+ optional username/password) are set, the
    Playwright context must be created with the expected ``proxy=`` kwarg, and
    telemetry must record a credential-masked proxy_configured marker."""
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)
    monkeypatch.setenv("WALMART_PROXY_URL", "http://gate.brightdata.com:22225")
    monkeypatch.setenv("WALMART_PROXY_USERNAME", "user-abc")
    monkeypatch.setenv("WALMART_PROXY_PASSWORD", "s3cr3t")

    page = _FakePage()
    captured = _install_fake_playwright(monkeypatch, page=page)

    scraper = get_scraper("Walmart")
    assert scraper is not None

    list(scraper.scrape(keywords=["Cashier"], max_postings=1))

    kwargs = captured["context_kwargs"]
    assert kwargs is not None, "new_context should have been called"
    assert kwargs.get("proxy") == {
        "server": "http://gate.brightdata.com:22225",
        "username": "user-abc",
        "password": "s3cr3t",
    }
    # Credential masking: telemetry must contain host but never the password.
    proxy_reason = next(
        (r for r in scraper.last_run_telemetry["reasons"] if r.startswith("proxy_configured=")),
        None,
    )
    assert proxy_reason is not None
    assert "gate.brightdata.com" in proxy_reason
    assert "s3cr3t" not in proxy_reason
    assert "user-abc" not in proxy_reason


def test_no_proxy_kwarg_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: without the proxy env vars, ``new_context`` must NOT
    receive a ``proxy=`` kwarg — otherwise Playwright would refuse to launch
    when the operator hasn't signed up for a proxy yet."""
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)
    monkeypatch.delenv("WALMART_PROXY_URL", raising=False)
    monkeypatch.delenv("WALMART_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("WALMART_PROXY_PASSWORD", raising=False)

    page = _FakePage()
    captured = _install_fake_playwright(monkeypatch, page=page)

    scraper = get_scraper("Walmart")
    assert scraper is not None
    list(scraper.scrape(keywords=["Cashier"], max_postings=1))

    kwargs = captured["context_kwargs"]
    assert kwargs is not None
    assert "proxy" not in kwargs


def test_challenge_page_raises_walmart_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the search-page HTML contains a known Akamai/PerimeterX marker we
    must raise WalmartBlocked from within _scrape_live so the orchestrator's
    fallback catches it with a precise reason."""
    from app.scrapers.walmart import WalmartBlocked, WalmartScraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)
    monkeypatch.delenv("WALMART_PROXY_URL", raising=False)

    challenge_html = (
        "<html><body><h1>Pardon Our Interruption</h1>"
        "<p>...as you were browsing...</p></body></html>"
    )
    page = _FakePage(content=challenge_html)

    scraper = WalmartScraper()
    with pytest.raises(WalmartBlocked) as ei:
        # _search_for_links is what raises — call it directly so we get the
        # exception itself (rather than the orchestrator's fallback swallowing
        # it).
        scraper._search_for_links(page, "Cashier", 5, 0.0)
    assert "challenge" in str(ei.value).lower()


def test_walmart_blocked_falls_back_to_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end version of the above: a challenge page during scrape() must
    fall back to fixture postings AND record a WalmartBlocked reason in
    telemetry so the operator sees *why* the live run failed."""
    from app.scrapers.registry import get_scraper

    monkeypatch.delenv("FIXTURE_MODE", raising=False)
    monkeypatch.delenv("WALMART_PROXY_URL", raising=False)

    page = _FakePage(content="<html><body>Pardon Our Interruption</body></html>")
    _install_fake_playwright(monkeypatch, page=page)

    scraper = get_scraper("Walmart")
    assert scraper is not None

    postings = list(scraper.scrape(keywords=["Cashier"], max_postings=2))
    assert len(postings) >= 1, "should fall back to fixture postings"
    assert all(p.competitor_name == "Walmart" for p in postings)
    # And the precise reason must appear in telemetry — that's the operator-
    # visible win over the previous vague "no result links" message.
    reasons = " | ".join(scraper.last_run_telemetry["reasons"])
    assert "WalmartBlocked" in reasons
    assert "challenge" in reasons.lower()


def test_fixture_file_present_and_realistic() -> None:
    """The fixture file must exist and contain the markers the LLM extractor
    keys off of, plus a parseable JSON-LD block with full structured address."""
    from app.scrapers.base_employer import parse_jobposting_jsonld
    from app.scrapers.walmart import FIXTURE_DIR, FIXTURE_FILE

    path = FIXTURE_DIR / FIXTURE_FILE
    assert path.exists(), f"fixture HTML missing at {path}"
    html = path.read_text(encoding="utf-8")
    assert "<h1" in html.lower()
    assert "Pay" in html
    assert "$" in html
    assert "per hour" in html.lower()
    # JSON-LD JobPosting must be parseable (after placeholder substitution).
    rendered = (
        html.replace("{{CITY}}", "Bentonville")
        .replace("{{STATE}}", "AR")
        .replace("{{TITLE}}", "Cashier")
        .replace("{{STREET}}", "406 S Walton Blvd")
        .replace("{{ZIP}}", "72712")
    )
    ld = parse_jobposting_jsonld(rendered)
    assert ld is not None, "JSON-LD JobPosting block must be parseable"
    addr = ld["jobLocation"]["address"]
    assert addr["addressLocality"] == "Bentonville"
    assert addr["addressRegion"] == "AR"
    assert addr["streetAddress"] == "406 S Walton Blvd"
    assert addr["postalCode"] == "72712"
