"""BLS OEWS (Occupational Employment & Wage Statistics) baseline reads.

Surfaces state-level BLS wage data as an authoritative anchor alongside the scraped
employer postings. NOT mixed into the inverse-distance weighted competitive blend
(different methodology — occupation × geography, not posting × distance).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BlsOewsWage

log = logging.getLogger(__name__)


def baseline_for(s: Session, state: str, *, bucket: str | None = None) -> list[dict[str, Any]]:
    q = select(BlsOewsWage).where(BlsOewsWage.state == state)
    if bucket:
        q = q.where(BlsOewsWage.bucket == bucket)
    q = q.order_by(BlsOewsWage.bucket, BlsOewsWage.occ_code)
    rows = list(s.execute(q).scalars())
    return [
        {
            "state": r.state,
            "occ_code": r.occ_code,
            "occ_title": r.occ_title,
            "bucket": r.bucket,
            "year": r.year,
            "mean_hourly": r.mean_hourly,
            "p10": r.p10, "p25": r.p25, "p50": r.p50, "p75": r.p75, "p90": r.p90,
        }
        for r in rows
    ]


def baseline_blended_p50(s: Session, state: str, bucket: str) -> float | None:
    """Median wage across the occupations in the given bucket for a state. Useful as a
    single comparison number against Copart's wage at a yard in that state."""
    rows = baseline_for(s, state, bucket=bucket)
    if not rows:
        return None
    values = [r["p50"] for r in rows if r["p50"]]
    if not values:
        return None
    return round(sum(values) / len(values), 2)
