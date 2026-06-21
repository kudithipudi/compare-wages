"""Tests for the role-discovery orchestrator + admin routes.

The harness sets ``USE_MOCK_LLM=true`` (see ``conftest.py``) so the LLM batch
call goes through ``_mock_classify_titles_batch`` — deterministic, no network.
That's also what makes the assertions stable: any title containing 'warehouse'
or 'loader' lands as outdoor; 'clerk' or 'cashier' as indoor; everything else
as not_relevant.
"""
from __future__ import annotations

from sqlalchemy import select
from starlette.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import (
    Competitor,
    CompetitorLocation,
    JobPosting,
    RoleDiscoverySuggestion,
    RoleMapping,
)
from app.services import role_discovery as role_discovery_module
from app.services.role_discovery import (
    _unmapped_titles_for_competitor,
    discover_from_existing_postings,
    discover_from_web_search,
)


def _make_competitor(name: str) -> int:
    with session_scope() as s:
        c = Competitor(name=name, source_priority=2, source_tier="employer_owned", careers_url="")
        s.add(c)
        s.flush()
        return c.id


def _add_competitor_location(s, competitor_id: int) -> int:
    cl = CompetitorLocation(
        competitor_id=competitor_id,
        name="Test", city="Springfield", state="IL", lat=39.78, lng=-89.65,
    )
    s.add(cl)
    s.flush()
    return cl.id


def _seed_postings(competitor_id: int, titles: list[str]) -> None:
    with session_scope() as s:
        loc_id = _add_competitor_location(s, competitor_id)
        for t in titles:
            s.add(JobPosting(
                competitor_id=competitor_id,
                competitor_location_id=loc_id,
                raw_title=t,
                source_tier="employer_owned",
                source_url="https://example.test/job",
            ))


def test_discover_writes_pending_suggestions(seeded_session):
    """Run discovery against a fresh competitor with a few JobPostings — verify
    pending suggestions land with the mock-LLM substring buckets."""
    competitor_id = _make_competitor("DiscoveryCo")
    _seed_postings(competitor_id, [
        "Warehouse Operator",
        "Title Clerk",
        "Marketing Director",       # not_relevant — no hint
        "Forklift Driver",
    ])

    with session_scope() as s:
        stats = discover_from_existing_postings(s, competitor_id=competitor_id)
        assert stats["new_suggestions"] == 4
        assert stats["refreshed_suggestions"] == 0
        assert stats["processed_titles"] == 4

    with session_scope() as s:
        rows = list(s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.competitor_id == competitor_id
            ).order_by(RoleDiscoverySuggestion.raw_title)
        ).scalars())
        assert len(rows) == 4
        by_title = {r.raw_title: r for r in rows}
        # Each should still be pending.
        assert all(r.status == "pending" for r in rows)
        # Mock-LLM bucket rules.
        assert by_title["Warehouse Operator"].suggested_bucket == "outdoor"
        assert by_title["Forklift Driver"].suggested_bucket == "outdoor"
        assert by_title["Title Clerk"].suggested_bucket == "indoor"
        assert by_title["Marketing Director"].suggested_bucket == "not_relevant"


def test_already_mapped_titles_are_not_resuggested(seeded_session):
    """A raw_title that ALREADY has a RoleMapping row must not appear as a
    suggestion. This is the loop-closing invariant of the workflow."""
    competitor_id = _make_competitor("AlreadyMappedCo")
    _seed_postings(competitor_id, [
        "Warehouse Operator",
        "Title Clerk",
    ])
    # Pre-map "Warehouse Operator" — discovery should skip it.
    with session_scope() as s:
        s.add(RoleMapping(
            competitor_id=competitor_id,
            copart_role="Yard Attendant",
            competitor_role="Warehouse Operator",
            bucket="outdoor",
            confidence=0.9,
        ))

    with session_scope() as s:
        unmapped = _unmapped_titles_for_competitor(s, competitor_id)
        assert "Warehouse Operator" not in unmapped
        assert "Title Clerk" in unmapped

    with session_scope() as s:
        discover_from_existing_postings(s, competitor_id=competitor_id)

    with session_scope() as s:
        rows = list(s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.competitor_id == competitor_id
            )
        ).scalars())
        titles = {r.raw_title for r in rows}
        assert titles == {"Title Clerk"}


def test_rerun_does_not_resurface_accepted_or_rejected(seeded_session):
    """Operator-final decisions stick: an accepted or rejected suggestion must
    not bounce back to pending on the next discovery run."""
    competitor_id = _make_competitor("ReRunCo")
    _seed_postings(competitor_id, [
        "Warehouse Operator",  # will be accepted → RoleMapping → filtered out next run
        "Title Clerk",         # will be rejected → tombstoned
        "Forklift Driver",     # left pending
    ])
    with session_scope() as s:
        discover_from_existing_postings(s, competitor_id=competitor_id)

    # Accept the warehouse one and reject the clerk one.
    with session_scope() as s:
        warehouse = s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
            RoleDiscoverySuggestion.raw_title == "Warehouse Operator",
        )).scalar_one()
        clerk = s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
            RoleDiscoverySuggestion.raw_title == "Title Clerk",
        )).scalar_one()
        # Mimic the accept route: write a RoleMapping then mark accepted.
        s.add(RoleMapping(
            competitor_id=competitor_id,
            copart_role="Yard Attendant",
            competitor_role=warehouse.raw_title,
            bucket=warehouse.suggested_bucket,
            confidence=warehouse.confidence,
        ))
        warehouse.status = "accepted"
        clerk.status = "rejected"

    # Re-run discovery — accepted disappears (it's now in role_mappings) and
    # rejected stays tombstoned (status doesn't get reset to pending).
    with session_scope() as s:
        stats = discover_from_existing_postings(s, competitor_id=competitor_id)
        # Title Clerk is still in the unmapped pool (no role_mapping was written
        # for it) and the orchestrator counts it as a skipped_existing.
        assert stats["skipped_existing"] >= 1
        # No NEW suggestion for Title Clerk (still rejected).
        assert stats["new_suggestions"] == 0

    with session_scope() as s:
        rows = list(s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id
        )).scalars())
        by_title = {r.raw_title: r for r in rows}
        # Warehouse Operator: still 'accepted', not back to pending.
        assert by_title["Warehouse Operator"].status == "accepted"
        # Title Clerk: still 'rejected'.
        assert by_title["Title Clerk"].status == "rejected"
        # Forklift Driver: still pending.
        assert by_title["Forklift Driver"].status == "pending"


def test_bulk_accept_creates_role_mappings(seeded_session):
    """The bulk-accept route writes RoleMapping rows for every pending
    outdoor/indoor suggestion above the confidence floor, skips not_relevant."""
    # Boot the admin session — bypasses login by setting the cookie directly via
    # the TestClient session middleware.
    competitor_id = _make_competitor("BulkAcceptCo")
    _seed_postings(competitor_id, [
        "Warehouse Operator",   # outdoor 0.85 — eligible
        "Title Clerk",          # indoor 0.80 — eligible
        "Marketing Director",   # not_relevant — skipped
    ])
    with session_scope() as s:
        discover_from_existing_postings(s, competitor_id=competitor_id)

    client = TestClient(app)
    # Forge an authed session via the cookie — same shape the login route writes.
    with client:
        # Set session via the middleware: we can't sign one directly without the
        # secret, so we use the in-app login form path. Configure credentials
        # via env on conftest if missing; otherwise we go straight through the
        # form. For this test we rely on the dependency-bypass approach instead:
        # patch the dep at app.dependency_overrides level.
        from app.security import require_admin
        app.dependency_overrides[require_admin] = lambda: None
        try:
            r = client.post(
                "/admin/role-discovery/bulk-accept",
                data={"min_confidence": "0.75"},
                follow_redirects=False,
            )
            assert r.status_code == 303
        finally:
            app.dependency_overrides.pop(require_admin, None)

    # Verify: outdoor + indoor became role_mappings; not_relevant did not.
    with session_scope() as s:
        mappings = list(s.execute(select(RoleMapping).where(
            RoleMapping.competitor_id == competitor_id,
        )).scalars())
        titles = {m.competitor_role for m in mappings}
        assert "Warehouse Operator" in titles
        assert "Title Clerk" in titles
        assert "Marketing Director" not in titles

        suggestions = list(s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
        )).scalars())
        by_title = {r.raw_title: r for r in suggestions}
        assert by_title["Warehouse Operator"].status == "accepted"
        assert by_title["Title Clerk"].status == "accepted"
        # not_relevant stays pending (it was filtered out of the bulk accept).
        assert by_title["Marketing Director"].status == "pending"


# ------------------------- V2: web-search discovery -------------------------


def _fake_search_results() -> list[dict]:
    """A small, deterministic batch of search results that the mock LLM
    extract will pull title-cased role-ish phrases out of."""
    return [
        {
            "title": "Forklift Driver jobs hiring · WebSearchCo",
            "snippet": (
                "Forklift Driver and Warehouse Associate openings are available. "
                "Customer Service Associate roles also posted."
            ),
            "url": "https://example.test/forklift",
        },
        {
            "title": "Cashier and Stocker positions · WebSearchCo Careers",
            "snippet": (
                "Cashier, Stocker, and Material Handler positions hire entry-level "
                "applicants. Apply today."
            ),
            "url": "https://example.test/cashier",
        },
    ]


def test_web_search_discovery_writes_suggestions(seeded_session, monkeypatch):
    """V2 happy path: a competitor with NO existing JobPosting rows still gets
    suggestions when web search returns hits — this is the bootstrap gap V1
    couldn't fill."""
    competitor_id = _make_competitor("WebSearchCo")

    monkeypatch.setattr(
        role_discovery_module.web_search,
        "search",
        lambda query, max_results=15: _fake_search_results(),
    )

    with session_scope() as s:
        stats = discover_from_web_search(s, competitor_id=competitor_id)
        # Four query templates × one fake-results call each.
        assert stats["queries_issued"] == 4
        # At least one new suggestion landed.
        assert stats["new_suggestions"] > 0
        # processed_competitors counts the loop, not the writes.
        assert stats["processed_competitors"] == 1

    with session_scope() as s:
        rows = list(s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.competitor_id == competitor_id
            )
        ).scalars())
        assert len(rows) > 0
        # Every V2-written row carries the new source string.
        assert all(r.source == "web_search" for r in rows)
        # Spot-check that a known role-ish phrase from the fake snippets
        # ("Forklift Driver" / "Warehouse Associate") landed as a suggestion.
        titles = {r.raw_title for r in rows}
        assert "Forklift Driver" in titles or "Warehouse Associate" in titles


def test_web_search_discovery_skips_existing_mappings(seeded_session, monkeypatch):
    """A raw_title that the mock search surfaces but which ALREADY has a
    RoleMapping row must not appear as a V2 suggestion. Same filter rule as
    V1 — we share the helper deliberately."""
    competitor_id = _make_competitor("WebSearchSkipCo")
    # Pre-map "Forklift Driver" so V2's filter must drop it.
    with session_scope() as s:
        s.add(RoleMapping(
            competitor_id=competitor_id,
            copart_role="Yard Attendant",
            competitor_role="Forklift Driver",
            bucket="outdoor",
            confidence=0.9,
        ))

    monkeypatch.setattr(
        role_discovery_module.web_search,
        "search",
        lambda query, max_results=15: _fake_search_results(),
    )

    with session_scope() as s:
        discover_from_web_search(s, competitor_id=competitor_id)

    with session_scope() as s:
        rows = list(s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.competitor_id == competitor_id
            )
        ).scalars())
        titles = {r.raw_title for r in rows}
        # Forklift Driver was pre-mapped — must not resurface.
        assert "Forklift Driver" not in titles
        # But other extracted titles (e.g. Warehouse Associate) should be present.
        assert len(titles) > 0


def test_web_search_discovery_no_duplicate_pending_from_v1(seeded_session, monkeypatch):
    """V1 + V2 over the same competitor with overlap: the (competitor_id,
    raw_title) unique constraint must yield exactly one row per title. V2's
    source-tiebreaker rule: only flip source to 'web_search' when web's
    confidence beats whatever's already there."""
    competitor_id = _make_competitor("DualSourceCo")
    # Seed JobPostings whose raw_titles overlap with what the mock search
    # will surface. "Forklift Driver" matches both paths; "Marketing Director"
    # only the DB path; "Cashier" only the web-search path.
    _seed_postings(competitor_id, [
        "Forklift Driver",
        "Marketing Director",
    ])

    monkeypatch.setattr(
        role_discovery_module.web_search,
        "search",
        lambda query, max_results=15: _fake_search_results(),
    )

    # V1 first — writes pending rows from the DB titles.
    with session_scope() as s:
        discover_from_existing_postings(s, competitor_id=competitor_id)

    # Snapshot V1's confidence for the overlap title BEFORE V2 runs.
    with session_scope() as s:
        v1_row = s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
            RoleDiscoverySuggestion.raw_title == "Forklift Driver",
        )).scalar_one()
        v1_confidence = v1_row.confidence
        assert v1_row.source == "existing_postings"

    # V2 over the same competitor — should UPDATE the existing row in place,
    # not crash on the unique constraint and not insert a duplicate.
    with session_scope() as s:
        discover_from_web_search(s, competitor_id=competitor_id)

    with session_scope() as s:
        rows = list(s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
            RoleDiscoverySuggestion.raw_title == "Forklift Driver",
        )).scalars())
        # Exactly one row for the overlap title.
        assert len(rows) == 1
        overlap = rows[0]
        # The mock LLM gives both paths the same confidence for "Forklift Driver"
        # (0.85, outdoor). Because new_conf == old_conf, the tiebreaker rule says
        # leave source alone — so it should still be V1's 'existing_postings'.
        assert overlap.confidence == v1_confidence
        assert overlap.source == "existing_postings"

    # Now bump V1's row to a LOWER confidence and re-run V2 — this time V2
    # should win the source flip because its confidence exceeds the now-lowered
    # baseline. Exercises the "higher confidence wins" branch of the tiebreaker.
    with session_scope() as s:
        row = s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
            RoleDiscoverySuggestion.raw_title == "Forklift Driver",
        )).scalar_one()
        row.confidence = 0.10  # below the mock's 0.85
        row.source = "existing_postings"

    with session_scope() as s:
        discover_from_web_search(s, competitor_id=competitor_id)

    with session_scope() as s:
        rows = list(s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == competitor_id,
            RoleDiscoverySuggestion.raw_title == "Forklift Driver",
        )).scalars())
        assert len(rows) == 1
        # V2's confidence (0.85) > 0.10 — source flipped.
        assert rows[0].source == "web_search"
        assert rows[0].confidence > 0.10


def test_discovery_for_all_competitors(seeded_session):
    """``competitor_id=None`` walks every competitor in one call."""
    a = _make_competitor("AllCompCoA")
    b = _make_competitor("AllCompCoB")
    _seed_postings(a, ["Warehouse Operator"])
    _seed_postings(b, ["Title Clerk"])

    with session_scope() as s:
        stats = discover_from_existing_postings(s, competitor_id=None)
        # >=2 because seeded competitors may also have unmapped titles.
        assert stats["new_suggestions"] >= 2

    with session_scope() as s:
        a_rows = list(s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == a
        )).scalars())
        b_rows = list(s.execute(select(RoleDiscoverySuggestion).where(
            RoleDiscoverySuggestion.competitor_id == b
        )).scalars())
        assert any(r.raw_title == "Warehouse Operator" for r in a_rows)
        assert any(r.raw_title == "Title Clerk" for r in b_rows)
