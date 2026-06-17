"""Orchestrates: employer scraper → ScrapedPosting stream → CompetitorLocation + JobPosting rows.

Each Scraper subclass (see `app.scrapers.base.Scraper`) yields `ScrapedPosting` objects.
This service:
  1. Looks up the Competitor by id and resolves the registered scraper via the registry.
  2. Creates a ScraperRun row immediately so the admin UI can render a progress page.
  3. Calls `scraper.scrape(max_postings=...)` from a daemon thread (when async_mode).
  4. For each posting: writes raw HTML to disk, matches-or-creates a CompetitorLocation,
     and inserts a JobPosting with `wage_low=None` so the existing LLM extraction
     pipeline can fill the wage fields next time `Run Now` is clicked.

The downstream LLM ingestion (`app.services.ingestion.run_ingestion`) remains the
extraction step — this service deliberately doesn't call the LLM itself, so the two
surfaces (scraping vs extraction) stay independent on the admin UI.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, or_, select

from app.db import session_scope
from app.models import Competitor, CompetitorLocation, CopartLocation, JobPosting, RoleMapping, ScraperRun
from app.scrapers.base import ScrapedPosting
from app.scrapers.registry import get_scraper
from app.services.geocoding import geocode
from app.services.ingestion import extract_postings_by_ids

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_HTML_DIR = REPO_ROOT / "data" / "raw_html"

# How often the background thread commits progress to the ScraperRun row.
PROGRESS_FLUSH_EVERY = 3


def _create_run(
    competitor_name: str,
    triggered_by: str,
    *,
    status: str = "running",
    notes: str = "",
    finished: bool = False,
) -> int:
    with session_scope() as s:
        run = ScraperRun(
            competitor_name=competitor_name,
            triggered_by=triggered_by,
            status=status,
            notes=notes,
        )
        if finished:
            run.finished_at = datetime.utcnow()
        s.add(run)
        s.flush()
        return run.id


def _slug(name: str) -> str:
    """Filesystem-safe slug for the competitor name."""
    return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_") or "competitor"


def _match_or_create_location(
    s,
    *,
    competitor_id: int,
    city: str,
    state: str,
    competitor_name: str,
    street: str = "",
    zip_code: str = "",
) -> int:
    """Find a matching CompetitorLocation or create one with real geocoded coords.

    Match is (competitor_id, state, city case-insensitive). If we have to create a row,
    we geocode via the Census Geocoder API. On any geocode failure we still create the
    row but with lat/lng=(0, 0) — those are easy to find later (`WHERE lat = 0 AND lng = 0`)
    for a backfill, and the row's presence still records that the scraper saw the city.
    """
    state_norm = (state or "").upper().strip()
    city_norm = (city or "").strip()

    existing = s.execute(
        select(CompetitorLocation).where(
            CompetitorLocation.competitor_id == competitor_id,
            CompetitorLocation.state == state_norm,
            func.lower(CompetitorLocation.city) == city_norm.lower(),
        )
    ).scalar_one_or_none()
    if existing:
        # If we previously stored (0, 0) because geocoding wasn't wired up, try again now
        # that we may have a more specific address.
        if existing.lat == 0.0 and existing.lng == 0.0 and city_norm and state_norm:
            coords = geocode(city=city_norm, state=state_norm, street=street, zip_code=zip_code)
            if coords:
                existing.lat, existing.lng = coords
        return existing.id

    lat = lng = 0.0
    if city_norm and state_norm:
        coords = geocode(city=city_norm, state=state_norm, street=street, zip_code=zip_code)
        if coords:
            lat, lng = coords

    cl = CompetitorLocation(
        competitor_id=competitor_id,
        name=f"{competitor_name} {city_norm}".strip(),
        city=city_norm,
        state=state_norm,
        lat=lat,
        lng=lng,
    )
    s.add(cl)
    s.flush()
    return cl.id


def _write_raw_html(html: str, *, competitor_slug: str, run_id: int, sequence: int) -> str:
    """Write the posting's HTML to data/raw_html and return the repo-root-relative path."""
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"scraped_{competitor_slug}_{run_id}_{sequence}.html"
    path = RAW_HTML_DIR / fname
    path.write_text(html or "")
    return str(path.relative_to(REPO_ROOT))


def _flush_progress(run_id: int, *, candidates: int, saved: int) -> None:
    with session_scope() as s:
        run = s.get(ScraperRun, run_id)
        if run is None:
            return
        run.candidates_found = candidates
        run.postings_saved = saved


def active_yard_locations(s) -> list[tuple[str, str]]:
    """Return ``[(city, state), ...]`` from currently-active CopartLocations.

    Used by the scraping orchestrator to target each scrape at the geographic catchment
    of the operator's selected yards instead of relying on the employer's global ranking
    (which used to return the same handful of metros every run, leaving smaller yards
    permanently empty). Deduplicated so the same metro doesn't double-pair with every
    keyword.
    """
    rows = list(
        s.execute(
            select(CopartLocation.city, CopartLocation.state)
            .where(CopartLocation.active.is_(True))
        ).all()
    )
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for city, state in rows:
        key = ((city or "").strip(), (state or "").strip().upper())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def keywords_for_competitor(
    s, competitor_id: int, *, min_confidence: float = 0.7
) -> list[str]:
    """Build the keyword list a scraper should search for, derived from RoleMapping rows
    scoped to this competitor OR globally-applicable (competitor_id IS NULL).

    Returns a deduplicated, sorted list of competitor_role strings. Operators expand
    coverage by editing /admin/role-mappings — no scraper code change required.
    """
    rows = list(
        s.execute(
            select(RoleMapping.competitor_role)
            .where(
                or_(
                    RoleMapping.competitor_id == competitor_id,
                    RoleMapping.competitor_id.is_(None),
                )
            )
            .where(RoleMapping.confidence >= min_confidence)
        ).scalars()
    )
    return sorted({r for r in rows if r})


def _do_scrape(run_id: int, competitor_id: int, max_postings: int) -> None:
    """Body of a scrape run. Synchronous; called both inline and in a daemon thread."""
    # Resolve competitor + scraper inside the worker so the thread is self-contained.
    with session_scope() as s:
        competitor = s.get(Competitor, competitor_id)
        if competitor is None:
            run = s.get(ScraperRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = datetime.utcnow()
                run.notes = (run.notes or "") + " | competitor not found"
            return
        competitor_name = competitor.name
        keywords = keywords_for_competitor(s, competitor_id)
        # Active-yard locations are passed through to the scraper so each search query
        # targets a real yard's catchment. Empty list = no active yards = fall back to
        # global keyword search (preserves backwards-compat for fresh deployments).
        locations = active_yard_locations(s)
        # Capture every active yard's (lat, lng) for the post-fetch geographic filter
        # below. The scraper-side `location` URL params (HD's &city=X&state=Y, etc.) are
        # advisory at best — most sites use them only to influence ranking, not to
        # strictly filter. So even after a "Cashier @ Hueytown, AL" query we get hits
        # from Newark NJ. This filter is the truth: drop postings whose geocoded
        # location is outside DISTANCE_CUTOFF_MILES of EVERY active yard.
        from app.models import CopartLocation as _CL
        from app.services.geo import haversine_miles  # local import: tight scope
        from app.config import get_settings as _gs
        active_yard_coords = [
            (y.lat, y.lng)
            for y in s.execute(select(_CL).where(_CL.active.is_(True))).scalars()
        ]
        catchment_miles = _gs().distance_cutoff_miles

    scraper = get_scraper(competitor_name)
    if scraper is None:
        # Belt-and-braces: run_scrape should have caught this, but guard the worker too.
        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = datetime.utcnow()
                run.notes = (run.notes or "") + f" | no scraper registered for {competitor_name}"
        return

    if not keywords:
        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = datetime.utcnow()
                run.notes = (
                    (run.notes or "")
                    + f" | no role mappings for {competitor_name} (add some at /admin/role-mappings)"
                )
        return

    competitor_slug = _slug(competitor_name)
    candidates = 0
    saved = 0
    sequence = 0
    saved_posting_ids: list[int] = []

    out_of_catchment = 0

    def _in_catchment(lat: float, lng: float) -> bool:
        """Truthy if the posting is within DISTANCE_CUTOFF_MILES of any active yard.

        When there are no active yards we keep everything — gives operators a way to
        see what scrapers can return before they activate yards (better than empty).
        Postings with (0, 0) coordinates (geocoder failed) are kept; the dashboard's
        own geo filter will exclude them visually but we still persist so a future
        backfill can re-geocode.
        """
        if not active_yard_coords:
            return True
        if lat == 0.0 and lng == 0.0:
            return True
        for y_lat, y_lng in active_yard_coords:
            if haversine_miles(y_lat, y_lng, lat, lng) <= catchment_miles:
                return True
        return False

    try:
        for posting in scraper.scrape(
            keywords=keywords,
            locations=locations or None,
            max_postings=max_postings,
        ):
            candidates += 1
            sequence += 1

            if not isinstance(posting, ScrapedPosting):
                # Defensive: skip malformed yields rather than blowing up the run.
                continue

            try:
                with session_scope() as s:
                    cl_id = _match_or_create_location(
                        s,
                        competitor_id=competitor_id,
                        city=posting.location_city,
                        state=posting.location_state,
                        competitor_name=competitor_name,
                        street=getattr(posting, "street_address", ""),
                        zip_code=getattr(posting, "zip_code", ""),
                    )
                    # Now that the CompetitorLocation row has geocoded coords, apply the
                    # post-fetch geographic filter. If the posting's location is more than
                    # `catchment_miles` from every active yard, skip persisting it.
                    cl_for_filter = s.get(CompetitorLocation, cl_id)
                    if cl_for_filter is not None and not _in_catchment(cl_for_filter.lat, cl_for_filter.lng):
                        out_of_catchment += 1
                        continue

                    raw_path = _write_raw_html(
                        posting.raw_html,
                        competitor_slug=competitor_slug,
                        run_id=run_id,
                        sequence=sequence,
                    )

                    jp = JobPosting(
                        competitor_id=competitor_id,
                        competitor_location_id=cl_id,
                        raw_title=posting.raw_title,
                        source_url=posting.source_url,
                        source_tier="employer_owned",
                        raw_html_path=raw_path,
                        wage_low=None,  # filled by the extract pass that follows below
                    )
                    s.add(jp)
                    s.flush()
                    saved_posting_ids.append(jp.id)

                saved += 1
            except Exception:
                # One bad posting shouldn't kill the whole run.
                pass

            if candidates % PROGRESS_FLUSH_EVERY == 0:
                _flush_progress(run_id, candidates=candidates, saved=saved)

        # Immediately extract wages on the postings we just scraped, so the operator
        # sees them on the dashboard without having to click Run Now afterward. This
        # ignores yard/active filters — if you bothered to scrape it, you want wages.
        # The summary distinguishes honest "no wage disclosed on the page" outcomes
        # from real transport failures so the operator can triage without confusion.
        extract_summary = {
            "processed": 0, "success": 0, "no_wage_found": 0, "transport_failed": 0,
        }
        if saved_posting_ids:
            extract_summary = extract_postings_by_ids(saved_posting_ids)

        # Pull telemetry off the scraper instance (BaseEmployerScraper populates this).
        tel = getattr(scraper, "last_run_telemetry", {}) or {}
        fallback = " · fixture-fallback" if tel.get("fallback_to_fixtures") else ""
        first_reason = (tel.get("reasons") or [None])[0]
        reason_tail = f" · why: {first_reason}" if (fallback and first_reason) else ""

        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            if run is not None:
                run.finished_at = datetime.utcnow()
                run.candidates_found = candidates
                run.postings_saved = saved
                run.extraction_no_wage_found = extract_summary["no_wage_found"]
                run.status = "success"
                kw_summary = ",".join(keywords[:4]) + ("…" if len(keywords) > 4 else "")
                # Note shape: `extracted=ok/no-wage/failed` — three counters so an operator
                # scanning the runs page can instantly tell "Home Depot disclosed nothing"
                # (high no-wage, zero failed) from "OpenRouter is down" (high failed).
                ooc_note = f" out-of-catchment={out_of_catchment}" if out_of_catchment else ""
                run.notes = (
                    (run.notes + " | " if run.notes else "")
                    + f"candidates={candidates} saved={saved}{ooc_note} "
                    + f"extracted={extract_summary['success']}/"
                    + f"{extract_summary['no_wage_found']}/"
                    + f"{extract_summary['transport_failed']} "
                    + f"keywords=[{kw_summary}]{fallback}{reason_tail}"
                )
    except Exception as exc:
        with session_scope() as s:
            run = s.get(ScraperRun, run_id)
            if run is not None:
                run.finished_at = datetime.utcnow()
                run.candidates_found = candidates
                run.postings_saved = saved
                run.status = "failed"
                run.notes = (
                    (run.notes + " | " if run.notes else "")
                    + f"error: {type(exc).__name__}: {exc}"
                )


def run_scrape(
    *,
    competitor_id: int,
    triggered_by: str = "manual",
    max_postings: int = 25,
    async_mode: bool = False,
) -> int:
    """Trigger a scrape for a single competitor. Returns the ScraperRun id immediately.

    `async_mode=True` creates the row, kicks off a daemon thread, and returns so the
    admin UI can render an auto-refreshing progress page.

    Returns the ScraperRun id even on early-failure paths (no scraper registered, scraper
    not available) so the caller can always link to a detail page with the explanation.
    """
    # Resolve competitor + scraper up front so we can record a clear failure row.
    with session_scope() as s:
        competitor = s.get(Competitor, competitor_id)
        if competitor is None:
            return _create_run(
                competitor_name=f"id={competitor_id}",
                triggered_by=triggered_by,
                status="failed",
                finished=True,
                notes=f"competitor id={competitor_id} not found",
            )
        competitor_name = competitor.name

    scraper = get_scraper(competitor_name)
    if scraper is None:
        return _create_run(
            competitor_name=competitor_name,
            triggered_by=triggered_by,
            status="failed",
            finished=True,
            notes=f"no scraper registered for {competitor_name}",
        )

    # Polite pre-check (robots.txt / HEAD). Scrapers that would be blocked surface the
    # reason here instead of failing mid-flight halfway through a real run.
    try:
        available = scraper.is_available()
    except Exception as exc:
        return _create_run(
            competitor_name=competitor_name,
            triggered_by=triggered_by,
            status="blocked",
            finished=True,
            notes=f"is_available() raised: {type(exc).__name__}: {exc}",
        )

    if not available:
        return _create_run(
            competitor_name=competitor_name,
            triggered_by=triggered_by,
            status="blocked",
            finished=True,
            notes=f"{competitor_name} scraper reports not available (robots.txt or pre-check failed)",
        )

    run_id = _create_run(
        competitor_name=competitor_name,
        triggered_by=triggered_by,
        status="running",
    )

    if async_mode:
        t = threading.Thread(
            target=_do_scrape,
            args=(run_id, competitor_id, max_postings),
            daemon=True,
        )
        t.start()
        return run_id

    _do_scrape(run_id, competitor_id, max_postings)
    return run_id


def mark_orphaned_scraper_runs_failed() -> int:
    """On boot, any ScraperRun left in `status=running` is a thread interrupted by a
    previous restart. Mark them failed so the UI doesn't show a phantom forever.
    """
    with session_scope() as s:
        running = list(
            s.execute(select(ScraperRun).where(ScraperRun.status == "running")).scalars()
        )
        for r in running:
            r.status = "failed"
            r.finished_at = datetime.utcnow()
            r.notes = (r.notes or "") + " | interrupted by server restart"
        return len(running)
