from sqlalchemy import select, update

from app.config import get_settings
from app.db import session_scope
from app.models import CompetitorLocation, CopartLocation, JobPosting, Narrative, ScrapeRun
from app.services.geo import haversine_miles
from app.services.ingestion import mark_orphaned_runs_failed, run_ingestion


def test_ingestion_end_to_end(seeded_session):
    # Seed defaults yards to inactive; activate everything so the implicit full run
    # exercises the same code path as before this change.
    with session_scope() as s:
        s.execute(update(CopartLocation).values(active=True))

    run_id = run_ingestion(triggered_by="test")

    postings = list(seeded_session.execute(select(JobPosting)).scalars())
    assert postings, "expected seeded postings"
    for p in postings:
        assert p.wage_low is not None
        assert p.wage_high is not None
        assert p.role_bucket is not None

    narrative = seeded_session.execute(
        select(Narrative).where(Narrative.scope == "national")
    ).scalars().first()
    assert narrative is not None

    run = seeded_session.get(ScrapeRun, run_id)
    assert run.status == "success"
    assert run.extraction_success > 0


def test_ingestion_implicit_full_only_touches_active_yards(seeded_session):
    """Default Run Now (no yard_ids) processes postings near ACTIVE yards only."""
    # Start with all-inactive (seed default) — implicit run should be a no-op.
    with session_scope() as s:
        s.execute(update(CopartLocation).values(active=False))
        s.execute(update(JobPosting).values(wage_low=None, wage_high=None, role_bucket=None))

    run_id = run_ingestion(triggered_by="test")
    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        assert run.postings_collected == 0
        assert run.extraction_success == 0
        assert run.status == "success"  # nothing to do is not a failure

    # Activate just CA-LAX. Run again → only its in-range postings get processed.
    with session_scope() as s:
        s.execute(update(CopartLocation).where(CopartLocation.code == "CA-LAX").values(active=True))
        yard = s.execute(select(CopartLocation).where(CopartLocation.code == "CA-LAX")).scalar_one()
        cutoff = get_settings().distance_cutoff_miles
        cls = list(s.execute(select(CompetitorLocation)).scalars())
        in_range_ids = {cl.id for cl in cls if haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng) <= cutoff}
        expected = (
            s.execute(select(JobPosting).where(JobPosting.competitor_location_id.in_(in_range_ids)))
            .scalars().all().__len__()
        )

    run_id = run_ingestion(triggered_by="test")
    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        assert run.extraction_success == expected
        assert run.scope_yard_codes == ""  # implicit, not explicit
        with_wage = (
            s.execute(select(JobPosting).where(JobPosting.wage_low.is_not(None)))
            .scalars().all().__len__()
        )
        assert with_wage == expected


def _pick_yard_with_postings(s) -> CopartLocation:
    """Pick the first yard (active or not) with at least one in-range competitor location."""
    cutoff = get_settings().distance_cutoff_miles
    yards = list(s.execute(select(CopartLocation)).scalars())
    cls = list(s.execute(select(CompetitorLocation)).scalars())
    for yard in yards:
        for cl in cls:
            if haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng) <= cutoff:
                return yard
    raise AssertionError("no yard with in-range competitor locations in seeded data")


def _reset_wages(s) -> None:
    s.execute(
        update(JobPosting).values(
            wage_low=None, wage_high=None, wage_unit=None,
            extraction_confidence=None, role_bucket=None,
            normalized_role=None, classification_confidence=None,
        )
    )


def test_ingestion_yard_scoped_only_touches_in_range(seeded_session):
    cutoff = get_settings().distance_cutoff_miles
    yard = _pick_yard_with_postings(seeded_session)

    # In-range competitor locations for this yard
    cls = list(seeded_session.execute(select(CompetitorLocation)).scalars())
    in_range_ids = {cl.id for cl in cls if haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng) <= cutoff}

    # Pick at least one CL definitely OUT of range so we can assert non-interference.
    out_of_range_ids = {cl.id for cl in cls if cl.id not in in_range_ids}
    assert in_range_ids and out_of_range_ids, "test fixture must contain both in- and out-of-range CLs"

    # Reset wages so we can observe the effect of the scoped run.
    with session_scope() as s:
        _reset_wages(s)

    run_id = run_ingestion(triggered_by="test", yard_ids=[yard.id])

    with session_scope() as s:
        # Every posting at an in-range CL should now have wage_low set.
        in_range_postings = list(
            s.execute(select(JobPosting).where(JobPosting.competitor_location_id.in_(in_range_ids))).scalars()
        )
        assert in_range_postings, "expected at least one in-range posting"
        assert all(p.wage_low is not None for p in in_range_postings)

        # An out-of-range posting must remain untouched.
        out_postings = list(
            s.execute(select(JobPosting).where(JobPosting.competitor_location_id.in_(out_of_range_ids))).scalars()
        )
        assert any(p.wage_low is None for p in out_postings), \
            "scoped run incorrectly touched out-of-range postings"

        run = s.get(ScrapeRun, run_id)
        assert run.extraction_success == len(in_range_postings)
        assert run.status == "success"


def test_ingestion_yard_scoped_records_scope_on_run(seeded_session):
    yard = _pick_yard_with_postings(seeded_session)
    run_id = run_ingestion(triggered_by="test", yard_ids=[yard.id])
    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        assert run.scope_yard_codes == yard.code
        assert yard.code in run.notes


def test_ingestion_yard_scoped_regenerates_narrative(seeded_session):
    yard = _pick_yard_with_postings(seeded_session)
    with session_scope() as s:
        before_id = s.execute(
            select(Narrative.id).where(Narrative.scope == "national").order_by(Narrative.id.desc())
        ).scalar()

    run_ingestion(triggered_by="test", yard_ids=[yard.id])

    with session_scope() as s:
        after_id = s.execute(
            select(Narrative.id).where(Narrative.scope == "national").order_by(Narrative.id.desc())
        ).scalar()
    assert after_id is not None
    if before_id is not None:
        assert after_id > before_id, "narrative should be regenerated after a scoped run"


def test_ingestion_yard_scoped_ignores_active_flag(seeded_session):
    """Explicit yard_ids honors the operator's choice even for inactive yards."""
    with session_scope() as s:
        s.execute(update(CopartLocation).values(active=False))  # all inactive
        s.execute(update(JobPosting).values(wage_low=None))

    yard = _pick_yard_with_postings(seeded_session)
    run_ingestion(triggered_by="test", yard_ids=[yard.id])
    with session_scope() as s:
        with_wage = (
            s.execute(select(JobPosting).where(JobPosting.wage_low.is_not(None)))
            .scalars().all().__len__()
        )
        assert with_wage > 0, "explicit yard scope must work even if yard is inactive"


def test_async_ingestion_returns_run_id_immediately_and_finishes(seeded_session):
    """async_mode=True returns the ScrapeRun id while a daemon thread processes."""
    import time as _time
    with session_scope() as s:
        s.execute(update(CopartLocation).where(CopartLocation.code == "CA-LAX").values(active=True))

    run_id = run_ingestion(triggered_by="test", async_mode=True)
    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        assert run is not None
        assert run.status == "running"

    # Wait briefly for the thread to finish.
    deadline = _time.time() + 30
    while _time.time() < deadline:
        with session_scope() as s:
            run = s.get(ScrapeRun, run_id)
            if run.status in ("success", "failed"):
                break
        _time.sleep(0.1)

    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        assert run.status == "success"
        assert run.extraction_success > 0
        assert run.finished_at is not None


def test_mark_orphaned_runs_failed_handles_interrupted_runs(seeded_session):
    with session_scope() as s:
        # Manufacture a stuck "running" row.
        orphan = ScrapeRun(triggered_by="test", status="running", scope_yard_codes="")
        s.add(orphan)
        s.flush()
        orphan_id = orphan.id

    n = mark_orphaned_runs_failed()
    assert n >= 1
    with session_scope() as s:
        orphan = s.get(ScrapeRun, orphan_id)
        assert orphan.status == "failed"
        assert orphan.finished_at is not None
        assert "interrupted" in orphan.notes.lower()


def test_ingestion_yard_scoped_empty_list_fails_cleanly(seeded_session):
    with session_scope() as s:
        before_count = s.execute(select(JobPosting)).scalars().all().__len__()

    run_id = run_ingestion(triggered_by="test", yard_ids=[])

    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        assert run.status == "failed"
        assert run.extraction_success == 0
        assert run.scope_yard_codes == ""
        assert "no yards selected" in run.notes.lower()
        after_count = s.execute(select(JobPosting)).scalars().all().__len__()
        assert before_count == after_count
