import os
from pathlib import Path

os.environ["USE_MOCK_LLM"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///./data/test_wages.db"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _REPO_ROOT / "data" / "test_wages.db"
if _DB_PATH.exists():
    _DB_PATH.unlink()

import pytest

from app.config import get_settings
from app.db import init_db, session_scope
from app.seed import run_seed


get_settings.cache_clear()
_ = get_settings()


@pytest.fixture
def db_session():
    init_db()
    with session_scope() as s:
        yield s


@pytest.fixture(scope="session")
def _seeded_once():
    run_seed(store_raw_html_to_disk=True)
    yield


@pytest.fixture
def seeded_session(_seeded_once):
    with session_scope() as s:
        yield s


def pytest_sessionfinish(session, exitstatus):
    if _DB_PATH.exists():
        try:
            _DB_PATH.unlink()
        except OSError:
            pass
