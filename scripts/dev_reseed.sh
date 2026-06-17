#!/usr/bin/env bash
# Re-seed a DEV DB without ever touching prod. Always uses ./data/wages_dev.db.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f data/wages_dev.db data/test_wages.db
DATABASE_URL="sqlite:///./data/wages_dev.db" .venv/bin/python -m app.seed
echo
echo "Dev DB seeded at data/wages_dev.db"
echo "To run uvicorn against it:"
echo "  DATABASE_URL=sqlite:///./data/wages_dev.db .venv/bin/uvicorn app.main:app --reload"
