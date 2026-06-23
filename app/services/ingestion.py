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

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.log_context import operation_context
from app.models import CompetitorLocation, CopartLocation, JobPosting, Narrative, ScrapeRun
from app.services import llm
from app.services.geo import haversine_miles
from app.services.market import national_facts, write_wage_snapshots

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# How often the background thread commits progress to the ScrapeRun row.
PROGRESS_FLUSH_EVERY = 3

# How often (in postings) we emit an INFO progress log. Distinct from the DB
# flush cadence above — log lines are cheap, DB writes aren't, so we log more
# often than we commit. 25 keeps a typical 200-posting run at ~8 progress
# lines, which is enough to follow on /admin/logs without flooding it.
LOG_PROGRESS_EVERY = 25


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


ExtractOutcome = Literal["success", "no_wage", "transport"]


def _extract_one(s, posting_id: int) -> ExtractOutcome:
    """Extract wage + classify role for one posting. Returns the outcome bucket.

    Outcomes (deliberately three — counters keep the same four-bucket shape because
    ``processed`` is just the call count):

    * ``"success"``    — LLM parsed cleanly AND returned a usable wage_low; row mutated
                         with wage_low/high/unit/confidence and (best-effort) classification.
    * ``"no_wage"``    — LLM parsed cleanly but the page disclosed no wage. Honest miss
                         (Home Depot outside mandated states is the canonical example).
    * ``"transport"``  — Posting row missing, raw HTML on disk missing, LLM call raised,
                         OR the model's response failed schema validation. All of these
                         are infrastructure-class problems, not honest no-wage outcomes.

    The classification call is best-effort: if it fails, we still return ``"success"``.
    The DB row is mutated through the caller's session — no commit here; ``session_scope``
    in the caller is responsible for that.
    """
    p = s.get(JobPosting, posting_id)
    if p is None:
        return "transport"
    html = _load_html(p)
    if not html:
        return "transport"
    try:
        extr = llm.extract_wage(html, p.raw_title, related_posting_id=p.id)
    except Exception:
        return "transport"
    if not extr.validation_ok:
        return "transport"
    wage_low_val = extr.parsed.get("wage_low")
    if not wage_low_val:
        return "no_wage"

    p.wage_low = float(wage_low_val)
    p.wage_high = float(extr.parsed.get("wage_high", wage_low_val))
    p.wage_unit = extr.parsed.get("wage_unit") or "hourly"
    p.extraction_confidence = float(extr.parsed.get("confidence") or 0.5)

    try:
        cls = llm.classify_role(p.raw_title, related_posting_id=p.id)
        if cls.validation_ok:
            p.normalized_role = cls.parsed.get("normalized_role") or p.raw_title
            p.role_bucket = cls.parsed.get("bucket")
    except Exception:
        pass

    return "success"


def _process_run(run_id: int, *, yard_ids: list[int] | None, refresh_all: bool) -> None:
    """The body of an ingestion run. Synchronous; called both inline and in a thread."""
    started = time.perf_counter()
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
                outcome = _extract_one(s, pid)
        except Exception:
            # Outermost catch — DB hiccup, session error, etc. Bucket as transport.
            outcome = "transport"

        if outcome == "success":
            success += 1
        elif outcome == "no_wage":
            no_wage_found += 1
        else:
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

        # Coarser INFO progress log every LOG_PROGRESS_EVERY postings (or on the
        # final item) so an operator following /admin/logs sees forward motion
        # without a wall of lines.
        if processed and (processed % LOG_PROGRESS_EVERY == 0 or processed == total):
            log.info(
                "extract progress: %d/%d processed (success=%d no_wage=%d failed=%d)",
                processed, total, success, no_wage_found, transport_failed,
            )

    # Write wage snapshots BEFORE the narrative regenerates so the narrative
    # query (`national_facts`) and the snapshot query both read the same just-
    # extracted state. Snapshot failure shouldn't fail the run either.
    try:
        with session_scope() as s:
            write_wage_snapshots(s)
    except Exception as exc:
        log.warning("wage snapshot write failed: %s: %s", type(exc).__name__, exc)

    # Regenerate the national narrative against the current full metric store.
    log.info("regenerating national narrative")
    narrative_failure: str | None = None
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
    except Exception as exc:
        # Narrative failure shouldn't fail the whole run — extraction work is the
        # meat. Used to be a silent swallow; now we log loudly AND stamp the
        # ScrapeRun row so operators see narrative regressions on /admin/logs
        # AND on /admin/runs/{id} without having to combine the two surfaces.
        log.warning("narrative generation failed: %s: %s", type(exc).__name__, exc)
        narrative_failure = f"{type(exc).__name__}: {exc}"[:200]

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
        if narrative_failure:
            run.notes += f" | narrative_failed: {narrative_failure}"

    duration = time.perf_counter() - started
    # Wages is success+no_wage_found — both are clean LLM parses, and that's the
    # number an operator cares about for "how much new info did this run yield".
    log.info(
        "run_ingestion complete: postings=%d wages=%d duration=%.1fs",
        processed, success + no_wage_found, duration,
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

    The entire run runs inside an :func:`operation_context` so every log line
    emitted by ``_process_run`` (and the LLM calls it makes) carries the same
    ``op_id`` tag. Daemon threads inherit the parent's ``contextvars`` — see
    the docstring on ``app.log_context`` for why that's correct here.
    """
    with operation_context("ingest"):
        log.info(
            "run_ingestion starting (yard_ids=%s, triggered_by=%s)",
            yard_ids, triggered_by,
        )

        if yard_ids is not None and len(yard_ids) == 0:
            log.warning("run_ingestion: no yards selected; marking failed and returning")
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
                outcome = _extract_one(s, pid)
        except Exception:
            # Outermost catch — DB hiccup, session error, etc. Bucket as transport.
            outcome = "transport"

        if outcome == "success":
            success += 1
        elif outcome == "no_wage":
            no_wage_found += 1
        else:
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
