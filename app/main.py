import logging
import logging.handlers
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from app.log_context import OperationContextFilter

# Repo root + canonical app.log path. Kept module-level so the /admin/logs
# route can read from the same file the handler writes to without re-deriving
# the path.
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
APP_LOG_PATH = LOG_DIR / "app.log"

# Rotation knobs: 10 MB per file, keep 5 rotated copies → ~50 MB ceiling on
# disk for app.log. The systemd unit owns /var/www/compare-wages/logs as
# www-data so rotation succeeds inside the hardened sandbox.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


def _configure_logging() -> None:
    """Install a global root-logger configuration so every module's
    ``logging.getLogger(__name__)`` call produces output in a consistent shape.

    Driven by the ``LOG_LEVEL`` environment variable (default ``INFO``).
    Format: ``<asctime> <LEVEL> [<op_id>] <logger.name> :: <message>``. ``op_id``
    is a one-dash placeholder for non-orchestrator lines (see
    :mod:`app.log_context`); orchestrators stamp it via ``operation_context``.

    Two handlers: stdout (journalctl pickup) + a rotating file at
    ``logs/app.log`` (admin /admin/logs viewer + ``tail -f`` on the box). The
    file handler is wrapped in try/except so tests or fresh checkouts where
    ``logs/`` isn't writable still get stdout-only logging instead of an
    import-time crash.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s %(levelname)-5s [%(op_id)s] %(name)s :: %(message)s"
    formatter = logging.Formatter(fmt)
    op_filter = OperationContextFilter()

    # Force=True so we cleanly replace whatever ``basicConfig`` may have been
    # called with earlier in the process (uvicorn's reloader, pytest's capture).
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(op_filter)
    root.addHandler(stdout_handler)

    # File handler — best-effort. If logs/ isn't writable (CI, sandboxed tests,
    # a dev box without the dir mkdir'd), warn once and continue stdout-only.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            APP_LOG_PATH,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(op_filter)
        root.addHandler(file_handler)
    except OSError as e:
        # Don't crash. Operators reading stdout will see this warning and the
        # /admin/logs page will render the empty-state hint.
        logging.getLogger(__name__).warning(
            "could not open app.log for writing (%s); continuing stdout-only", e,
        )

    # Tame chatty third-party loggers that would otherwise drown out our signal.
    for noisy in (
        "httpx",
        "httpcore",
        "asyncio",
        "urllib3",
        "apscheduler.scheduler",
        "apscheduler.executors.default",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()

log = logging.getLogger(__name__)


import app.scrapers  # noqa: F401  populates the scraper registry on boot  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.routers import admin, dashboard  # noqa: E402
from app.scheduler import apply_config  # noqa: E402
from app.services.ingestion import mark_orphaned_runs_failed  # noqa: E402
from app.services.scraping import mark_orphaned_scraper_runs_failed  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a short request id to every request/response.

    On entry we mint an 8-char hex id (``secrets.token_hex(4)``), stash it on
    ``request.state.request_id``, and add it to the response as the
    ``X-Request-ID`` header so prod errors observed by a user / monitoring
    system can be correlated back to a specific request in the server logs.

    If the downstream handler raises, we log the exception with the request
    id and re-raise so FastAPI's normal error handling still runs.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        request_id = secrets.token_hex(4)
        request.state.request_id = request_id
        try:
            response: Response = await call_next(request)
        except Exception:
            log.exception(
                "unhandled exception for request %s %s [request_id=%s]",
                request.method,
                request.url.path,
                request_id,
            )
            raise
        response.headers["X-Request-ID"] = request_id
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    mark_orphaned_runs_failed()
    mark_orphaned_scraper_runs_failed()
    apply_config()
    yield


_settings = get_settings()
# NOTE: don't pass root_path here. nginx strips the /compare-wages prefix before
# forwarding (see /etc/nginx/sites-enabled/lab.kudithipudi.org), so the app sees
# bare paths like /static/css/app.css. Setting FastAPI root_path makes Starlette's
# Mount routing expect the prefix to still be present, and the /static mount 404s.
# Templates still get the public prefix via ROOT_PATH env -> {{ prefix }} global.
app = FastAPI(
    title="ACME Competitive Wage Intelligence",
    lifespan=lifespan,
)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret,
    session_cookie="admin_session",
    max_age=_settings.session_max_age_seconds,
    same_site="lax",
    # Auto-on when behind a sub-path proxy (a stand-in heuristic for "we're on HTTPS"); flip
    # explicitly in dev if you need cookies over http (Browsers ignore Secure-flagged cookies
    # on http origins).
    https_only=bool(_settings.root_path),
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(dashboard.router)
app.include_router(admin.auth_router)
app.include_router(admin.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
