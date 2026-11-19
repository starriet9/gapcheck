"""Guideline oracle: the decision table of SPEC 3.1-3.4 in code.

Source: 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on
the Management of Dyslipidemia. J Am Coll Cardiol 2026;87(19):2624-2757,
doi:10.1016/j.jacc.2025.11.016. Page numbers below are the journal pages
printed in that article (`page`) and the page of the article PDF (`pdf_page`,
1 = journal page 2624). Quotes are verbatim; "≥" is printed as a symbol that
some text extractors render as "$".

Main entry point: `label(facts, reasons, variant)`.

Rules (SPEC 3.1-3.4):
- First matching row wins. Row 0 (active statin) first, then the cohort
  exclusions (HIV; ESRD, dialysis, transplant), then rows 1-11.
- Thresholds apply to unrounded PREVENT risk.
- Closed-world inputs (absence = "no"): ASCVD, diabetes, CKD stage, row-5
  features, statin and BP treatment, diabetes duration. Open-world inputs
  (absence = unknown): lipid panel, SBP, smoking status, eGFR.
- An open-world input X is deciding if the label changes when X is set to
  every admissible value (grid in SPEC 3.4) while the other inputs keep their
  recorded values. Inputs that are absent or out of range have no usable
  recorded value; they are varied jointly with X (so that, e.g., an absent
  eGFR and an absent smoking status are both named). CANNOT_DETERMINE names
  every absent or out-of-range input that is deciding.
- The lipid panel is one input (TC, HDL-C, LDL-C from one date). LDL-C has no
  range limit; only TC and HDL-C have PREVENT ranges. If TC or HDL-C is out
  of range, only TC/HDL-C are treated as unknown (LDL-C stays as recorded).
- A documented reason (sec. 3.3) dated 0-365 days before index changes GAP
  to DOCUMENTED_REASON, and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import cache

import numpy as np

from . import prevent

# ---------------------------------------------------------------- statuses
ON_THERAPY = "ON_THERAPY"
GAP = "GAP"
DOCUMENTED_REASON = "DOCUMENTED_REASON"
DISCUSS = "DISCUSS"
NOT_INDICATED = "NOT_INDICATED"
CANNOT_DETERMINE = "CANNOT_DETERMINE"
STATUSES = (ON_THERAPY, GAP, DOCUMENTED_REASON, DISCUSS, NOT_INDICATED, CANNOT_DETERMINE)
# Internal only: HIV or ESRD/dialysis/transplant. These patients are not in
# the cohort, so no system ever sees this value.
EXCLUDED = "EXCLUDED"

VARIANTS = ("main", "S-A", "S-C")
OPEN_INPUTS = ("lipid_panel", "sbp", "smoking", "egfr")

# Risk cut points (fractions, unrounded risk).
RISK_HIGH, RISK_INTERMEDIATE, RISK_BORDERLINE = 0.10, 0.05, 0.03
RISK_30Y_ELEVATED = 0.10
REASON_WINDOW_DAYS = 365


# ---------------------------------------------------------------- table
@dataclass(frozen=True)
class Source:
    section: str          # guideline section, e.g. "4.2.3.7"
    rec: str              # recommendation number, or "supportive text 7", "Figure 6", ...
    cor: str | None       # class of recommendation ("1", "2a", "2b", "3: No Benefit")
    loe: str | None       # level of evidence
    quote: str
    page: int             # journal page
    pdf_page: int


@dataclass(frozen=True)
class Row:
    id: str
    group: str
    rule: str
    label: str            # main-analysis label
    sources: tuple[Source, ...]
    encoding: str = ""    # encoding choices (SPEC 3.2) and what is not encoded


def _src(section, rec, cor, loe, page, quote):
    return Source(section, rec, cor, loe, quote, page, page - 2623)


ROWS: tuple[Row, ...] = (
    Row("0", "Any", "Active statin at index (start <= index and no stop or stop > index).",
        ON_THERAPY, (),
        "Not a guideline row. Statins matched by ingredient, including ezetimibe/simvastatin."),
    Row("X", "Excluded from the cohort",
        "HIV infection, or ESRD, maintenance dialysis or kidney transplant (any code on or "
        "before index). Checked before row 1.",
        EXCLUDED,
        (_src("4.2.8.9", "1", "1", "B-R", 2703,
              "In people living with HIV aged 40 to 75 years on stable combination "
              "antiretroviral therapy, statin therapy is recommended to reduce risk of a first "
              "ASCVD event and reduce the rate of coronary atherosclerosis progression."),
         _src("4.2.8.8", "3", "2b", "C-LD", 2702,
              "In adults with CKD who require maintenance hemodialysis, it may be reasonable "
              "to continue statin therapy to reduce the risk of ASCVD events.")),
        "Encoded but excluded from the cohort: HIV needs antiretroviral data we lack; the "
        "dialysis recommendation covers only continuation, not initiation."),
    Row("1", "Clinical ASCVD",
        "Any qualifying ASCVD code on or before index (stop dates ignored). No age limit.",
        GAP,
        (_src("4.2.6", "1", "1", "A", 2685,
              "In adults with clinical ASCVD who are not at very high risk (Figure 10), "
              "high-intensity statin therapy should be initiated to achieve a ≥50% reduction "
              "in LDL-C and a goal of LDL-C <70 mg/dL (1.8 mmol/L) and non–HDL-C <100 mg/dL to "
              "reduce the risk of recurrent ASCVD events."),
         _src("4.2.6", "4", "1", "A", 2685,
              "In adults with clinical ASCVD* who are at very high risk (Figure 10 and Figure "
              "11), high-intensity statin therapy should be initiated to achieve a ≥50% "
              "reduction in LDL-C and a goal LDL-C <55 mg/dL (1.4 mmol/L) and non–HDL-C "
              "<85 mg/dL (2.2 mmol/L) and to reduce the risk of ASCVD events."),
         _src("4.2.4.3", "footnote †", None, None, 2675,
              "†Clinical ASCVD includes ACS, a history of MI, stable or unstable angina, "
              "coronary or other arterial revascularization, stroke, TIA, lower-extremity PAD, "
              "or other atherosclerotic forms of PAD, including aortic aneurysm.")),
        "Ischemic heart disease code 414545008 counts as clinical ASCVD (encoding choice). "
        "PAD, TIA, aortic aneurysm and stable angina are not generated by the data."),
    Row("2", "Severe hypercholesterolemia", "Latest LDL-C >= 190 mg/dL. No age limit.", GAP,
        (_src("4.2.4.3", "2", "1", "B-R", 2674,
              "In adults with severe hypercholesterolemia with an LDL-C ≥190 mg/dL "
              "(4.9 mmol/L),* treatment with maximally tolerated statin therapy is recommended "
              "to lower LDL-C and reduce ASCVD risk."),
         _src("4.2.4.3", "1", "1", "B-NR", 2674,
              "In adults with severe hypercholesterolemia with an LDL-C ≥190 mg/dL "
              "(4.9 mmol/L),* secondary causes of dyslipidemia should be excluded and "
              "addressed to reduce LDL-C (Table 15).")),
        "Rec 1 (exclude secondary causes first) is not encoded. The table footnote reads "
        "'LDL-C >190 mg/dL'; the recommendations read '≥190'; the oracle uses >= 190."),
    Row("3", "Diabetes, 40-75", "Active diabetes and age 40-75. No LDL-C floor.", GAP,
        (_src("4.2.5", "1", "1", "A", 2681,
              "In adults 40 to 75 years of age with diabetes and without clinical ASCVD, "
              "moderate-intensity statin therapy is indicated to achieve a ≥30% to 49% "
              "reduction in LDL-C and a goal of LDL-C <100 mg/dL (2.6 mmol/L) and non–HDL-C "
              "<130 mg/dL (3.4 mmol/L) to reduce ASCVD risk."),),
        "Diabetes excludes prediabetes."),
    Row("3b", "CKD, 40-75",
        "Active CKD stage 3 or 4 diagnosis, age 40-75, latest LDL-C 70-189 mg/dL.", GAP,
        (_src("4.2.8.8", "1", "1", "B-R", 2702,
              "In adults 40 to 75 years of age with CKD stage 3 or higher and an LDL-C of 70 "
              "to 189 mg/dL (1.8-4.9 mmol/L), moderate-intensity statin therapy or "
              "moderate-intensity statin combined with ezetimibe is recommended to reduce "
              "ASCVD risk."),),
        "Defined by diagnosis code (highest active stage), not by a single eGFR value. "
        "Stage 5 is not generated; ESRD is excluded (row X)."),
    Row("4", "Diabetes, >75", "Active diabetes and age 76-79.", DISCUSS,
        (_src("4.2.5", "6", "2b", "C-LD", 2681,
              "In adults >75 years of age with diabetes and an estimated life expectancy of at "
              "least 2.5 years, it may be reasonable to initiate moderate-intensity statin "
              "therapy after a clinician–patient discussion of potential benefits and risks "
              "to reduce ASCVD risk."),),
        "Life-expectancy condition not encoded."),
    Row("5", "Diabetes, 30-39",
        "Active diabetes and age 30-39. GAP via rows 8-9 if LDL-C 70-189 and 10-year risk "
        ">= 5%. Otherwise DISCUSS if any of: type 2 diabetes >= 10 years, albuminuria or "
        "proteinuria, CKD stage >= 3, retinopathy, neuropathy. Otherwise rows 7, 10, 11.",
        DISCUSS,
        (_src("4.2.5", "7", "2b", "C-LD", 2681,
              "In adults 20 to 39 years of age with diabetes of long duration (≥10 years of "
              "type 2 diabetes, ≥20 years of type 1 diabetes), albuminuria (≥30 μg of "
              "albumin/mg creatinine), estimated glomerular filtration rate (eGFR) "
              "<60 mL/min/1.73 m2, retinopathy, neuropathy, or ankle-brachial index <0.9, it "
              "may be reasonable to initiate moderate-intensity statin therapy to reduce "
              "ASCVD risk."),
         _src("4.2.5", "supportive text 7", None, None, 2684,
              "In those individuals >30 years of age, calculation of 10- and 30-year risk by "
              "PREVENT-ASCVD equations should be used to guide treatment decisions according "
              "to the primary prevention algorithm (Figure 6, Section 4.2.3.7, “Primary "
              "Prevention in Adults 30 to 79 Years of Age With LDL-C Levels 70 to 189 mg/dL "
              "[1.8-4.9 mmol/L]”).")),
        "eGFR <60 is encoded as a CKD stage >= 3 diagnosis code. ABI and type 1 diabetes do "
        "not occur in the data. Age 30 is treated as covered by supportive text 7 ('>30')."),
    Row("6", "Older adults",
        "Age 76-79, no ASCVD, no diabetes, LDL-C < 190 mg/dL (primary prevention).", DISCUSS,
        (_src("4.2.8.3", "2", "2b", "B-NR", 2697,
              "In adults aged >75 years with an estimated life expectancy of at least 2.5 "
              "years, it may be reasonable to initiate moderate-intensity statin therapy "
              "after a clinician–patient discussion of potential benefits and risks to reduce "
              "ASCVD risk."),),
        "Encoding choice (SPEC 3.2): sec. 4.2.3.7 covers ages 30-79 but this COR 2b "
        "recommendation covers all adults >75; DISCUSS is the conservative reading. "
        "Life-expectancy condition not encoded."),
    Row("7", "LDL-C < 70", "Latest LDL-C < 70 mg/dL.", NOT_INDICATED,
        (_src("4.2.3.7", "12", "3: No Benefit", "B-NR", 2668,
              "In adults with a baseline untreated LDL-C <70 mg/dL (1.8 mmol/L) and "
              "non–HDL-C <100 mg/dL (2.6 mmol/L) and without additional ASCVD risk factors, "
              "initiation of LLT for primary prevention is unlikely to reduce ASCVD risk."),),
        "No recommendation to start a statin applies: sec. 4.2.3.7 and Figure 6 cover LDL-C "
        "70-189. Rec 12 covers only the subset with non-HDL-C <100 and no other risk factors."),
    Row("8", "PREVENT >= 10%", "10-year PREVENT ASCVD risk >= 10%.", GAP,
        (_src("4.2.3.7", "7", "1", "A", 2667,
              "In adults at high (≥10%) 10-year estimated risk for ASCVD in whom LLT is "
              "initiated for primary prevention, high-intensity statin therapy is recommended "
              "to achieve an LDL-C reduction of ≥50% to reduce the risk of ASCVD."),
         _src("4.2.3.7", "supportive text 7", None, None, 2670,
              "High-intensity LLT is recommended as first-line LDL-C–lowering therapy for "
              "primary prevention of ASCVD in individuals at high risk (≥10%) for ASCVD, with "
              "an expected LDL-C lowering of ≥50%."),
         _src("4.2.3.7", "Figure 6", "1", None, 2669,
              "High 10-y Risk (≥10%) → Start high-intensity statin")),
        "Encoding choice (SPEC 3.2): rec 7 is worded conditionally ('in whom LLT is "
        "initiated'); GAP because rec 5 (5 to <10%) is unconditional. S-A and S-C relabel "
        "this row DISCUSS."),
    Row("9", "PREVENT 5 to < 10%", "10-year risk 5% to < 10%.", GAP,
        (_src("4.2.3.7", "5", "1", "A", 2667,
              "In adults at intermediate (5% to <10%) 10-year estimated risk for ASCVD, at "
              "least a moderate-intensity statin is recommended to achieve ≥30% to 49% LDL-C "
              "reduction and to reduce ASCVD risk; for those in the higher end of this risk "
              "range, a high-intensity statin is beneficial to further reduce LDL-C by ≥50% "
              "and reduce ASCVD risk."),),
        "S-C relabels this row DISCUSS."),
    Row("10", "PREVENT 3 to < 5%", "10-year risk 3% to < 5%.", DISCUSS,
        (_src("4.2.3.7", "4", "2a", "A", 2667,
              "In adults at borderline (3% to <5%) 10-year estimated risk for ASCVD in whom a "
              "decision is made to initiate statin therapy for primary prevention, a "
              "moderate-intensity statin is reasonable to achieve ≥30% to 49% LDL-C reduction "
              "and to reduce ASCVD risk."),),
        "Conditional on a decision to treat."),
    Row("11a", "PREVENT < 3%, 30-59, elevated",
        "Age 30-59, 10-year risk < 3%, and LDL-C 160-189 or 30-year risk >= 10%.", DISCUSS,
        (_src("4.2.3.7", "3", "2a", "C-LD", 2667,
              "In adults aged 30 to 59 years, at low (<3%) 10-year estimated risk for ASCVD but "
              "with an LDL-C of 160 to 189 mg/dL (4.1-4.9 mmol/L) or a 30-year ASCVD risk ≥10% "
              "(for those aged 30-59 years), a moderate-intensity statin is reasonable to "
              "reduce cumulative exposure to atherogenic lipoproteins."),),
        "30-year risk is computed only at ages 30-59."),
    Row("11b", "PREVENT < 3%, otherwise", "All other patients at 10-year risk < 3%.",
        NOT_INDICATED,
        (_src("4.2.3.7", "2", "1", "A", 2667,
              "In adults aged 30 to 59 years, at low (<3%) 10-year estimated risk for ASCVD who "
              "have an LDL-C <160 mg/dL (4.1 mmol/L) and a 30-year risk estimate of <10%, "
              "counseling on health behaviors is recommended to reduce LDL-C and risk for "
              "ASCVD."),),
        "Ages 60-79: no recommendation to start a statin applies (recs 2-3 are limited to "
        "ages 30-59); NOT_INDICATED rests on guideline silence there."),
)
ROW_BY_ID = {r.id: r for r in ROWS}

# Supporting statements quoted for the reason taxonomy and for context.
REASON_SOURCES: dict[str, tuple[Source, ...]] = {
    "statin_intolerance": (
        _src("4.2.5", "2", "1", "B-R", 2681,
             "In adults with diabetes who have statin-attributed side effects, initiation of "
             "ezetimibe and/or bempedoic acid or a PCSK9 mAb is recommended to lower LDL-C and "
             "reduce ASCVD risk."),
        _src("4.2.11", "1", "1", "C-LD", 2714,
             "In adults with statin-attributed muscle symptoms, assessment should include "
             "evaluation for secondary causes (Table 24), and in those with severe myalgias or "
             "weakness, objective clinical measures of muscle strength and measurement of CK "
             "are recommended to assess severity of the condition."),
    ),
    "patient_declined": (),
    "pregnancy": (
        _src("4.2.8.4", "1", "1", "C-LD", 2698,
             "Persons of childbearing age with hypercholesterolemia who are not at high risk "
             "for ASCVD and plan to become pregnant should stop statin therapy 1 to 2 months "
             "before attempting to become pregnant or as soon as pregnancy is discovered to "
             "avoid uncertain risks to the fetus."),
    ),
    "liver_failure": (
        _src("5.1", "3", "2a", "B-R", 2721,
             "In adults at elevated ASCVD risk with chronic, stable liver disease (including "
             "metabolic dysfunction-associated steatotic liver disease), it is reasonable to "
             "treat with statin therapy to reduce ASCVD risk."),
    ),
    "palliative": (
        _src("4.2.3.7", "11", "2b", "B-R", 2668,
             "In individuals with a life expectancy of <1 year, it may be reasonable to "
             "discontinue LLT that was prescribed for primary prevention purposes to avoid "
             "unnecessary medication use or adverse medication effects."),
        _src("4.2.8.3", "3", "2b", "B-R", 2697,
             "In patients with a life expectancy of <1 year, it may be reasonable to "
             "discontinue LDL-lowering therapy to avoid unnecessary medication use or adverse "
             "medication effects."),
    ),
}
CONTEXT_SOURCES: tuple[Source, ...] = (
    _src("4.2.3.7", "1", "1", "A", 2667,
         "In adults being assessed for primary prevention of ASCVD, health behavior "
         "recommendations should be provided in addition to a benefit-risk discussion for "
         "consideration of LLT (Section 4.1.2, “Dietary Approaches in Dyslipidemia”)."),
    _src("5.1", "1", "1", "A", 2720,
         "In adults with dyslipidemia, an individualized clinician–patient discussion prior "
         "to initiating lipid-lowering medication is recommended to review the benefits and "
         "risks of pharmacotherapy to promote patient engagement and medication adherence."),
    _src("Top take-home messages", "3", None, None, 2632,
         "Low-density lipoprotein (LDL)–lowering therapy can be considered in adults for "
         "primary prevention of ASCVD with a 10-year PREVENT-ASCVD risk estimate of 3% to <5% "
         "(borderline risk) and should be considered for those at 5% to <10% (intermediate "
         "risk) 10-year risk after a clinician–patient discussion."),
)

# Row labels per analysis variant (SPEC 3.2).
_ROW_IDS = ("X", "0", "1", "2", "3", "3b", "4", "5", "6", "7", "8", "9", "10", "11a", "11b")
_RI = {r: i for i, r in enumerate(_ROW_IDS)}
_ST = {s: i for i, s in enumerate(STATUSES + (EXCLUDED,))}
_STATUS_NAMES = STATUSES + (EXCLUDED,)


def row_status(row: str, variant: str = "main") -> str:
    """Label of a row under an analysis variant (before reasons)."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
    s = ROW_BY_ID[row].label
    if variant == "S-A" and row == "8":
        return DISCUSS
    if variant == "S-C" and row in ("8", "9"):
        return DISCUSS
    return s


@cache
def _status_lut(variant: str, has_reason: bool) -> np.ndarray:
    lut = np.empty(len(_ROW_IDS), dtype=np.int8)
    for r, i in _RI.items():
        s = row_status(r, variant)
        if has_reason and s == GAP:
            s = DOCUMENTED_REASON
        lut[i] = _ST[s]
    return lut


# ---------------------------------------------------------------- reasons
REASON_CATEGORIES = tuple(REASON_SOURCES)
PREGNANCY_REASON_AGES = (30, 49)


@dataclass(frozen=True)
class Reason:
    """A documented reason for not prescribing (SPEC 3.3)."""
    category: str      # one of REASON_CATEGORIES
    date: date
    source: str        # record row id or note id

    def __post_init__(self):
        if self.category not in REASON_CATEGORIES:
            raise ValueError(f"unknown reason category {self.category!r}")


def reasons_from_facts(f) -> tuple[Reason, ...]:
    """Structured reasons (SPEC 3.3): allergy or intolerance rows that mention a
    statin; pregnancy conditions; hospice or palliative-care encounters,
    procedures or conditions (`facts.reason_entries`, dated 0-365 days
    before index; the window is applied again in `applicable_reasons`)."""
    out = [Reason("statin_intolerance", a.start, a.row_id)
           for a in f.allergies if a.statin_related]
    out += [Reason(e.category, e.date, e.row_id) for e in getattr(f, "reason_entries", ())]
    return tuple(out)


def applicable_reasons(reasons, index_date: date, sex: str, age: int) -> tuple[Reason, ...]:
    """Reasons dated 0-365 days before index; pregnancy only for women aged 30-49."""
    out = []
    for r in reasons:
        days = (index_date - r.date).days
        if not 0 <= days <= REASON_WINDOW_DAYS:
            continue
        if r.category == "pregnancy" and not (
                sex == "F" and PREGNANCY_REASON_AGES[0] <= age <= PREGNANCY_REASON_AGES[1]):
            continue
        out.append(r)
    return tuple(sorted(out, key=lambda r: (r.date, r.source), reverse=True))


# ---------------------------------------------------------------- inputs
@dataclass(frozen=True)
class OracleInputs:
    """Plain values the decision table reads (None = open-world input absent)."""
    age: int
    sex: str                      # "M" or "F"
    excluded: str | None          # "HIV", "ESRD/dialysis/transplant" or None
    on_statin: bool
    ascvd: bool
    diabetes: bool
    ckd_stage: int | None
    row5_feature: bool
    bp_treated: bool
    tc: float | None = None
    hdl: float | None = None
    ldl: float | None = None
    sbp: float | None = None
    smoker: bool | None = None
    smoking_recognized: bool = True
    egfr: float | None = None

    @property
    def lipid_present(self) -> bool:
        return None not in (self.tc, self.hdl, self.ldl)


def inputs_from_facts(f) -> OracleInputs:
    lp = f.lipid_panel
    excl = "HIV" if f.hiv else ("ESRD/dialysis/transplant" if f.esrd else None)
    return OracleInputs(
        age=f.age, sex=f.sex, excluded=excl, on_statin=f.on_statin, ascvd=f.has_ascvd,
        diabetes=f.has_diabetes, ckd_stage=f.ckd_stage, row5_feature=any(f.row5_features.values()),
        bp_treated=f.on_bp_drug,
        tc=None if lp is None else float(lp.tc.value),
        hdl=None if lp is None else float(lp.hdl.value),
        ldl=None if lp is None else float(lp.ldl.value),
        sbp=None if f.sbp is None else float(f.sbp.value),
        smoker=None if f.smoking is None else f.smoking.current,
        smoking_recognized=True if f.smoking is None else f.smoking.recognized,
        egfr=None if f.egfr is None else float(f.egfr.value),
    )


def _in(x, lo, hi) -> bool:
    return x is not None and lo <= x <= hi


def tc_hdl_valid(x: OracleInputs) -> bool:
    R = prevent.RANGES
    return _in(x.tc, *R["tc"]) and _in(x.hdl, *R["hdl"])


def usable(x: OracleInputs) -> dict[str, bool]:
    """Open-world inputs with a usable recorded value (present and in range)."""
    R = prevent.RANGES
    return {
        "lipid_panel": x.lipid_present and tc_hdl_valid(x),
        "sbp": _in(x.sbp, *R["sbp"]),
        "smoking": x.smoker is not None and x.smoking_recognized,
        "egfr": _in(x.egfr, *R["egfr"]),
    }


def absent(x: OracleInputs) -> tuple[str, ...]:
    pres = {"lipid_panel": x.lipid_present, "sbp": x.sbp is not None,
            "smoking": x.smoker is not None, "egfr": x.egfr is not None}
    return tuple(k for k in OPEN_INPUTS if not pres[k])


# ---------------------------------------------------------------- grids (SPEC 3.4)
SBP_GRID = np.arange(90, 201, 1, dtype=float)
EGFR_GRID = np.arange(15, 151, 1, dtype=float)
SMOKING_GRID = np.array([0.0, 1.0])
TC_GRID = np.arange(130, 321, 5, dtype=float)
HDL_GRID = np.arange(20, 101, 5, dtype=float)
LDL_EXTRA = (69.0, 70.0, 159.0, 160.0, 189.0, 190.0)
# The label depends on LDL-C only through these bands (rows 2, 3b, 5, 6, 7, 11a).
LDL_BANDS = ((-np.inf, 70.0), (70.0, 160.0), (160.0, 190.0), (190.0, np.inf))


@cache
def lipid_grid() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Admissible lipid panels, reduced to one LDL-C value per reachable band.

    LDL-C values: 30 to TC - HDL-C by 5, plus 69, 70, 159, 160, 189, 190 when
    they do not exceed TC - HDL-C. The label depends on LDL-C only through
    LDL_BANDS, so one value per (TC, HDL-C, band) gives the same label set.
    """
    tcs, hdls, ldls = [], [], []
    for tc in TC_GRID:
        for hdl in HDL_GRID:
            top = tc - hdl
            vals = sorted(set(np.arange(30.0, top + 0.5, 5.0)) | {v for v in LDL_EXTRA if v <= top})
            for lo, hi in LDL_BANDS:
                inband = [v for v in vals if lo <= v < hi]
                if inband:
                    tcs.append(tc)
                    hdls.append(hdl)
                    ldls.append(inband[0])
    return np.array(tcs), np.array(hdls), np.array(ldls)


@cache
def tc_hdl_grid() -> tuple[np.ndarray, np.ndarray]:
    t, h = np.meshgrid(TC_GRID, HDL_GRID, indexing="ij")
    return t.ravel(), h.ravel()


GRID_CAP = 3_000_000  # elements per joint grid; above it SBP/eGFR use a coarser step


# ---------------------------------------------------------------- decision table
def _risk(x: OracleInputs, tc, hdl, sbp, smk, egfr, diabetes: bool, horizon: int):
    sex = "male" if x.sex == "M" else "female"
    return prevent.ascvd_risk_array(sex, x.age, tc, hdl, sbp, int(x.bp_treated),
                                    int(x.on_statin), int(diabetes), smk, egfr, horizon)


def _row_codes(x: OracleInputs, tc, hdl, ldl, sbp, smk, egfr, ignore_row0: bool,
               trace: list | None = None, risks: dict | None = None):
    """Matched row (index into _ROW_IDS) for every element of the broadcast inputs.

    Every open-world value passed here must be admissible; the caller varies
    unusable inputs over their grids. `trace` collects the rows whose
    condition was evaluated, in order.
    """
    def tr(r):
        if trace is not None and (not trace or trace[-1] != r):
            trace.append(r)

    tr("0")
    if not ignore_row0 and x.on_statin:
        return np.int8(_RI["0"])
    tr("X")
    if x.excluded:
        return np.int8(_RI["X"])
    tr("1")
    if x.ascvd:
        return np.int8(_RI["1"])

    arrs = [np.asarray(a, dtype=float) for a in (tc, hdl, ldl, sbp, smk, egfr)]
    shape = np.broadcast_shapes(*(a.shape for a in arrs))
    tc, hdl, ldl, sbp, smk, egfr = (np.broadcast_to(a, shape) for a in arrs)
    out = np.full(shape, -1, dtype=np.int8)
    todo = np.ones(shape, dtype=bool)

    def put(mask, row):
        """Assign `row` where mask holds and no row matched yet; True when all done."""
        tr(row)
        m = todo & mask
        out[m] = _RI[row]
        todo[m] = False
        return not todo.any()

    memo: dict = {}

    def risk(horizon):
        if horizon not in memo:
            memo[horizon] = _risk(x, tc, hdl, sbp, smk, egfr, x.diabetes, horizon)
            if risks is not None:
                risks[f"risk{horizon}"] = memo[horizon]
        return memo[horizon]

    a = x.age
    mid = (ldl >= 70) & (ldl < 190)
    if put(ldl >= 190, "2"):
        return out
    tr("3")
    if x.diabetes and 40 <= a <= 75:
        put(np.True_, "3")
        return out
    tr("3b")
    if x.ckd_stage in (3, 4) and 40 <= a <= 75:
        if put(mid, "3b"):
            return out
    tr("4")
    if x.diabetes and a >= 76:
        put(np.True_, "4")
        return out
    tr("5")
    if x.diabetes and a <= 39:
        # Row 5: GAP through rows 8-9 if LDL-C 70-189 and 10-year risk >= 5%.
        if (mid & todo).any():
            r = risk(10)
            if put(mid & (r >= RISK_HIGH), "8") or put(mid & (r >= RISK_INTERMEDIATE), "9"):
                return out
        if x.row5_feature:
            put(np.True_, "5")
            return out
        if put(ldl < 70, "7"):  # then rows 7, 10, 11
            return out
    else:
        tr("6")
        if a >= 76:
            put(np.True_, "6")  # LDL-C < 190 here: row 2 came first
            return out
        if put(ldl < 70, "7"):
            return out
        r = risk(10)
        if put(r >= RISK_HIGH, "8") or put(r >= RISK_INTERMEDIATE, "9"):
            return out
    if put(risk(10) >= RISK_BORDERLINE, "10"):
        return out
    if a <= 59:
        if put((ldl >= 160) | (risk(30) >= RISK_30Y_ELEVATED), "11a"):
            return out
    put(np.True_, "11b")
    return out


def _axis_values(x: OracleInputs, name: str, step: int = 1) -> dict[str, np.ndarray]:
    if name == "lipid":
        tc, hdl, ldl = lipid_grid()
        return {"tc": tc, "hdl": hdl, "ldl": ldl}
    if name == "tc_hdl":
        tc, hdl = tc_hdl_grid()
        return {"tc": tc, "hdl": hdl}
    if name == "sbp":
        g = SBP_GRID if step == 1 else np.unique(np.r_[SBP_GRID[::step], 110.0, 200.0])
        return {"sbp": g}
    if name == "egfr":
        g = EGFR_GRID if step == 1 else np.unique(np.r_[EGFR_GRID[::step], 59.0, 60.0, 150.0])
        return {"egfr": g}
    if name == "smoking":
        return {"smk": SMOKING_GRID}
    raise KeyError(name)


def _recorded(x: OracleInputs) -> dict[str, float]:
    nan = np.nan
    return {"tc": nan if x.tc is None else x.tc, "hdl": nan if x.hdl is None else x.hdl,
            "ldl": nan if x.ldl is None else x.ldl, "sbp": nan if x.sbp is None else x.sbp,
            "smk": nan if x.smoker is None else float(x.smoker),
            "egfr": nan if x.egfr is None else x.egfr}


def _tensor(x: OracleInputs, axes: list[str], ignore_row0: bool):
    """Row codes over the joint grid of `axes` (one array axis each).

    Returns (rows, approx): approx is True if SBP/eGFR grids were coarsened.
    """
    def build(step):
        vals = _recorded(x)
        lens = []
        for k, name in enumerate(axes):
            av = _axis_values(x, name, step)
            n = len(next(iter(av.values())))
            lens.append(n)
            shp = [1] * len(axes)
            shp[k] = n
            for key, v in av.items():
                vals[key] = v.reshape(shp)
        return vals, lens

    step, approx = 1, False
    vals, lens = build(step)
    while int(np.prod(lens)) > GRID_CAP and step < 20:
        step, approx = step * 5, True
        vals, lens = build(step)
    rows = _row_codes(x, vals["tc"], vals["hdl"], vals["ldl"], vals["sbp"], vals["smk"],
                      vals["egfr"], ignore_row0)
    rows = np.broadcast_to(rows, tuple(lens)) if axes else np.asarray(rows)
    return rows, approx


def _varies(t: np.ndarray, axis: int) -> bool:
    if t.ndim == 0 or t.shape[axis] < 2:
        return False
    return bool(np.any(t != np.take(t, [0], axis=axis)))


# ---------------------------------------------------------------- result
@dataclass(frozen=True)
class OracleResult:
    status: str
    row: str | None                 # matched row; None if CANNOT_DETERMINE (or not unique)
    variant: str
    missing: tuple[str, ...]        # absent open-world inputs that are deciding
    out_of_range: tuple[str, ...]   # present but invalid open-world inputs that are deciding
    deciding: tuple[str, ...]       # every deciding open-world input
    absent: tuple[str, ...]         # every absent open-world input
    risk10: float | None
    risk30: float | None
    path: tuple[str, ...]           # rows tried, in order, up to the match
    inputs_used: dict = field(default_factory=dict)  # input -> source row ids
    reason: Reason | None = None    # reason that turned GAP into DOCUMENTED_REASON
    reasons_in_window: tuple[Reason, ...] = ()
    indication_row: str | None = None  # first matching row among X, 1-11 (row 0 ignored)
    approx: bool = False            # a coarse grid was used (very sparse records only)
    row_set: tuple[str, ...] = ()   # rows matched over the grid of unusable inputs ((row,) if none)


# Inputs each row reads (for `inputs_used`; "prevent" expands below).
PREVENT_INPUTS = ("age", "sex", "lipid_panel", "sbp", "bp_drugs", "statins", "diabetes",
                  "smoking", "egfr")
ROW_INPUTS = {
    "0": ("statins",), "X": ("hiv", "esrd"), "1": ("ascvd",), "2": ("lipid_panel",),
    "3": ("diabetes", "age"), "3b": ("ckd", "age", "lipid_panel"), "4": ("diabetes", "age"),
    "5": ("diabetes", "age", "lipid_panel", "row5_features", "prevent"),
    "6": ("age", "lipid_panel"), "7": ("lipid_panel",), "8": ("prevent",),
    "9": ("prevent",), "10": ("prevent",), "11a": ("age", "lipid_panel", "prevent"),
    "11b": ("age", "lipid_panel", "prevent"),
}


def _sources(f, names) -> dict[str, tuple[str, ...]]:
    if f is None:
        return {}
    lp = f.lipid_panel
    src = {
        "age": (), "sex": (),
        "statins": tuple(r.row_id for r in f.statins),
        "bp_drugs": tuple(r.row_id for r in f.bp_drugs),
        "hiv": tuple(r.row_id for r in f.hiv), "esrd": tuple(r.row_id for r in f.esrd),
        "ascvd": tuple(r.row_id for r in f.ascvd),
        "diabetes": tuple(r.row_id for r in f.diabetes),
        "ckd": tuple(r.row_id for r in f.ckd),
        "row5_features": tuple(r.row_id for r in f.retinopathy + f.neuropathy + f.albuminuria),
        "lipid_panel": () if lp is None else lp.row_ids,
        "sbp": () if f.sbp is None else (f.sbp.row_id,),
        "smoking": () if f.smoking is None else (f.smoking.obs.row_id,),
        "egfr": () if f.egfr is None else (f.egfr.row_id,),
    }
    out = {}
    for n in names:
        for k in (PREVENT_INPUTS if n == "prevent" else (n,)):
            out[k] = src[k]
    return out


def evaluate(x: OracleInputs, variant: str = "main", has_reason: bool = False,
             ignore_row0: bool = False) -> dict:
    """Core of the oracle on plain inputs. Returns a dict of OracleResult fields."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
    if not 30 <= x.age <= 79:
        raise ValueError(f"age {x.age} outside 30-79 (out of scope)")
    lut = _status_lut(variant, has_reason)
    ok = usable(x)
    gone = absent(x)
    res = {"variant": variant, "absent": gone, "approx": False}

    # Closed-world rows decide alone: no open-world input can be deciding.
    trace: list = []
    rec = _recorded(x)
    early = _row_codes(x, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, ignore_row0, trace) \
        if (x.on_statin and not ignore_row0) or x.excluded or x.ascvd else None
    if early is not None:
        row = _ROW_IDS[int(early)]
        res.update(status=_STATUS_NAMES[lut[int(early)]], row=row, row_set=(row,), missing=(),
                   out_of_range=(), deciding=(), risk10=None, risk30=None, path=tuple(trace))
        return res

    # Unknown inputs (absent or out of range), varied jointly.
    D: list[str] = []
    if not x.lipid_present:
        D.append("lipid")
    elif not tc_hdl_valid(x):
        D.append("tc_hdl")
    for k in ("sbp", "smoking", "egfr"):
        if not ok[k]:
            D.append(k)
    name_of = {"lipid": "lipid_panel", "tc_hdl": "lipid_panel", "sbp": "sbp",
               "smoking": "smoking", "egfr": "egfr"}

    deciding: set[str] = set()
    blockers: list[str] = []
    risks: dict = {}
    if D:
        rows, approx = _tensor(x, D, ignore_row0)
        res["approx"] |= approx
        st = lut[rows]
        for k, ax in enumerate(D):
            if _varies(st, k):
                blockers.append(name_of[ax])
                deciding.add(name_of[ax])
        row_set = tuple(r for r in _ROW_IDS if _RI[r] in set(np.unique(rows).tolist()))
        if blockers:
            status, row = CANNOT_DETERMINE, None
        else:
            status = _STATUS_NAMES[st.flat[0]]
            row = row_set[0] if len(row_set) == 1 else None
        path = ()
    else:
        trace = []
        code = _row_codes(x, rec["tc"], rec["hdl"], rec["ldl"], rec["sbp"], rec["smk"],
                          rec["egfr"], ignore_row0, trace, risks)
        code = int(np.asarray(code).ravel()[0])
        row, status = _ROW_IDS[code], _STATUS_NAMES[lut[code]]
        row_set = (row,)
        path = tuple(dict.fromkeys(trace))
    uses_prevent = bool(risks) or bool(D)

    # Deciding test for every open-world input not yet settled.
    for name in OPEN_INPUTS:
        if name in deciding:
            continue
        if name == "lipid_panel":
            ax = "lipid"
        else:
            ax = name
            if ok[name] and not uses_prevent:
                continue  # risk never computed: SBP, smoking, eGFR cannot change the label
        others = [d for d in D if name_of[d] != name]
        rows, approx = _tensor(x, [ax] + others, ignore_row0)
        res["approx"] |= approx
        if _varies(lut[rows], 0):
            deciding.add(name)

    def first(a):
        return None if a is None else float(np.asarray(a).ravel()[0])

    res.update(
        status=status, row=row,
        missing=tuple(n for n in OPEN_INPUTS if n in blockers and n in gone),
        out_of_range=tuple(n for n in OPEN_INPUTS if n in blockers and n not in gone),
        deciding=tuple(n for n in OPEN_INPUTS if n in deciding),
        risk10=first(risks.get("risk10")), risk30=first(risks.get("risk30")),
        path=path, row_set=row_set)
    return res


def label(f, reasons=None, variant: str = "main", ignore_row0: bool = False) -> OracleResult:
    """Oracle label for one patient's facts (a `facts.PatientFacts`).

    reasons: list of `Reason` records (structured or note-planted). None means
    the statin-related structured allergy rows of `f`. Only reasons dated
    0-365 days before index count (SPEC 3.3).
    ignore_row0: give the indication group (first matching row among X, 1-11).
    """
    x = inputs_from_facts(f)
    if reasons is None:
        reasons = reasons_from_facts(f)
    window = applicable_reasons(reasons, f.index_date, f.sex, f.age)
    r = evaluate(x, variant, bool(window), ignore_row0)
    reason = window[0] if r["status"] == DOCUMENTED_REASON else None
    names = []
    for row in r["path"]:
        names.extend(ROW_INPUTS.get(row, ()))
    if r["status"] == CANNOT_DETERMINE:
        names.extend(r["missing"] + r["out_of_range"])
    return OracleResult(inputs_used=_sources(f, dict.fromkeys(names)), reason=reason,
                        reasons_in_window=window,
                        indication_row=r["row"] if ignore_row0 else None, **r)


def indication(f) -> OracleResult:
    """Indication group: the oracle with row 0 ignored and no reasons (SPEC 4.1)."""
    return label(f, reasons=(), variant="main", ignore_row0=True)
