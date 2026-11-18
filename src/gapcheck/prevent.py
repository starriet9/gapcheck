"""PREVENT base equations: 10- and 30-year ASCVD (and total CVD) risk.

Source: Khan SS et al. Development and validation of the American Heart
Association's PREVENT equations. Circulation 2024;149:430-449.
Supplemental Table S12A (base model, 10-year) and Table S12F (base model,
30-year). The coefficients live in `prevent_coefficients.json`, written from
the supplement by `scripts/extract_prevent_coefficients.py`.

The model is a logistic equation: derived variables (centering, scaling,
piece-wise linear splines, interactions; see `derived_terms`) are multiplied
by the coefficients and summed with the constant to give the log-odds, and
risk = exp(log-odds) / (1 + exp(log-odds)). BMI is not in the ASCVD or total
CVD base equations (its coefficients are blank in Table S12A/S12F), so it is
not an input here.

Valid input ranges are those of Table S12A ("Allowable range"). Table S12F
repeats the same ranges, including age 30-79. We compute 30-year risk only for
ages 30-59, as pyprevent does; preventr flags 30-year estimates above age 59
as questionable.
"""

import json
import math
from functools import cache
from pathlib import Path

COEFFICIENTS_FILE = Path(__file__).with_name("prevent_coefficients.json")

# Cholesterol mg/dL -> mmol/L, as used in Table S12A.
MMOL_PER_MG_DL = 0.02586

# Inclusive valid ranges (Table S12A, "Allowable range").
RANGES = {
    "age": (30, 79),
    "tc": (130, 320),
    "hdl": (20, 100),
    "sbp": (90, 200),
    "egfr": (15, 150),
}
AGE_RANGE_30_YEAR = (30, 59)

SEXES = ("female", "male")
HORIZONS = (10, 30)
OUTCOMES = ("ascvd", "total_cvd")


class PreventRangeError(ValueError):
    """A continuous input is missing or outside its valid range.

    `inputs` names every offending input (e.g. ("sbp", "egfr")).
    """

    def __init__(self, problems):
        self.inputs = tuple(name for name, _, _ in problems)
        parts = [
            f"{name}={value!r} not in {low}-{high}" for name, value, (low, high) in problems
        ]
        super().__init__("PREVENT input out of range: " + "; ".join(parts))


@cache
def load_coefficients():
    """Return the parsed coefficient file."""
    with COEFFICIENTS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def derived_terms(age, tc, hdl, sbp, bp_treated, statin, diabetes, smoker, egfr):
    """Derived variables of the base model, named as in the coefficient file.

    Formulas follow the intermediate-calculation table of Table S12A/S12F.
    Cholesterol in mg/dL, SBP in mm Hg, eGFR in mL/min/1.73 m2. Flags are 0/1.
    No range check here.
    """
    a = (age - 55) / 10
    non_hdl = (tc - hdl) * MMOL_PER_MG_DL - 3.5
    hdl_c = (hdl * MMOL_PER_MG_DL - 1.3) / 0.3
    sbp_ge_110 = (max(sbp, 110) - 130) / 20
    egfr_lt_60 = (min(egfr, 60) - 60) / -15
    return {
        "age": a,
        "age_squared": a * a,  # 30-year model only
        "non_hdl": non_hdl,
        "hdl": hdl_c,
        "sbp_lt_110": (min(sbp, 110) - 110) / 20,
        "sbp_ge_110": sbp_ge_110,
        "diabetes": diabetes,
        "smoker": smoker,
        "egfr_lt_60": egfr_lt_60,
        "egfr_ge_60": (max(egfr, 60) - 90) / -15,
        "bp_treated": bp_treated,
        "statin": statin,
        "bp_treated_x_sbp_ge_110": bp_treated * sbp_ge_110,
        "statin_x_non_hdl": statin * non_hdl,
        "age_x_non_hdl": a * non_hdl,
        "age_x_hdl": a * hdl_c,
        "age_x_sbp_ge_110": a * sbp_ge_110,
        "age_x_diabetes": a * diabetes,
        "age_x_smoker": a * smoker,
        "age_x_egfr_lt_60": a * egfr_lt_60,
        "constant": 1.0,
    }


def log_odds(coefficients, terms):
    """Sum of coefficient x derived term over the model's terms."""
    return sum(beta * terms[name] for name, beta in coefficients.items())


def _flag(name, value):
    if value in (0, 1):  # also accepts True/False
        return int(value)
    raise ValueError(f"{name} must be 0/1 or True/False, got {value!r}")


def _number(value):
    if value is None:
        return math.nan
    return float(value)


def check_ranges(age, tc, hdl, sbp, egfr, horizon=10):
    """Raise PreventRangeError naming every missing or out-of-range input."""
    ranges = dict(RANGES)
    if horizon == 30:
        ranges["age"] = AGE_RANGE_30_YEAR
    values = {"age": age, "tc": tc, "hdl": hdl, "sbp": sbp, "egfr": egfr}
    problems = []
    for name, (low, high) in ranges.items():
        x = _number(values[name])
        if not low <= x <= high:  # NaN fails too
            problems.append((name, values[name], (low, high)))
    if problems:
        raise PreventRangeError(problems)


def prevent_risk(
    outcome, sex, age, tc, hdl, sbp, bp_treated, statin, diabetes, smoker, egfr, horizon=10
):
    """Unrounded base-model risk (0-1) of `outcome` ("ascvd" or "total_cvd")."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if sex not in SEXES:
        raise ValueError(f"sex must be one of {SEXES}, got {sex!r}")
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be 10 or 30, got {horizon!r}")
    check_ranges(age, tc, hdl, sbp, egfr, horizon)
    terms = derived_terms(
        float(age),
        float(tc),
        float(hdl),
        float(sbp),
        _flag("bp_treated", bp_treated),
        _flag("statin", statin),
        _flag("diabetes", diabetes),
        _flag("smoker", smoker),
        float(egfr),
    )
    coefficients = load_coefficients()["models"][outcome][f"{horizon}yr"][sex]
    x = log_odds(coefficients, terms)
    return math.exp(x) / (1 + math.exp(x))


def ascvd_risk(sex, age, tc, hdl, sbp, bp_treated, statin, diabetes, smoker, egfr, horizon=10):
    """Unrounded PREVENT base-model ASCVD risk (0-1) over 10 or 30 years.

    sex: "female" or "male". age in years; tc, hdl in mg/dL; sbp in mm Hg;
    egfr in mL/min/1.73 m2; bp_treated, statin, diabetes, smoker: 0/1 or bool.
    Raises PreventRangeError if a continuous input is missing or out of range
    (age 30-79, or 30-59 for horizon=30; TC 130-320; HDL-C 20-100; SBP 90-200;
    eGFR 15-150; all inclusive).
    """
    return prevent_risk(
        "ascvd", sex, age, tc, hdl, sbp, bp_treated, statin, diabetes, smoker, egfr, horizon
    )
