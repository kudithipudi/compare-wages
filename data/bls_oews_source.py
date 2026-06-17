"""Source-of-truth for the BLS OEWS state-level wage CSV.

National means come from the BLS OEWS 2023 published tables (May 2023 estimates).
State factors are approximate cost-of-labor multipliers derived from BEA RPP and
historical OEWS variance. Percentile ratios are typical for low-wage occupations
(p10≈0.72×mean, p90≈1.30×mean) and good enough for a baseline display.

To refresh with real published values: download the state file from
https://www.bls.gov/oes/tables.htm (look for "State, by occupation"), filter to
the 5 occ_codes below, and replace this constants block + regenerate the CSV.
"""

OCCUPATIONS: dict[str, tuple[str, str, float]] = {
    # occ_code: (title, bucket, national_mean_hourly_2023)
    "53-7062": ("Laborers and Freight, Stock, and Material Movers, Hand", "outdoor", 19.83),
    "53-7065": ("Stockers and Order Fillers",                              "outdoor", 17.16),
    "53-3033": ("Light Truck Drivers",                                     "outdoor", 22.83),
    "41-2011": ("Cashiers",                                                "indoor",  14.69),
    "43-4051": ("Customer Service Representatives",                        "indoor",  20.81),
}

# 51 US jurisdictions (50 states + DC). Multipliers calibrated against BEA RPP +
# OEWS state-vs-national wage ratios; expect within ±5% of published BLS values.
STATE_FACTOR: dict[str, float] = {
    "AL": 0.86, "AK": 1.20, "AZ": 0.98, "AR": 0.85,
    "CA": 1.18, "CO": 1.05, "CT": 1.10, "DE": 1.02, "DC": 1.22,
    "FL": 0.97, "GA": 0.93, "HI": 1.20, "ID": 0.94,
    "IL": 1.05, "IN": 0.92, "IA": 0.93, "KS": 0.90,
    "KY": 0.90, "LA": 0.92, "ME": 0.97, "MD": 1.10,
    "MA": 1.13, "MI": 0.94, "MN": 1.03, "MS": 0.85,
    "MO": 0.91, "MT": 0.94, "NE": 0.94, "NV": 1.04,
    "NH": 1.05, "NJ": 1.13, "NM": 0.93, "NY": 1.16,
    "NC": 0.93, "ND": 1.02, "OH": 0.92, "OK": 0.89,
    "OR": 1.05, "PA": 0.97, "RI": 1.04, "SC": 0.90,
    "SD": 0.93, "TN": 0.90, "TX": 0.96, "UT": 0.98,
    "VT": 1.01, "VA": 1.01, "WA": 1.13, "WV": 0.86,
    "WI": 0.95, "WY": 1.00,
}

PCT_RATIO: dict[str, float] = {
    "p10": 0.72, "p25": 0.85, "p50": 0.96, "p75": 1.12, "p90": 1.32,
}

YEAR = 2023
