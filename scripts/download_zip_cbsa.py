"""Download HUD USPS ZIP→CBSA crosswalk for all 50 states + DC, collapse one row per ZIP
(pick the CBSA with the highest bus_ratio — that's where the jobs are), and write to
`data/zip_to_cbsa.csv`.

Requires HUD_API_TOKEN in the env. Token is a long-lived JWT obtained from the HUD User
portal at https://www.huduser.gov/portal/dataset/uspszip-api.html.

Run: .venv/bin/python scripts/download_zip_cbsa.py
"""
from __future__ import annotations

import csv
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "zip_to_cbsa.csv"

HUD_URL = "https://www.huduser.gov/hudapi/public/usps"
STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]


def main() -> None:
    token = os.environ.get("HUD_API_TOKEN")
    if not token:
        # Fallback: read from .env
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("HUD_API_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        print("HUD_API_TOKEN missing in env or .env", file=sys.stderr)
        sys.exit(1)

    headers = {"Authorization": f"Bearer {token}"}
    year_quarter = os.environ.get("HUD_YQ", "2024Q1")  # year=2024, quarter=1
    year, quarter = year_quarter[:4], year_quarter[5]

    # ZIP → list of (cbsa, bus_ratio, city, state)
    per_zip: dict[str, list[tuple[str, float, str, str]]] = defaultdict(list)
    total_rows = 0

    with httpx.Client(timeout=60.0) as client:
        for st in STATES:
            r = client.get(
                HUD_URL,
                params={"type": 3, "query": st, "year": year, "quarter": quarter},
                headers=headers,
            )
            if r.status_code != 200:
                print(f"  {st}: HTTP {r.status_code} — skipping ({r.text[:100]})", file=sys.stderr)
                continue
            data = r.json()
            results = data.get("data", {}).get("results", [])
            for row in results:
                zip_ = str(row["zip"]).zfill(5)
                cbsa = str(row.get("geoid") or "").strip()
                if not cbsa or cbsa in ("99999", "0"):
                    continue
                bus = float(row.get("bus_ratio") or 0)
                per_zip[zip_].append((cbsa, bus, row.get("city", ""), row.get("state", st)))
            total_rows += len(results)
            print(f"  {st}: {len(results)} rows (running zip count: {len(per_zip)})")
            time.sleep(0.4)  # be polite to HUD

    print(f"\ntotal raw rows fetched: {total_rows}")
    print(f"unique zips: {len(per_zip)}")

    # Collapse: per ZIP pick the CBSA with the highest bus_ratio (jobs > residents for
    # wage comparisons). Ties broken by tot_ratio (here just first row, since we already
    # sorted by bus desc).
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["zip", "cbsa_code", "bus_ratio", "city", "state"])
        for zip_, candidates in sorted(per_zip.items()):
            candidates.sort(key=lambda t: t[1], reverse=True)
            cbsa, bus, city, st = candidates[0]
            w.writerow([zip_, cbsa, f"{bus:.4f}", city, st])
    print(f"wrote {len(per_zip)} rows to {OUT}")


if __name__ == "__main__":
    main()
