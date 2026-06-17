"""Regenerate data/bls_oews_2023.csv from data/bls_oews_source.py.

Run: .venv/bin/python scripts/generate_bls_oews.py
"""
from __future__ import annotations

import csv
from pathlib import Path

# Allow `python scripts/...` to find the data package.
ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from data.bls_oews_source import OCCUPATIONS, PCT_RATIO, STATE_FACTOR, YEAR  # noqa: E402

OUT = ROOT / "data" / "bls_oews_2023.csv"


def main() -> None:
    rows: list[dict] = []
    for state, factor in sorted(STATE_FACTOR.items()):
        for occ_code, (title, bucket, nat_mean) in OCCUPATIONS.items():
            mean = round(nat_mean * factor, 2)
            row = {
                "state": state,
                "occ_code": occ_code,
                "occ_title": title,
                "bucket": bucket,
                "year": YEAR,
                "mean_hourly": mean,
            }
            for pct, ratio in PCT_RATIO.items():
                row[pct] = round(mean * ratio, 2)
            rows.append(row)

    fieldnames = ["state", "occ_code", "occ_title", "bucket", "year",
                  "mean_hourly", "p10", "p25", "p50", "p75", "p90"]
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
