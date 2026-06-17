"""Geocode US addresses via the Census Bureau's free Geocoder API.

API docs: https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.pdf
- Free, no API key required.
- Rate limit: ~10 req/sec; we cap at 1 req/sec to be polite.
- US only. Returns (lat, lng) on a successful match.
- Returns None for failures (network, no match, junk address). Callers must NOT pretend
  None means (0, 0) — that's the bug we're fixing.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
_last_call_at = 0.0
_MIN_GAP_SECONDS = 1.0


def _throttle() -> None:
    global _last_call_at
    gap = time.monotonic() - _last_call_at
    if gap < _MIN_GAP_SECONDS:
        time.sleep(_MIN_GAP_SECONDS - gap)
    _last_call_at = time.monotonic()


def geocode(
    *,
    city: str,
    state: str,
    street: str = "",
    zip_code: str = "",
    timeout: float = 15.0,
) -> tuple[float, float] | None:
    """Return (lat, lng) for the most specific available address. None on any failure."""
    if not city or not state:
        return None
    if city.strip().lower() == "unknown" or state.strip().upper() == "XX":
        return None

    parts = []
    if street:
        parts.append(street.strip())
    parts.append(f"{city.strip()}, {state.strip().upper()}")
    if zip_code:
        parts[-1] = parts[-1] + f" {zip_code.strip()}"
    address = ", ".join(parts)

    _throttle()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(
                CENSUS_URL,
                params={
                    "address": address,
                    "benchmark": "Public_AR_Current",
                    "format": "json",
                },
            )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.info("Census geocode failed for %r: %s", address, e)
        return None

    matches = data.get("result", {}).get("addressMatches", []) or []
    if not matches:
        log.info("Census geocode: no match for %r", address)
        return None

    coords = matches[0].get("coordinates", {}) or {}
    try:
        # Census returns x=longitude, y=latitude.
        lng = float(coords.get("x"))
        lat = float(coords.get("y"))
    except (TypeError, ValueError):
        log.info("Census geocode: malformed coords for %r: %r", address, coords)
        return None
    return lat, lng
