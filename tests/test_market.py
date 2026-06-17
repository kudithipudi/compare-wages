from sqlalchemy import select

from app.models import CopartLocation
from app.services.ingestion import run_ingestion
from app.services.market import (
    CompetitorWageObservation,
    all_yard_summaries,
    blended_competitive_wage,
    national_facts,
    state_rollup,
    yard_summary,
)


def _ingest_once(state={"done": False}):
    if not state["done"]:
        run_ingestion(triggered_by="test")
        state["done"] = True


def _obs(d, w):
    return CompetitorWageObservation(
        competitor_id=1, competitor_name="X", competitor_location_id=1,
        distance_miles=d, midpoint_wage=w, wage_low=w, wage_high=w,
        bucket="outdoor", source_tier="employer_owned", source_url="",
        posting_id=1, extraction_confidence=1.0,
    )


def test_blended_competitive_wage_inverse_distance():
    obs = [_obs(1.0, 20.0), _obs(2.0, 22.0), _obs(4.0, 18.0)]
    num = 20.0 / 1.0 + 22.0 / 2.0 + 18.0 / 4.0
    den = 1.0 / 1.0 + 1.0 / 2.0 + 1.0 / 4.0
    expected = round(num / den, 2)
    assert blended_competitive_wage(obs) == expected


def test_blended_empty_returns_zero():
    assert blended_competitive_wage([]) == 0.0


def test_yard_summary_keys_and_gap(seeded_session):
    _ingest_once()
    yard = seeded_session.execute(select(CopartLocation)).scalars().first()
    sm = yard_summary(seeded_session, yard)
    for key in ("yard", "rpp", "blended_competitive_wage", "gap", "observation_count", "observations"):
        assert key in sm
    if sm["blended_competitive_wage"]:
        assert sm["gap"] == round(sm["blended_competitive_wage"] - yard.copart_hourly_wage, 2)


def test_all_yard_summaries_one_per_active_yard(seeded_session):
    _ingest_once()
    n_active = seeded_session.execute(
        select(CopartLocation).where(CopartLocation.active.is_(True))
    ).scalars().all()
    summaries = all_yard_summaries(seeded_session)
    assert len(summaries) == len(n_active)
    for sm in summaries:
        assert sm["pressure_quartile"] in {0, 1, 2, 3, 4}


def test_state_rollup_sorted_desc(seeded_session):
    _ingest_once()
    summaries = all_yard_summaries(seeded_session)
    rows = state_rollup(summaries)
    gaps = [r["avg_gap"] for r in rows]
    assert gaps == sorted(gaps, reverse=True)


def test_national_facts_required_keys(seeded_session):
    _ingest_once()
    facts = national_facts(seeded_session)
    for key in (
        "location_count", "national_wage_gap",
        "highest_pressure_state", "lowest_pressure_state",
        "top_competitor_pressure", "avg_employer_premium",
    ):
        assert key in facts
