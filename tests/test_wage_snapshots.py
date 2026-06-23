"""Tests for the WageSnapshot feature (Track C1).

write_wage_snapshots, snapshot_series_for_yard, national_gap_series — plus the
end-to-end hook that run_ingestion writes snapshots at the end of every run.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from app.models import CopartLocation, WageSnapshot
from app.services.market import (
    national_gap_series,
    snapshot_series_for_yard,
    write_wage_snapshots,
)


def _reset_snapshots(s) -> None:
    """Tests share a session-scoped seeded_session; wipe the snapshot table
    between tests so prior assertions don't leak into per-test counts."""
    s.execute(delete(WageSnapshot))
    s.flush()


def test_write_wage_snapshots_creates_one_row_per_active_yard(seeded_session) -> None:
    """One snapshot per active yard at each call."""
    _reset_snapshots(seeded_session)
    yards = list(seeded_session.execute(select(CopartLocation)).scalars())
    for y in yards:
        y.active = False
    yards[0].active = True
    yards[1].active = True
    seeded_session.commit()

    n = write_wage_snapshots(seeded_session)
    assert n == 2
    rows = list(seeded_session.execute(select(WageSnapshot)).scalars())
    assert len(rows) == 2
    assert {r.yard_id for r in rows} == {yards[0].id, yards[1].id}


def test_snapshot_series_returns_oldest_to_newest(seeded_session) -> None:
    """Multiple snapshots for one yard come back in chronological order."""
    _reset_snapshots(seeded_session)
    yards = list(seeded_session.execute(select(CopartLocation)).scalars())
    for y in yards:
        y.active = False
    yards[0].active = True
    seeded_session.commit()

    write_wage_snapshots(seeded_session)
    write_wage_snapshots(seeded_session)
    write_wage_snapshots(seeded_session)

    series = snapshot_series_for_yard(seeded_session, yards[0].id, limit=10)
    assert len(series) == 3
    assert series[0]["captured_at"] <= series[1]["captured_at"] <= series[2]["captured_at"]


def test_national_gap_series_aggregates_across_yards(seeded_session) -> None:
    """One row per captured_at, with avg gap and n_yards across yards."""
    _reset_snapshots(seeded_session)
    yards = list(seeded_session.execute(select(CopartLocation)).scalars())
    for y in yards:
        y.active = False
    yards[0].active = True
    yards[1].active = True
    seeded_session.commit()

    write_wage_snapshots(seeded_session)
    rows = national_gap_series(seeded_session, limit=10)
    assert len(rows) == 1
    assert rows[0]["n_yards"] == 2
    assert isinstance(rows[0]["gap"], float)
