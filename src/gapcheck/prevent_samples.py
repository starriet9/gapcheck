"""Seeded random valid PREVENT inputs, shared by the cross-checks.

The ranges are the intersection of what this package, pyprevent 0.1.7 and
preventr 0.12.2 accept, except SBP: rows with SBP above 180 are generated and
skipped only in the preventr comparison (preventr rejects them). eGFR stops
at 140 and BMI (not used by the ASCVD equation) lies in 18.5-39.9 because
both packages validate them.
"""

import random

SEED_10_YEAR = 20241110
SEED_30_YEAR = 20241111


def random_inputs(n=1000, seed=SEED_10_YEAR, max_age=79):
    """Return n dicts with keys sex, age, tc, hdl, sbp, bp_treated, statin,
    diabetes, smoker, egfr, bmi. Continuous values are uniform floats."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        rows.append(
            {
                "sex": rng.choice(("female", "male")),
                "age": rng.uniform(30, max_age),
                "tc": rng.uniform(130, 320),
                "hdl": rng.uniform(20, 100),
                "sbp": rng.uniform(90, 200),
                "bp_treated": int(rng.random() < 0.4),
                "statin": int(rng.random() < 0.3),
                "diabetes": int(rng.random() < 0.3),
                "smoker": int(rng.random() < 0.3),
                "egfr": rng.uniform(15, 140),
                "bmi": rng.uniform(18.5, 39.9),
            }
        )
    return rows


def crosscheck_inputs(n=1000):
    """(horizon, inputs) pairs: n rows aged 30-79 for 10-year risk and n rows
    aged 30-59 for 30-year risk."""
    return [(10, r) for r in random_inputs(n, SEED_10_YEAR, 79)] + [
        (30, r) for r in random_inputs(n, SEED_30_YEAR, 59)
    ]
