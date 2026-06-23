from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CopartLocation(Base):
    __tablename__ = "copart_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    address: Mapped[str] = mapped_column(String)
    city: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, index=True)
    zip: Mapped[str] = mapped_column(String)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    copart_hourly_wage: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # CBSA (Core-Based Statistical Area) code, populated from data/zip_to_cbsa.csv at seed.
    # Empty when ZIP doesn't map to any CBSA (rural yards outside metro/micro areas).
    cbsa_code: Mapped[str] = mapped_column(String, default="", index=True)


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    source_priority: Mapped[int] = mapped_column(Integer, default=2)
    source_tier: Mapped[str] = mapped_column(String, default="employer_owned")
    careers_url: Mapped[str] = mapped_column(String, default="")

    locations: Mapped[list["CompetitorLocation"]] = relationship(back_populates="competitor")


class CompetitorLocation(Base):
    __tablename__ = "competitor_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    city: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)

    competitor: Mapped["Competitor"] = relationship(back_populates="locations")


class RoleMapping(Base):
    __tablename__ = "role_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable: NULL means "applies to all competitors" (generic mapping). A specific
    # competitor_id means "this mapping is for that competitor's vernacular". The scraping
    # service uses (competitor_id == X OR competitor_id IS NULL) to build keywords.
    competitor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("competitors.id"), nullable=True, index=True
    )
    copart_role: Mapped[str] = mapped_column(String)
    competitor_role: Mapped[str] = mapped_column(String)
    bucket: Mapped[str] = mapped_column(String)  # "outdoor" | "indoor"
    confidence: Mapped[float] = mapped_column(Float, default=0.8)


class RoleDiscoverySuggestion(Base):
    """A candidate ``raw_title`` mined from existing ``JobPosting`` rows that the
    operator hasn't yet decided to map (or reject) on ``/admin/role-mappings``.

    The Role Discovery workflow (``app/services/role_discovery.py``) writes pending
    rows; the operator reviews on ``/admin/role-discovery`` and either accepts
    (which materializes a ``RoleMapping`` row that the next scrape will use) or
    rejects (so future re-runs don't keep re-surfacing the same title).
    """
    __tablename__ = "role_discovery_suggestions"
    __table_args__ = (
        # Re-runs MUST upsert, not collide. Per-(competitor, title) uniqueness is the
        # join key the orchestrator uses to skip already-decided suggestions.
        UniqueConstraint("competitor_id", "raw_title", name="uq_role_discovery_comp_title"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    raw_title: Mapped[str] = mapped_column(String)
    suggested_bucket: Mapped[str] = mapped_column(String)  # outdoor|indoor|not_relevant
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|accepted|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # V1 always writes "existing_postings". V2 will add "careers_search" for active
    # discovery against the employer's careers site instead of mining the DB.
    source: Mapped[str] = mapped_column(String, default="existing_postings")


class JobPosting(Base):
    __tablename__ = "job_postings"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    competitor_location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("competitor_locations.id"), nullable=True)
    raw_title: Mapped[str] = mapped_column(String)
    normalized_role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    role_bucket: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # outdoor|indoor
    wage_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    wage_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    wage_unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # hourly|annual
    extraction_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_tier: Mapped[str] = mapped_column(String, default="sample")  # "seed" (synthetic, demo only) | "employer_owned" (live scrape)
    source_url: Mapped[str] = mapped_column(String, default="")
    raw_html_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BeaRpp(Base):
    __tablename__ = "bea_rpp"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    rpp: Mapped[float] = mapped_column(Float)
    year: Mapped[int] = mapped_column(Integer, default=2023)


class ZipCbsa(Base):
    """HUD USPS ZIP→CBSA crosswalk. One row per ZIP (the load step collapses
    multi-ZIP rows by picking the highest bus_ratio winner).
    Source: HUD USPS API (https://www.huduser.gov/portal/dataset/uspszip-api.html).
    """
    __tablename__ = "zip_cbsa"

    zip: Mapped[str] = mapped_column(String, primary_key=True)
    cbsa_code: Mapped[str] = mapped_column(String, index=True)
    city: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="")


class CbsaName(Base):
    """CBSA code → human title (e.g. 31080 → "Los Angeles-Long Beach-Anaheim, CA").
    Source: Census Bureau CBSA delineation file."""
    __tablename__ = "cbsa_names"

    cbsa_code: Mapped[str] = mapped_column(String, primary_key=True)
    cbsa_title: Mapped[str] = mapped_column(String)


class BlsOewsWage(Base):
    """BLS Occupational Employment & Wage Statistics — state × occupation × year."""
    __tablename__ = "bls_oews_wages"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    occ_code: Mapped[str] = mapped_column(String, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True, default=2023)
    occ_title: Mapped[str] = mapped_column(String)
    bucket: Mapped[str] = mapped_column(String)  # outdoor|indoor
    mean_hourly: Mapped[float] = mapped_column(Float)
    p10: Mapped[float] = mapped_column(Float)
    p25: Mapped[float] = mapped_column(Float)
    p50: Mapped[float] = mapped_column(Float)
    p75: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running|success|failed
    triggered_by: Mapped[str] = mapped_column(String, default="manual")  # manual|scheduled
    postings_collected: Mapped[int] = mapped_column(Integer, default=0)
    extraction_success: Mapped[int] = mapped_column(Integer, default=0)
    extraction_failed: Mapped[int] = mapped_column(Integer, default=0)
    # Postings where the LLM call returned cleanly but no wage was disclosed
    # (e.g. Home Depot in non-pay-transparency states). Honest data outcome — NOT a bug.
    # Kept distinct from `extraction_failed` (transport/parse failures) so operators
    # can triage real failures without being drowned in expected no-wage misses.
    extraction_no_wage_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Comma-separated yard codes when the run was scoped; empty string = full run.
    scope_yard_codes: Mapped[str] = mapped_column(String, default="")


class ScraperRun(Base):
    """One run of an employer scraper (e.g. Home Depot Playwright). Separate from
    ScrapeRun (which is the LLM-extraction pipeline) so the two surfaces don't bleed.
    """
    __tablename__ = "scraper_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running|success|failed|blocked
    competitor_name: Mapped[str] = mapped_column(String, index=True)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    postings_saved: Mapped[int] = mapped_column(Integer, default=0)
    # Mirror of ScrapeRun.extraction_no_wage_found — the auto-extract pass that follows a
    # scrape records its no-wage count here so operators see triage-friendly counters on
    # the Scrape Runs page without having to cross-reference Ingestion Runs.
    extraction_no_wage_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String, default="manual")


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    purpose: Mapped[str] = mapped_column(String)  # extraction|classification|narrative
    model: Mapped[str] = mapped_column(String)
    mocked: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    validation_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_error: Mapped[str] = mapped_column(Text, default="")
    related_posting_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ScheduleConfig(Base):
    __tablename__ = "schedule_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    cron_expression: Mapped[str] = mapped_column(String, default="0 6 * * *")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class LlmModelConfig(Base):
    __tablename__ = "llm_model_config"

    purpose: Mapped[str] = mapped_column(String, primary_key=True)  # extraction|classification|narrative
    model: Mapped[str] = mapped_column(String)
    temperature: Mapped[float] = mapped_column(Float, default=0.1)
    notes: Mapped[str] = mapped_column(String, default="")


class Narrative(Base):
    __tablename__ = "narratives"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String)  # national|state|location
    scope_key: Mapped[str] = mapped_column(String, default="US")
    body: Mapped[str] = mapped_column(Text)
    grounding: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WageSnapshot(Base):
    """A point-in-time record of a yard's blended competitive wage + gap.

    Written by the ingestion orchestrator at the end of every run. Lets the
    dashboard show trends ("gap widened from +$0.40 to +$1.40 over 8 weeks")
    instead of just today's still photo. One row per (yard, run) tuple.
    """
    __tablename__ = "wage_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    yard_id: Mapped[int] = mapped_column(ForeignKey("copart_locations.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    copart_wage: Mapped[float] = mapped_column(Float)
    blended_competitive_wage: Mapped[float] = mapped_column(Float, default=0.0)
    gap: Mapped[float] = mapped_column(Float, default=0.0)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    pressure_quartile: Mapped[int] = mapped_column(Integer, default=0)
