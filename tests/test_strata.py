"""Stratum assignment and the 'not sampled' rules (SPEC 5.1), on hand-built facts."""
from dataclasses import replace
from datetime import timedelta

import numpy as np

from gapcheck import oracle, prevent, strata
from vignette import INDEX, make

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
    hi = dict(age=70, sex="M", tc=255.0, hdl=40.0, ldl=170.0)  # >= 5% at any SBP or eGFR
    for kw in (dict(sbp=85.0), dict(sbp=None), dict(sbp=150.0, egfr=None)):
        s = st(make(**hi, **kw))
        assert s.indication_status == oracle.GAP and s.candidate == "S4"
        assert s.blocking() == ("prevent_input_missing_or_out_of_range",)
    s = st(make(age=50, diabetes=True, sbp=85.0))  # S3: PREVENT not needed
    assert s.stratum == "S3"


def test_native_egfr_above_140_only_where_prevent_needed():
    """The oracle computes with eGFR up to 150 (PREVENT Tables S12A, S12F), but the packages
    and the AHA online calculator accept only 15-140, so a native eGFR above 140 where PREVENT
    is needed is not sampled."""
    for e in (145.0, 155.0):
        assert st(make(age=50, diabetes=True, egfr=e)).stratum == "S3"
        assert st(make(age=50, ascvd=("MI",), egfr=e)).stratum == "S1"
    hi = dict(age=70, sex="M", tc=255.0, hdl=40.0, ldl=170.0, sbp=150.0)  # >= 5% at any eGFR
    s = st(make(**hi, egfr=155.0))
    assert s.candidate == "S4" and "native_egfr_gt140" in s.blocking()
    s = st(make(**hi, egfr=141.0))
    assert s.candidate == "S4" and s.blocking() == ("native_egfr_gt140",)
    assert oracle.usable(oracle.inputs_from_facts(make(**hi, egfr=141.0)))["egfr"]
    assert st(make(**hi, egfr=140.0)).stratum == "S4"


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


def test_heart_failure_not_sampled_whatever_the_row():
    # Sec. 4.2.6 rec 8 and sec. 4.2.8.6 rec 1 are not encoded: the label stays the
    # row's, but no patient with a heart failure code is a base patient.
    for kw in ({"ascvd": ("IHD",)}, {"age": 50, "diabetes": True}, {}):
        s = st(make(hf=True, **kw))
        assert "heart_failure" in s.exclusions and s.stratum is None, kw
        assert st(make(**kw)).indication_row == s.indication_row


def test_anthracycline_within_365_days_not_sampled():
    # Sec. 4.2.8.10 rec 3 (COR 2b) is not encoded.
    assert "anthracycline_365d" in st(make(anthracycline_stop=None)).exclusions
    assert "anthracycline_365d" in st(make(anthracycline_stop=INDEX - timedelta(days=365))).exclusions
    assert "anthracycline_365d" not in st(make(anthracycline_stop=INDEX - timedelta(days=366))).exclusions
    assert "anthracycline_365d" not in st(make()).exclusions


def test_aids_code_is_hiv():
    from gapcheck import codes
    assert "62479008" in codes.HIV and "86406008" in codes.HIV


def test_pregnancy_within_5_years_not_sampled():
    """Sec. 4.2.8.4 supportive text 2: statins may be deferred 'for a period of 1 year, and
    possibly, several years'. A pregnancy whose stop (start if it has no stop) is within
    1,826 days before index removes the patient from every stratum, at any age or sex; the
    oracle's reason window stays 365 days."""
    s2 = dict(age=35, sex="F", ldl=200.0, tc=290.0, ldl_earlier_ge190=(195.0,))
    assert st(make(**s2)).stratum == "S2"
    for days in (418, 773, 1826):
        stop = INDEX - timedelta(days=days)
        f = make(**s2, pregnancy_past=((stop - timedelta(days=280), stop),))
        s = st(f)
        assert s.candidate == "S2" and s.blocking() == ("pregnancy_5y",), days
    stop = INDEX - timedelta(days=1827)
    assert st(make(**s2, pregnancy_past=((stop - timedelta(days=280), stop),))).stratum == "S2"
    # no stop date: the start counts
    f = make(**s2, pregnancy_past=((INDEX - timedelta(days=1000), None),))
    assert "pregnancy_5y" in st(f).exclusions
    # any stratum, any age or sex
    stop = INDEX - timedelta(days=600)
    preg = ((stop - timedelta(days=280), stop),)
    s = st(make(age=60, sex="M", ascvd=("MI",), pregnancy_past=preg))
    assert s.candidate == "S1" and s.blocking() == ("pregnancy_5y",)
    s = st(make(age=50, diabetes=True, pregnancy_past=preg))
    assert s.candidate == "S3" and s.blocking() == ("pregnancy_5y",)
    # the oracle still counts a pregnancy reason only within 365 days
    f = make(**s2, structured=(("pregnancy", 418),))
    assert oracle.label(f).status == oracle.GAP
    f = make(**s2, structured=(("pregnancy", 300),))
    assert oracle.label(f).status == oracle.DOCUMENTED_REASON


def test_nonhdl_ge220_with_ldl_lt190_not_sampled_in_s4_s5():
    """Footnote * of the sec. 4.2.4.3 recommendations reads severe hypercholesterolemia as
    'LDL-C >190 mg/dL, non-HDL-C >220 mg/dL, and/or apoB >140 mg/dL'; row 2 reads LDL-C only.
    Latest LDL-C < 190 with TC - HDL-C >= 220 is not sampled in S4-S5 (S1 and S3 are GAP or
    ON_THERAPY either way)."""
    s4 = dict(age=55, sex="M", hdl=50.0, ldl=120.0, sbp=170.0)
    s = st(make(**s4, tc=270.0))  # non-HDL-C 220
    assert s.candidate == "S4" and s.blocking() == ("nonhdl_ge220_ldl_lt190",)
    assert st(make(**s4, tc=269.9)).stratum == "S4"
    s5 = dict(age=62, sex="F", hdl=70.0, ldl=100.0, sbp=105.0)
    s = st(make(**s5, tc=290.0))
    assert (s.indication_row, s.candidate) == ("11b", "S5")
    assert s.blocking() == ("nonhdl_ge220_ldl_lt190",)
    assert st(make(**s5, tc=289.9)).stratum == "S5"
    for kw, stratum in ((dict(ascvd=("MI",)), "S1"), (dict(diabetes=True), "S3")):
        s = st(make(age=50, tc=280.0, hdl=50.0, ldl=120.0, **kw))
        assert "nonhdl_ge220_ldl_lt190" in s.exclusions and s.stratum == stratum
    # LDL-C >= 190 is row 2 (S2), not this rule
    s = st(make(ldl=190.0, tc=290.0, ldl_earlier_ge190=(195.0,)))
    assert s.stratum == "S2" and "nonhdl_ge220_ldl_lt190" not in s.exclusions


def test_egfr_lt60_without_ckd_code_not_sampled_in_s4_s5():
    """CKD stage 3 is defined by eGFR and sec. 4.2.8.8 rec 1 is COR 1, while the oracle reads
    the CKD stage from codes: a latest eGFR < 60 with no active CKD stage 3-4 code is not
    sampled in S4-S5."""
    s4 = dict(age=55, sex="M", tc=200.0, hdl=50.0, ldl=120.0, sbp=170.0)
    for kw in (dict(egfr=55.0), dict(egfr=55.0, ckd=2)):
        s = st(make(**s4, **kw))
        assert s.candidate == "S4" and s.blocking() == ("egfr_lt60_no_ckd_code",), kw
    assert st(make(**s4, egfr=60.0)).stratum == "S4"
    s = st(make(**s4, egfr=55.0, ckd=3))  # coded: row 3b, not this rule
    assert s.indication_row == "3b" and "egfr_lt60_no_ckd_code" not in s.exclusions
    s = st(make(**LOW, egfr=50.0))
    assert s.candidate == "S5" and s.blocking() == ("egfr_lt60_no_ckd_code",)
    s = st(make(age=50, ascvd=("MI",), egfr=50.0))
    assert "egfr_lt60_no_ckd_code" in s.exclusions and s.stratum == "S1"
