"""Scraper registry. Each Scraper subclass calls `@register(competitor_name)` at import.

To trigger a scrape for a competitor: look up via `get_scraper(competitor.name)`.
"""
from __future__ import annotations

from typing import Callable

from app.scrapers.base import Scraper

SCRAPERS: dict[str, type[Scraper]] = {}


def register(competitor_name: str) -> Callable[[type[Scraper]], type[Scraper]]:
    def _deco(cls: type[Scraper]) -> type[Scraper]:
        if not getattr(cls, "name", ""):
            cls.name = competitor_name
        SCRAPERS[competitor_name] = cls
        return cls
    return _deco


def get_scraper(competitor_name: str) -> Scraper | None:
    cls = SCRAPERS.get(competitor_name)
    return cls() if cls else None


def has_scraper(competitor_name: str) -> bool:
    return competitor_name in SCRAPERS
