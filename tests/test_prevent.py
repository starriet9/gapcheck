"""Tests of the PREVENT base equations (gapcheck.prevent)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gapcheck.prevent import (
    COEFFICIENTS_FILE,
    PreventRangeError,
    ascvd_risk,
    prevent_risk,
)
from gapcheck.prevent_samples import crosscheck_inputs

REPO = Path(__file__).resolve().parents[1]

# Worked example of Supplemental Tables S12A (10-year) and S12F (30-year),
# Khan et al. Circulation 2024: age 50, TC 200, HDL-C 45, SBP 160, diabetes,
# non-smoker, BMI 35 (not used), eGFR 90, antihypertensive use, no statin.
EXAMPLE = dict(
    age=50, tc=200, hdl=45, sbp=160, bp_treated=1, statin=0, diabetes=1, smoker=0, egfr=90
)
# Expected risks copied from the "Risk" row of each table.
EXPECTED = {
    # Table S12A, base model, 10-year
    ("ascvd", "female", 10): 0.09195089753632872,
    ("ascvd", "male", 10): 0.10194362642179755,
    ("total_cvd", "female", 10): 0.14683939095644824,
    ("total_cvd", "male", 10): 0.16317056158151638,
    # Table S12F, base model, 30-year
    ("ascvd", "female", 30): 0.3542551988690411,
    ("ascvd", "male", 30): 0.3485940384298915,
    ("total_cvd", "female", 30): 0.5298503870635785,
    ("total_cvd", "male", 30): 0.5143976549039669,
}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_worked_example(key):
    outcome, sex, horizon = key
    risk = prevent_risk(outcome, sex, horizon=horizon, **EXAMPLE)
    assert abs(risk - EXPECTED[key]) < 1e-9


def test_ascvd_risk_matches_prevent_risk():
    assert ascvd_risk("female", **EXAMPLE) == prevent_risk("ascvd", "female", **EXAMPLE)


# --- valid ranges -----------------------------------------------------------

BOUNDS = [
    # (input, horizon, lowest valid, highest valid)
    ("age", 10, 30, 79),
    ("age", 30, 30, 59),
    ("tc", 10, 130, 320),
    ("hdl", 10, 20, 100),
    ("sbp", 10, 90, 200),
    ("egfr", 10, 15, 150),
]


@pytest.mark.parametrize("name,horizon,low,high", BOUNDS)
@pytest.mark.parametrize("sex", ["female", "male"])
def test_range_boundaries(name, horizon, low, high, sex):
    for value in (low, high):
        risk = ascvd_risk(sex, **{**EXAMPLE, name: value}, horizon=horizon)
        assert 0 < risk < 1
    for value in (low - 0.5, high + 0.5, None, float("nan")):
        with pytest.raises(PreventRangeError) as err:
            ascvd_risk(sex, **{**EXAMPLE, name: value}, horizon=horizon)
        assert err.value.inputs == (name,)
        assert name in str(err.value)


def test_range_error_names_every_input():
    with pytest.raises(PreventRangeError) as err:
        ascvd_risk("male", **{**EXAMPLE, "sbp": 210, "egfr": 10, "hdl": None})
    assert set(err.value.inputs) == {"sbp", "egfr", "hdl"}


def test_range_error_is_value_error():
    assert issubclass(PreventRangeError, ValueError)


@pytest.mark.parametrize(
    "kwargs",
    [{"sex": "f"}, {"horizon": 20}, {"smoker": 2}, {"statin": 0.5}],
)
def test_bad_categorical_inputs(kwargs):
    args = {"sex": "female", **EXAMPLE, **kwargs}
    with pytest.raises(ValueError) as err:
        ascvd_risk(**args)
    assert not isinstance(err.value, PreventRangeError)


def test_booleans_accepted():
    flags = dict(bp_treated=True, statin=False, diabetes=True, smoker=False)
    assert ascvd_risk("female", **{**EXAMPLE, **flags}) == ascvd_risk("female", **EXAMPLE)


# --- SBP non-monotonicity ---------------------------------------------------


@pytest.mark.parametrize("sex", ["female", "male"])
@pytest.mark.parametrize("horizon", [10, 30])
def test_risk_falls_with_sbp_below_110(sex, horizon):
    """The SBP spline term (min(SBP, 110) - 110) / 20 has a negative
    coefficient, so risk falls as SBP rises from 90 to 110 and rises above
    110. Testing only the ends of the SBP range is therefore not enough."""
    base = {**EXAMPLE, "bp_treated": 0, "diabetes": 0}
    r = {s: ascvd_risk(sex, **{**base, "sbp": s}, horizon=horizon) for s in (90, 100, 110, 130)}
    assert r[90] > r[100] > r[110]
    assert r[130] > r[110]


def test_sbp_non_monotone_example():
    # Woman, 50, TC 200, HDL-C 45, untreated, no diabetes, non-smoker, eGFR 90.
    base = {**EXAMPLE, "bp_treated": 0, "diabetes": 0}
    r90 = ascvd_risk("female", **{**base, "sbp": 90})
    r110 = ascvd_risk("female", **{**base, "sbp": 110})
    r120 = ascvd_risk("female", **{**base, "sbp": 120})
    assert r90 > r110 < r120


# --- cross-check with pyprevent 0.1.7 ----------------------------------------


def test_matches_pyprevent():
    pyprevent = pytest.importorskip("pyprevent")
    funcs = {
        10: pyprevent.calculate_10_yr_ascvd_risk,
        30: pyprevent.calculate_30_yr_ascvd_risk,
    }
    max_diff = {10: 0.0, 30: 0.0}
    for horizon, row in crosscheck_inputs():
        ours = ascvd_risk(
            row["sex"], row["age"], row["tc"], row["hdl"], row["sbp"], row["bp_treated"],
            row["statin"], row["diabetes"], row["smoker"], row["egfr"], horizon=horizon,
        )
        theirs = funcs[horizon](
            sex=row["sex"],
            age=row["age"],
            total_cholesterol=row["tc"],
            hdl_cholesterol=row["hdl"],
            systolic_bp=row["sbp"],
            has_diabetes=bool(row["diabetes"]),
            current_smoker=bool(row["smoker"]),
            bmi=row["bmi"],
            egfr=row["egfr"],
            on_htn_meds=bool(row["bp_treated"]),
            on_cholesterol_meds=bool(row["statin"]),
        ) / 100  # pyprevent returns percent
        max_diff[horizon] = max(max_diff[horizon], abs(ours - theirs))
    print(f"max abs diff vs pyprevent (risk 0-1): {max_diff}")
    for horizon, d in max_diff.items():
        assert d < 0.001, f"{horizon}-year: {d}"  # 0.1 percentage point


# --- coefficient file matches the supplement ---------------------------------


@pytest.mark.skipif(
    not os.environ.get("GAPCHECK_PREVENT_SUPPLEMENT"),
    reason="set GAPCHECK_PREVENT_SUPPLEMENT to the supplement xlsx",
)
def test_coefficients_match_supplement(tmp_path):
    out = tmp_path / "coefficients.json"
    subprocess.run(
        [sys.executable, "-I", str(REPO / "scripts" / "extract_prevent_coefficients.py"),
         "--output", str(out)],
        check=True,
    )
    assert json.loads(out.read_text()) == json.loads(COEFFICIENTS_FILE.read_text())
