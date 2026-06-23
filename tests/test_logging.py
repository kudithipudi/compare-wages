"""Tests for the logging infrastructure introduced in Track A.

Covers:
  - operation_context contextvar propagation (single + nested).
  - /admin/logs route — empty state when no log file exists, and filtering
    by level when a fake log is present.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.log_context import OperationContextFilter, op_id_var, operation_context
from app.main import app


def test_operation_context_propagates_op_id(caplog) -> None:
    """Inside the context manager every LogRecord carries the expected op_id."""
    caplog.set_level(logging.INFO)

    # caplog's internal handler doesn't run our filter, so install it manually
    # for the duration of this test.
    op_filter = OperationContextFilter()
    caplog.handler.addFilter(op_filter)
    try:
        with operation_context("test") as op_id:
            logging.getLogger("ctx.test").info("hello")
        assert op_id.startswith("test/")
        records = [r for r in caplog.records if r.name == "ctx.test"]
        assert records, "expected at least one log record from ctx.test"
        assert getattr(records[-1], "op_id", None) == op_id
    finally:
        caplog.handler.removeFilter(op_filter)


def test_operation_context_nests_then_restores() -> None:
    """Nested contexts restore the parent op_id on exit."""
    assert op_id_var.get() == "-"
    with operation_context("outer") as outer_id:
        assert op_id_var.get() == outer_id
        with operation_context("inner") as inner_id:
            assert op_id_var.get() == inner_id
            assert outer_id != inner_id
        # back to outer
        assert op_id_var.get() == outer_id
    # back to default
    assert op_id_var.get() == "-"


import pytest


@pytest.fixture
def admin_client():
    """TestClient with the admin auth dependency overridden to a no-op.

    Direct cookie-stamping doesn't work in TestClient because the prod config
    sets ``https_only=True`` on the session cookie (driven by ``ROOT_PATH``)
    and TestClient runs over http://testserver — so a hand-crafted cookie is
    never sent. The dependency_overrides path is the canonical FastAPI test
    pattern and sidesteps that entirely.
    """
    from app.security import require_admin
    app.dependency_overrides[require_admin] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_admin_logs_route_returns_200_with_empty_state(admin_client, monkeypatch, tmp_path) -> None:
    """When the log file is absent the page still renders cleanly."""
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "APP_LOG_PATH", tmp_path / "does_not_exist.log")

    r = admin_client.get("/admin/logs")
    assert r.status_code == 200
    assert "Log file not created yet." in r.text


def test_admin_logs_filter_by_level(admin_client, monkeypatch, tmp_path) -> None:
    """Writing a synthetic log file and filtering by level surfaces only matching rows."""
    log_file = tmp_path / "app.log"
    lines = [
        "2026-06-21 17:00:00,000 INFO  [-] app.foo :: routine progress",
        "2026-06-21 17:00:01,000 WARNING [-] app.bar :: something off",
        "2026-06-21 17:00:02,000 INFO  [discover_db/aabbccdd] app.services.role_discovery :: discovery starting",
        "2026-06-21 17:00:03,000 ERROR [-] app.qux :: oh no",
    ]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    import app.main as main_mod
    monkeypatch.setattr(main_mod, "APP_LOG_PATH", log_file)

    r = admin_client.get("/admin/logs")
    assert r.status_code == 200
    assert "routine progress" in r.text
    assert "something off" in r.text
    assert "oh no" in r.text

    r = admin_client.get("/admin/logs?level=WARNING")
    assert r.status_code == 200
    assert "something off" in r.text
    assert "routine progress" not in r.text
    assert "oh no" not in r.text

    r = admin_client.get("/admin/logs?op_id=discover_db")
    assert r.status_code == 200
    assert "discovery starting" in r.text
    assert "routine progress" not in r.text


# ─── Freshness chip helper (Track C / Slice 3) ────────────────────────────
# Lives in this file because it's a small, isolated templating helper; no need
# to spin up a dedicated test_freshness.py for a single function.


def test_freshness_class_buckets_by_age() -> None:
    """``_freshness_class`` returns emerald < 48h, amber 48h–7d, rose > 7d, and
    a neutral slate trio for ``None`` (no data yet)."""
    from datetime import datetime, timedelta

    from app.templating import _freshness_class

    now = datetime.utcnow()

    # None → neutral slate, no claim of freshness.
    assert _freshness_class(None) == "bg-slate-50 border-slate-200 text-slate-700"

    # Fresh (< 48h): emerald.
    assert _freshness_class(now - timedelta(minutes=10)) == "bg-emerald-50 border-emerald-200 text-emerald-800"
    assert _freshness_class(now - timedelta(hours=47)) == "bg-emerald-50 border-emerald-200 text-emerald-800"

    # Stale (48h < age <= 7d): amber.
    assert _freshness_class(now - timedelta(hours=49)) == "bg-amber-50 border-amber-200 text-amber-800"
    assert _freshness_class(now - timedelta(days=6)) == "bg-amber-50 border-amber-200 text-amber-800"

    # Very stale (> 7d): rose.
    assert _freshness_class(now - timedelta(days=8)) == "bg-rose-50 border-rose-200 text-rose-800"
    assert _freshness_class(now - timedelta(days=30)) == "bg-rose-50 border-rose-200 text-rose-800"


def test_freshness_dot_class_matches_chip_color() -> None:
    """The companion dot helper picks the variant that matches the chip
    background, so the visual stays in lockstep as data ages."""
    from datetime import datetime, timedelta

    from app.templating import _freshness_dot_class

    now = datetime.utcnow()
    assert _freshness_dot_class(None) == "fresh-dot fresh-dot-slate"
    assert _freshness_dot_class(now - timedelta(hours=1)) == "fresh-dot"
    assert _freshness_dot_class(now - timedelta(hours=72)) == "fresh-dot fresh-dot-amber"
    assert _freshness_dot_class(now - timedelta(days=10)) == "fresh-dot fresh-dot-rose"
