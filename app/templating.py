from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.scrapers.registry import has_scraper as _registry_has_scraper

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Inject ROOT_PATH as `prefix` so every template renders correctly behind a sub-path
# proxy (e.g. /compare-wages). Templates emit URLs as `{{ prefix }}/admin/...`.
templates.env.globals["prefix"] = get_settings().root_path

# Template helper for the Competitors page: True iff a scraper is registered for
# this competitor name. Imported lazily-tolerantly — the registry is empty until
# `app.scrapers` is imported in main.py at boot.
templates.env.globals["scraper_for_name"] = _registry_has_scraper


def _format_money(v) -> str:
    try:
        return f"${float(v):.2f}"
    except Exception:
        return "—"


def _format_signed_money(v) -> str:
    try:
        return f"{'+' if float(v) >= 0 else '−'}${abs(float(v)):.2f}"
    except Exception:
        return "—"


def _pressure_color(q: int) -> str:
    return {1: "bg-rose-600", 2: "bg-amber-500", 3: "bg-emerald-500", 4: "bg-sky-600"}.get(q, "bg-zinc-400")


def _pressure_label(q: int) -> str:
    return {1: "Highest pressure", 2: "Elevated", 3: "Moderate", 4: "Lowest"}.get(q, "—")


def _relative_time(dt) -> str:
    if not dt:
        return "—"
    now = datetime.utcnow().replace(tzinfo=None) if dt.tzinfo is None else datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = secs // 60
        return f"{m} min ago" if m > 1 else "1 min ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hr ago" if h > 1 else "1 hr ago"
    d = secs // 86400
    return f"{d} days ago" if d > 1 else "1 day ago"


def _freshness_class(dt) -> str:
    """Return a Tailwind class trio for the freshness chip background based on
    how stale dt is. Default emerald, amber > 48h, rose > 7d."""
    if not dt:
        return "bg-slate-50 border-slate-200 text-slate-700"
    now = datetime.utcnow().replace(tzinfo=None) if dt.tzinfo is None else datetime.now(timezone.utc)
    delta = now - dt
    hours = delta.total_seconds() / 3600
    if hours > 168:
        return "bg-rose-50 border-rose-200 text-rose-800"
    if hours > 48:
        return "bg-amber-50 border-amber-200 text-amber-800"
    return "bg-emerald-50 border-emerald-200 text-emerald-800"


def _freshness_dot_class(dt) -> str:
    """Companion to ``_freshness_class``: returns the matching pulse-dot class
    (``fresh-dot``, ``fresh-dot-amber``, ``fresh-dot-rose``) so the dot color
    stays in lockstep with the chip background. None → neutral slate dot."""
    if not dt:
        return "fresh-dot fresh-dot-slate"
    now = datetime.utcnow().replace(tzinfo=None) if dt.tzinfo is None else datetime.now(timezone.utc)
    delta = now - dt
    hours = delta.total_seconds() / 3600
    if hours > 168:
        return "fresh-dot fresh-dot-rose"
    if hours > 48:
        return "fresh-dot fresh-dot-amber"
    return "fresh-dot"


templates.env.filters["money"] = _format_money
templates.env.filters["signed_money"] = _format_signed_money
templates.env.filters["pressure_color"] = _pressure_color
templates.env.filters["pressure_label"] = _pressure_label
templates.env.filters["relative_time"] = _relative_time
templates.env.filters["freshness_class"] = _freshness_class
templates.env.filters["freshness_dot_class"] = _freshness_dot_class
