"""Seed the database with 10 Copart locations, 5 competitors, sample job postings, and BEA RPP.

Idempotent: safe to re-run. Wipes job_postings on each run so the ingestion pipeline
can re-extract from the same raw sample HTML and produce a clean LlmCall trail.

SAFETY: see `run_seed()` — refuses to wipe a populated production DB unless explicitly
allowed via `ALLOW_PROD_SEED=1` env var or `force=True`.
"""
from __future__ import annotations

import csv
import json
import os
import random
from pathlib import Path
from typing import Iterable

from sqlalchemy import delete

from app.db import init_db, session_scope
from app.models import (
    BeaRpp,
    BlsOewsWage,
    CbsaName,
    Competitor,
    CompetitorLocation,
    CopartLocation,
    JobPosting,
    LlmCall,
    LlmModelConfig,
    Narrative,
    RoleMapping,
    ScheduleConfig,
    ScrapeRun,
    ZipCbsa,
)
from app.seed_data import COPART_YARDS as YARDS_RAW, STATE_BASE_WAGE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TEMPLATE_DIR = DATA_DIR / "sample_postings"


def _wage_for(state: str, code: str) -> float:
    base = STATE_BASE_WAGE.get(state, 15.0)
    jitter = ((sum(ord(c) for c in code) % 11) - 5) / 10.0
    return round(base + jitter, 2)


# (code, name, address, city, state, zip, lat, lng, copart_hourly_wage)
COPART_YARDS: list[tuple[str, str, str, str, str, str, float, float, float]] = [
    (code, name, addr, city, st, zip_, lat, lng, _wage_for(st, code))
    for (code, name, addr, city, st, zip_, lat, lng) in YARDS_RAW
]

# (name, source_priority, source_tier, careers_url)
COMPETITORS: list[tuple[str, int, str, str]] = [
    ("Walmart", 2, "employer_owned", "https://careers.walmart.com/"),
    ("Amazon", 1, "employer_owned", "https://www.amazon.jobs/"),
    ("Home Depot", 2, "employer_owned", "https://careers.homedepot.com/"),
    ("Costco", 1, "employer_owned", "https://www.costco.com/jobs.html"),
    ("Starbucks", 2, "employer_owned", "https://www.starbucks.com/careers/"),
]

# (copart_role, competitor_role, bucket, confidence, competitor_name_or_None)
# competitor_name is the competitor whose vernacular this title comes from.
# None = generic mapping that applies regardless of competitor.
ROLE_MAPPINGS: list[tuple[str, str, str, float, str | None]] = [
    # outdoor — yard / warehouse / lot work
    ("Yard Attendant", "Warehouse Associate",    "outdoor", 0.85, None),
    ("Yard Attendant", "Fulfillment Associate",  "outdoor", 0.85, "Amazon"),
    ("Yard Attendant", "Lot Associate",          "outdoor", 0.90, "Home Depot"),
    ("Vehicle Handler", "Material Handler",       "outdoor", 0.88, None),
    ("Vehicle Handler", "Sortation Associate",    "outdoor", 0.80, "Amazon"),
    ("Vehicle Handler", "Freight Associate",      "outdoor", 0.82, "Home Depot"),
    ("Vehicle Handler", "Receiving Associate",    "outdoor", 0.80, "Home Depot"),
    ("Loader",         "Loader",                 "outdoor", 0.95, None),
    ("Loader",         "Warehouse Operator",     "outdoor", 0.85, "Amazon"),
    # indoor — register / customer service / office
    ("CSR",            "Cashier",                "indoor",  0.85, "Walmart"),
    ("CSR",            "Cashier",                "indoor",  0.85, "Home Depot"),
    ("CSR",            "Customer Service Associate","indoor",0.90, None),
    ("CSR",            "Front End Assistant",    "indoor",  0.80, "Costco"),
    ("CSR",            "Barista",                "indoor",  0.55, "Starbucks"),
    ("Dispatch",       "Logistics Coordinator",  "indoor",  0.85, None),
    ("Dispatch",       "Shift Supervisor",       "indoor",  0.65, "Starbucks"),
    ("Admin",          "Office Associate",       "indoor",  0.80, None),
    ("Admin",          "Stocking Associate",     "indoor",  0.40, "Walmart"),
    ("Admin",          "Service Deli Clerk",     "indoor",  0.40, "Costco"),
]

# Wage ranges per (employer, state, bucket) -> (low, high)
WAGE_TABLE: dict[tuple[str, str, str], tuple[float, float]] = {}
def _populate_wage_table() -> None:
    state_factor = {
        "CA": 1.18, "NY": 1.16, "NJ": 1.15, "WA": 1.13, "MA": 1.13,
        "IL": 1.05, "CO": 1.04, "OR": 1.03,
        "AZ": 0.98, "FL": 0.98, "TX": 0.97,
        "NC": 0.94, "GA": 0.95,
        "OH": 0.92, "PA": 0.96, "TN": 0.93,
    }
    base = {
        # outdoor (warehouse-ish), indoor (retail-ish)
        "Walmart":   {"outdoor": (16.0, 20.0), "indoor": (15.0, 18.0)},
        "Amazon":    {"outdoor": (18.5, 22.0), "indoor": (17.5, 20.5)},
        "Home Depot":{"outdoor": (16.5, 20.0), "indoor": (15.5, 18.5)},
        "Costco":    {"outdoor": (19.5, 22.5), "indoor": (19.0, 22.0)},
        "Starbucks": {"outdoor": (15.5, 18.5), "indoor": (16.0, 19.5)},
    }
    for emp, by_bucket in base.items():
        for bucket, (lo, hi) in by_bucket.items():
            for state in {y[4] for y in COPART_YARDS} | set(state_factor):
                f = state_factor.get(state, 0.96)
                WAGE_TABLE[(emp, state, bucket)] = (round(lo * f, 2), round(hi * f, 2))

_populate_wage_table()

# Role pools per (employer, bucket) — varied free-text titles so the LLM classifier actually works
ROLE_POOL: dict[tuple[str, str], list[str]] = {
    ("Walmart",    "outdoor"): ["Stocking Associate", "Freight Handler", "Order Filler"],
    ("Walmart",    "indoor"):  ["Cashier", "Customer Host", "Self-Checkout Host"],
    ("Amazon",     "outdoor"): ["Warehouse Operator", "Sortation Associate - Night Shift", "Fulfillment Associate"],
    ("Amazon",     "indoor"):  ["Customer Service Associate", "Returns Specialist"],
    ("Home Depot", "outdoor"): ["Lot Associate", "Freight Associate", "Receiving Associate"],
    ("Home Depot", "indoor"):  ["Cashier", "Customer Service Associate"],
    ("Costco",     "outdoor"): ["Front End Assistant (Cart)", "Stocker", "Receiving Clerk"],
    ("Costco",     "indoor"):  ["Service Deli Clerk", "Front End Assistant", "Cashier"],
    ("Starbucks",  "outdoor"): ["Drive-Thru Barista"],
    ("Starbucks",  "indoor"):  ["Barista", "Shift Supervisor"],
}

ROLE_BLURBS: dict[str, str] = {
    "default": "perform safe and efficient work supporting daily operations",
    "Cashier": "process customer transactions and provide friendly service",
    "Lot Associate": "load merchandise, retrieve carts, and support store operations from the lot",
    "Barista": "craft beverages and connect with customers",
    "Sortation Associate - Night Shift": "scan, sort, and stage packages during overnight operations",
}


def _render_html(employer: str, role: str, city: str, state: str, wage_low: float, wage_high: float, rng: random.Random) -> str:
    tpl = (TEMPLATE_DIR / f"{employer.lower().replace(' ', '')}.html").read_text()
    return tpl.format(
        role=role,
        city=city,
        state=state,
        wage_low=f"{wage_low:.2f}",
        wage_high=f"{wage_high:.2f}",
        store_id=str(rng.randint(100, 9999)),
        facility_code=f"{state}{rng.randint(1,9)}",
        posting_id=f"p{rng.randint(100000, 999999)}",
        role_blurb=ROLE_BLURBS.get(role, ROLE_BLURBS["default"]),
    )


def _wipe_for_reseed(session) -> None:
    session.execute(delete(LlmCall))
    session.execute(delete(Narrative))
    session.execute(delete(JobPosting))
    session.execute(delete(CompetitorLocation))
    session.execute(delete(RoleMapping))
    session.execute(delete(Competitor))
    session.execute(delete(CopartLocation))
    session.execute(delete(BeaRpp))
    session.execute(delete(BlsOewsWage))
    session.execute(delete(ZipCbsa))
    session.execute(delete(CbsaName))
    session.execute(delete(ScrapeRun))
    session.execute(delete(ScheduleConfig))
    session.execute(delete(LlmModelConfig))


def run_seed(*, store_raw_html_to_disk: bool = True, force: bool = False) -> None:
    """Wipe + re-seed the DB.

    SAFETY: refuses to run against the production DB path (`data/wages.db`) if rows already
    exist, unless `force=True` or the env var `ALLOW_PROD_SEED=1` is set. This catches the
    "I rm'd the prod DB by accident running dev work in /var/www/compare-wages" mistake.

    Prefer setting `DATABASE_URL=sqlite:///./data/wages_dev.db` (or pointing at any other
    file) when iterating locally. The systemd unit always points at `data/wages.db`.
    """
    init_db()

    from app.config import get_settings as _gs
    db_url = _gs().database_url
    looks_like_prod = db_url.endswith("/data/wages.db") or db_url.endswith(":./data/wages.db")
    allowed = force or os.environ.get("ALLOW_PROD_SEED") == "1"
    if looks_like_prod and not allowed:
        from sqlalchemy import select as _select
        with session_scope() as s:
            existing = s.execute(_select(CopartLocation).limit(1)).first()
        if existing:
            raise SystemExit(
                "REFUSING to wipe a populated DB at the production path "
                "(DATABASE_URL=" + db_url + "). To override:\n"
                "  • set ALLOW_PROD_SEED=1 in the env, OR pass force=True from Python, OR\n"
                "  • point DATABASE_URL at a dev DB: "
                "DATABASE_URL=sqlite:///./data/wages_dev.db .venv/bin/python -m app.seed"
            )

    rng = random.Random(42)

    with session_scope() as s:
        _wipe_for_reseed(s)
        s.flush()

        # BEA RPP
        with open(DATA_DIR / "bea_rpp.csv") as f:
            for row in csv.DictReader(f):
                s.add(BeaRpp(state=row["state"], rpp=float(row["rpp"]), year=int(row["year"])))

        # HUD ZIP→CBSA crosswalk (one row per ZIP, highest bus_ratio wins)
        zc_csv = DATA_DIR / "zip_to_cbsa.csv"
        zip_to_cbsa: dict[str, str] = {}
        if zc_csv.exists():
            with open(zc_csv) as f:
                for row in csv.DictReader(f):
                    s.add(ZipCbsa(
                        zip=row["zip"], cbsa_code=row["cbsa_code"],
                        city=row.get("city", ""), state=row.get("state", ""),
                    ))
                    zip_to_cbsa[row["zip"]] = row["cbsa_code"]

        # Census Bureau CBSA names (code → "Los Angeles-Long Beach-Anaheim, CA")
        cn_csv = DATA_DIR / "cbsa_names.csv"
        if cn_csv.exists():
            with open(cn_csv) as f:
                for row in csv.DictReader(f):
                    s.add(CbsaName(
                        cbsa_code=row["cbsa_code"],
                        cbsa_title=row["cbsa_title"],
                    ))

        # BLS OEWS state-level baseline (5 occupations × 51 jurisdictions)
        bls_csv = DATA_DIR / "bls_oews_2023.csv"
        if bls_csv.exists():
            with open(bls_csv) as f:
                for row in csv.DictReader(f):
                    s.add(BlsOewsWage(
                        state=row["state"],
                        occ_code=row["occ_code"],
                        year=int(row["year"]),
                        occ_title=row["occ_title"],
                        bucket=row["bucket"],
                        mean_hourly=float(row["mean_hourly"]),
                        p10=float(row["p10"]),
                        p25=float(row["p25"]),
                        p50=float(row["p50"]),
                        p75=float(row["p75"]),
                        p90=float(row["p90"]),
                    ))

        # Copart yards — cbsa_code resolved via the ZIP→CBSA crosswalk loaded above.
        copart_objs: list[CopartLocation] = []
        for code, name, addr, city, state, zip_, lat, lng, wage in COPART_YARDS:
            obj = CopartLocation(
                code=code, name=name, address=addr, city=city, state=state, zip=zip_,
                lat=lat, lng=lng, copart_hourly_wage=wage, active=False,
                cbsa_code=zip_to_cbsa.get(zip_.zfill(5), ""),
            )
            s.add(obj)
            copart_objs.append(obj)

        # Competitors
        competitor_objs: dict[str, Competitor] = {}
        for name, prio, tier, url in COMPETITORS:
            c = Competitor(name=name, source_priority=prio, source_tier=tier, careers_url=url)
            s.add(c)
            competitor_objs[name] = c

        s.flush()  # competitors need to be flushed so we can FK to them

        # Role mappings. (copart_role, competitor_role, bucket, confidence,
        # competitor_name_or_None). Some titles are competitor-specific (Lot Associate
        # is Home Depot's term); generic ones (Warehouse Associate) get None.
        seeded_competitors = {c.name: c for c in competitor_objs.values()}
        for copart_role, comp_role, bucket, conf, comp_name in ROLE_MAPPINGS:
            cid = seeded_competitors[comp_name].id if comp_name else None
            s.add(RoleMapping(
                competitor_id=cid,
                copart_role=copart_role, competitor_role=comp_role,
                bucket=bucket, confidence=conf,
            ))

        s.flush()  # so foreign keys resolve

        # Competitor locations + raw job postings (unextracted — ingestion will run extraction)
        raw_dir = DATA_DIR / "raw_html"
        raw_dir.mkdir(exist_ok=True)
        for yard in copart_objs:
            for comp_name, competitor in competitor_objs.items():
                # 1 competitor location per (competitor × yard) — sized to keep total postings
                # manageable across ~150 yards while still producing both outdoor + indoor signal.
                c_lat = yard.lat + rng.uniform(-0.18, 0.18)
                c_lng = yard.lng + rng.uniform(-0.22, 0.22)
                cl = CompetitorLocation(
                    competitor_id=competitor.id,
                    name=f"{comp_name} {yard.city} #{rng.randint(100,9999)}",
                    city=yard.city, state=yard.state, lat=c_lat, lng=c_lng,
                )
                s.add(cl)
                s.flush()

                for bucket in ("outdoor", "indoor"):
                        pool = ROLE_POOL.get((comp_name, bucket), [])
                        if not pool:
                            continue
                        role = rng.choice(pool)
                        lo, hi = WAGE_TABLE.get((comp_name, yard.state, bucket), (15.0, 18.0))
                        # add a little jitter
                        lo_j = round(lo + rng.uniform(-0.5, 0.5), 2)
                        hi_j = round(hi + rng.uniform(-0.5, 0.8), 2)
                        if hi_j <= lo_j:
                            hi_j = round(lo_j + 1.0, 2)

                        html = _render_html(comp_name, role, yard.city, yard.state, lo_j, hi_j, rng)
                        raw_path = ""
                        if store_raw_html_to_disk:
                            fname = f"{comp_name.lower().replace(' ','')}_{yard.code}_{cl.id}_{bucket}.html"
                            (raw_dir / fname).write_text(html)
                            raw_path = str((raw_dir / fname).relative_to(DATA_DIR.parent))

                        posting = JobPosting(
                            competitor_id=competitor.id,
                            competitor_location_id=cl.id,
                            raw_title=role,
                            normalized_role=None,
                            role_bucket=None,
                            wage_low=None,
                            wage_high=None,
                            wage_unit=None,
                            extraction_confidence=None,
                            # Synthetic seed data — marked distinctly so observations_for_yard
                            # excludes it from the production view by default. Live scrapers
                            # write source_tier='employer_owned' for real postings.
                            source_tier="seed",
                            source_url=f"{competitor.careers_url}jobs/sample-{cl.id}-{bucket}",
                            raw_html_path=raw_path,
                        )
                        s.add(posting)

        # Default schedule config
        s.add(ScheduleConfig(cron_expression="0 6 * * 1", enabled=False))

        # Default per-purpose LLM model selection.
        s.add(LlmModelConfig(
            purpose="extraction", model="anthropic/claude-haiku-4.5", temperature=0.1,
            notes="Structured wage extraction — needs reliable JSON output, low cost per call.",
        ))
        s.add(LlmModelConfig(
            purpose="classification", model="openai/gpt-4o-mini", temperature=0.0,
            notes="Title → role bucket. High volume, simple decision — pick the cheapest competent model.",
        ))
        s.add(LlmModelConfig(
            purpose="narrative", model="anthropic/claude-haiku-4.5", temperature=0.4,
            notes="Executive narrative. Defaulting to Haiku because it's a known-working model on OpenRouter; pick a stronger model via /admin/llm-models if your key has access.",
        ))

    print("seed: done")


if __name__ == "__main__":
    run_seed()
