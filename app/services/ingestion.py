"""Orchestrates: pending postings → wage extraction → role classification → persisted result.

This is the deterministic backbone; LLMs are called as functional steps inside it.

A run can be scoped to a subset of yards via `yard_ids`. Scoping is geographic: every
competitor location within `DISTANCE_CUTOFF_MILES` (Haversine) of any selected yard has
its postings re-extracted. National narrative is always regenerated — it reads the full
metric store, so it stays nationally coherent regardless of scope.

`async_mode=True` returns the ScrapeRun id immediately and processes in a daemon thread.
The thread commits progress (`postings_collected`, `extraction_success`,
`extraction_failed`) every few postings so the admin UI can show live progress.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import CompetitorLocation, CopartLocation, JobPosting, Narrative, ScrapeRun
from app.services import llm
from app.services.geo import haversine_miles
from app.services.market import national_facts

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# How often the background thread commits progress to the ScrapeRun row.
PROGRESS_FLUSH_EVERY = 3


def _load_html(posting: JobPosting) -> str:
    if posting.raw_html_path:
        p = REPO_ROOT / posting.raw_html_path
        if p.exists():
            return p.read_text()
    return ""


def _competitor_location_ids_near(s, yards: list[CopartLocation]) -> set[int]:
    """Return the set of competitor_location.id within DISTANCE_CUTOFF_MILES of any yard."""
    cutoff = get_settings().distance_cutoff_miles
    cls = list(s.execute(select(CompetitorLocation)).scalars())
    keep: set[int] = set()
    for cl in cls:
        for yard in yards:
            if haversine_miles(yard.lat, yard.lng, cl.lat, cl.lng) <= cutoff:
                keep.add(cl.id)
                break
    return keep


def _resolve_scope(yard_ids: list[int] | None) -> tuple[str, set[int]]:
    """Return (scope_yard_codes_string, in_range_competitor_location_ids)."""
    explicit_scope = yard_ids is not None
    with session_scope() as s:
        if explicit_scope:
            yards = list(
                s.execute(select(CopartLocation).where(CopartLocation.id.in_(yard_ids))).scalars()
            )
            scope_codes = ",".join(sorted(y.code for y in yards))
        else:
            yards = list(
                s.execute(select(CopartLocation).where(CopartLocation.active.is_(True))).scalars()
            )
            scope_codes = ""
        in_range = _competitor_location_ids_near(s, yards)
    return scope_codes, in_range


def _create_run(triggered_by: str, scope_codes: str, *, status: str = "running",
                notes: str = "", finished: bool = False) -> int:
    with session_scope() as s:
        run = ScrapeRun(
            triggered_by=triggered_by,
            status=status,
            scope_yard_codes=scope_codes,
            notes=notes,
        )
        if finished:
            run.finished_at = datetime.utcnow()
        s.add(run)
        s.flush()
        return run.id


def _process_run(run_id: int, *, yard_ids: list[int] | None, refresh_all: bool) -> None:
    """The body of an ingestion run. Synchronous; called both inline and in a thread."""
    explicit_scope = yard_ids is not None
    scope_codes, in_range_cl_ids = _resolve_scope(yard_ids)

    # Pull the list of postings to work on.
    with session_scope() as s:
        q = select(JobPosting)
        if not refresh_all:
            q = q.where(JobPosting.wage_low.is_(None))
        if not in_range_cl_ids:
            posting_ids: list[int] = []
        else:
            q = q.where(JobPosting.competitor_location_id.in_(in_range_cl_ids))
            posting_ids = [p.id for p in s.execute(q).scalars()]

    total = len(posting_ids)
    success = 0
    no_wage_found = 0
    transport_failed = 0
    processed = 0

    # Stamp initial progress so the UI knows the denominator.
    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        run.postings_collected = 0
        run.notes = f"scope={scope_codes or 'all-active'} planned={total}"

    for pid in posting_ids:
        try:
            with session_scope() as s:
                p = s.get(JobPosting, pid)
                if p is None:
                    # No posting row to extract from — treat as a transport-style failure
                    # (the work item itself is broken, not an honest no-wage outcome).
                    transport_failed += 1
                    processed += 1
                    continue
                html = _load_html(p)
                if not html:
                    # Missing raw HTML on disk = infrastructure problem, not a no-wage hit.
                    transport_failed += 1
                    processed += 1
                    continue

                try:
                    extr = llm.extract_wage(html, p.raw_title, related_posting_id=p.id)
                except Exception:
                    # LLM call raised → transport failure (network, 4xx/5xx, parse).
                    transport_failed += 1
                    processed += 1
                    continue

                if not extr.validation_ok:
                    # Schema-validation failed → the model's output was unusable; that's
                    # a transport-class problem (bad JSON, missing required keys, etc.).
                    transport_failed += 1
                    processed += 1
                    continue

                wage_low_val = extr.parsed.get("wage_low")
                if not wage_low_val:
                    # Model parsed cleanly but reported no wage on the page. This is the
                    # Home Depot / non-mandated-state reality — record it distinctly.
                    no_wage_found += 1
                    processed += 1
                    continue

                p.wage_low = float(wage_low_val)
                p.wage_high = float(extr.parsed.get("wage_high", wage_low_val))
                p.wage_unit = extr.parsed.get("wage_unit") or "hourly"
                p.extraction_confidence = float(extr.parsed.get("confidence") or 0.5)

                try:
                    cls = llm.classify_role(p.raw_title, related_posting_id=p.id)
                    if cls.validation_ok:
                        p.normalized_role = cls.parsed.get("normalized_role") or p.raw_title
                        p.role_bucket = cls.parsed.get("bucket")
                        p.classification_confidence = float(cls.parsed.get("confidence") or 0.5)
                except Exception:
                    pass

                success += 1
                processed += 1
        except Exception:
            # Outermost catch — DB hiccup, etc. Bucket as transport failure.
            transport_failed += 1
            processed += 1

        # Periodically flush progress so the UI's auto-refresh has fresh numbers.
        if processed % PROGRESS_FLUSH_EVERY == 0 or processed == total:
            with session_scope() as s:
                run = s.get(ScrapeRun, run_id)
                run.postings_collected = processed
                run.extraction_success = success
                run.extraction_no_wage_found = no_wage_found
                run.extraction_failed = transport_failed

    # Regenerate the national narrative against the current full metric store.
    try:
        with session_scope() as s:
            facts = national_facts(s)
            narr_result = llm.generate_narrative(facts)
            s.add(
                Narrative(
                    scope="national",
                    scope_key="US",
                    body=narr_result.parsed.get("body", ""),
                    grounding=facts,
                )
            )
    except Exception:
        # Narrative failure shouldn't fail the whole run — extraction work is the meat.
        pass

    with session_scope() as s:
        run = s.get(ScrapeRun, run_id)
        run.finished_at = datetime.utcnow()
        # A run that finds no wages but had no transport errors is still a SUCCESS — the
        # pipeline did its job and learned that none of those postings disclose pay.
        # Only flip to failed when we processed work AND every item blew up transport-wise.
        if processed == 0:
            run.status = "success"
        elif success > 0 or no_wage_found > 0:
            run.status = "success"
        else:
            run.status = "failed"
        run.postings_collected = processed
        run.extraction_success = success
        run.extraction_no_wage_found = no_wage_found
        run.extraction_failed = transport_failed
        scope_label = scope_codes if explicit_scope else "all-active"
        run.notes = (
            f"scope={scope_label} processed={processed} "
            f"ok={success} no-wage={no_wage_found} failed={transport_failed}"
        )


def run_ingestion(
    *,
    triggered_by: str = "manual",
    refresh_all: bool = True,
    yard_ids: list[int] | None = None,
    async_mode: bool = False,
) -> int:
    """Run ingestion. Returns the ScrapeRun id.

    `async_mode=True` creates the run row, kicks off a daemon thread, and returns
    immediately so the admin UI can render a progress page that auto-refreshes.
    """
    if yard_ids is not None and len(yard_ids) == 0:
        return _create_run(
            triggered_by, scope_codes="",
            status="failed", finished=True, notes="no yards selected",
        )

    scope_codes, _ = _resolve_scope(yard_ids)
    run_id = _create_run(triggered_by, scope_codes, status="running")

    if async_mode:
        t = threading.Thread(
            target=_process_run,
            args=(run_id,),
            kwargs={"yard_ids": yard_ids, "refresh_all": refresh_all},
            daemon=True,
        )
        t.start()
        return run_id

    _process_run(run_id, yard_ids=yard_ids, refresh_all=refresh_all)
    return run_id


def extract_postings_by_ids(posting_ids: list[int]) -> dict[str, int]:
    """Run wage extraction + classification on a specific set of postings, ignoring
    yard/active filters. Used by the scrape pipeline to immediately enrich just-scraped
    postings so the operator sees wages without a second Run Now click.

    Returns a four-counter breakdown so callers can distinguish honest "no wage on the
    page" outcomes (Home Depot outside mandated states) from real transport bugs
    (LLM 4xx/5xx, network, parse failure):

        {
          "processed":         total postings attempted,
          "success":           LLM parsed AND returned a usable wage_low,
          "no_wage_found":     LLM parsed OK but wage_low was null/0 (honest miss),
          "transport_failed":  LLM call raised OR validation failed (real bug),
        }
    """
    success = no_wage_found = transport_failed = processed = 0
    for pid in posting_ids:
        processed += 1
        try:
            with session_scope() as s:
                p = s.get(JobPosting, pid)
                if p is None:
                    # Stale ID — no posting to extract. Treat as transport-style failure
                    # so it's surfaced as a real bug (likely a race or deletion).
                    transport_failed += 1
                    continue
                html = _load_html(p)
                if not html:
                    # Missing raw HTML on disk = infrastructure problem, not a no-wage hit.
                    transport_failed += 1
                    continue
                try:
                    extr = llm.extract_wage(html, p.raw_title, related_posting_id=p.id)
                except Exception:
                    # LLM call raised → network/4xx/5xx/parse exception.
                    transport_failed += 1
                    continue
                if not extr.validation_ok:
                    # Model responded but the response failed schema validation. That's
                    # a bug surface (bad JSON, missing keys), not an honest no-wage outcome.
                    transport_failed += 1
                    continue
                wage_low_val = extr.parsed.get("wage_low")
                if not wage_low_val:
                    # Clean LLM parse but no wage disclosed on the page. Reality, not a bug.
                    no_wage_found += 1
                    continue
                p.wage_low = float(wage_low_val)
                p.wage_high = float(extr.parsed.get("wage_high", wage_low_val))
                p.wage_unit = extr.parsed.get("wage_unit") or "hourly"
                p.extraction_confidence = float(extr.parsed.get("confidence") or 0.5)
                try:
                    cls = llm.classify_role(p.raw_title, related_posting_id=p.id)
                    if cls.validation_ok:
                        p.normalized_role = cls.parsed.get("normalized_role") or p.raw_title
                        p.role_bucket = cls.parsed.get("bucket")
                        p.classification_confidence = float(cls.parsed.get("confidence") or 0.5)
                except Exception:
                    pass
                success += 1
        except Exception:
            # Outermost catch — DB hiccup, session error, etc. Bucket as transport.
            transport_failed += 1
    return {
        "processed": processed,
        "success": success,
        "no_wage_found": no_wage_found,
        "transport_failed": transport_failed,
    }


def mark_orphaned_runs_failed() -> int:
    """On boot, runs left in `status=running` are interrupted by a previous restart.
    Mark them failed so the UI doesn't show a phantom in-flight run forever.
    """
    with session_scope() as s:
        running = list(s.execute(select(ScrapeRun).where(ScrapeRun.status == "running")).scalars())
        for r in running:
            r.status = "failed"
            r.finished_at = datetime.utcnow()
            r.notes = (r.notes or "") + " | interrupted by server restart"
        return len(running)
