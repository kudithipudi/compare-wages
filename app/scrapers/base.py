"""Contract every employer scraper implements.

Scrapers yield `ScrapedPosting` objects from a remote source (employer careers site, RSS
feed, etc.). The service layer turns each ScrapedPosting into:
  - a CompetitorLocation row (matched or newly created) at (city, state)
  - a JobPosting row with raw_html_path pointing to data/raw_html/<file>.html
  - the same LLM extraction pipeline then runs on the raw_html to fill wage_low / wage_high / role_bucket

Scrapers MUST:
  - Be polite (1 req/sec default, configurable via RATE_LIMIT_HZ)
  - Identify themselves with a realistic User-Agent
  - Respect robots.txt — return False from is_available() if their target paths are disallowed
  - Cap output via `max_postings` so an admin trigger can't run away
  - Be safe to invoke from a daemon thread (no asyncio at the public boundary)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator


@dataclass
class ScrapedPosting:
    competitor_name: str           # must match Competitor.name in the DB
    raw_title: str                 # free-text job title from the source
    location_city: str
    location_state: str            # two-letter state code, uppercase
    raw_html: str                  # the HTML the LLM extractor will read
    source_url: str
    discovered_at: datetime = field(default_factory=datetime.utcnow)
    # Optional, used for higher-accuracy geocoding when available (e.g. parsed from
    # JSON-LD jobLocation.address.streetAddress). Empty string when only city/state
    # are known. The service layer geocodes the most specific available address.
    street_address: str = ""
    zip_code: str = ""


class Scraper(ABC):
    """Abstract base. Concrete subclasses must set `name` and implement scrape()."""

    name: str = ""               # competitor name, must match Competitor.name exactly
    rate_limit_hz: float = 1.0   # requests per second, default 1.0

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap pre-check (e.g. robots.txt + a HEAD request). Return False if a real run
        would be blocked or against ToS — the admin route surfaces this as a clear error
        instead of trying and failing mid-flight."""

    @abstractmethod
    def scrape(
        self,
        *,
        keywords: list[str],
        locations: list[tuple[str, str]] | None = None,
        max_postings: int = 25,
    ) -> Iterator[ScrapedPosting]:
        """Yield up to `max_postings` ScrapedPosting objects whose role titles match any
        keyword in `keywords` (typically competitor-role strings from RoleMapping, e.g.
        ['Lot Associate', 'Freight Associate']).

        `locations` is an optional list of `(city, state)` pairs the service layer derives
        from active Copart yards — passing it tells the scraper to query each location's
        catchment instead of the employer's global ranking. `None` or `[]` means "global
        search" (today's default behavior; preserves backwards compat).

        Implementations should use the rate limit, handle their own browser/HTTP client
        setup + teardown, deduplicate discovered postings across (location × keyword)
        pairs, and raise clearly-typed exceptions on hard failures (which the service
        catches and records on the ScraperRun row). An empty `keywords` list MUST yield
        nothing — the operator configures coverage by editing role mappings, never by
        relying on a hardcoded default keyword set."""
