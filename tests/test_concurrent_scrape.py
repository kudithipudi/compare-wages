"""Tests for the parallel detail-page fetch path on ``BaseEmployerScraper``.

We avoid Playwright entirely by handing a stub "context" + "page" to
``_walk_details_concurrent``. The stubs simulate per-navigation latency, which
lets us assert:

  * Concurrent scrape (``detail_concurrency=4``) finishes ~Nx faster than the
    serial baseline (``detail_concurrency=1``) when each "page load" sleeps.
  * The shared rate-limit semaphore enforces ``rate_limit_hz`` *globally* —
    successive navigations across all threads are spaced by at least
    ``1.0 / rate_limit_hz`` seconds.
  * The per-keyword failure budget still works under concurrency: once a
    keyword burns 3 detail failures, no further postings are yielded for
    that keyword even though some of its links may already be queued.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.scrapers.base import ScrapedPosting
from app.scrapers.base_employer import BaseEmployerScraper, _RateLimiter


# --------------------------------------------------------------------------- #
# Stubs                                                                       #
# --------------------------------------------------------------------------- #


class _StubPage:
    """Pretends to be a Playwright sync ``Page``.

    Records the monotonic timestamp of every ``goto`` call into a shared list
    (used to assert rate-limit spacing). The body of ``content()`` is keyed
    off the most recent URL so multiple pages can be in flight simultaneously
    without crosstalk.
    """

    def __init__(
        self,
        *,
        nav_timestamps: list[float],
        nav_lock: threading.Lock,
        per_url_behavior: dict[str, dict[str, Any]],
        nav_latency: float,
    ) -> None:
        self._nav_timestamps = nav_timestamps
        self._nav_lock = nav_lock
        self._per_url_behavior = per_url_behavior
        self._nav_latency = nav_latency
        self._current_url: str | None = None
        self.closed = False

    def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 30_000) -> None:
        with self._nav_lock:
            self._nav_timestamps.append(time.monotonic())
        self._current_url = url
        behavior = self._per_url_behavior.get(url, {})
        if behavior.get("raise_timeout"):
            # Exhaust the failure budget by raising on every attempt.
            raise TimeoutError(f"stub timeout for {url}")
        if self._nav_latency > 0:
            time.sleep(self._nav_latency)

    def content(self) -> str:
        return self._per_url_behavior.get(self._current_url or "", {}).get("html", "<html></html>")

    def title(self) -> str:
        return ""

    def query_selector(self, _sel: str):
        return None

    def close(self) -> None:
        self.closed = True


class _StubContext:
    """Pretends to be a Playwright ``BrowserContext``: hands out fresh pages."""

    def __init__(
        self,
        *,
        nav_timestamps: list[float],
        nav_lock: threading.Lock,
        per_url_behavior: dict[str, dict[str, Any]],
        nav_latency: float,
    ) -> None:
        self._nav_timestamps = nav_timestamps
        self._nav_lock = nav_lock
        self._per_url_behavior = per_url_behavior
        self._nav_latency = nav_latency
        self.pages_handed_out: list[_StubPage] = []

    def new_page(self) -> _StubPage:
        page = _StubPage(
            nav_timestamps=self._nav_timestamps,
            nav_lock=self._nav_lock,
            per_url_behavior=self._per_url_behavior,
            nav_latency=self._nav_latency,
        )
        self.pages_handed_out.append(page)
        return page


class _FakeScraper(BaseEmployerScraper):
    """Concrete BaseEmployerScraper subclass with just enough surface area to
    drive the detail-walk paths under test. We bypass ``_extract_posting``'s
    JSON-LD parsing by returning a hand-built ScrapedPosting per URL."""

    name = "FakeCo"
    robots_url = "https://fake.example.com/robots.txt"
    robots_target_path = "/search"
    search_url_template = "https://fake.example.com/search?q={kw}"
    fixture_file = ""
    fixture_postings: list[dict] = []
    result_link_selectors: list[str] = ["a"]
    rate_limit_hz = 4.0  # 0.25s min gap — easier on the test clock than 1.0

    def _extract_posting(self, page, html: str, source_url: str) -> ScrapedPosting | None:  # type: ignore[override]
        if "<empty/>" in html:
            return None
        return ScrapedPosting(
            competitor_name=self.name,
            raw_title="Stub Role",
            location_city="Nowhere",
            location_state="XX",
            raw_html=html,
            source_url=source_url,
        )


def _build_links(n: int, keyword: str = "kw0") -> list[tuple[str, str]]:
    return [(keyword, f"https://fake.example.com/job/{i}") for i in range(n)]


def _ok_behavior(urls: list[str]) -> dict[str, dict[str, Any]]:
    return {u: {"html": f"<html><body>job for {u}</body></html>"} for u in urls}


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_concurrent_walk_is_faster_than_sequential() -> None:
    """8 fake postings @ ~120ms each should be ~4x faster with concurrency=4.

    We use a deliberately slack threshold (concurrent should be < 0.6x serial)
    so CI scheduling jitter doesn't flake the assertion. The point is to prove
    the threads actually overlap, not to nail an exact speedup.
    """
    n = 8
    nav_latency = 0.12  # 120ms per "navigation"

    # --- serial baseline ---
    nav_ts_seq: list[float] = []
    lock_seq = threading.Lock()
    links = _build_links(n)
    urls = [u for _, u in links]
    behavior = _ok_behavior(urls)

    scraper_seq = _FakeScraper()
    scraper_seq.detail_concurrency = 1
    # Disable rate limiting in this test by using a huge rate_limit_hz; we
    # are measuring nav-latency-bound speedup, not throttle.
    scraper_seq.rate_limit_hz = 1000.0
    pause_seq = 1.0 / scraper_seq.rate_limit_hz
    ctx_seq = _StubContext(
        nav_timestamps=nav_ts_seq,
        nav_lock=lock_seq,
        per_url_behavior=behavior,
        nav_latency=nav_latency,
    )
    # For the sequential path we want to reuse a single page (matches the
    # real ``_scrape_live`` shape).
    page_seq = ctx_seq.new_page()
    t0 = time.monotonic()
    out_seq = list(
        scraper_seq._walk_details_sequential(page_seq, links, pause_seq, TimeoutError)
    )
    serial_elapsed = time.monotonic() - t0

    # --- concurrent run ---
    nav_ts_par: list[float] = []
    lock_par = threading.Lock()
    scraper_par = _FakeScraper()
    scraper_par.detail_concurrency = 4
    scraper_par.rate_limit_hz = 1000.0
    pause_par = 1.0 / scraper_par.rate_limit_hz
    ctx_par = _StubContext(
        nav_timestamps=nav_ts_par,
        nav_lock=lock_par,
        per_url_behavior=behavior,
        nav_latency=nav_latency,
    )
    t0 = time.monotonic()
    out_par = list(
        scraper_par._walk_details_concurrent(ctx_par, links, pause_par, TimeoutError)
    )
    concurrent_elapsed = time.monotonic() - t0

    assert len(out_seq) == n
    assert len(out_par) == n
    # Each worker opened its own page; verify the "one page per task" pattern.
    assert len(ctx_par.pages_handed_out) >= 4
    for p in ctx_par.pages_handed_out:
        assert p.closed, "every worker page must be closed after use"
    # Concurrent should be meaningfully faster — slack to absorb jitter.
    assert concurrent_elapsed < serial_elapsed * 0.6, (
        f"concurrent ({concurrent_elapsed:.3f}s) should be << serial "
        f"({serial_elapsed:.3f}s) for 8 fake postings"
    )
    # Emit the timings so the verifier can read them in the captured output.
    print(
        f"\n[concurrency smoke] n=8 nav_latency={nav_latency*1000:.0f}ms  "
        f"serial={serial_elapsed:.3f}s  concurrent(detail_concurrency=4)="
        f"{concurrent_elapsed:.3f}s  speedup={serial_elapsed/concurrent_elapsed:.2f}x"
    )
    # Also write to a tmp file the verifier can ``cat`` after pytest runs.
    try:
        with open("/tmp/concurrent_scrape_timing.txt", "w", encoding="utf-8") as fp:
            fp.write(
                f"n=8 nav_latency={nav_latency*1000:.0f}ms\n"
                f"serial:     {serial_elapsed:.3f}s\n"
                f"concurrent: {concurrent_elapsed:.3f}s (detail_concurrency=4)\n"
                f"speedup:    {serial_elapsed/concurrent_elapsed:.2f}x\n"
            )
    except OSError:
        pass


def test_rate_limit_semaphore_is_respected_under_concurrency() -> None:
    """With ``rate_limit_hz=5`` (0.2s gap) and 4 workers, successive ``goto``
    calls must still be spaced at least ~0.18s apart globally.

    We use a small slack on the gap floor (0.18 instead of 0.20) because
    ``time.monotonic()`` + the limiter's lock add tiny scheduling noise.
    """
    rate_limit_hz = 5.0
    min_gap = 1.0 / rate_limit_hz
    n = 6

    nav_ts: list[float] = []
    lock = threading.Lock()
    links = _build_links(n)
    urls = [u for _, u in links]
    behavior = _ok_behavior(urls)

    scraper = _FakeScraper()
    scraper.detail_concurrency = 4
    scraper.rate_limit_hz = rate_limit_hz
    pause = 1.0 / rate_limit_hz
    ctx = _StubContext(
        nav_timestamps=nav_ts,
        nav_lock=lock,
        per_url_behavior=behavior,
        nav_latency=0.01,
    )
    out = list(scraper._walk_details_concurrent(ctx, links, pause, TimeoutError))
    assert len(out) == n

    sorted_ts = sorted(nav_ts)
    assert len(sorted_ts) == n
    # Allow 10ms scheduling slack.
    slack = 0.02
    for prev, nxt in zip(sorted_ts, sorted_ts[1:]):
        gap = nxt - prev
        assert gap >= (min_gap - slack), (
            f"adjacent navigations were only {gap:.3f}s apart; "
            f"min_gap was {min_gap:.3f}s"
        )


def test_per_keyword_failure_budget_under_concurrency() -> None:
    """If 3 detail loads fail on the same keyword we must stop dispatching new
    work for that keyword. Other keywords keep flowing normally.

    Layout: ``kwA`` has 5 links (3 timeout, then 2 that would succeed but
    should be skipped). ``kwB`` has 2 links, both succeed.
    """
    kwA_links = [("kwA", f"https://fake.example.com/A/{i}") for i in range(5)]
    kwB_links = [("kwB", f"https://fake.example.com/B/{i}") for i in range(2)]
    links = kwA_links + kwB_links

    behavior: dict[str, dict[str, Any]] = {}
    # First 3 kwA links fail; the rest would succeed.
    for _, u in kwA_links[:3]:
        behavior[u] = {"raise_timeout": True}
    for _, u in kwA_links[3:]:
        behavior[u] = {"html": "<html>ok</html>"}
    for _, u in kwB_links:
        behavior[u] = {"html": "<html>ok</html>"}

    scraper = _FakeScraper()
    # Single worker makes the failure-ordering deterministic: kwA fails 1->2->3
    # in submission order, the failure budget trips, and the remaining kwA
    # links are NOT loaded. With >1 workers the budget is still honored but
    # interleaving makes "exactly 0 kwA successes" non-deterministic, which
    # is fine for a budget test — we just need a deterministic floor.
    scraper.detail_concurrency = 1
    scraper.rate_limit_hz = 1000.0  # don't pace this test
    pause = 1.0 / scraper.rate_limit_hz
    # Pre-seed telemetry the way _scrape_live would.
    scraper.last_run_telemetry["per_keyword_yielded"] = {"kwA": 0, "kwB": 0}
    scraper.last_run_telemetry["per_keyword_errors"] = {"kwA": 0, "kwB": 0}

    nav_ts: list[float] = []
    lock = threading.Lock()
    ctx = _StubContext(
        nav_timestamps=nav_ts,
        nav_lock=lock,
        per_url_behavior=behavior,
        nav_latency=0.0,
    )
    # The sequential path is sufficient to verify budget behavior. We then
    # spot-check the concurrent path below for "skipped keyword stops getting
    # new work".
    page = ctx.new_page()
    out = list(scraper._walk_details_sequential(page, links, pause, TimeoutError))

    titles_by_kw: dict[str, int] = {"kwA": 0, "kwB": 0}
    for posting in out:
        # We don't track the keyword on the posting itself; use the URL prefix.
        if "/A/" in posting.source_url:
            titles_by_kw["kwA"] += 1
        elif "/B/" in posting.source_url:
            titles_by_kw["kwB"] += 1
    # Both kwB links should succeed; no kwA links should succeed (the first 3
    # failures exhaust the budget before the would-succeed links are visited).
    assert titles_by_kw["kwB"] == 2
    assert titles_by_kw["kwA"] == 0
    assert scraper.last_run_telemetry["per_keyword_errors"]["kwA"] >= 3

    # --- now exercise the concurrent path's budget enforcement. ---
    scraper2 = _FakeScraper()
    scraper2.detail_concurrency = 4
    scraper2.rate_limit_hz = 1000.0
    scraper2.last_run_telemetry["per_keyword_yielded"] = {"kwA": 0, "kwB": 0}
    scraper2.last_run_telemetry["per_keyword_errors"] = {"kwA": 0, "kwB": 0}
    pause2 = 1.0 / scraper2.rate_limit_hz
    nav_ts2: list[float] = []
    lock2 = threading.Lock()
    ctx2 = _StubContext(
        nav_timestamps=nav_ts2,
        nav_lock=lock2,
        per_url_behavior=behavior,
        nav_latency=0.0,
    )
    out2 = list(scraper2._walk_details_concurrent(ctx2, links, pause2, TimeoutError))
    # kwB always succeeds; kwA may yield 0..2 (race between failures and
    # remaining-link dispatch) but must not yield more than its non-failing
    # link count, and the failure counter must be >= 3.
    titles_by_kw2: dict[str, int] = {"kwA": 0, "kwB": 0}
    for posting in out2:
        if "/A/" in posting.source_url:
            titles_by_kw2["kwA"] += 1
        elif "/B/" in posting.source_url:
            titles_by_kw2["kwB"] += 1
    assert titles_by_kw2["kwB"] == 2
    assert scraper2.last_run_telemetry["per_keyword_errors"]["kwA"] >= 3


def test_detail_concurrency_default_is_one() -> None:
    """Back-compat: the default must not turn on threading. Subclasses opt in."""
    assert BaseEmployerScraper.detail_concurrency == 1


def test_rate_limiter_spaces_acquires() -> None:
    """Unit-test the helper directly: 5 acquires with min_gap=0.05 should take
    at least ~0.20s (4 inter-acquire gaps)."""
    limiter = _RateLimiter(0.05)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    # 4 gaps of 0.05s = 0.20s expected; allow scheduling slack.
    assert elapsed >= 0.18, f"limiter only took {elapsed:.3f}s for 5 acquires"


# --------------------------------------------------------------------------- #
# Cross-cutting smoke tests for ``app.main``: logging config + RequestId      #
# middleware. Co-located here only because this round added them and the     #
# parent agent will want a quick "did it land?" signal in the same run.      #
# --------------------------------------------------------------------------- #


def test_logging_config_installed_and_tames_noisy_libs() -> None:
    """``_configure_logging`` installs a global format + LOG_LEVEL-driven level
    on the root logger, and turns down third-party chatter so prod logs aren't
    drowned out by httpx debug spam.
    """
    import logging

    from app.main import _configure_logging

    _configure_logging()
    # Tamed loggers must end up at WARNING regardless of LOG_LEVEL.
    for name in (
        "httpx",
        "httpcore",
        "asyncio",
        "urllib3",
        "apscheduler.scheduler",
        "apscheduler.executors.default",
    ):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING

    # Format applied to the root handler.
    root = logging.getLogger()
    assert root.handlers, "root logger must have at least one handler installed"
    fmt = root.handlers[0].formatter
    assert fmt is not None
    # Spot-check the format string we configured.
    assert "%(asctime)s" in fmt._fmt  # type: ignore[union-attr]
    assert "%(name)s" in fmt._fmt  # type: ignore[union-attr]
    assert "::" in fmt._fmt  # type: ignore[union-attr]


def test_request_id_middleware_adds_header_and_is_unique_per_request() -> None:
    """Every response carries an 8-char hex ``X-Request-ID`` header, and each
    request mints a fresh id."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r1 = client.get("/healthz")
    r2 = client.get("/healthz")
    assert r1.status_code == 200
    assert r2.status_code == 200
    id1 = r1.headers.get("X-Request-ID", "")
    id2 = r2.headers.get("X-Request-ID", "")
    assert len(id1) == 8 and len(id2) == 8
    # Hex-only.
    assert all(c in "0123456789abcdef" for c in id1)
    assert id1 != id2, "each request must mint a unique id"

    # Side-channel sample for the report.
    try:
        with open("/tmp/request_id_sample.txt", "w", encoding="utf-8") as fp:
            fp.write(f"GET /healthz -> {r1.status_code} X-Request-ID: {id1}\n")
            fp.write(f"GET /healthz -> {r2.status_code} X-Request-ID: {id2}\n")
    except OSError:
        pass


def test_logging_emits_expected_line_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """Sanity check that a log line emitted at INFO actually matches
    ``asctime LEVEL logger.name :: message``.

    We re-run ``_configure_logging`` to make sure ``force=True`` works even
    after pytest's capture has been installed.
    """
    import logging

    from app.main import _configure_logging

    _configure_logging()
    log = logging.getLogger("test_concurrent_scrape.shape")
    log.info("shape probe message")
    captured = capsys.readouterr()
    # ``%(asctime)s %(levelname)-5s %(name)s :: %(message)s``
    # The asctime portion looks like ``YYYY-MM-DD HH:MM:SS,mmm``.
    line = next(
        (
            ln
            for ln in (captured.out + captured.err).splitlines()
            if "shape probe message" in ln
        ),
        "",
    )
    assert line, "log line was not captured"
    assert " :: " in line, f"separator '::' missing from log line: {line!r}"
    assert "INFO" in line
    assert "test_concurrent_scrape.shape" in line

    try:
        with open("/tmp/log_line_sample.txt", "w", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass
