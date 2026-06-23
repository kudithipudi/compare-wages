"""Operation-context tagging for log lines.

Orchestrators (``run_ingestion``, ``run_scrape``, ``discover_from_*``) wrap
their bodies in :func:`operation_context` so every log line emitted inside
— including from background threads they spawn, async tasks they await, and
sub-services they call — shares a single ``op_id`` string of the shape
``"<kind>/<8-hex>"``. The :class:`OperationContextFilter` injects that id
onto every :class:`logging.LogRecord` so the configured formatter can
render it inline (see :func:`app.main._configure_logging`).

Why ``contextvars`` and not a thread-local? FastAPI runs handlers in
threadpools AND in the asyncio event loop. A thread-local would leak
``op_id`` between requests served by the same worker thread, and would be
empty on tasks suspended-then-resumed by ``asyncio``. ``contextvars`` give
us correct propagation across both axes — and ``threading.Thread`` started
inside a context inherits the parent's vars by default (Python 3.7+), so
the orchestrators' daemon-thread workers see the same ``op_id`` without
any explicit hand-off.
"""
from __future__ import annotations

import contextvars
import logging
import uuid
from contextlib import contextmanager
from typing import Iterator

op_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("op_id", default="-")


@contextmanager
def operation_context(op_kind: str) -> Iterator[str]:
    """Wrap an orchestrator body so every log line inside shares an op_id tag.

    ``op_kind`` is a short stable label (e.g. ``"ingest"``, ``"scrape"``,
    ``"discover_db"``) that prefixes the per-call hex suffix. Operators filter
    by either the prefix (all runs of a kind) or the full id (one specific
    invocation) on ``/admin/logs``.
    """
    op_id = f"{op_kind}/{uuid.uuid4().hex[:8]}"
    token = op_id_var.set(op_id)
    try:
        yield op_id
    finally:
        op_id_var.reset(token)


class OperationContextFilter(logging.Filter):
    """Inject ``op_id`` onto every LogRecord so the formatter can render it.

    Always returns True — this is a pass-through that mutates the record,
    not a real predicate filter. Default value is ``"-"`` (a single dash)
    so log lines outside any operation read cleanly in the file.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.op_id = op_id_var.get()
        return True
