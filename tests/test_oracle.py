"""Hand-written vignettes for the decision table (SPEC 3.1-3.4)."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from gapcheck import codes as C
from gapcheck import facts, oracle, prevent
from gapcheck.oracle import (CANNOT_DETERMINE, DISCUSS, DOCUMENTED_REASON, EXCLUDED, GAP,
                             NOT_INDICATED, ON_THERAPY, Reason)
from gapcheck.prevent_samples import crosscheck_inputs
from vignette import INDEX, make

LOW = dict(age=45, sex="F", tc=170.0, hdl=65.0, ldl=90.0, sbp=112.0)  # 10-year risk well < 3%


def lab(f, **kw):
    return oracle.label(f, **kw)


def sbp_at(target, horizon=10, **p):
    """SBP (110-200) at which the PREVENT risk of profile p equals target (bisection)."""
    sex = "male" if p.get("sex", "M") == "M" else "female"
    g = lambda s: prevent.ascvd_risk(sex, p["age"], p["tc"], p["hdl"], s, int(p.get("bp", False)),
                                     0, int(p.get("diabetes", False)), int(bool(p.get("smoker"))),
                                     p.get("egfr", 90.0), horizon)
    lo, hi = 110.0, 200.0
    assert g(lo) < target < g(hi)
    for _ in range(60):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if g(m) < target else (lo, m)
    return lo


# ---------------------------------------------------------------- table
def all_sources():
    srcs = [s for r in oracle.ROWS for s in r.sources]
    srcs += [s for r in oracle.REASONS for s in r.sources] + list(oracle.CONTEXT_SOURCES)
    srcs += [s for c in oracle.CONVENTIONS for s in c.sources]
    return srcs


def test_table_metadata():
    ids = [r.id for r in oracle.ROWS]
    assert ids == ["0", "X", "1", "2", "3", "3b", "4", "5", "6", "7", "8", "9", "10", "11a", "11b"]
    srcs = all_sources()
    for s in srcs:
        assert 2624 <= s.page <= 2757 and s.pdf_page == s.page - 2623
        assert s.quote and s.section
        assert s.kind in oracle.SOURCE_KINDS and s.role in oracle.SOURCE_ROLES
        assert (s.kind == "figure") == s.section.startswith("Figure"), s
    for r in oracle.ROWS:
        if r.id != "0":
            assert r.sources and r.scope, r.id
        assert isinstance(r.encoding, tuple)
        if r.label == GAP:  # every GAP row rests on a quoted COR 1 recommendation
            assert any(s.cor == "1" and s.kind == "quote" and s.role == "basis"
                       for s in r.sources), r.id
    assert {s.cor for s in srcs if s.cor} <= {"1", "2a", "2b", "3: No Benefit"}
    assert oracle.REASON_CATEGORIES == tuple(oracle.REASON_SOURCES)
    assert oracle.REASON_SOURCES["liver_failure"][0].section == "Table 25"


def test_row_status_variants():
    assert [oracle.row_status(r) for r in ("8", "9", "10")] == [GAP, GAP, DISCUSS]
    assert [oracle.row_status(r, "S-A") for r in ("8", "9")] == [DISCUSS, GAP]
    assert [oracle.row_status(r, "S-C") for r in ("1", "2", "3", "3b", "8", "9")] == \
        [GAP, GAP, GAP, GAP, DISCUSS, DISCUSS]
    with pytest.raises(ValueError):
        oracle.row_status("8", "S-B")


def test_vectorized_prevent_matches_scalar():
    for horizon, r in crosscheck_inputs(200):
        args = {k: r[k] for k in ("sex", "age", "tc", "hdl", "sbp", "bp_treated", "statin",
                                  "diabetes", "smoker", "egfr")}
        assert prevent.ascvd_risk_array(**args, horizon=horizon) == pytest.approx(
            prevent.ascvd_risk(**args, horizon=horizon), abs=1e-14)


# ---------------------------------------------------------------- row 0 and exclusions
def test_statin_active_is_on_therapy():
    r = lab(make(statin=True, ldl=None, lipid=False, ascvd=("MI",)))
    assert (r.status, r.row, r.deciding, r.missing) == (ON_THERAPY, "0", (), ())


def test_statin_stopped_before_index_is_not_on_therapy():
    stopped = INDEX - timedelta(days=30)
    assert lab(make(statin=True, statin_stop=stopped, ascvd=("MI",))).status == GAP
    r = lab(make(statin=True, statin_stop=stopped, ldl=60.0))
    assert (r.status, r.row) == (NOT_INDICATED, "7")


def test_exclusions_hiv_esrd():
    assert lab(make(hiv=True)).status == EXCLUDED
    r = lab(make(esrd=True, ckd=4, ascvd=("MI",)))
    assert (r.status, r.row) == (EXCLUDED, "X")
    assert lab(make(esrd=True, statin=True)).status == ON_THERAPY  # row 0 comes first
    assert lab(make(hiv=True), ignore_row0=True).indication_row == "X"


def test_age_out_of_scope():
    for age in (29, 80):
        with pytest.raises(ValueError):
            lab(make(age=age))


# ---------------------------------------------------------------- row 1
def test_ascvd_is_gap_whatever_open_inputs():
    r = lab(make(age=45, ascvd=("MI",), lipid=False, sbp=None, smoker=None, egfr=None))
    assert (r.status, r.row, r.deciding, r.missing) == (GAP, "1", (), ())
    assert r.absent == ("lipid_panel", "sbp", "smoking", "egfr")


def test_ascvd_no_age_limit():
    for t in ("STROKE", "REVASC", "IHD"):
        r = lab(make(age=79, ascvd=(t,), ldl=50.0))
        assert (r.status, r.row) == (GAP, "1")


# ---------------------------------------------------------------- row 2
def test_ldl_189_190():
    r = lab(make(**LOW | {"ldl": 190.0, "tc": 280.0}))
    assert (r.status, r.row, r.deciding) == (GAP, "2", ("lipid_panel",))
    r = lab(make(**LOW | {"ldl": 189.0, "tc": 280.0}))
    assert r.row != "2" and r.status == DISCUSS  # 11a: age 45, LDL-C 160-189


def test_ldl_190_no_age_limit_and_189_at_78():
    assert lab(make(age=78, ldl=190.0, tc=280.0)).row == "2"
    r = lab(make(age=78, ldl=189.0, tc=280.0))
    assert (r.status, r.row) == (DISCUSS, "6")


# ---------------------------------------------------------------- rows 3 and 4
@pytest.mark.parametrize("age,status,row", [(40, GAP, "3"), (75, GAP, "3"),
                                            (76, DISCUSS, "4"), (79, DISCUSS, "4")])
def test_diabetes_age_bounds(age, status, row):
    r = lab(make(age=age, diabetes=True))
    assert (r.status, r.row) == (status, row)


def test_diabetes_39_goes_to_row5():
    r = lab(make(**LOW | {"age": 39}, diabetes=True))
    assert "5" in r.path and r.row in ("5", "7", "11b")
    assert r.status in (DISCUSS, NOT_INDICATED)


def test_diabetes_no_ldl_floor_and_lipid_not_deciding():
    r = lab(make(age=50, diabetes=True, ldl=50.0, tc=140.0))
    assert (r.status, r.row, r.deciding) == (GAP, "3", ())
    r = lab(make(age=50, diabetes=True, lipid=False))
    assert (r.status, r.missing) == (GAP, ())
    r = lab(make(age=50, diabetes=True, ldl=220.0, tc=300.0))
    assert (r.status, r.row, r.deciding) == (GAP, "2", ())  # row 3 would give GAP too


# ---------------------------------------------------------------- row 3b
@pytest.mark.parametrize("ldl,row", [(69.0, "7"), (70.0, "3b"), (189.0, "3b"), (190.0, "2")])
def test_ckd3_ldl_bounds(ldl, row):
    r = lab(make(age=50, ckd=3, ldl=ldl, tc=max(200.0, ldl + 60)))
    assert r.row == row
    assert r.status == (NOT_INDICATED if row == "7" else GAP)
    assert "lipid_panel" in r.deciding


def test_ckd_stage_and_age():
    assert lab(make(age=50, ckd=4)).row == "3b"
    assert lab(make(**LOW | {"age": 50}, ckd=2)).row != "3b"
    assert lab(make(**LOW | {"age": 39}, ckd=3)).row != "3b"
    assert lab(make(age=76, ckd=3)).row == "6"
    assert lab(make(age=50, ckd=3, esrd=True)).status == EXCLUDED  # ESRD, not CKD 3


# ---------------------------------------------------------------- row 5
def test_diabetes_30_39_high_risk_is_discuss_not_gap():
    """Figure 9: at 30-39, 10-year risk >= 3% is COR 2a at any level (no GAP via rows 8-9)."""
    f = make(age=35, sex="M", diabetes=True, tc=240.0, hdl=40.0, ldl=160.0, sbp=130.0,
             bp=True, smoker=True)
    r = lab(f)
    assert r.risk10 >= 0.05 and (r.status, r.row) == (DISCUSS, "5")
    assert "8" not in r.path and "9" not in r.path
    assert lab(f, variant="S-C").status == DISCUSS


DM35 = dict(age=35, sex="M", tc=200.0, hdl=50.0, ldl=120.0, diabetes=True)


def test_diabetes_30_39_10_year_3_percent_cut():
    s = sbp_at(0.03, **DM35)
    lo, hi = lab(make(**DM35, sbp=s - 0.3)), lab(make(**DM35, sbp=s + 0.3))
    assert lo.risk10 < 0.03 <= hi.risk10
    assert (hi.status, hi.row) == (DISCUSS, "5")
    if lo.risk30 < 0.10:
        assert (lo.status, lo.row) == (NOT_INDICATED, "11b")
    else:
        assert (lo.status, lo.row) == (DISCUSS, "5")


def test_diabetes_30_39_30_year_10_percent_cut():
    p = dict(age=32, sex="F", tc=170.0, hdl=65.0, ldl=100.0, diabetes=True)
    s = sbp_at(0.10, horizon=30, **p)
    lo, hi = lab(make(**p, sbp=s - 0.3)), lab(make(**p, sbp=s + 0.3))
    assert hi.risk10 < 0.03 and lo.risk30 < 0.10 <= hi.risk30
    assert (lo.status, lo.row, hi.status, hi.row) == (NOT_INDICATED, "11b", DISCUSS, "5")


def test_diabetes_30_39_risk_applies_at_any_ldl():
    """Figure 9 has no LDL-C condition: LDL-C < 70 with risk >= 3% is DISCUSS, not row 7."""
    f = make(age=35, sex="M", diabetes=True, tc=200.0, hdl=40.0, ldl=60.0, sbp=150.0,
             bp=True, smoker=True)
    r = lab(f)
    assert r.risk10 >= 0.03 and (r.status, r.row) == (DISCUSS, "5")


@pytest.mark.parametrize("ldl,status,row", [(159.0, NOT_INDICATED, "11b"), (160.0, DISCUSS, "5"),
                                            (189.0, DISCUSS, "5"), (190.0, GAP, "2")])
def test_diabetes_30_39_ldl_160_189_low_risk_is_discuss(ldl, status, row):
    """LDL-C 160-189 at low risk (10-year < 3%, 30-year < 10%, no feature): Figure 9 reads
    NO (counseling), Figure 6's low-risk branch reads COR 2a; two readings give DISCUSS."""
    p = dict(age=30, sex="F", tc=ldl + 85.0, hdl=80.0, sbp=100.0, diabetes=True)
    r = lab(make(**p, ldl=ldl))
    if row != "2":
        assert r.risk10 < 0.03 and r.risk30 < 0.10
    assert (r.status, r.row) == (status, row)


def test_diabetes_30_39_ldl_band_makes_lipid_and_smoking_deciding_as_usual():
    """CANNOT_DETERMINE handling is the same as in the other rows: without a lipid panel the
    LDL-C band decides (row 2 GAP, row 5 DISCUSS, row 11b NOT_INDICATED); with LDL-C 160-189
    a missing smoking status no longer decides, since the LDL-C band gives DISCUSS alone."""
    p = dict(age=30, sex="F", tc=250.0, hdl=80.0, sbp=100.0, diabetes=True)
    r = lab(make(**p, ldl=165.0, lipid=False))
    assert (r.status, r.missing) == (CANNOT_DETERMINE, ("lipid_panel",))
    r = lab(make(**p, ldl=165.0, smoker=None))
    assert (r.status, r.row, r.missing) == (DISCUSS, "5", ())
    assert "smoking" not in r.deciding


def test_diabetes_30_39_missing_smoking_deciding():
    p = dict(age=32, sex="F", tc=170.0, hdl=65.0, ldl=100.0, diabetes=True)
    s = sbp_at(0.10, horizon=30, **p) - 0.3  # NOT_INDICATED as a non-smoker
    assert lab(make(**p, sbp=s)).status == NOT_INDICATED
    assert lab(make(**p, sbp=s, smoker=True)).status == DISCUSS
    r = lab(make(**p, sbp=s, smoker=None))
    assert r.status == CANNOT_DETERMINE and r.missing == ("smoking",)
    r = lab(make(**p, sbp=s, smoker=None, retinopathy=True))
    assert (r.status, r.row, r.missing) == (DISCUSS, "5", ())  # a feature decides alone


@pytest.mark.parametrize("years,status,row", [(9.9, NOT_INDICATED, "11b"), (10.0, DISCUSS, "5")])
def test_diabetes_duration_10_years(years, status, row):
    f = make(**LOW | {"age": 35, "ldl": 100.0}, diabetes=True, dm_years=years)
    r = lab(f)
    if row == "11b":  # no feature: PREVENT decides (10-year < 3%, 30-year < 10%)
        assert r.risk10 < 0.03 and r.risk30 < 0.10
    assert (r.status, r.row) == (status, row)


def test_diabetes_30_39_features():
    base = LOW | {"age": 35, "ldl": 100.0}
    for kw in (dict(retinopathy=True), dict(neuropathy=True), dict(albuminuria=True), dict(ckd=3)):
        r = lab(make(**base, diabetes=True, **kw))
        assert (r.status, r.row) == (DISCUSS, "5"), kw
    r = lab(make(**base | {"ldl": 60.0, "tc": 140.0}, diabetes=True, retinopathy=True))
    assert (r.status, r.row) == (DISCUSS, "5")
    r = lab(make(**base | {"ldl": 60.0, "tc": 140.0}, diabetes=True))
    assert (r.status, r.row) == (NOT_INDICATED, "7")


# ---------------------------------------------------------------- rows 6 and 7
def test_age_75_76_primary_prevention():
    assert lab(make(age=76, ldl=120.0)).row == "6"
    assert lab(make(age=76, ldl=120.0)).status == DISCUSS
    assert lab(make(age=75, ldl=120.0)).row in ("8", "9", "10", "11b")


def test_ldl_69_70():
    r = lab(make(**LOW | {"ldl": 69.0, "tc": 150.0}))
    assert (r.status, r.row, r.deciding) == (NOT_INDICATED, "7", ("lipid_panel",))
    assert r.risk10 is None  # PREVENT not needed
    r = lab(make(**LOW | {"ldl": 70.0, "tc": 150.0}))
    assert r.row in ("10", "11a", "11b") and r.risk10 is not None


# ---------------------------------------------------------------- rows 8-11 (risk cuts)
P10 = dict(age=65, sex="M", tc=200.0, hdl=50.0, ldl=120.0)
P5 = dict(age=55, sex="M", tc=200.0, hdl=50.0, ldl=120.0)
P3 = dict(age=62, sex="F", tc=180.0, hdl=60.0, ldl=110.0)


def test_risk_10_percent_cut_and_variants():
    s = sbp_at(0.10, **P10)
    below, above = make(**P10, sbp=s - 0.3), make(**P10, sbp=s + 0.3)
    rb, ra = lab(below), lab(above)
    assert rb.risk10 < 0.10 <= ra.risk10 and ra.risk10 - rb.risk10 < 0.001
    assert (rb.status, rb.row, ra.status, ra.row) == (GAP, "9", GAP, "8")
    assert (lab(below, variant="S-A").status, lab(above, variant="S-A").status) == (GAP, DISCUSS)
    assert (lab(below, variant="S-C").status, lab(above, variant="S-C").status) == (DISCUSS, DISCUSS)


def test_risk_5_percent_cut():
    s = sbp_at(0.05, **P5)
    rb, ra = lab(make(**P5, sbp=s - 0.3)), lab(make(**P5, sbp=s + 0.3))
    assert rb.risk10 < 0.05 <= ra.risk10
    assert (rb.status, rb.row, ra.status, ra.row) == (DISCUSS, "10", GAP, "9")


def test_risk_3_percent_cut_age_62():
    s = sbp_at(0.03, **P3)
    rb, ra = lab(make(**P3, sbp=s - 0.5)), lab(make(**P3, sbp=s + 0.5))
    assert rb.risk10 < 0.03 <= ra.risk10
    assert (rb.status, rb.row, ra.status, ra.row) == (NOT_INDICATED, "11b", DISCUSS, "10")
    assert rb.risk30 is None  # 30-year risk only at ages 30-59


def test_30_year_risk_at_59_and_60():
    p = dict(sex="F", tc=180.0, hdl=60.0, ldl=110.0)
    s = sbp_at(0.10, horizon=30, age=59, **p)
    hi, lo = lab(make(age=59, **p, sbp=s + 0.5)), lab(make(age=59, **p, sbp=s - 0.5))
    assert hi.risk10 < 0.03 and hi.risk30 >= 0.10 > lo.risk30
    assert (hi.status, hi.row, lo.status, lo.row) == (DISCUSS, "11a", NOT_INDICATED, "11b")
    r60 = lab(make(age=60, **p, sbp=s + 0.5))
    assert r60.risk10 < 0.03 and r60.risk30 is None
    assert (r60.status, r60.row) == (NOT_INDICATED, "11b")


def test_ldl_159_160_low_risk():
    a = lab(make(**LOW | {"ldl": 159.0, "tc": 240.0}))
    b = lab(make(**LOW | {"ldl": 160.0, "tc": 240.0}))
    assert a.risk30 < 0.10
    assert (a.status, a.row, b.status, b.row) == (NOT_INDICATED, "11b", DISCUSS, "11a")
    # Age 62: LDL-C 160-189 counts at any age (Table 1, Figure 6); 30-year risk does not.
    c = lab(make(**P3 | {"ldl": 160.0, "tc": 240.0}, sbp=112.0))
    assert c.risk10 < 0.03 and c.risk30 is None and (c.status, c.row) == (DISCUSS, "11a")
    d = lab(make(**P3 | {"ldl": 159.0, "tc": 240.0}, sbp=112.0))
    assert (d.status, d.row) == (NOT_INDICATED, "11b")


@pytest.mark.parametrize("age,row", [(59, "11a"), (60, "11a"), (64, "11a"), (76, "6")])
def test_ldl_160_189_low_risk_ages_30_75(age, row):
    f = make(age=age, sex="F", tc=240.0, hdl=70.0, ldl=170.0, sbp=100.0)
    r = lab(f)
    assert r.row == row and r.status == DISCUSS
    if row == "11a":
        assert r.risk10 < 0.03


# ---------------------------------------------------------------- deciding inputs
def test_missing_lipid_panel_deciding_vs_not():
    r = lab(make(**P5, sbp=150.0, lipid=False))
    assert (r.status, r.missing, r.row) == (CANNOT_DETERMINE, ("lipid_panel",), None)
    r = lab(make(age=60, ascvd=("STROKE",), lipid=False))
    assert (r.status, r.missing) == (GAP, ())


def test_missing_smoking_deciding_vs_not():
    f = make(**P5, sbp=150.0)
    assert lab(f).status == DISCUSS and "smoking" in lab(f).deciding  # 4.3%; 6.2% if smoking
    r = lab(make(**P5, sbp=150.0, smoker=None))
    assert (r.status, r.missing) == (CANNOT_DETERMINE, ("smoking",))
    hi = dict(age=70, sex="M", tc=240.0, hdl=40.0, ldl=160.0, sbp=180.0, bp=True)
    r = lab(make(**hi, smoker=None))
    assert (r.status, r.missing) == (GAP, ()) and "smoking" not in r.deciding


def test_sbp_out_of_range():
    r = lab(make(**P5, sbp=85.0))
    assert r.status == CANNOT_DETERMINE and r.out_of_range == ("sbp",) and r.missing == ()
    r = lab(make(**P5, sbp=201.0))
    assert r.status == CANNOT_DETERMINE and r.out_of_range == ("sbp",)
    r = lab(make(age=50, diabetes=True, sbp=85.0))
    assert (r.status, r.row, r.out_of_range) == (GAP, "3", ())


def test_two_missing_inputs_both_named():
    r = lab(make(**P5, sbp=158.0, smoker=None, egfr=None))
    assert r.status == CANNOT_DETERMINE and set(r.missing) == {"smoking", "egfr"}


def test_tc_out_of_range_only_matters_where_prevent_is_used():
    r = lab(make(**LOW | {"tc": 125.0, "ldl": 50.0, "hdl": 60.0}))
    assert (r.status, r.row, r.out_of_range) == (NOT_INDICATED, "7", ())
    assert "lipid_panel" in r.deciding
    r = lab(make(**P5 | {"tc": 125.0, "hdl": 40.0, "ldl": 75.0}, sbp=150.0))
    assert r.status == CANNOT_DETERMINE and r.out_of_range == ("lipid_panel",)


def test_unrecognized_smoking_value_is_out_of_range():
    r = lab(make(**P5, sbp=150.0, smoking_value="Unknown if ever smoked"))
    assert r.status == CANNOT_DETERMINE and r.out_of_range == ("smoking",)


def test_lipid_panel_deciding_unless_settled_by_rows_0_1_3():
    rng = np.random.default_rng(7)
    n = 0
    for _ in range(300):
        kw = dict(age=int(rng.integers(30, 80)), sex=str(rng.choice(["M", "F"])),
                  tc=float(rng.integers(130, 320)), hdl=float(rng.integers(20, 100)),
                  sbp=float(rng.integers(90, 200)), smoker=bool(rng.random() < 0.3),
                  egfr=float(rng.integers(15, 150)), diabetes=bool(rng.random() < 0.3),
                  ckd=[None, 3, 4][int(rng.integers(3))], bp=bool(rng.random() < 0.5),
                  ascvd=("MI",) if rng.random() < 0.15 else (), statin=bool(rng.random() < 0.1),
                  retinopathy=bool(rng.random() < 0.3))
        kw["ldl"] = float(rng.integers(40, max(41, int(kw["tc"] - kw["hdl"]))))
        f = make(**kw)
        r = lab(f)
        settled = kw["statin"] or kw["ascvd"] or (kw["diabetes"] and 40 <= kw["age"] <= 75)
        assert ("lipid_panel" in r.deciding) == (not settled), (kw, r)
        n += not settled
    assert n > 100


def test_tie_takes_smallest_row_id():
    """Two LDL-C values at the latest timestamp: facts (and so the oracle) use the smaller id."""
    ts = "2026-10-01T09:00:00Z"

    def ob(rid, code, value, unit="mg/dL"):
        return {"row_id": rid, "patch": None, "DATE": ts, "CODE": code, "VALUE": value,
                "UNITS": unit, "DESCRIPTION": ""}

    def record(ids):
        o = pd.DataFrame([ob("obs_0a", C.TC.code, "280"), ob("obs_0b", C.HDL.code, "50"),
                          ob(ids[0], C.LDL.code, "189"), ob(ids[1], C.LDL.code, "195"),
                          ob("obs_0e", C.SBP.code, "112", "mm[Hg]"),
                          ob("obs_0f", C.SMOKING.code, C.SMOKING_NOT_CURRENT[0], None),
                          ob("obs_0g", C.EGFR.code, "90", C.EGFR_UNIT_KEEP)])
        e = pd.DataFrame()
        return facts.PatientRecord("p", date(2026, 11, 1), date(1981, 1, 1), "F", e, o, e, e, e)

    f = facts.facts_at_index(record(("obs_1", "obs_2")))
    assert f.lipid_panel.ldl.value == 189.0 and f.lipid_panel.tied
    assert lab(f).row != "2"
    f = facts.facts_at_index(record(("obs_2", "obs_1")))
    assert f.lipid_panel.ldl.value == 195.0 and lab(f).row == "2"


# ---------------------------------------------------------------- reasons
@pytest.mark.parametrize("days,status", [(0, DOCUMENTED_REASON), (364, DOCUMENTED_REASON),
                                         (365, DOCUMENTED_REASON), (366, GAP), (-1, GAP)])
def test_reason_window(days, status):
    f = make(ascvd=("MI",))
    r = lab(f, reasons=[Reason("patient_declined", INDEX - timedelta(days=days), "note_1")])
    assert r.status == status
    assert (r.reason is not None) == (status == DOCUMENTED_REASON)


def test_reason_changes_only_gap():
    why = [Reason("statin_intolerance", INDEX - timedelta(days=10), "note_1")]
    assert lab(make(**LOW | {"ldl": 60.0, "tc": 140.0}), reasons=why).status == NOT_INDICATED
    assert lab(make(age=77), reasons=why).status == DISCUSS
    assert lab(make(statin=True), reasons=why).status == ON_THERAPY
    assert lab(make(**P5, sbp=150.0, smoker=None), reasons=why).status == CANNOT_DETERMINE
    s = sbp_at(0.05, **P5)
    f = make(**P5, sbp=s + 0.3)
    assert lab(f, reasons=why).status == DOCUMENTED_REASON
    assert lab(f, reasons=why, variant="S-C").status == DISCUSS


@pytest.mark.parametrize("sex,age,status", [("F", 45, DOCUMENTED_REASON), ("F", 49, DOCUMENTED_REASON),
                                            ("F", 50, GAP), ("M", 45, GAP)])
def test_pregnancy_reason_women_30_49(sex, age, status):
    why = [Reason("pregnancy", INDEX - timedelta(days=30), "note_1")]
    assert lab(make(age=age, sex=sex, ascvd=("MI",)), reasons=why).status == status


def test_structured_statin_allergy_is_a_reason():
    f = make(ascvd=("MI",), allergies=[("Statin intolerance", INDEX - timedelta(days=100))])
    r = lab(f)
    assert r.status == DOCUMENTED_REASON and r.reason.category == "statin_intolerance"
    assert r.reason.source == f.allergies[0].row_id
    other = make(ascvd=("MI",), allergies=[("Aspirin", INDEX - timedelta(days=100))])
    assert lab(other).status == GAP


@pytest.mark.parametrize("start,stop,status", [
    (400, None, DOCUMENTED_REASON),    # active at index, started long ago
    (3000, None, DOCUMENTED_REASON),
    (100, -10, DOCUMENTED_REASON),     # stop after index: still active
    (400, 200, DOCUMENTED_REASON),     # ended, stop within 365 days
    (100, 0, DOCUMENTED_REASON),       # ended on the index date
    (900, 400, GAP),                   # ended more than 365 days ago
    (300, 366, GAP)])                  # (stop before start is not generated; still ended)
def test_statin_allergy_active_or_recently_ended(start, stop, status):
    a = [("atorvastatin intolerance", INDEX - timedelta(days=start),
          None if stop is None else INDEX - timedelta(days=stop))]
    f = make(ascvd=("MI",), allergies=a)
    assert lab(f).status == status


@pytest.mark.parametrize("desc,match", [
    ("Statin intolerance", True), ("Statin-induced myopathy", True),
    ("Allergy to statins", True), ("Rosuvastatin", True), ("simvastatin 20 MG", True),
    ("Cerivastatin", True), ("Allergy to HMG-CoA reductase inhibitor", True),
    ("Nystatin", False), ("Allergy to nystatin (finding)", False), ("Cilastatin", False),
    ("Imipenem and cilastatin", False), ("Somatostatin", False), ("Ecallantide", False),
    ("Aspirin", False), ("Penicillin V", False)])
def test_statin_allergy_pattern(desc, match):
    import re
    assert bool(re.search(C.STATIN_ALLERGY_PATTERN, desc, re.I)) == match
    f = make(ascvd=("MI",), allergies=[(desc, INDEX - timedelta(days=10))])
    assert (lab(f).status == DOCUMENTED_REASON) == match


@pytest.mark.parametrize("cat,sex,age,days,status", [
    ("palliative", "M", 60, 0, DOCUMENTED_REASON), ("palliative", "M", 60, 365, DOCUMENTED_REASON),
    ("palliative", "M", 60, 366, GAP), ("pregnancy", "F", 35, 100, DOCUMENTED_REASON),
    ("pregnancy", "F", 52, 100, GAP), ("pregnancy", "F", 35, 400, GAP)])
def test_structured_pregnancy_and_palliative_reasons(cat, sex, age, days, status):
    f = make(age=age, sex=sex, ascvd=("MI",), structured=((cat, days),))
    r = lab(f)
    assert r.status == status
    if status == DOCUMENTED_REASON:
        assert r.reason.category == cat and r.reason.source == f.reason_entries[0].row_id


def test_structured_reason_changes_only_gap():
    f = make(**LOW | {"ldl": 60.0, "tc": 140.0}, structured=(("palliative", 10),))
    assert lab(f).status == NOT_INDICATED
    assert lab(make(statin=True, structured=(("palliative", 10),))).status == ON_THERAPY


def _tab(rows, cols):
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


def test_reason_entries_from_record():
    """Code list and dates of structured reasons; palliative dental care does not count."""
    ix = INDEX
    d = lambda n: (ix - timedelta(days=n)).isoformat() + "T10:00:00Z"
    cond = _tab([("cond_a", d(500), d(200), "72892002", "Normal pregnancy (finding)"),
                 ("cond_b", d(900), d(600), "72892002", "Normal pregnancy (finding)"),
                 ("cond_c", d(30), None, "Z51.5", "Encounter for palliative care")],
                ["row_id", "START", "STOP", "CODE", "DESCRIPTION"])
    proc = _tab([("proc_a", d(20), d(20), "D9110", "palliative treatment of dental pain - per visit"),
                 ("proc_b", d(40), d(40), "385763009", "Hospice care (regime/therapy)"),
                 ("proc_c", d(10), d(10), "306237005", "Referral to palliative care service (procedure)")],
                ["row_id", "START", "STOP", "CODE", "DESCRIPTION"])
    enc = _tab([("enc_a", d(5), d(5), "305336008", "Admission to hospice (procedure)"),
                ("enc_b", d(3), d(3), "185349003", "Encounter for check up")],
               ["row_id", "START", "STOP", "CODE", "DESCRIPTION"])
    empty = pd.DataFrame(columns=["row_id", "START", "STOP", "CODE", "DESCRIPTION"])
    obs = pd.DataFrame(columns=["row_id", "patch", "DATE", "CODE", "VALUE", "UNITS"])
    rec = facts.PatientRecord("p", ix, date(1980, 1, 1), "F", cond, obs, empty, proc, empty,
                              encounters=enc)
    got = {(e.row_id, e.category, e.date) for e in facts.facts_at_index(rec).reason_entries}
    assert got == {("cond_a", "pregnancy", ix - timedelta(days=200)),  # STOP in window
                   ("cond_c", "palliative", ix - timedelta(days=30)),
                   ("proc_b", "palliative", ix - timedelta(days=40)),
                   ("proc_c", "palliative", ix - timedelta(days=10)),
                   ("enc_a", "palliative", ix - timedelta(days=5))}
    assert "D9110" in C.PALLIATIVE_NOT_COUNTED and "D9110" not in C.PALLIATIVE_PROCEDURES


def test_reason_category_checked():
    with pytest.raises(ValueError):
        Reason("will revisit next visit", INDEX, "note_1")


# ---------------------------------------------------------------- coverage
def test_every_row_reachable():
    s5, s10 = sbp_at(0.05, **P5), sbp_at(0.10, **P10)
    cases = {
        "0": make(statin=True), "X": make(hiv=True), "1": make(ascvd=("MI",)),
        "2": make(ldl=200.0, tc=290.0), "3": make(age=50, diabetes=True),
        "3b": make(age=50, ckd=3), "4": make(age=77, diabetes=True),
        "5": make(**LOW | {"age": 35}, diabetes=True, neuropathy=True), "6": make(age=77),
        "7": make(ldl=60.0, tc=150.0), "8": make(**P10, sbp=s10 + 1),
        "9": make(**P5, sbp=s5 + 1), "10": make(**P5, sbp=s5 - 1),
        "11a": make(**LOW | {"ldl": 170.0, "tc": 260.0}), "11b": make(**LOW),
    }
    assert {r: lab(f).row for r, f in cases.items()} == {r: r for r in cases}
    assert set(cases) == set(oracle.ROW_BY_ID)


def test_inputs_used_cite_sources():
    f = make(**P5, sbp=150.0)
    r = lab(f)
    assert r.inputs_used["lipid_panel"] == f.lipid_panel.row_ids
    assert r.inputs_used["sbp"] == (f.sbp.row_id,)
    assert r.inputs_used["statins"] == () and r.inputs_used["ascvd"] == ()  # ABSENT claims
    assert r.path[:3] == ("0", "X", "1") and r.path[-1] == r.row


def test_ignore_row0_gives_indication_group():
    f = make(**P5, sbp=170.0, statin=True)
    assert lab(f).status == ON_THERAPY
    r = oracle.indication(f)
    assert r.indication_row in ("8", "9") and r.status == GAP
