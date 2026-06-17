#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/python -m app.seed
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
