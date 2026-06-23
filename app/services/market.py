"""Market analytics over collected job postings.

Public functions are read-only; they accept a SQLAlchemy session and return plain dicts
suitable for template rendering and JSON endpoints.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import BeaRpp, Competitor, CompetitorLocation, CopartLocation, JobPosting
from app.services.geo import haversine_miles

log = logging.getLogger(__name__)

# all_yard_summaries is on the home-page hot path. If it slips above this
# threshold the page goes from snappy to noticeably slow — emit a WARNING
# canary so the regression is visible on /admin/logs without instrumenting
# every caller.
_SUMMARIES_SLOW_THRESHOLD_S = 1.0


def _midpoint(p: JobPosting) -> float:
    if p.wage_low is None or p.wage_high is None:
        return 0.0
    return (p.wage_low + p.wage_high) / 2.0


@dataclass
class CompetitorWageObservation:
    competitor_id: int
    competitor_name: str
    competitor_location_id: int
    distance_miles: float
    midpoint_wage: float
    wage_low: float
    wage_high: float
    bucket: str
    source_tier: str
    source_url: str
    posting_id: int
    extraction_confidence: float


def observations_for_yard(
    s: Session, yard: CopartLocation, *, bucket: str | None = None, include_seed: bool = False
) -> list[CompetitorWageObservation]:
    """Find competitor wage observations within the geographic cutoff of a yard.

    By default this EXCLUDES synthetic seed-generated postings (source_tier='seed') so
    the dashboard only reflects real scraped data. Pass include_seed=True for demo runs
    or to show fallback data when nothing has been scraped yet.
    """
    cutoff = get_settings().distance_cutoff_miles
    q = (
        select(JobPosting, CompetitorLocation, Competitor)
        .join(CompetitorLocation, JobPosting.competitor_location_id == CompetitorLocation.id)
        .join(Competitor, JobPosting.competitor_id == Competitor.id)
        .where(JobPosting.wage_low.is_not(None))
    )
    if not include_seed:
        q = q.where(JobPosting.source_tier != "seed")
    if bucket:
        q = q.where(JobPosting.role_bucket == bucket)

    out: list[CompetitorWageObservation] = []
    for posting, cl, comp in s.execute(q).all():
        d = haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng)
        if d > cutoff:
            continue
        out.append(
            CompetitorWageObservation(
                competitor_id=comp.id,
                competitor_name=comp.name,
                competitor_location_id=cl.id,
                distance_miles=round(d, 2),
                midpoint_wage=round(_midpoint(posting), 2),
                wage_low=posting.wage_low,
                wage_high=posting.wage_high,
                bucket=posting.role_bucket or "",
                source_tier=posting.source_tier,
                source_url=posting.source_url,
                posting_id=posting.id,
                extraction_confidence=posting.extraction_confidence or 0.0,
            )
        )
    return out


def blended_competitive_wage(observations: list[CompetitorWageObservation]) -> float:
    """Inverse-distance weighted average of competitor midpoint wages.

    Mirrors the reference site's formula: Σ(wage_i / dist_i) / Σ(1 / dist_i).
    """
    num = 0.0
    den = 0.0
    for o in observations:
        d = max(o.distance_miles, 0.5)
        num += o.midpoint_wage / d
        den += 1.0 / d
    return round(num / den, 2) if den else 0.0


def yard_summary(s: Session, yard: CopartLocation, *, bucket: str | None = None) -> dict[str, Any]:
    obs = observations_for_yard(s, yard, bucket=bucket)
    blended = blended_competitive_wage(obs)
    rpp = s.get(BeaRpp, yard.state)
    rpp_factor = rpp.rpp if rpp else 1.0
    gap = round(blended - yard.copart_hourly_wage, 2) if blended else 0.0
    return {
        "yard": {
            "id": yard.id, "code": yard.code, "name": yard.name,
            "city": yard.city, "state": yard.state, "lat": yard.lat, "lng": yard.lng,
            "copart_wage": yard.copart_hourly_wage,
        },
        "rpp": round(rpp_factor, 3),
        "rpp_adjusted_copart_wage": round(yard.copart_hourly_wage / rpp_factor, 2),
        "blended_competitive_wage": blended,
        "rpp_adjusted_blended_wage": round(blended / rpp_factor, 2) if blended else 0.0,
        "gap": gap,
        "observation_count": len(obs),
        "observations": [o.__dict__ for o in obs],
    }


def _quartile(value: float, all_values: list[float]) -> int:
    if not all_values or value == 0:
        return 0
    sorted_vals = sorted(all_values)
    n = len(sorted_vals)
    cutoffs = [sorted_vals[int(n * 0.25)], sorted_vals[int(n * 0.5)], sorted_vals[int(n * 0.75)]]
    if value >= cutoffs[2]:
        return 1
    if value >= cutoffs[1]:
        return 2
    if value >= cutoffs[0]:
        return 3
    return 4


def all_yard_summaries(
    s: Session,
    *,
    bucket: str | None = None,
    include_observations: bool = True,
) -> list[dict[str, Any]]:
    """Compute summaries for every active yard in three queries instead of N+1.

    `include_observations=False` skips packing the per-yard observation list into the
    returned dicts — useful for the overview map, which only needs yard-level fields.
    """
    cutoff = get_settings().distance_cutoff_miles

    yards = list(s.execute(select(CopartLocation).where(CopartLocation.active.is_(True))).scalars())

    posting_q = (
        select(JobPosting, CompetitorLocation, Competitor)
        .join(CompetitorLocation, JobPosting.competitor_location_id == CompetitorLocation.id)
        .join(Competitor, JobPosting.competitor_id == Competitor.id)
        .where(JobPosting.wage_low.is_not(None))
    )
    if bucket:
        posting_q = posting_q.where(JobPosting.role_bucket == bucket)
    all_rows = list(s.execute(posting_q).all())

    rpp_map = {r.state: r.rpp for r in s.execute(select(BeaRpp)).scalars()}

    summaries: list[dict[str, Any]] = []
    for yard in yards:
        obs_list: list[CompetitorWageObservation] = []
        for posting, cl, comp in all_rows:
            d = haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng)
            if d > cutoff:
                continue
            obs_list.append(
                CompetitorWageObservation(
                    competitor_id=comp.id,
                    competitor_name=comp.name,
                    competitor_location_id=cl.id,
                    distance_miles=round(d, 2),
                    midpoint_wage=round(_midpoint(posting), 2),
                    wage_low=posting.wage_low,
                    wage_high=posting.wage_high,
                    bucket=posting.role_bucket or "",
                    source_tier=posting.source_tier,
                    source_url=posting.source_url,
                    posting_id=posting.id,
                    extraction_confidence=posting.extraction_confidence or 0.0,
                )
            )

        blended = blended_competitive_wage(obs_list)
        rpp_factor = rpp_map.get(yard.state, 1.0)
        gap = round(blended - yard.copart_hourly_wage, 2) if blended else 0.0

        summaries.append({
            "yard": {
                "id": yard.id, "code": yard.code, "name": yard.name,
                "city": yard.city, "state": yard.state, "lat": yard.lat, "lng": yard.lng,
                "copart_wage": yard.copart_hourly_wage,
            },
            "rpp": round(rpp_factor, 3),
            "rpp_adjusted_copart_wage": round(yard.copart_hourly_wage / rpp_factor, 2),
            "blended_competitive_wage": blended,
            "rpp_adjusted_blended_wage": round(blended / rpp_factor, 2) if blended else 0.0,
            "gap": gap,
            "observation_count": len(obs_list),
            "observations": [o.__dict__ for o in obs_list] if include_observations else [],
        })

    gap_values = [x["gap"] for x in summaries if x["gap"]]
    blended_values = [x["blended_competitive_wage"] for x in summaries if x["blended_competitive_wage"]]
    for sm in summaries:
        sm["pressure_quartile"] = _quartile(sm["gap"], gap_values)
        sm["wage_quartile"] = _quartile(sm["blended_competitive_wage"], blended_values)
    return summaries


def state_rollup(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_state: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sm in summaries:
        by_state[sm["yard"]["state"]].append(sm)
    rows: list[dict[str, Any]] = []
    for state, items in by_state.items():
        rows.append({
            "state": state,
            "location_count": len(items),
            "avg_copart_wage": round(mean(i["yard"]["copart_wage"] for i in items), 2),
            "avg_blended_competitive_wage": round(mean(i["blended_competitive_wage"] for i in items if i["blended_competitive_wage"]) if any(i["blended_competitive_wage"] for i in items) else 0.0, 2),
            "avg_gap": round(mean(i["gap"] for i in items), 2),
            "rpp": items[0]["rpp"],
        })
    rows.sort(key=lambda r: r["avg_gap"], reverse=True)
    all_gaps = [r["avg_gap"] for r in rows]
    for r in rows:
        r["pressure_quartile"] = _quartile(r["avg_gap"], all_gaps)
    return rows


def national_facts(s: Session, *, summaries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if summaries is None:
        summaries = all_yard_summaries(s)
    gaps_by_state = defaultdict(list)
    for sm in summaries:
        gaps_by_state[sm["yard"]["state"]].append(sm["gap"])
    avg_gap_by_state = {st: mean(v) for st, v in gaps_by_state.items() if v}

    employer_contrib: dict[str, list[float]] = defaultdict(list)
    for sm in summaries:
        for o in sm["observations"]:
            employer_contrib[o["competitor_name"]].append(o["midpoint_wage"] - sm["yard"]["copart_wage"])
    avg_employer_premium = {emp: round(mean(v), 2) for emp, v in employer_contrib.items() if v}

    highest = max(avg_gap_by_state.items(), key=lambda kv: kv[1], default=("—", 0))
    lowest = min(avg_gap_by_state.items(), key=lambda kv: kv[1], default=("—", 0))

    return {
        "location_count": len(summaries),
        "national_wage_gap": round(mean(sm["gap"] for sm in summaries), 2) if summaries else 0.0,
        "highest_pressure_state": highest[0],
        "highest_pressure_state_gap": round(highest[1], 2),
        "lowest_pressure_state": lowest[0],
        "lowest_pressure_state_gap": round(lowest[1], 2),
        "top_competitor_pressure": max(avg_employer_premium.items(), key=lambda kv: kv[1], default=("—", 0))[0],
        "avg_employer_premium": avg_employer_premium,
    }


def competitor_benchmarks(s: Session, *, bucket: str | None = None) -> list[dict[str, Any]]:
    """Per-competitor coverage strip for the home page.

    Independent of summaries so it works in the bucket-None branch (where summaries
    are computed without observations for payload reasons). Returns every known
    competitor — including ones with zero postings — so the strip stays at full
    width and missing coverage is visible.
    """
    cutoff = get_settings().distance_cutoff_miles
    competitors = list(s.execute(select(Competitor).order_by(Competitor.name)).scalars())
    active_yards = list(s.execute(select(CopartLocation).where(CopartLocation.active.is_(True))).scalars())

    q = (
        select(JobPosting, CompetitorLocation, Competitor)
        .join(CompetitorLocation, JobPosting.competitor_location_id == CompetitorLocation.id)
        .join(Competitor, JobPosting.competitor_id == Competitor.id)
        .where(JobPosting.wage_low.is_not(None))
        .where(JobPosting.source_tier != "seed")
    )
    if bucket:
        q = q.where(JobPosting.role_bucket == bucket)

    obs: dict[str, list[float]] = defaultdict(list)
    premiums: dict[str, list[float]] = defaultdict(list)
    for posting, cl, comp in s.execute(q).all():
        mid = _midpoint(posting)
        if not mid:
            continue
        obs[comp.name].append(mid)
        for yard in active_yards:
            d = haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng)
            if d <= cutoff:
                premiums[comp.name].append(mid - yard.copart_hourly_wage)

    rows: list[dict[str, Any]] = []
    for comp in competitors:
        w = obs.get(comp.name, [])
        p = premiums.get(comp.name, [])
        rows.append({
            "name": comp.name,
            "obs_count": len(w),
            "avg_posting_wage": round(mean(w), 2) if w else 0.0,
            "avg_premium": round(mean(p), 2) if p else 0.0,
            "source_tier": comp.source_tier,
        })
    rows.sort(key=lambda r: r["obs_count"], reverse=True)
    return rows


def last_observation_at(s: Session):
    """Most recent non-seed JobPosting.ingested_at, or None if no live data yet."""
    return s.execute(
        select(func.max(JobPosting.ingested_at)).where(JobPosting.source_tier != "seed")
    ).scalar()


def total_live_observations(s: Session) -> int:
    """Count of non-seed postings with an extracted wage — the unit feeding the model."""
    return s.execute(
        select(func.count(JobPosting.id))
        .where(JobPosting.source_tier != "seed")
        .where(JobPosting.wage_low.is_not(None))
    ).scalar() or 0


def total_live_postings(s: Session) -> int:
    """Count of all non-seed postings, regardless of wage extraction.

    Pairs with ``total_live_observations`` to surface the wage-extraction
    coverage on the freshness chip (``X wages from Y postings``).
    """
    return s.execute(
        select(func.count(JobPosting.id))
        .where(JobPosting.source_tier != "seed")
    ).scalar() or 0


def write_wage_snapshots(s: Session) -> int:
    """Snapshot every active yard's current blended wage + gap.

    Called at the end of each ingestion run by ``run_ingestion``. Reuses the
    already-computed all-yard summary so we don't re-do the IDW math here.
    Returns the count of snapshots written. Calls ``s.flush()`` so callers
    can immediately query the new rows back in the same session (tests + the
    overview render right after a manual snapshot trigger).
    """
    from app.models import WageSnapshot
    summaries = all_yard_summaries(s, include_observations=False)
    now = datetime.utcnow()
    for sm in summaries:
        s.add(WageSnapshot(
            yard_id=sm["yard"]["id"],
            captured_at=now,
            copart_wage=sm["yard"]["copart_wage"],
            blended_competitive_wage=sm["blended_competitive_wage"],
            gap=sm["gap"],
            observation_count=sm["observation_count"],
            pressure_quartile=sm.get("pressure_quartile", 0),
        ))
    s.flush()
    log.info("wage snapshots written: %d yards", len(summaries))
    return len(summaries)


def snapshot_series_for_yard(s: Session, yard_id: int, *, limit: int = 12) -> list[dict[str, Any]]:
    """Return the last N snapshots for a yard, oldest→newest. Suitable for
    sparkline rendering.
    """
    from app.models import WageSnapshot
    rows = list(s.execute(
        select(WageSnapshot)
        .where(WageSnapshot.yard_id == yard_id)
        .order_by(WageSnapshot.captured_at.desc())
        .limit(limit)
    ).scalars())
    rows.reverse()
    return [
        {
            "captured_at": r.captured_at.isoformat(),
            "blended_competitive_wage": r.blended_competitive_wage,
            "copart_wage": r.copart_wage,
            "gap": r.gap,
            "observation_count": r.observation_count,
        }
        for r in rows
    ]


def national_gap_series(s: Session, *, limit: int = 12) -> list[dict[str, Any]]:
    """Mean gap across all yards, bucketed by captured_at. Powers the
    overview sparkline. Returns oldest→newest list of ``{ts, gap, n_yards}``.
    """
    from app.models import WageSnapshot
    rows = list(s.execute(
        select(
            WageSnapshot.captured_at,
            func.avg(WageSnapshot.gap).label("avg_gap"),
            func.count(WageSnapshot.id).label("n_yards"),
        )
        .group_by(WageSnapshot.captured_at)
        .order_by(WageSnapshot.captured_at.desc())
        .limit(limit)
    ).all())
    rows.reverse()
    return [
        {
            "captured_at": r.captured_at.isoformat() if r.captured_at else "",
            "gap": round(float(r.avg_gap or 0.0), 2),
            "n_yards": int(r.n_yards or 0),
        }
        for r in rows
    ]
