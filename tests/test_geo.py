from app.services.geo import haversine_miles


LONG_BEACH = (33.8155, -118.2381)
SACRAMENTO = (38.5008, -121.4101)
HOUSTON = (29.9354, -95.4416)
DALLAS = (32.5798, -96.6868)


def _within_tolerance(actual, expected, pct=0.05):
    return abs(actual - expected) <= expected * pct


def test_long_beach_to_sacramento():
    d = haversine_miles(*LONG_BEACH, *SACRAMENTO)
    assert _within_tolerance(d, 370.0)


def test_houston_to_dallas():
    d = haversine_miles(*HOUSTON, *DALLAS)
    assert _within_tolerance(d, 197.0)


def test_symmetry():
    a = haversine_miles(*LONG_BEACH, *HOUSTON)
    b = haversine_miles(*HOUSTON, *LONG_BEACH)
    assert abs(a - b) < 1e-6


def test_zero_distance():
    assert haversine_miles(*LONG_BEACH, *LONG_BEACH) == 0.0
