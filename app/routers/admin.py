import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db

log = logging.getLogger(__name__)
from app.models import (
    Competitor,
    CbsaName,
    CopartLocation,
    JobPosting,
    LlmCall,
    LlmModelConfig,
    RoleDiscoverySuggestion,
    RoleMapping,
    ScheduleConfig,
    ScrapeRun,
    ScraperRun,
)
from app.scheduler import apply_config, next_run_at
from app.security import credentials_configured, require_admin, verify_credentials
from app.services.ingestion import run_ingestion
from app.services.role_discovery import (
    discover_from_existing_postings,
    discover_from_web_search,
)
from app.services.scraping import run_scrape
from app.templating import templates

# Public (no-auth) admin routes: login form, login submit, logout.
auth_router = APIRouter(prefix="/admin")

# Gated admin routes: everything else.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _redirect(path: str) -> RedirectResponse:
    """Build a RedirectResponse that respects ROOT_PATH so it works behind a sub-path proxy."""
    return RedirectResponse(get_settings().root_path + path, status_code=303)


def _safe_next(next_url: str) -> str:
    """Open-redirect guard: only allow internal /admin/* targets."""
    if next_url and next_url.startswith("/admin"):
        return next_url
    return "/admin"


@auth_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = ""):
    if request.session.get("authed"):
        return _redirect(_safe_next(next))
    error = None
    if not credentials_configured():
        error = "Admin auth not configured — set ADMIN_USERNAME and ADMIN_PASSWORD in .env."
    return templates.TemplateResponse(
        request, "admin/login.html",
        {"next": next, "error": error, "username": ""},
    )


@auth_router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    if verify_credentials(username, password):
        request.session["authed"] = True
        request.session["user"] = username
        log.info("admin login ok user=%s from=%s", username, request.client.host if request.client else "?")
        return _redirect(_safe_next(next))
    log.warning(
        "admin login failed user=%s from=%s configured=%s",
        username, request.client.host if request.client else "?", credentials_configured(),
    )
    error = (
        "Admin auth not configured — set ADMIN_USERNAME and ADMIN_PASSWORD in .env."
        if not credentials_configured() else "Invalid credentials."
    )
    return templates.TemplateResponse(
        request, "admin/login.html",
        {"next": next, "error": error, "username": username},
        status_code=401,
    )


@auth_router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _redirect("/admin/login")


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, s: Session = Depends(get_db)):
    counts = {
        "locations": s.execute(select(CopartLocation)).scalars().all().__len__(),
        "competitors": s.execute(select(Competitor)).scalars().all().__len__(),
        "role_mappings": s.execute(select(RoleMapping)).scalars().all().__len__(),
        "llm_calls": s.execute(select(LlmCall)).scalars().all().__len__(),
    }
    last_run = s.execute(select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(1)).scalar_one_or_none()
    cfg = s.execute(select(ScheduleConfig)).scalar_one_or_none()
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "counts": counts,
            "last_run": last_run,
            "schedule": cfg,
            "next_run": next_run_at(),
        },
    )


# ----- Copart locations CRUD -----

@router.get("/locations", response_class=HTMLResponse)
def list_locations(request: Request, s: Session = Depends(get_db)):
    rows = list(s.execute(select(CopartLocation).order_by(CopartLocation.state, CopartLocation.city)).scalars())
    cbsa_names = {c.cbsa_code: c.cbsa_title for c in s.execute(select(CbsaName)).scalars()}
    return templates.TemplateResponse(
        request, "admin/locations.html",
        {"rows": rows, "cbsa_names": cbsa_names},
    )


@router.post("/locations")
def create_location(
    code: str = Form(...), name: str = Form(...), address: str = Form(...),
    city: str = Form(...), state: str = Form(...), zip: str = Form(...),
    lat: float = Form(...), lng: float = Form(...), copart_hourly_wage: float = Form(...),
    s: Session = Depends(get_db),
):
    s.add(CopartLocation(
        code=code, name=name, address=address, city=city, state=state, zip=zip,
        lat=lat, lng=lng, copart_hourly_wage=copart_hourly_wage, active=True,
    ))
    s.commit()
    return _redirect("/admin/locations")


@router.post("/locations/{id}/update")
def update_location(
    id: int, copart_hourly_wage: float = Form(...), active: Optional[str] = Form(None),
    s: Session = Depends(get_db),
):
    row = s.get(CopartLocation, id)
    if not row:
        raise HTTPException(404)
    row.copart_hourly_wage = copart_hourly_wage
    row.active = bool(active)
    s.commit()
    return _redirect("/admin/locations")


@router.post("/locations/bulk-toggle-state")
def bulk_toggle_locations_by_state(
    state: str = Form(...), active: str = Form(...),
    s: Session = Depends(get_db),
):
    st = state.strip().upper()
    if len(st) != 2:
        raise HTTPException(400, "state must be a 2-letter code")
    flag = active == "1"
    rows = list(s.execute(select(CopartLocation).where(CopartLocation.state == st)).scalars())
    for row in rows:
        row.active = flag
    s.commit()
    return _redirect("/admin/locations")


# ----- Competitors CRUD -----

@router.get("/competitors", response_class=HTMLResponse)
def list_competitors(request: Request, s: Session = Depends(get_db)):
    rows = list(s.execute(select(Competitor).order_by(Competitor.source_priority, Competitor.name)).scalars())
    return templates.TemplateResponse(request, "admin/competitors.html", {"rows": rows})


@router.post("/competitors")
def create_competitor(
    name: str = Form(...), source_priority: int = Form(2),
    source_tier: str = Form("employer_owned"), careers_url: str = Form(""),
    s: Session = Depends(get_db),
):
    s.add(Competitor(name=name, source_priority=source_priority, source_tier=source_tier, careers_url=careers_url))
    s.commit()
    return _redirect("/admin/competitors")


@router.post("/competitors/{id}/update")
def update_competitor(
    id: int, source_priority: int = Form(...), source_tier: str = Form(...),
    careers_url: str = Form(""), s: Session = Depends(get_db),
):
    row = s.get(Competitor, id)
    if not row:
        raise HTTPException(404)
    row.source_priority = source_priority
    row.source_tier = source_tier
    row.careers_url = careers_url
    s.commit()
    return _redirect("/admin/competitors")


# ----- Role mappings CRUD -----

@router.get("/role-mappings", response_class=HTMLResponse)
def list_role_mappings(request: Request, s: Session = Depends(get_db)):
    rows = list(s.execute(
        select(RoleMapping).order_by(RoleMapping.copart_role, RoleMapping.competitor_role)
    ).scalars())
    competitors = list(s.execute(select(Competitor).order_by(Competitor.name)).scalars())
    competitor_names = {c.id: c.name for c in competitors}
    return templates.TemplateResponse(
        request, "admin/role_mappings.html",
        {"rows": rows, "competitors": competitors, "competitor_names": competitor_names},
    )


@router.post("/role-mappings")
def create_role_mapping(
    copart_role: str = Form(...), competitor_role: str = Form(...),
    bucket: str = Form("outdoor"), confidence: float = Form(0.8),
    competitor_id: str = Form(""),
    s: Session = Depends(get_db),
):
    cid: Optional[int] = None
    if competitor_id.strip().isdigit():
        cid = int(competitor_id)
    s.add(RoleMapping(
        copart_role=copart_role, competitor_role=competitor_role,
        bucket=bucket, confidence=confidence, competitor_id=cid,
    ))
    s.commit()
    return _redirect("/admin/role-mappings")


@router.post("/role-mappings/{id}/delete")
def delete_role_mapping(id: int, s: Session = Depends(get_db)):
    row = s.get(RoleMapping, id)
    if row:
        s.delete(row)
        s.commit()
    return _redirect("/admin/role-mappings")


# ----- Role discovery -----
#
# Mining-then-review workflow that 10x-es operator throughput on expanding the
# scraper keyword set vs. typing one role-mapping form at a time. See
# `app/services/role_discovery.py` for the orchestrator and
# `app/templates/admin/role_discovery.html` for the UI.

# Picked once per bucket on accept. The scraper only cares about `bucket` (it
# joins on `competitor_role`); these strings exist so the materialized
# RoleMapping row is human-readable on /admin/role-mappings without an extra
# inherit-from-suggestion form roundtrip.
_BUCKET_TO_COPART_ROLE = {
    "outdoor": "Yard Attendant",
    "indoor": "Title Clerk",
}


@router.get("/role-discovery", response_class=HTMLResponse)
def list_role_discovery(
    request: Request,
    ran: int = 0,
    ran_web: int = 0,
    accepted: int = 0,
    s: Session = Depends(get_db),
):
    rows = list(
        s.execute(
            select(RoleDiscoverySuggestion)
            .order_by(
                RoleDiscoverySuggestion.status,        # pending first (alpha-sort)
                RoleDiscoverySuggestion.confidence.desc(),
                RoleDiscoverySuggestion.created_at.desc(),
            )
        ).scalars()
    )
    competitors = list(s.execute(select(Competitor).order_by(Competitor.name)).scalars())
    competitor_names = {c.id: c.name for c in competitors}
    return templates.TemplateResponse(
        request,
        "admin/role_discovery.html",
        {
            "rows": rows,
            "competitors": competitors,
            "competitor_names": competitor_names,
            "ran_count": ran,
            "ran_web_count": ran_web,
            "accepted_count": accepted,
        },
    )


@router.post("/role-discovery/run")
def run_role_discovery(
    competitor_id: str = Form(""),
    s: Session = Depends(get_db),
):
    cid: Optional[int] = None
    if competitor_id.strip().isdigit():
        cid = int(competitor_id)
    log.info("triggered role-discovery (db) competitor_id=%s", cid)
    stats = discover_from_existing_postings(s, competitor_id=cid)
    n = stats["new_suggestions"] + stats["refreshed_suggestions"]
    return _redirect(f"/admin/role-discovery?ran={n}")


@router.post("/role-discovery/run-web")
def run_role_discovery_web(
    competitor_id: str = Form(""),
    s: Session = Depends(get_db),
):
    """V2 entry point: web-search-driven role discovery. Queries the open web
    for each competitor (or just the one specified), extracts candidate titles
    from search snippets via the LLM, and queues them in the same suggestion
    table V1 writes to (``source='web_search'``)."""
    cid: Optional[int] = None
    if competitor_id.strip().isdigit():
        cid = int(competitor_id)
    log.info("triggered role-discovery (web) competitor_id=%s", cid)
    stats = discover_from_web_search(s, competitor_id=cid)
    n = stats["new_suggestions"] + stats["refreshed_suggestions"]
    return _redirect(f"/admin/role-discovery?ran_web={n}")


def _accept_suggestion(
    s: Session, suggestion: RoleDiscoverySuggestion
) -> Optional[RoleMapping]:
    """Materialize a RoleMapping from a suggestion. Returns the new mapping (or
    ``None`` if the suggestion's bucket is ``not_relevant`` — callers must check
    this and surface a flash error). Marks the suggestion ``accepted``."""
    if suggestion.suggested_bucket not in ("outdoor", "indoor"):
        return None
    copart_role = _BUCKET_TO_COPART_ROLE.get(suggestion.suggested_bucket, "Yard Attendant")
    mapping = RoleMapping(
        competitor_id=suggestion.competitor_id,
        copart_role=copart_role,
        competitor_role=suggestion.raw_title,
        bucket=suggestion.suggested_bucket,
        confidence=suggestion.confidence,
    )
    s.add(mapping)
    suggestion.status = "accepted"
    return mapping


@router.post("/role-discovery/{id}/accept")
def accept_role_discovery(id: int, s: Session = Depends(get_db)):
    row = s.get(RoleDiscoverySuggestion, id)
    if not row:
        raise HTTPException(404)
    if row.status != "pending":
        # Idempotent no-op: already decided. Bounce to the list, don't crash.
        return _redirect("/admin/role-discovery")
    mapping = _accept_suggestion(s, row)
    if mapping is None:
        raise HTTPException(
            400,
            "Cannot accept a 'not_relevant' suggestion — only outdoor/indoor titles map to a scraper keyword.",
        )
    s.commit()
    return _redirect("/admin/role-discovery")


@router.post("/role-discovery/{id}/reject")
def reject_role_discovery(id: int, s: Session = Depends(get_db)):
    row = s.get(RoleDiscoverySuggestion, id)
    if not row:
        raise HTTPException(404)
    if row.status == "pending":
        row.status = "rejected"
        s.commit()
    return _redirect("/admin/role-discovery")


@router.post("/role-discovery/bulk-accept")
def bulk_accept_role_discovery(
    min_confidence: float = Form(0.8),
    s: Session = Depends(get_db),
):
    """Accept every pending suggestion at or above the confidence floor whose
    bucket is mappable (outdoor/indoor). ``not_relevant`` rows are skipped
    silently — they're not eligible regardless of confidence."""
    pending = list(
        s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.status == "pending",
                RoleDiscoverySuggestion.confidence >= min_confidence,
                RoleDiscoverySuggestion.suggested_bucket.in_(("outdoor", "indoor")),
            )
        ).scalars()
    )
    n = 0
    for row in pending:
        if _accept_suggestion(s, row) is not None:
            n += 1
    s.commit()
    return _redirect(f"/admin/role-discovery?accepted={n}")


# ----- Ingestion / Run Now -----

@router.post("/run-now")
def trigger_run():
    log.info("triggered ingestion (all active yards)")
    run_id = run_ingestion(triggered_by="manual", async_mode=True)
    return _redirect(f"/admin/runs/{run_id}")


@router.post("/run-now/yard/{code}")
def trigger_run_for_yard(code: str, s: Session = Depends(get_db)):
    yard = s.execute(select(CopartLocation).where(CopartLocation.code == code)).scalar_one_or_none()
    if not yard:
        raise HTTPException(404)
    log.info("triggered ingestion yard=%s id=%s", code, yard.id)
    run_id = run_ingestion(triggered_by="manual", yard_ids=[yard.id], async_mode=True)
    return _redirect(f"/admin/runs/{run_id}")


@router.post("/run-now/yards")
def trigger_run_for_yards(
    yard_codes: list[str] = Form(default=[]),
    s: Session = Depends(get_db),
):
    if not yard_codes:
        return _redirect("/admin/locations")
    ids = list(
        s.execute(
            select(CopartLocation.id).where(CopartLocation.code.in_(yard_codes))
        ).scalars()
    )
    log.info("triggered ingestion yards=%s (%d ids)", yard_codes, len(ids))
    run_id = run_ingestion(triggered_by="manual", yard_ids=ids, async_mode=True)
    return _redirect(f"/admin/runs/{run_id}")


@router.get("/runs", response_class=HTMLResponse)
def list_runs(request: Request, s: Session = Depends(get_db)):
    rows = list(s.execute(select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(50)).scalars())
    return templates.TemplateResponse(request, "admin/runs.html", {"rows": rows})


@router.get("/runs/{id}", response_class=HTMLResponse)
def run_detail(id: int, request: Request, s: Session = Depends(get_db)):
    row = s.get(ScrapeRun, id)
    if not row:
        raise HTTPException(404)
    calls = list(s.execute(
        select(LlmCall).where(LlmCall.created_at >= row.started_at).order_by(LlmCall.created_at.desc())
    ).scalars())
    return templates.TemplateResponse(request, "admin/run_detail.html", {"row": row, "calls": calls})


# ----- Employer scrapers / Scrape Now -----

@router.post("/scrape/{competitor_id}")
def trigger_scrape(competitor_id: int, s: Session = Depends(get_db)):
    competitor = s.get(Competitor, competitor_id)
    if not competitor:
        raise HTTPException(404)
    log.info("triggered scrape competitor=%s id=%d", competitor.name, competitor_id)
    run_id = run_scrape(competitor_id=competitor_id, triggered_by="manual", async_mode=True)
    return _redirect(f"/admin/scrape-runs/{run_id}")


@router.get("/scrape-runs", response_class=HTMLResponse)
def list_scrape_runs(request: Request, s: Session = Depends(get_db)):
    rows = list(
        s.execute(select(ScraperRun).order_by(ScraperRun.started_at.desc()).limit(50)).scalars()
    )
    return templates.TemplateResponse(request, "admin/scrape_runs.html", {"rows": rows})


@router.get("/scrape-runs/{id}", response_class=HTMLResponse)
def scrape_run_detail(id: int, request: Request, s: Session = Depends(get_db)):
    row = s.get(ScraperRun, id)
    if not row:
        raise HTTPException(404)
    # New JobPostings created during this run (best-effort filter: same competitor name
    # and ingested_at >= run.started_at). Useful to verify what landed.
    competitor = s.execute(
        select(Competitor).where(Competitor.name == row.competitor_name)
    ).scalar_one_or_none()
    postings: list[JobPosting] = []
    if competitor is not None:
        postings = list(
            s.execute(
                select(JobPosting)
                .where(
                    JobPosting.competitor_id == competitor.id,
                    JobPosting.ingested_at >= row.started_at,
                )
                .order_by(JobPosting.ingested_at.desc())
                .limit(200)
            ).scalars()
        )
    # LlmCalls produced AFTER scrape start — surfaces follow-on extraction if the
    # operator clicked Run Now afterwards.
    calls = list(
        s.execute(
            select(LlmCall)
            .where(LlmCall.created_at >= row.started_at)
            .order_by(LlmCall.created_at.desc())
            .limit(200)
        ).scalars()
    )
    return templates.TemplateResponse(
        request,
        "admin/scrape_run_detail.html",
        {"row": row, "postings": postings, "calls": calls},
    )


# ----- Schedule config -----

@router.get("/scheduler", response_class=HTMLResponse)
def schedule_form(request: Request, s: Session = Depends(get_db)):
    cfg = s.execute(select(ScheduleConfig)).scalar_one_or_none()
    return templates.TemplateResponse(
        request, "admin/scheduler.html",
        {"cfg": cfg, "next_run": next_run_at()},
    )


@router.post("/scheduler")
def schedule_update(
    cron_expression: str = Form(...), enabled: Optional[str] = Form(None),
    s: Session = Depends(get_db),
):
    cfg = s.execute(select(ScheduleConfig)).scalar_one_or_none()
    if not cfg:
        cfg = ScheduleConfig(cron_expression=cron_expression, enabled=bool(enabled))
        s.add(cfg)
    else:
        cfg.cron_expression = cron_expression
        cfg.enabled = bool(enabled)
    s.commit()
    apply_config()
    return _redirect("/admin/scheduler")


# ----- AI Operations -----

@router.get("/ai-ops", response_class=HTMLResponse)
def ai_ops(request: Request, s: Session = Depends(get_db)):
    calls = list(s.execute(select(LlmCall).order_by(LlmCall.created_at.desc()).limit(200)).scalars())
    by_purpose: dict[str, list[LlmCall]] = {}
    for c in calls:
        by_purpose.setdefault(c.purpose, []).append(c)

    def _agg(items: list[LlmCall]) -> dict:
        if not items:
            return {"n": 0, "ok_rate": 0, "avg_latency": 0, "total_cost": 0}
        return {
            "n": len(items),
            "ok_rate": round(100 * sum(1 for x in items if x.validation_ok) / len(items), 1),
            "avg_latency": round(sum(x.latency_ms for x in items) / len(items)),
            "total_cost": round(sum(x.cost_usd for x in items), 4),
        }

    aggregates = {p: _agg(v) for p, v in by_purpose.items()}
    return templates.TemplateResponse(
        request, "admin/ai_ops.html",
        {"calls": calls, "aggregates": aggregates, "total": _agg(calls)},
    )


@router.get("/ai-ops/{id}", response_class=HTMLResponse)
def ai_op_detail(id: int, request: Request, s: Session = Depends(get_db)):
    row = s.get(LlmCall, id)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin/ai_op_detail.html", {"row": row})


# ----- Per-purpose LLM model config -----

SUGGESTED_MODELS = [
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3-opus",
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat",
]


KNOWN_PURPOSES = ("extraction", "classification", "narrative")


@router.get("/llm-models", response_class=HTMLResponse)
def llm_models(request: Request, s: Session = Depends(get_db)):
    settings = get_settings()
    db_rows = {r.purpose: r for r in s.execute(select(LlmModelConfig)).scalars()}

    purposes = list(KNOWN_PURPOSES) + [p for p in db_rows if p not in KNOWN_PURPOSES]
    cards: list[dict] = []
    for p in purposes:
        env_value = getattr(settings, f"{p}_model", "") or ""
        db_row = db_rows.get(p)
        if db_row:
            effective_model = db_row.model
            source = "db"
        elif env_value:
            effective_model = env_value
            source = "env"
        else:
            effective_model = settings.openrouter_model
            source = "default"
        cards.append({
            "purpose": p,
            "db_row": db_row,
            "env_value": env_value,
            "effective_model": effective_model,
            "source": source,
        })

    return templates.TemplateResponse(
        request, "admin/llm_models.html",
        {
            "cards": cards,
            "suggestions": SUGGESTED_MODELS,
            "global_default": settings.openrouter_model,
        },
    )


@router.post("/llm-models")
def llm_models_update(
    purpose: str = Form(...),
    model: str = Form(...),
    temperature: float = Form(0.1),
    notes: str = Form(""),
    s: Session = Depends(get_db),
):
    row = s.get(LlmModelConfig, purpose)
    if not row:
        row = LlmModelConfig(purpose=purpose, model=model, temperature=temperature, notes=notes)
        s.add(row)
    else:
        row.model = model
        row.temperature = temperature
        row.notes = notes
    s.commit()
    return _redirect("/admin/llm-models")


@router.post("/llm-models/{purpose}/reset")
def llm_models_reset(purpose: str, s: Session = Depends(get_db)):
    """Delete the DB override for this purpose. The env value (or global default) then wins."""
    row = s.get(LlmModelConfig, purpose)
    if row:
        s.delete(row)
        s.commit()
    return _redirect("/admin/llm-models")


# ----- Evaluation harness -----

@router.get("/eval", response_class=HTMLResponse)
def eval_view(request: Request):
    from tests.eval_harness import run_extraction_evals
    results, summary = run_extraction_evals()
    return templates.TemplateResponse(
        request, "admin/eval.html", {"results": results, "summary": summary},
    )


# ----- Logs viewer -----

# Format string: "<asctime> <LEVEL> [<op_id>] <logger> :: <message>"
# Parser regex matches everything but is tolerant of malformed lines (writes them as raw).
import re as _re  # noqa: E402  -- local rename so tests don't shadow stdlib re
from collections import deque as _deque  # noqa: E402

_LOG_LINE_RE = _re.compile(
    r"^(?P<ts>\S+ \S+)\s+(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"\[(?P<op_id>[^\]]*)\]\s+(?P<logger>\S+)\s+::\s+(?P<message>.*)$"
)


def _read_log_tail(path, n: int):
    """Return up to n last lines from path as a list. Empty list if file is missing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return list(_deque(f, maxlen=n))
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("could not read log file %s: %s", path, e)
        return []


@router.get("/logs", response_class=HTMLResponse)
def logs_view(
    request: Request,
    level: str = "",
    module: str = "",
    op_id: str = "",
    tail: int = 500,
):
    """Live tail of logs/app.log with module/level/op_id filters.

    No auto-refresh. Bounded by tail (default 500, max 2000) so a runaway log
    can't OOM the page renderer.
    """
    from app.main import APP_LOG_PATH

    tail = max(1, min(int(tail or 500), 2000))
    raw_lines = _read_log_tail(APP_LOG_PATH, tail)
    rows: list[dict] = []
    for line in raw_lines:
        line = line.rstrip("\n")
        m = _LOG_LINE_RE.match(line)
        if not m:
            rows.append({"raw": line, "level": "", "ts": "", "op_id": "", "logger": "", "message": line})
            continue
        rows.append({
            "raw": line,
            "ts": m.group("ts"),
            "level": m.group("level"),
            "op_id": m.group("op_id"),
            "logger": m.group("logger"),
            "message": m.group("message"),
        })

    # Apply filters in Python after reading the tail. Filtering up front would
    # mean reading the entire file for the rare-level case, defeating the tail.
    level_f = level.strip().upper()
    if level_f:
        rows = [r for r in rows if r.get("level") == level_f]
    if module.strip():
        m_low = module.strip().lower()
        rows = [r for r in rows if m_low in r.get("logger", "").lower()]
    if op_id.strip():
        o_low = op_id.strip().lower()
        rows = [r for r in rows if o_low in r.get("op_id", "").lower()]

    # Reverse so newest is on top — feed-like reading.
    rows.reverse()

    return templates.TemplateResponse(
        request,
        "admin/logs.html",
        {
            "rows": rows,
            "log_path": str(APP_LOG_PATH),
            "log_exists": APP_LOG_PATH.exists(),
            "filter_level": level_f,
            "filter_module": module,
            "filter_op_id": op_id,
            "tail": tail,
            "levels": ["DEBUG", "INFO", "WARNING", "ERROR"],
        },
    )


@router.get("/logs/download")
def logs_download():
    """Stream the raw log file as an attachment."""
    from fastapi.responses import FileResponse
    from app.main import APP_LOG_PATH
    if not APP_LOG_PATH.exists():
        raise HTTPException(404, "no log file yet")
    return FileResponse(
        path=str(APP_LOG_PATH),
        media_type="text/plain",
        filename="app.log",
    )
