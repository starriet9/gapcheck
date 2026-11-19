"""Stratum assignment and the 'not sampled' rules (SPEC 5.1), on hand-built facts."""
from dataclasses import replace

import numpy as np

from gapcheck import oracle, prevent, strata
from vignette import make

P5 = dict(age=55, sex="M", tc=200.0, hdl=50.0, ldl=120.0)
LOW = dict(age=45, sex="F", tc=170.0, hdl=65.0, ldl=90.0, sbp=112.0)


def sbp_at(target, egfr=90.0, **p):
    sex = "male" if p["sex"] == "M" else "female"
    g = lambda s: prevent.ascvd_risk(sex, p["age"], p["tc"], p["hdl"], s, 0, 0, 0, 0, egfr)
    lo, hi = 110.0, 200.0
    for _ in range(60):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if g(m) < target else (lo, m)
    return lo


def st(f):
    return strata.assign(f)


def test_s1_any_age_and_treated():
    s = st(make(age=78, ascvd=("STROKE",), statin=True))
    assert (s.candidate, s.stratum, s.ascvd_type, s.on_statin_native) == ("S1", "S1", "stroke_only", True)
    s = st(make(age=50, ascvd=("MI", "STROKE"), vet=True))  # 55822004 does not apply to S1
    assert (s.stratum, s.ascvd_type) == ("S1", "MI/ACS")


def test_s2_needs_an_earlier_ldl_ge190():
    s = st(make(ldl=200.0, tc=290.0))
    assert s.candidate == "S2" and s.stratum is None and s.blocking() == ("s2_no_earlier_ldl_ge190",)
    s = st(make(ldl=200.0, tc=290.0, ldl_earlier_ge190=(195.0,)))
    assert s.stratum == "S2"


def test_s2_primary_prevention_76_79_and_diabetes_outside_40_75():
    s = st(make(age=77, ldl=200.0, tc=290.0, ldl_earlier_ge190=(195.0,)))
    assert s.candidate == "S2" and "primary_prevention_76_79" in s.blocking()
    s = st(make(age=35, diabetes=True, ldl=200.0, tc=290.0, ldl_earlier_ge190=(195.0,)))
    assert s.candidate == "S2" and s.blocking() == ("diabetes_outside_40_75",)


def test_s3_to_s5_no_ldl_ge190_ever():
    s = st(make(age=50, diabetes=True, ldl_ever_max=195.0))
    assert s.candidate == "S3" and s.blocking() == ("ldl_ever_ge190",)
    assert st(make(age=50, diabetes=True)).stratum == "S3"


def test_s3_55822004_and_pregnancy():
    assert st(make(age=50, diabetes=True, vet=True)).blocking() == ("dx_55822004",)
    s = st(make(age=45, sex="F", diabetes=True, pregnancy=True))
    assert s.blocking() == ("structured_reason_365d",)


def test_diabetes_evidence_without_diagnosis_not_sampled_in_any_stratum():
    """A diabetic complication or a glucose-lowering drug with no diabetes code (a Synthea
    artifact) removes the patient from every stratum; the oracle still reads no diabetes.
    With a diabetes code the same evidence changes nothing."""
    kidney = (("conditions", "127013003"),)
    insulin = (("medications", "106892"),)
    for f, cand in ((make(**P5, sbp=170.0, dm_evidence=kidney), "S4"),
                    (make(**LOW, dm_evidence=insulin), "S5"),
                    (make(age=50, ascvd=("MI",), dm_evidence=kidney + insulin), "S1"),
                    (make(ldl=200.0, tc=290.0, ldl_earlier_ge190=(195.0,),
                          dm_evidence=(("medications", "860975"),)), "S2")):
        s = st(f)
        assert s.candidate == cand and s.stratum is None, cand
        assert s.blocking() == ("diabetes_evidence_without_diagnosis",), cand
        assert not f.has_diabetes and oracle.indication(f).row != "3"
    s = st(make(age=50, diabetes=True, dm_evidence=kidney + insulin))
    assert s.stratum == "S3" and "diabetes_evidence_without_diagnosis" not in s.exclusions
    # The lists hold complications and drugs only: not prediabetes, not diabetes itself.
    from gapcheck import codes as C
    assert set(C.DIABETIC_COMPLICATIONS).isdisjoint(set(C.DIABETES) | set(C.PREDIABETES))


def test_structured_reason_within_365_days_not_sampled():
    for cat in ("palliative", "pregnancy"):
        s = st(make(age=45, sex="F", diabetes=True, structured=((cat, 200),)))
        assert s.candidate == "S3" and s.blocking() == ("structured_reason_365d",)
    s = st(make(age=60, sex="M", diabetes=True, structured=(("pregnancy", 10),)))  # any age/sex
    assert s.blocking() == ("structured_reason_365d",)
    s = st(make(age=50, ascvd=("MI",), structured=(("palliative", 30),)))
    assert s.candidate == "S1" and s.stratum is None
    assert st(make(age=50, diabetes=True, structured=(("palliative", 400),))).stratum == "S3"


def test_s4_bins_and_p1_egfr_rule():
    s10 = sbp_at(0.10, **P5 | {"age": 65})
    s = st(make(**P5 | {"age": 65}, sbp=s10 + 3))
    assert (s.stratum, s.s4_bin) == ("S4", ">=10")
    s = st(make(**P5, sbp=170.0))
    assert (s.stratum, s.s4_bin) == ("S4", "5-<10")
    # Near 10%: the risk bin moves with eGFR 60-140, the main label does not.
    s = st(make(**P5 | {"age": 65}, sbp=s10 + 0.2, egfr_p1=True))
    assert s.p1_row_dependent and not s.p1_label_dependent
    assert "p1_egfr_dependent" in s.blocking()
    # Near 5%: the main label moves too.
    s5 = sbp_at(0.05, **P5)
    s = st(make(**P5, sbp=s5 + 0.2, egfr_p1=True))
    assert s.p1_row_dependent and s.p1_label_dependent
    # Far from any cut: kept.
    s = st(make(**P5, sbp=175.0, egfr_p1=True))
    assert s.egfr_p1 and s.p1_row_dependent is False and s.stratum == "S4"
    # Same profile with a native eGFR: rule does not apply.
    assert st(make(**P5 | {"age": 65}, sbp=s10 + 0.2)).stratum == "S4"


def test_rows_over_egfr_matches_oracle():
    f = make(**P5, sbp=sbp_at(0.05, **P5) + 0.2)
    x = oracle.inputs_from_facts(f)
    vals = np.array([60.0, 90.0, 140.0])
    fast = strata.rows_over_egfr(x, vals)
    from dataclasses import replace
    slow = [oracle.evaluate(replace(x, egfr=v), ignore_row0=True)["row"] for v in vals]
    assert list(fast) == slow


def test_s5_rules():
    s = st(make(**LOW))
    assert (s.indication_row, s.stratum) == ("11b", "S5")
    s = st(make(**LOW | {"ldl": 60.0, "tc": 150.0}))
    assert (s.indication_row, s.stratum) == ("7", "S5")
    # LDL-C < 70 with 10-year risk >= 5% (GAP at LDL-C 70)
    s = st(make(**P5 | {"ldl": 60.0}, sbp=190.0))
    assert s.indication_row == "7" and s.blocking() == ("row7_label_changes_at_ldl70",)
    # age 60-75, risk < 3%, LDL-C 160-189: row 11a now (not a stratum), still flagged
    s = st(make(age=62, sex="F", tc=240.0, hdl=70.0, ldl=165.0, sbp=112.0))
    assert s.indication_row == "11a" and s.candidate is None
    assert "age60_75_risk_lt3_ldl160_189" in s.exclusions
    # diabetes 30-39 landing in row 7
    s = st(make(**LOW | {"age": 35, "ldl": 60.0, "tc": 150.0}, diabetes=True))
    assert s.indication_row == "7" and "diabetes_outside_40_75" in s.blocking()


def test_row7_rule_label_at_ldl70():
    """Row 7 is sampled only if the label stays NOT_INDICATED at LDL-C 70 (M1)."""
    s3 = sbp_at(0.03, **P5 | {"ldl": 60.0})
    # 10-year risk 3-5%: DISCUSS (row 10) at LDL-C 70
    s = st(make(**P5 | {"ldl": 60.0}, sbp=s3 + 1.0))
    assert s.indication_row == "7" and s.blocking() == ("row7_label_changes_at_ldl70",)
    # 30-year risk >= 10% at 30-59 (row 11a at LDL-C 70)
    p = dict(age=45, sex="M", tc=230.0, hdl=40.0, ldl=60.0)
    s30 = sbp_at(0.03, **p)
    f = make(**p, sbp=s30 - 2.0)
    r = oracle.label(f)
    assert r.row == "7"
    r70 = oracle.evaluate(replace(oracle.inputs_from_facts(f), ldl=70.0), ignore_row0=True)
    assert (r70["row"] == "11a") == ("row7_label_changes_at_ldl70" in st(f).exclusions)
    # CKD stage 3 at 40-75: GAP (row 3b) at LDL-C 70
    s = st(make(**LOW | {"age": 50, "ldl": 60.0, "tc": 150.0}, ckd=3))
    assert s.indication_row == "7" and s.blocking() == ("row7_label_changes_at_ldl70",)


def test_row7_rule_checked_over_p1_egfr_range():
    # Risk just under 5% at the recorded eGFR but over 5% at eGFR 60 -> excluded with P1;
    # just under 3% at eGFR 140 and over 3% at eGFR 60 -> excluded with P1 too.
    p = P5 | {"ldl": 65.0}
    for cut in (0.05, 0.03):
        s_cut = sbp_at(cut, egfr=140.0, **p)
        f_native = make(**p, sbp=s_cut - 0.5, egfr=140.0)
        f_p1 = make(**p, sbp=s_cut - 0.5, egfr=140.0, egfr_p1=True)
        risks = prevent.ascvd_risk_array("male", 55, 200.0, 50.0, s_cut - 0.5, 0, 0, 0, 0,
                                         strata.P1_EGFR_VALUES)
        assert risks.min() < cut <= risks.max()
        if cut == 0.05:  # at eGFR 140 the risk is 3-5%: DISCUSS at 70, as for P1
            r70 = oracle.evaluate(replace(oracle.inputs_from_facts(f_native), ldl=70.0),
                                  ignore_row0=True)
            assert r70["status"] == oracle.DISCUSS
        assert "row7_label_changes_at_ldl70" in st(f_p1).exclusions
        assert st(f_p1).blocking() == ("row7_label_changes_at_ldl70",)


def test_prevent_input_out_of_range_where_needed():
    hi = dict(age=70, sex="M", tc=260.0, hdl=40.0, ldl=170.0)  # >= 5% at any SBP or eGFR
    for kw in (dict(sbp=85.0), dict(sbp=None), dict(sbp=150.0, egfr=None)):
        s = st(make(**hi, **kw))
        assert s.indication_status == oracle.GAP and s.candidate == "S4"
        assert s.blocking() == ("prevent_input_missing_or_out_of_range",)
    s = st(make(age=50, diabetes=True, sbp=85.0))  # S3: PREVENT not needed
    assert s.stratum == "S3"


def test_native_egfr_above_150_only_where_prevent_needed():
    s = st(make(age=50, diabetes=True, egfr=155.0))
    assert s.stratum == "S3"
    s = st(make(age=50, ascvd=("MI",), egfr=155.0))
    assert s.stratum == "S1"
    hi = dict(age=70, sex="M", tc=260.0, hdl=40.0, ldl=170.0, sbp=150.0)  # >= 5% at any eGFR
    s = st(make(**hi, egfr=155.0))
    assert s.candidate == "S4" and "native_egfr_gt150" in s.blocking()


def test_s2_meeting_row3_not_sampled():
    s = st(make(age=50, diabetes=True, ldl=200.0, tc=290.0, ldl_earlier_ge190=(195.0,)))
    assert s.candidate == "S2" and s.blocking() == ("s2_meets_row3",)


def test_ties_only_in_deciding_inputs():
    s = st(make(**P5, sbp=170.0, ties=("lipid_panel",)))
    assert "tie_in_deciding_input" in s.blocking()
    s = st(make(age=50, diabetes=True, ties=("lipid_panel", "egfr")))  # nothing deciding in S3
    assert s.stratum == "S3"


def test_cannot_determine_and_row3b_not_sampled():
    s = st(make(**P5, sbp=150.0, lipid=False))
    assert s.candidate is None and "cannot_determine" in s.exclusions
    s = st(make(age=50, ckd=3))
    assert s.candidate is None and "row3b_ckd" in s.exclusions
    s = st(make(hiv=True))
    assert s.indication_row == "X" and s.exclusions == ("hiv",)
