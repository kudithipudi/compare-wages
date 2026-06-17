"""APScheduler wrapper that reads the single-row ScheduleConfig and (re)installs a cron job."""
from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.db import session_scope
from app.models import ScheduleConfig
from app.services.ingestion import run_ingestion

_scheduler: BackgroundScheduler | None = None
JOB_ID = "ingest-cron"


def _scheduled_run() -> None:
    run_ingestion(triggered_by="scheduled")
    with session_scope() as s:
        cfg = s.execute(select(ScheduleConfig)).scalar_one_or_none()
        if cfg:
            cfg.last_run_at = datetime.utcnow()


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.start()
    return _scheduler


def apply_config() -> None:
    sched = get_scheduler()
    with session_scope() as s:
        cfg = s.execute(select(ScheduleConfig)).scalar_one_or_none()
        enabled = bool(cfg.enabled) if cfg else False
        cron_expr = cfg.cron_expression if cfg else ""
    try:
        sched.remove_job(JOB_ID)
    except Exception:
        pass
    if enabled and cron_expr:
        try:
            trigger = CronTrigger.from_crontab(cron_expr)
            sched.add_job(_scheduled_run, trigger=trigger, id=JOB_ID, replace_existing=True)
        except Exception:
            pass


def next_run_at() -> datetime | None:
    sched = get_scheduler()
    job = sched.get_job(JOB_ID)
    return job.next_run_time if job else None
