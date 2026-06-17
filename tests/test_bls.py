from sqlalchemy import func, select

from app.models import BlsOewsWage
from app.services import bls


def test_seed_loads_bls_rows(seeded_session):
    count = seeded_session.execute(select(func.count()).select_from(BlsOewsWage)).scalar_one()
    assert count >= 250


def test_baseline_for_known_state_returns_five_rows(seeded_session):
    rows = bls.baseline_for(seeded_session, "CA")
    assert len(rows) == 5
    occ_codes = {r["occ_code"] for r in rows}
    assert occ_codes == {"53-7062", "53-7065", "53-3033", "41-2011", "43-4051"}


def test_baseline_for_unknown_state_is_empty(seeded_session):
    assert bls.baseline_for(seeded_session, "ZZ") == []


def test_baseline_for_with_bucket_filter(seeded_session):
    rows = bls.baseline_for(seeded_session, "CA", bucket="outdoor")
    assert rows
    assert all(r["bucket"] == "outdoor" for r in rows)


def test_baseline_blended_p50(seeded_session):
    p50 = bls.baseline_blended_p50(seeded_session, "CA", "outdoor")
    assert isinstance(p50, float)
    assert p50 > 0
    assert bls.baseline_blended_p50(seeded_session, "ZZ", "outdoor") is None
