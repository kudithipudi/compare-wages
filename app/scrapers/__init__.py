"""Real scrapers for collecting fresh competitor postings.

Each scraper implements `app.scrapers.base.Scraper` and registers itself in
`app.scrapers.registry.SCRAPERS` via the `@register(...)` decorator at import time.

When you add a new scraper, import it here so the registry picks it up. Imports are
wrapped in try/except so a missing or broken scraper module doesn't block app startup.
"""
for _mod in ("homedepot", "amazon", "costco", "walmart", "starbucks"):
    try:
        __import__(f"app.scrapers.{_mod}")
    except Exception:
        pass
