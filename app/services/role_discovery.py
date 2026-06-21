"""Role discovery orchestrator.

Mines ``JobPosting.raw_title`` for titles that aren't already in
``RoleMapping.competitor_role`` (per-competitor OR globally), batch-classifies
them with the LLM (``purpose="role_discovery"``), and writes the results to the
``RoleDiscoverySuggestion`` table as ``status="pending"``. The operator then
reviews on ``/admin/role-discovery`` and either accepts (which materializes a
``RoleMapping`` row the next scrape uses) or rejects (a tombstone that prevents
the same title resurfacing on re-runs).

Synchronous by design — V1 keeps the operator on the page for ~20s rather than
introduce daemon-thread infra that the rest of the codebase already over-uses
for the actual scrape work. If the worst-case shape grows (more competitors,
bigger title pools), the same code path can be lifted into a thread by the
caller without API change.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Competitor,
    JobPosting,
    RoleDiscoverySuggestion,
    RoleMapping,
)
from app.services import web_search
from app.services.llm import (
    classify_titles_batch,
    extract_titles_from_search_results,
)

log = logging.getLogger(__name__)

# Batch size for the LLM call. ~20 titles per batch keeps the prompt under the
# context window of every model on the panel and lets the operator's wall-clock
# wait stay near the spec'd 20s ceiling.
BATCH_SIZE = 20


def _mapped_titles_for_competitor(s: Session, competitor_id: int) -> set[str]:
    """All ``competitor_role`` strings already mapped for this competitor — both
    the per-competitor mappings AND the global ``competitor_id IS NULL`` ones.
    Pulled out as a helper so V2's web-search path can reuse the exact same
    filter rule V1's DB-mining path uses."""
    return set(
        s.execute(
            select(RoleMapping.competitor_role).where(
                or_(
                    RoleMapping.competitor_id == competitor_id,
                    RoleMapping.competitor_id.is_(None),
                )
            )
        ).scalars()
    )


def _unmapped_titles_for_competitor(
    s: Session, competitor_id: int
) -> list[str]:
    """Return distinct ``raw_title`` values from ``JobPosting`` for this competitor
    that are NOT already a ``competitor_role`` on a ``RoleMapping`` row scoped to
    this competitor OR globally (``competitor_id IS NULL``).

    Case-folded comparison: scraper keyword lookup is case-sensitive (it joins
    on the literal string), so we mirror that here — same casing as the
    ``RoleMapping`` row wins; only differently-cased duplicates re-surface, which
    is an explicit operator decision.
    """
    # All known competitor_role strings, scoped to this competitor or global.
    mapped = _mapped_titles_for_competitor(s, competitor_id)
    # Distinct raw_titles for this competitor's postings.
    posting_titles = list(
        s.execute(
            select(JobPosting.raw_title)
            .where(JobPosting.competitor_id == competitor_id)
            .distinct()
        ).scalars()
    )
    out: list[str] = []
    seen: set[str] = set()
    for t in posting_titles:
        if not t:
            continue
        t = t.strip()
        if not t or t in seen or t in mapped:
            continue
        seen.add(t)
        out.append(t)
    return out


def _batched(items: list[str], n: int) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def _process_competitor(
    s: Session, competitor: Competitor, stats: dict
) -> None:
    """Mine + classify + upsert for a single competitor."""
    titles = _unmapped_titles_for_competitor(s, competitor.id)
    stats["processed_titles"] += len(titles)
    if not titles:
        return

    # Pre-load existing suggestions for this competitor so we can decide between
    # update (pending) vs skip (accepted/rejected) vs insert (none). Pulling the
    # whole set in one query is cheaper than N point-lookups during the loop.
    existing_rows = list(
        s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.competitor_id == competitor.id
            )
        ).scalars()
    )
    by_title: dict[str, RoleDiscoverySuggestion] = {r.raw_title: r for r in existing_rows}

    # Titles that already have an accepted/rejected suggestion: don't re-classify,
    # don't re-cost the LLM, don't re-add to the operator's queue. (Accepted ones
    # are also no longer in `titles` because they're already in role_mappings, but
    # rejected titles ARE still in the unmapped set — this is the line that
    # filters them out.)
    to_classify: list[str] = []
    for t in titles:
        row = by_title.get(t)
        if row is not None and row.status in ("accepted", "rejected"):
            stats["skipped_existing"] += 1
            continue
        to_classify.append(t)

    if not to_classify:
        return

    for batch in _batched(to_classify, BATCH_SIZE):
        try:
            results = classify_titles_batch(batch, competitor.name)
        except Exception:
            # One batch's failure shouldn't take down the whole run — log and move on.
            # The orchestrator's surface promise is "best effort", and the LlmCall
            # log row already records the failure for /admin/ai-ops.
            log.exception(
                "role_discovery: classify_titles_batch failed for %s on a batch of %d titles",
                competitor.name, len(batch),
            )
            continue

        for r in results:
            title = r["title"]
            existing = by_title.get(title)
            if existing is None:
                s.add(
                    RoleDiscoverySuggestion(
                        competitor_id=competitor.id,
                        raw_title=title,
                        suggested_bucket=r["bucket"],
                        confidence=r["confidence"],
                        reasoning=r["reasoning"],
                        status="pending",
                        source="existing_postings",
                    )
                )
                stats["new_suggestions"] += 1
            else:
                # Refresh pending rows with the freshest classification so an
                # operator's later review reflects the current LLM's judgment
                # (e.g. after they swapped models on /admin/llm-models). Skip
                # accepted/rejected — those are operator-final.
                if existing.status == "pending":
                    existing.suggested_bucket = r["bucket"]
                    existing.confidence = r["confidence"]
                    existing.reasoning = r["reasoning"]
                    stats["refreshed_suggestions"] += 1
        # Commit per batch so partial progress survives if a later batch crashes.
        s.commit()


def discover_from_existing_postings(
    s: Session, *, competitor_id: Optional[int] = None
) -> dict:
    """Walk JobPosting.raw_title values for the given competitor (or all
    competitors when ``competitor_id is None``), classify titles that aren't
    already mapped, and upsert ``RoleDiscoverySuggestion`` rows.

    Returns a dict of operator-facing counters:

    - ``processed_titles``   — count of distinct unmapped titles encountered
    - ``new_suggestions``    — fresh rows inserted
    - ``refreshed_suggestions`` — pending rows whose classification was updated
    - ``skipped_existing``   — titles whose suggestion is already accepted/rejected
    """
    stats = {
        "processed_titles": 0,
        "new_suggestions": 0,
        "refreshed_suggestions": 0,
        "skipped_existing": 0,
    }
    if competitor_id is not None:
        competitor = s.get(Competitor, competitor_id)
        if competitor is None:
            return stats
        _process_competitor(s, competitor, stats)
    else:
        competitors = list(s.execute(select(Competitor).order_by(Competitor.name)).scalars())
        for c in competitors:
            _process_competitor(s, c, stats)
    return stats


# ------------------------- V2: web search discovery -------------------------

# Seed queries — combine competitor name with broad role-bucket nouns. The
# competitor name is double-quoted so search engines treat it as a phrase
# (Walmart vs "Walmart Inc" disambiguation is less of a hazard with the
# quoted form). Kept short on purpose — DDG's snippet quality drops sharply
# past ~6 keywords.
_WEB_SEARCH_QUERY_TEMPLATES = [
    '"{competitor}" hourly job titles warehouse OR yard OR loader',
    '"{competitor}" entry-level positions clerk OR cashier OR associate',
    '"{competitor}" hiring driver OR forklift OR stocker',
    '"{competitor}" careers page outdoor OR indoor',
]

# How many search-result rows to hand the LLM extractor per call. Snippets
# are short (~200 chars each); 5 keeps the prompt well under any model's
# context window and surfaces a small-ish LlmCall row on /admin/ai-ops.
_WEB_EXTRACT_BATCH = 5


def _batched_results(
    results: list[dict[str, str]], n: int
) -> list[list[dict[str, str]]]:
    return [results[i : i + n] for i in range(0, len(results), n)]


def _process_competitor_web(
    s: Session, competitor: Competitor, stats: dict
) -> None:
    """V2: web-search-driven discovery for a single competitor.

    Issues a small set of seed queries (``_WEB_SEARCH_QUERY_TEMPLATES``), runs
    each through ``web_search.search``, batches the result snippets into the
    ``extract_titles_from_search_results`` LLM step, then classifies the
    extracted titles via the same ``classify_titles_batch`` V1 uses. Upserts
    pending rows with ``source='web_search'``.
    """
    # 1. Issue queries.
    all_results: list[dict[str, str]] = []
    for tmpl in _WEB_SEARCH_QUERY_TEMPLATES:
        query = tmpl.format(competitor=competitor.name)
        stats["queries_issued"] += 1
        try:
            hits = web_search.search(query)
        except Exception:
            # `web_search.search` already swallows backend errors and returns []
            # but belt-and-braces: an unexpected raise from a future backend
            # shouldn't take down the whole competitor.
            log.exception(
                "role_discovery web_search: query failed for %s: %r",
                competitor.name, query,
            )
            continue
        all_results.extend(hits or [])

    if not all_results:
        return

    # 2. Extract candidate titles from snippets via the LLM (batched).
    extracted: list[str] = []
    seen_extracted: set[str] = set()
    for batch in _batched_results(all_results, _WEB_EXTRACT_BATCH):
        try:
            titles = extract_titles_from_search_results(batch, competitor.name)
        except Exception:
            log.exception(
                "role_discovery web_search: extract_titles_from_search_results "
                "failed for %s on a batch of %d results",
                competitor.name, len(batch),
            )
            continue
        for t in titles:
            if t not in seen_extracted:
                seen_extracted.add(t)
                extracted.append(t)

    stats["candidates_extracted"] += len(extracted)
    if not extracted:
        return

    # 3. Filter: drop titles already in RoleMapping (per-competitor OR global).
    mapped = _mapped_titles_for_competitor(s, competitor.id)
    candidates = [t for t in extracted if t not in mapped]

    # 4. Filter: drop titles already accepted/rejected for this competitor.
    existing_rows = list(
        s.execute(
            select(RoleDiscoverySuggestion).where(
                RoleDiscoverySuggestion.competitor_id == competitor.id
            )
        ).scalars()
    )
    by_title: dict[str, RoleDiscoverySuggestion] = {r.raw_title: r for r in existing_rows}
    to_classify: list[str] = []
    for t in candidates:
        row = by_title.get(t)
        if row is not None and row.status in ("accepted", "rejected"):
            stats["skipped_existing"] += 1
            continue
        to_classify.append(t)

    if not to_classify:
        return

    # 5. Classify via the SAME batch path V1 uses — that's the point: bucket
    # rules don't fork between the two discovery sources.
    for batch in _batched(to_classify, BATCH_SIZE):
        try:
            results = classify_titles_batch(batch, competitor.name)
        except Exception:
            log.exception(
                "role_discovery web_search: classify_titles_batch failed for "
                "%s on a batch of %d titles",
                competitor.name, len(batch),
            )
            continue

        # 6. Upsert. The unique constraint (competitor_id, raw_title) is the
        # join key. For an existing pending row from V1, we update bucket/
        # confidence/reasoning; we flip source to 'web_search' only when V2's
        # confidence beats V1's (tiebreaker: log the chosen source).
        for r in results:
            title = r["title"]
            existing = by_title.get(title)
            if existing is None:
                s.add(
                    RoleDiscoverySuggestion(
                        competitor_id=competitor.id,
                        raw_title=title,
                        suggested_bucket=r["bucket"],
                        confidence=r["confidence"],
                        reasoning=r["reasoning"],
                        status="pending",
                        source="web_search",
                    )
                )
                stats["new_suggestions"] += 1
            else:
                if existing.status == "pending":
                    # Capture pre-update values BEFORE overwriting — the source
                    # tiebreaker compares the web-search confidence against what
                    # was already there. Doing this after the assignment would
                    # always read the new value (subtle gotcha).
                    old_conf = float(existing.confidence or 0.0)
                    prior_source = existing.source
                    new_conf = float(r["confidence"] or 0.0)
                    existing.suggested_bucket = r["bucket"]
                    existing.confidence = new_conf
                    existing.reasoning = r["reasoning"]
                    if new_conf > old_conf:
                        existing.source = "web_search"
                        log.info(
                            "role_discovery web_search: title=%r competitor=%s "
                            "took source from %r (conf %.2f→%.2f)",
                            title, competitor.name, prior_source, old_conf, new_conf,
                        )
                    else:
                        log.info(
                            "role_discovery web_search: title=%r competitor=%s "
                            "left source as %r (web conf %.2f ≤ existing %.2f)",
                            title, competitor.name, prior_source, new_conf, old_conf,
                        )
                    stats["refreshed_suggestions"] += 1
        s.commit()


def discover_from_web_search(
    s: Session, *, competitor_id: Optional[int] = None
) -> dict:
    """V2 discovery path: query a generic web search engine for each competitor,
    extract candidate role titles from the result snippets via the LLM,
    classify them into ACME role buckets (via the same ``classify_titles_batch``
    V1 uses), and upsert ``RoleDiscoverySuggestion`` rows with
    ``source='web_search'``.

    Closes V1's bootstrap gap — a competitor with zero scraped ``JobPosting``
    rows has nothing for V1 to mine, but V2 can still surface candidate titles
    from the open web.

    Returns operator-facing counters:

    - ``processed_competitors``  — competitors actually walked
    - ``queries_issued``         — total search calls attempted (count includes
      mock-mode and cached hits — the operator's signal here is "how many
      query templates ran", not "how much $ I burned on Tavily")
    - ``candidates_extracted``   — distinct titles the LLM pulled from snippets
    - ``new_suggestions``        — fresh rows inserted with source='web_search'
    - ``refreshed_suggestions``  — pending rows updated (V1 or V2)
    - ``skipped_existing``       — candidate titles already accepted/rejected
    """
    stats = {
        "processed_competitors": 0,
        "queries_issued": 0,
        "candidates_extracted": 0,
        "new_suggestions": 0,
        "refreshed_suggestions": 0,
        "skipped_existing": 0,
    }
    if competitor_id is not None:
        competitor = s.get(Competitor, competitor_id)
        if competitor is None:
            return stats
        stats["processed_competitors"] += 1
        _process_competitor_web(s, competitor, stats)
    else:
        competitors = list(
            s.execute(select(Competitor).order_by(Competitor.name)).scalars()
        )
        for c in competitors:
            stats["processed_competitors"] += 1
            _process_competitor_web(s, c, stats)
    return stats
