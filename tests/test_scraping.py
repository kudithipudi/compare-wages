"""Tests for the employer-scraping service layer.

These tests deliberately avoid invoking any real Playwright/HTTP work. They exercise
the orchestration: ScraperRun row lifecycle, the no-scraper-registered fast path,
the orphaned-row sweep, and the success path with a fake in-memory scraper.
"""
from __future__ import annotations

from typing import Iterator

from sqlalchemy import select

from app.db import session_scope
from app.models import Competitor, JobPosting, RoleMapping, ScraperRun
from app.scrapers.base import ScrapedPosting, Scraper
from app.scrapers.registry import SCRAPERS
from app.services.scraping import (
    keywords_for_competitor,
    mark_orphaned_scraper_runs_failed,
    run_scrape,
)


def _make_competitor(name: str) -> int:
    with session_scope() as s:
        c = Competitor(name=name, source_priority=2, source_tier="employer_owned", careers_url="")
        s.add(c)
        s.flush()
        return c.id


def test_run_scrape_with_no_scraper_creates_failed_run(seeded_session):
    """A competitor without a registered scraper produces a clean failed run row."""
    # Use a name we know isn't registered (and isn't pre-seeded as a real competitor).
    name = "NoScraperCompetitor"
    assert name not in SCRAPERS, "test precondition: name must not be in registry"
    competitor_id = _make_competitor(name)

    run_id = run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)

    with session_scope() as s:
        run = s.get(ScraperRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert "no scraper" in run.notes.lower()
        assert run.competitor_name == name


def test_mark_orphaned_scraper_runs_failed(seeded_session):
    """A stuck running ScraperRun row is swept to failed on boot."""
    with session_scope() as s:
        orphan = ScraperRun(
            competitor_name="GhostCorp",
            triggered_by="test",
            status="running",
        )
        s.add(orphan)
        s.flush()
        orphan_id = orphan.id

    n = mark_orphaned_scraper_runs_failed()
    assert n >= 1

    with session_scope() as s:
        orphan = s.get(ScraperRun, orphan_id)
        assert orphan.status == "failed"
        assert orphan.finished_at is not None
        assert "interrupted" in orphan.notes.lower()


class _FakeScraper(Scraper):
    """In-memory scraper used to exercise the happy path without any I/O.

    Yields two postings: one in California, one in Texas. The same Texas city is yielded
    twice to exercise the case-insensitive match path on CompetitorLocation lookup.
    """

    name = "FakeCo"

    def is_available(self) -> bool:
        return True

    def scrape(
        self,
        *,
        keywords: list[str],
        locations: list[tuple[str, str]] | None = None,
        max_postings: int = 25,
    ) -> Iterator[ScrapedPosting]:
        yield ScrapedPosting(
            competitor_name=self.name,
            raw_title="Warehouse Associate",
            location_city="Los Angeles",
            location_state="CA",
            raw_html="<html><body>$18-$22/hr</body></html>",
            source_url="https://example.com/jobs/1",
            street_address="123 Main St",
        )
        yield ScrapedPosting(
            competitor_name=self.name,
            raw_title="Lot Attendant",
            location_city="Houston",
            location_state="TX",
            raw_html="<html><body>$16/hr</body></html>",
            source_url="https://example.com/jobs/2",
        )
        # Same city as previous, different case — should match the existing CL.
        yield ScrapedPosting(
            competitor_name=self.name,
            raw_title="Loader",
            location_city="houston",
            location_state="TX",
            raw_html="<html><body>$17/hr</body></html>",
            source_url="https://example.com/jobs/3",
        )


def test_run_scrape_happy_path_writes_postings_and_locations(seeded_session, monkeypatch):
    """Registered scraper → ScraperRun success, JobPostings created, CL matched/created."""
    name = "FakeCo"
    competitor_id = _make_competitor(name)
    SCRAPERS[name] = _FakeScraper
    # The service now requires at least one keyword from RoleMapping. Add a generic one.
    with session_scope() as s:
        s.add(RoleMapping(
            competitor_id=competitor_id, copart_role="Yard Attendant",
            competitor_role="Warehouse Associate", bucket="outdoor", confidence=0.9,
        ))
    # Stub the geocoder so the test stays offline and gets real (non-zero) coords.
    # Without this stub the city-only "Houston, TX" lookup hits the live Census
    # Geocoder, gets no match, and the CL persists with (0,0) — which the catchment
    # filter (correctly) treats as out-of-catchment, dropping the postings before save.
    monkeypatch.setattr(
        # Both cities resolve to LA-adjacent coords so they fall inside CA-LAX's catchment
        # regardless of which Copart yards are active at test time (test ordering can leave
        # only CA-LAX active by the time this runs). This test isn't about geography —
        # it's about the orchestration loop saving every posting it accepts.
        "app.services.scraping.geocode",
        lambda **kw: (34.05, -118.24) if kw.get("city") == "Los Angeles" else (34.06, -118.25),
    )
    try:
        run_id = run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)

        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            assert run is not None
            assert run.status == "success", run.notes
            assert run.candidates_found == 3
            assert run.postings_saved == 3
            assert run.finished_at is not None
            # The auto-extract pass now publishes a no-wage counter onto the ScraperRun
            # row. For the happy path it should be zero — every fake posting embeds a
            # parseable wage, so the mock LLM returns wage_low > 0 for all three.
            assert run.extraction_no_wage_found == 0
            # The notes line uses the new three-counter shape `extracted=ok/no-wage/failed`.
            assert "extracted=3/0/0" in run.notes

            postings = list(
                s.execute(
                    select(JobPosting).where(JobPosting.competitor_id == competitor_id)
                ).scalars()
            )
            assert len(postings) == 3
            # All postings should have a raw_html_path written under data/raw_html.
            # The scrape pipeline auto-runs LLM extraction at the end on the just-saved
            # postings, so wage_low IS populated when the HTML contains a parseable wage
            # (the FakeScraper HTML embeds "$18-$22/hr" / "$16/hr" / "$17/hr").
            for p in postings:
                assert p.raw_html_path and p.raw_html_path.startswith("data/raw_html/")
                assert p.wage_low is not None, "auto-extract should fill wage_low for parseable HTML"
                assert p.source_tier == "employer_owned"

            # Case-insensitive city match: the two Houston postings should share one CL.
            houston_ids = {
                p.competitor_location_id
                for p in postings
                if p.raw_title in ("Lot Attendant", "Loader")
            }
            assert len(houston_ids) == 1
    finally:
        SCRAPERS.pop(name, None)


def test_no_wage_vs_transport_failure_distinction(seeded_session, monkeypatch):
    """The auto-extract pass MUST distinguish "LLM responded but no wage on the page"
    (honest data outcome — Home Depot non-mandated state) from "LLM call blew up"
    (real bug — network, 4xx/5xx, parse error).

    Before this distinction existed, both were lumped into `extraction_failed` and an
    operator scanning the runs page couldn't tell at a glance whether to debug a real
    transport bug or accept that Home Depot just doesn't disclose pay in most states.

    The test asserts both the underlying counter (extract_postings_by_ids return shape)
    and the persisted ScraperRun.extraction_no_wage_found column. Crucially it asserts
    that no_wage_found and transport_failed are NOT collapsed together.
    """
    from app.services import ingestion as ingestion_mod
    from app.services.ingestion import extract_postings_by_ids

    name = "MixedFailCo"
    competitor_id = _make_competitor(name)
    SCRAPERS[name] = _FakeScraper
    with session_scope() as s:
        s.add(RoleMapping(
            competitor_id=competitor_id, copart_role="Yard Attendant",
            competitor_role="Warehouse Associate", bucket="outdoor", confidence=0.9,
        ))
    # Stub the geocoder so each FakeScraper city resolves to non-zero coords. Without
    # this the live Census Geocoder gets called for "Houston, TX" with no street and
    # returns no match → CL persists at (0,0) → the catchment filter (correctly)
    # drops the posting before save, breaking this test's "3 saved" precondition.
    monkeypatch.setattr(
        # Both cities resolve to LA-adjacent coords so they fall inside CA-LAX's catchment
        # regardless of which Copart yards are active at test time (test ordering can leave
        # only CA-LAX active by the time this runs). This test isn't about geography —
        # it's about the orchestration loop saving every posting it accepts.
        "app.services.scraping.geocode",
        lambda **kw: (34.05, -118.24) if kw.get("city") == "Los Angeles" else (34.06, -118.25),
    )

    # Patch extract_wage to return three different outcomes deterministically by
    # cycling through them in order, so over the three FakeScraper postings we get
    # one (a) no-wage, one (b) transport-fail, and one (c) success.
    state = {"i": 0}

    def _mixed_extract(html, raw_title, *, related_posting_id=None):
        i = state["i"]
        state["i"] += 1

        class _NoWage:
            validation_ok = True
            parsed = {"wage_low": None, "wage_high": None, "wage_unit": "hourly",
                      "role": raw_title, "confidence": 0.5, "reasoning": "page omits pay"}

        class _Ok:
            validation_ok = True
            parsed = {"wage_low": 21.0, "wage_high": 24.0, "wage_unit": "hourly",
                      "role": raw_title, "confidence": 0.9, "reasoning": "stub"}

        if i % 3 == 0:
            return _NoWage()
        if i % 3 == 1:
            raise RuntimeError("simulated upstream 502 from OpenRouter")
        return _Ok()

    monkeypatch.setattr("app.services.ingestion.llm.extract_wage", _mixed_extract)
    # Don't let classify_role muddy the LLM-failure signal — make it a no-op success.
    def _classify_stub(raw_title, *, related_posting_id=None):
        class _R:
            validation_ok = True
            parsed = {"normalized_role": raw_title, "bucket": "outdoor",
                      "confidence": 0.9, "reasoning": "stub"}
        return _R()
    monkeypatch.setattr("app.services.ingestion.llm.classify_role", _classify_stub)

    try:
        run_id = run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)

        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            assert run is not None
            assert run.status == "success"
            assert run.candidates_found == 3
            assert run.postings_saved == 3
            # The three counters MUST be distinct and the no-wage bucket MUST be > 0.
            # Both no-wage and transport-failed should appear AND not be collapsed.
            assert run.extraction_no_wage_found > 0, (
                "no-wage outcome must be tracked separately, not collapsed into failures"
            )
            # The notes line carries the breakdown — assert all three components show up.
            # With 3 postings cycling through (no-wage, raise, ok) we expect 1/1/1.
            assert "extracted=1/1/1" in run.notes, run.notes
            # And the no-wage and failed counters must NOT be the same number coming
            # from a lumped bucket — they're independent dimensions.
            assert run.extraction_no_wage_found == 1
            assert run.postings_saved == 3
    finally:
        SCRAPERS.pop(name, None)

    # Also exercise the underlying API directly: build a fresh set of postings and call
    # extract_postings_by_ids with the same mixed outcome generator, then assert the
    # returned dict carries all four keys and that no_wage_found / transport_failed are
    # both > 0 and distinct.
    state["i"] = 0  # reset the cycle

    with session_scope() as s:
        # Create three minimal postings pointing at real HTML files on disk. We reuse
        # the just-saved postings from the run above since they have raw_html_path set.
        from app.models import JobPosting as JP
        fresh_ids = [
            p.id
            for p in s.execute(
                select(JP).where(JP.competitor_id == competitor_id)
            ).scalars()
        ]
    assert len(fresh_ids) == 3

    summary = extract_postings_by_ids(fresh_ids)
    assert set(summary.keys()) == {"processed", "success", "no_wage_found", "transport_failed"}
    assert summary["processed"] == 3
    assert summary["no_wage_found"] > 0, "no-wage bucket must be populated"
    assert summary["transport_failed"] > 0, "transport-failed bucket must be populated"
    # The crux: the two outcomes are distinct counters, not a single "failed" lump.
    assert summary["no_wage_found"] + summary["transport_failed"] + summary["success"] == 3


def test_keywords_for_competitor_combines_scoped_and_global_mappings(seeded_session):
    """The service should aggregate competitor-specific + global (NULL competitor_id)
    RoleMapping rows above the confidence floor and return a sorted unique list."""
    competitor_id = _make_competitor("KwTestCo")
    with session_scope() as s:
        # scoped
        s.add(RoleMapping(competitor_id=competitor_id, copart_role="Yard Attendant",
                          competitor_role="Lot Associate", bucket="outdoor", confidence=0.9))
        # global
        s.add(RoleMapping(competitor_id=None, copart_role="Loader",
                          competitor_role="Loader", bucket="outdoor", confidence=0.95))
        # below floor — should be excluded
        s.add(RoleMapping(competitor_id=competitor_id, copart_role="Admin",
                          competitor_role="Junk Title", bucket="indoor", confidence=0.3))
        # scoped to a DIFFERENT competitor — must NOT bleed
        other_id = _make_competitor("OtherCo")
        s.add(RoleMapping(competitor_id=other_id, copart_role="CSR",
                          competitor_role="Should Not Appear", bucket="indoor", confidence=0.9))

    with session_scope() as s:
        kws = keywords_for_competitor(s, competitor_id)
    assert "Lot Associate" in kws
    assert "Loader" in kws
    assert "Junk Title" not in kws
    assert "Should Not Appear" not in kws
    assert kws == sorted(set(kws)), "expected sorted + unique"


def test_run_scrape_fails_when_no_role_mappings(seeded_session):
    """If a competitor has no role mappings (and no global ones either), the run fails
    fast with a clear message — we do NOT silently scrape with no keywords."""
    name = "EmptyMappingsCo"
    competitor_id = _make_competitor(name)
    SCRAPERS[name] = _FakeScraper
    try:
        # Wipe ALL RoleMapping rows so there's nothing scoped or global.
        with session_scope() as s:
            for m in list(s.execute(select(RoleMapping)).scalars()):
                s.delete(m)
        run_id = run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)
        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            assert run is not None
            assert run.status == "failed"
            assert "no role mappings" in run.notes.lower()
    finally:
        SCRAPERS.pop(name, None)


def test_scraped_locations_get_real_coords_not_zero_zero(seeded_session, monkeypatch):
    """A successful scrape must produce CompetitorLocation rows with non-(0,0) lat/lng.

    REGRESSION GUARD: before this test, the service wrote lat=0/lng=0 with a TODO
    comment, so every scraped posting ended up 6,000+ miles from every yard and the
    dashboard's 25-mi geographic filter silently excluded all of them.
    """
    name = "GeocodeTestCo"
    competitor_id = _make_competitor(name)
    SCRAPERS[name] = _FakeScraper
    with session_scope() as s:
        s.add(RoleMapping(
            competitor_id=competitor_id, copart_role="Yard Attendant",
            competitor_role="Warehouse Associate", bucket="outdoor", confidence=0.9,
        ))

    # Stub the geocoder to a known coord pair (LA-ish) so the test stays offline.
    monkeypatch.setattr(
        # Both cities resolve to LA-adjacent coords so they fall inside CA-LAX's catchment
        # regardless of which Copart yards are active at test time (test ordering can leave
        # only CA-LAX active by the time this runs). This test isn't about geography —
        # it's about the orchestration loop saving every posting it accepts.
        "app.services.scraping.geocode",
        lambda **kw: (34.05, -118.24) if kw.get("city") == "Los Angeles" else (34.06, -118.25),
    )

    try:
        run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)
        from app.models import CompetitorLocation
        with session_scope() as s:
            cls = list(s.execute(
                select(CompetitorLocation).where(CompetitorLocation.competitor_id == competitor_id)
            ).scalars())
            assert cls, "scraper should have created at least one CompetitorLocation"
            for cl in cls:
                assert not (cl.lat == 0.0 and cl.lng == 0.0), (
                    f"CompetitorLocation {cl.city},{cl.state} has placeholder (0,0) coords — "
                    "geocoding regression"
                )
    finally:
        SCRAPERS.pop(name, None)


def test_scraped_posting_near_active_yard_appears_in_observations(seeded_session, monkeypatch):
    """End-to-end: a scraped posting close to an active yard must show up in that yard's
    observations after the scraping pipeline finishes.

    This is the test that should have existed from day one — it asserts the *integration*
    between scraping, geocoding, the geographic filter, and the dashboard's observation
    query. The original location bug would have failed this loudly.
    """
    from app.models import CompetitorLocation, CopartLocation
    from app.services.market import observations_for_yard

    # Pick CA-LAX (Los Angeles) — coordinates already in the seed.
    with session_scope() as s:
        yard = s.execute(select(CopartLocation).where(CopartLocation.code == "CA-LAX")).scalar_one()
        s.execute(
            select(CopartLocation).where(CopartLocation.code == "CA-LAX")
        )
        # Activate it so the implicit "all active" code paths include it.
        yard.active = True

    # Stub the geocoder so it returns coords ~10 mi from the yard.
    yard_lat, yard_lng = 33.97, -118.24
    monkeypatch.setattr(
        "app.services.scraping.geocode",
        lambda **kw: (yard_lat + 0.1, yard_lng + 0.1),  # ~9 mi away
    )

    # Stub the LLM so extraction succeeds with a known wage on every posting.
    def _fake_extract(html, raw_title, *, related_posting_id=None):
        class _R:
            validation_ok = True
            parsed = {"wage_low": 19.0, "wage_high": 22.0, "wage_unit": "hourly",
                      "role": raw_title, "confidence": 0.9, "reasoning": "stub"}
        return _R()

    def _fake_classify(raw_title, *, related_posting_id=None):
        class _R:
            validation_ok = True
            parsed = {"normalized_role": raw_title, "bucket": "outdoor",
                      "confidence": 0.9, "reasoning": "stub"}
        return _R()

    monkeypatch.setattr("app.services.ingestion.llm.extract_wage", _fake_extract)
    monkeypatch.setattr("app.services.ingestion.llm.classify_role", _fake_classify)

    name = "NearbyCo"
    competitor_id = _make_competitor(name)
    SCRAPERS[name] = _FakeScraper
    with session_scope() as s:
        s.add(RoleMapping(
            competitor_id=competitor_id, copart_role="Yard Attendant",
            competitor_role="Warehouse Associate", bucket="outdoor", confidence=0.9,
        ))

    try:
        run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)
        with session_scope() as s:
            yard = s.execute(select(CopartLocation).where(CopartLocation.code == "CA-LAX")).scalar_one()
            obs = observations_for_yard(s, yard)
            # We don't care about ALL observations — just that AT LEAST ONE comes from our
            # scrape. Match on competitor name.
            from_us = [o for o in obs if o.competitor_name == name]
            assert from_us, (
                "expected at least one observation from the just-scraped competitor "
                f"({name}) near {yard.code} — the geographic filter rejected everything, "
                "which means the location bug is back"
            )
            assert all(o.wage_low > 0 for o in from_us), "scraped postings should have wages after auto-extraction"
    finally:
        SCRAPERS.pop(name, None)


def test_run_scrape_blocked_when_scraper_unavailable(seeded_session):
    """is_available() returning False produces a 'blocked' run row, no postings written."""
    class _UnavailableScraper(_FakeScraper):
        name = "BlockedCo"

        def is_available(self) -> bool:
            return False

    name = "BlockedCo"
    competitor_id = _make_competitor(name)
    SCRAPERS[name] = _UnavailableScraper
    try:
        before_count = 0
        with session_scope() as s:
            before_count = s.execute(
                select(JobPosting).where(JobPosting.competitor_id == competitor_id)
            ).scalars().all().__len__()

        run_id = run_scrape(competitor_id=competitor_id, triggered_by="test", async_mode=False)

        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            assert run is not None
            assert run.status == "blocked"
            assert run.finished_at is not None
            assert "not available" in run.notes.lower() or "blocked" in run.notes.lower()
            after_count = s.execute(
                select(JobPosting).where(JobPosting.competitor_id == competitor_id)
            ).scalars().all().__len__()
            assert after_count == before_count
    finally:
        SCRAPERS.pop(name, None)
