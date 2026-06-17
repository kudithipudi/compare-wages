from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CbsaName, CopartLocation, Narrative
from app.services import bls
from app.services.market import (
    all_yard_summaries,
    competitor_benchmarks,
    last_observation_at,
    national_facts,
    state_rollup,
    total_live_observations,
    yard_summary,
)
from app.templating import templates

router = APIRouter()


def _latest_narrative(s: Session, scope: str, key: str) -> str:
    q = (
        select(Narrative)
        .where(Narrative.scope == scope, Narrative.scope_key == key)
        .order_by(Narrative.created_at.desc())
        .limit(1)
    )
    row = s.execute(q).scalar_one_or_none()
    return row.body if row else ""


_MAP_KEYS = ("yard", "rpp", "rpp_adjusted_copart_wage", "blended_competitive_wage",
             "rpp_adjusted_blended_wage", "gap", "observation_count",
             "pressure_quartile", "wage_quartile")


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, bucket: str | None = None, s: Session = Depends(get_db)):
    # One pass for the bucketed view used in tables + per-yard rows. We deliberately skip
    # packing per-yard observations into the JSON sent to the browser — the home-page map
    # doesn't use them, and they were ~95% of the page weight.
    summaries = all_yard_summaries(s, bucket=bucket, include_observations=False)

    # National KPI tiles + employer chart need unbucketed facts. When the filter is "all"
    # we can reuse the summaries we already computed. When a bucket is selected we need a
    # separate unbucketed pass for the facts only.
    if bucket:
        facts = national_facts(s)
    else:
        facts = national_facts(s, summaries=summaries)

    # Strip the bigger summaries dict to only the keys the map JSON needs.
    summaries_for_map = [{k: sm[k] for k in _MAP_KEYS} for sm in summaries]

    rollup = state_rollup(summaries)
    narrative = _latest_narrative(s, "national", "US")
    benchmarks = competitor_benchmarks(s, bucket=bucket)
    last_at = last_observation_at(s)
    return templates.TemplateResponse(
        request,
        "exec/overview.html",
        {
            "summaries": summaries,
            "summaries_for_map": summaries_for_map,
            "facts": facts,
            "state_rollup": rollup,
            "narrative": narrative,
            "bucket": bucket or "all",
            "benchmarks": benchmarks,
            "last_observation_at": last_at,
            "total_observations": sum(b["obs_count"] for b in benchmarks),
        },
    )


@router.get("/location/{code}", response_class=HTMLResponse)
def location_detail(code: str, request: Request, bucket: str | None = None, s: Session = Depends(get_db)):
    yard = s.execute(select(CopartLocation).where(CopartLocation.code == code)).scalar_one_or_none()
    if not yard:
        raise HTTPException(status_code=404, detail="location not found")
    summary = yard_summary(s, yard, bucket=bucket)
    summary["observations"].sort(key=lambda o: (o["distance_miles"], -o["midpoint_wage"]))
    bls_rows = bls.baseline_for(s, yard.state)
    bls_outdoor_p50 = bls.baseline_blended_p50(s, yard.state, "outdoor")
    bls_indoor_p50 = bls.baseline_blended_p50(s, yard.state, "indoor")
    cbsa_title = ""
    if yard.cbsa_code:
        row = s.get(CbsaName, yard.cbsa_code)
        cbsa_title = row.cbsa_title if row else ""
    return templates.TemplateResponse(
        request,
        "exec/location_detail.html",
        {
            "summary": summary,
            "bucket": bucket or "all",
            "bls_rows": bls_rows,
            "bls_outdoor_p50": bls_outdoor_p50,
            "bls_indoor_p50": bls_indoor_p50,
            "cbsa_code": yard.cbsa_code,
            "cbsa_title": cbsa_title,
            "last_observation_at": last_observation_at(s),
            "total_observations": total_live_observations(s),
        },
    )


@router.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request, s: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "exec/methodology.html",
        {
            "last_observation_at": last_observation_at(s),
            "total_observations": total_live_observations(s),
        },
    )
