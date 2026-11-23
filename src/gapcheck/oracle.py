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
- A documented reason (sec. 3.3) dated 0-365 days before index (a structured
  statin allergy: active at index, or ended 0-365 days before) changes GAP to
  DOCUMENTED_REASON, and nothing else.
- Each row carries its sources (`kind` "quote" = verbatim, "figure" = a summary
  of figure boxes; `role` "basis", "context" or "conflict"), its encoding
  choices, effective scope, data limits, open points and sampling notes.
  `scripts/oracle_sheet.py` renders these tables as `reports/oracle_rules.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from functools import cache

import numpy as np

from . import prevent
from .facts import REASON_WINDOW_DAYS, allergy_reason_date, reason_in_window

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


# ---------------------------------------------------------------- table
@dataclass(frozen=True)
class Source:
    section: str          # guideline section, e.g. "4.2.3.7"; or "Table 1", "Figure 6", ...
    rec: str              # recommendation number, or "supportive text 7", "message 8", ...
    cor: str | None       # class of recommendation ("1", "2a", "2b", "3: No Benefit")
    loe: str | None       # level of evidence
    quote: str
    page: int             # journal page
    pdf_page: int
    kind: str = "quote"   # "quote": verbatim text; "figure": summary of figure boxes, not verbatim
    role: str = "basis"   # "basis"; "context" (shown for judgment, not the basis of the
    #                       label); "conflict" (reads differently; the encoding explains the choice)


SOURCE_KINDS = ("quote", "figure")
SOURCE_ROLES = ("basis", "context", "conflict")


@dataclass(frozen=True)
class Row:
    id: str
    group: str
    rule: str
    label: str            # main-analysis label
    sources: tuple[Source, ...]
    encoding: tuple[str, ...] = ()     # [encoding] choices (SPEC 3.2) and what is not encoded
    scope: str = ""                    # who reaches this row, given the rows before it
    data_limits: tuple[str, ...] = ()  # [data] what the generated data can and cannot test
    confirm: tuple[str, ...] = ()      # [confirm] open points for a human reviewer
    not_sampled: str = ""              # sampling rules that touch this row (SPEC 5.1, 5.2)


def _src(section, rec, cor, loe, page, quote, kind="quote", role="basis"):
    return Source(section, rec, cor, loe, quote, page, page - 2623, kind, role)


def _fig(figure, branch, cor, page, summary, role="basis"):
    """A figure-box summary: box texts joined by arrows, not a verbatim sentence."""
    return _src(figure, branch, cor, None, page, summary, kind="figure", role=role)


# Shared quotes.
_TAKE_HOME_8 = _src(
    "Top take-home messages", "message 8", None, None, 2633,
    "LDL-lowering therapy is recommended for primary prevention in adults aged 40 to 75 years "
    "with diabetes, chronic kidney disease stage 3 or 4, or human immunodeficiency virus, "
    "regardless of LDL-C level.")
_FIG9_YOUNG = (
    "'For adults ≥30 y of age, PREVENT ASCVD 10-y risk ≥3% or 30-y risk "
    "≥10%' → YES → 'Start moderate-intensity statin' (COR 2a); NO → 'Healthy behavior "
    "counseling' (COR 1). 'Diabetes-specific risk enhancers* present?' → YES → 'Start "
    "moderate-intensity statin' (COR 2b); NO → 'Healthy behavior counseling' (COR 1).")
_FIG6_LOW = (
    "'Low 10-y Risk (<3%)' → 'LDL-C 160–189 mg/dL or 30-y ASCVD risk ≥10%?' → YES → "
    "'Start moderate-intensity statin' (COR 2a); NO → 'Health behavior counseling' (COR 1). "
    "The figure covers adults 30–79 y with LDL-C 70–189 mg/dL; no other age limit on this "
    "branch.")
_SCOPE_PRIMARY = ("Age 30-75, LDL-C 70-189, no ASCVD, no diabetes, no CKD stage 3-4 at ages "
                  "40-75 (rows 1-7 first).")

ROWS: tuple[Row, ...] = (
    Row("0", "Any", "Active statin at index (start <= index and no stop or stop > index).",
        ON_THERAPY, (),
        ("Statins matched by ingredient, including ezetimibe/simvastatin.",),
        scope="Every patient (checked first)."),
    Row("X", "Excluded from the cohort",
        "HIV infection or AIDS, or ESRD, maintenance dialysis or kidney transplant (any code on or "
        "before index). Checked before row 1.",
        EXCLUDED,
        (_src("4.2.8.9", "1", "1", "B-R", 2703,
              "In people living with HIV aged 40 to 75 years on stable combination "
              "antiretroviral therapy, statin therapy is recommended to reduce risk of a first "
              "ASCVD event and reduce the rate of coronary atherosclerosis progression."),
         _src("4.2.8.8", "3", "2b", "C-LD", 2702,
              "In adults with CKD who require maintenance hemodialysis, it may be reasonable "
              "to continue statin therapy to reduce the risk of ASCVD events."),
         _src("4.2.8.8", "supportive text 3", None, None, 2703,
              "In adults with CKD who require dialysis treatment, initiation of a statin has not "
              "been shown to be beneficial in 2 large randomized clinical trials and in a "
              "subgroup analysis of a trial that included patients on dialysis.")),
        ("Encoded but excluded from the cohort: HIV. Rec 1 covers people 'on stable "
         "combination antiretroviral therapy'. The generated data has antiretroviral "
         "prescriptions, but the record gives no way to judge 'stable', and the dev cohort has "
         "25 patients with HIV or AIDS in 9,883; the exclusion is a design decision.",
         "Dialysis: rec 3 covers only continuation, and supportive text 3 reports no benefit "
         "from starting a statin.",
         "Kidney transplant has no recommendation of its own; its exclusion is a design "
         "decision. Stage 5 CKD without dialysis would fall under sec. 4.2.8.8 rec 1 ('stage 3 "
         "or higher'), but no stage 5 code is generated and every ESRD-coded patient in the dev "
         "cohort also has dialysis or a transplant."),
        scope="Patients not on a statin (row 0 first).",
        not_sampled="Never a base patient or twin (not in the cohort)."),
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
              "or other atherosclerotic forms of PAD, including aortic aneurysm."),
         _src("Table 1", "sec. 4.2.6, 2018 recommendation (revised in 2026)", None, None, 2630,
              "COR 1: In patients who are 75 years of age or younger with clinical ASCVD, "
              "high-intensity statin therapy should be initiated or continued with the aim of "
              "achieving a 50% or greater reduction in LDL-C levels.", role="context")),
        ("Ischemic heart disease code 414545008 counts as clinical ASCVD.",
         "No age limit: Table 1 lists the 2018 recommendation ('75 years of age or younger') "
         "as revised; the 2026 recommendation reads 'In adults with clinical ASCVD'."),
        scope="Every cohort patient not on a statin (rows 0 and X first).",
        data_limits=("PAD, TIA, aortic aneurysm and stable angina are not generated; row 1 is "
                     "tested on MI, revascularization, ischemic heart disease and stroke.",),
        not_sampled="Untreated S1 is sampled with at most half stroke-only."),
    Row("2", "Severe hypercholesterolemia", "Latest LDL-C >= 190 mg/dL. No age limit.", GAP,
        (_src("4.2.4.3", "2", "1", "B-R", 2674,
              "In adults with severe hypercholesterolemia with an LDL-C ≥190 mg/dL "
              "(4.9 mmol/L),* treatment with maximally tolerated statin therapy is recommended "
              "to lower LDL-C and reduce ASCVD risk."),
         _src("4.2.4.3", "1", "1", "B-NR", 2674,
              "In adults with severe hypercholesterolemia with an LDL-C ≥190 mg/dL "
              "(4.9 mmol/L),* secondary causes of dyslipidemia should be excluded and "
              "addressed to reduce LDL-C (Table 15).")),
        ("Rec 1 (exclude secondary causes first) is not encoded; rec 2 is not conditional on "
         "it.",
         "The recommendations read '≥190'; one table footnote reads 'LDL-C >190 mg/dL'. The "
         "oracle uses >= 190, applied to the recorded value without rounding (189.6 is below "
         "190).",
         "The footnote's non-HDL-C and apoB values are not encoded (rec 2 requires LDL-C "
         ">= 190).",
         "No age limit, so ages 76-79 with LDL-C >= 190 are GAP while other primary prevention "
         "at 76-79 is DISCUSS (row 6); such patients are not sampled."),
        scope="Cohort patients not on a statin, no ASCVD (rows 0-1 first).",
        not_sampled="S2 base patients: latest LDL-C >= 190 and an earlier value >= 190; not "
                    "if they also meet row 3 or are aged 76-79 or have diabetes outside "
                    "40-75."),
    Row("3", "Diabetes, 40-75", "Active diabetes and age 40-75. No LDL-C floor.", GAP,
        (_src("4.2.5", "1", "1", "A", 2681,
              "In adults 40 to 75 years of age with diabetes and without clinical ASCVD, "
              "moderate-intensity statin therapy is indicated to achieve a ≥30% to 49% "
              "reduction in LDL-C and a goal of LDL-C <100 mg/dL (2.6 mmol/L) and non–HDL-C "
              "<130 mg/dL (3.4 mmol/L) to reduce ASCVD risk."),
         _TAKE_HOME_8),
        ("Diabetes excludes prediabetes.",
         "No LDL-C floor: rec 1 has no LDL-C condition, and take-home message 8 says "
         "'regardless of LDL-C level'."),
        scope="Age 40-75, no ASCVD, LDL-C < 190 (rows 1-2 first; with LDL-C >= 190 row 2 "
              "gives GAP too)."),
    Row("3b", "CKD, 40-75",
        "Active CKD stage 3 or 4 diagnosis, age 40-75, latest LDL-C 70-189 mg/dL.", GAP,
        (_src("4.2.8.8", "1", "1", "B-R", 2702,
              "In adults 40 to 75 years of age with CKD stage 3 or higher and an LDL-C of 70 "
              "to 189 mg/dL (1.8-4.9 mmol/L), moderate-intensity statin therapy or "
              "moderate-intensity statin combined with ezetimibe is recommended to reduce "
              "ASCVD risk."),
         replace(_TAKE_HOME_8, role="conflict")),
        ("Defined by diagnosis code (highest active stage), not by a single eGFR value.",
         "LDL-C 70-189 as in rec 1. Take-home message 8 says 'regardless of LDL-C level'; CKD "
         "patients with LDL-C < 70 fall to row 7 and are not sampled (row 7 rule)."),
        scope="Age 40-75, no ASCVD, no diabetes (rows 1-3 first).",
        data_limits=("Stage 5 is not generated; ESRD is excluded (row X).",),
        not_sampled="Not sampled as base patients (SPEC 5.1)."),
    Row("4", "Diabetes, >75", "Active diabetes and age 76-79.", DISCUSS,
        (_src("4.2.5", "6", "2b", "C-LD", 2681,
              "In adults >75 years of age with diabetes and an estimated life expectancy of at "
              "least 2.5 years, it may be reasonable to initiate moderate-intensity statin "
              "therapy after a clinician–patient discussion of potential benefits and risks "
              "to reduce ASCVD risk."),),
        ("Life-expectancy condition not encoded.",),
        scope="Age 76-79, no ASCVD, LDL-C < 190.",
        not_sampled="Diabetes outside 40-75 is not sampled."),
    Row("5", "Diabetes, 30-39",
        "Active diabetes and age 30-39. DISCUSS if any of: type 2 diabetes >= 10 years, "
        "albuminuria or proteinuria, CKD stage >= 3, retinopathy, neuropathy; or 10-year "
        "PREVENT risk >= 3%; or 30-year risk >= 10%; or LDL-C 160-189. Otherwise "
        "NOT_INDICATED: row 7 if LDL-C < 70, else row 11b.",
        DISCUSS,
        (_src("4.2.5", "7", "2b", "C-LD", 2681,
              "In adults 20 to 39 years of age with diabetes of long duration (≥10 years of "
              "type 2 diabetes, ≥20 years of type 1 diabetes), albuminuria (≥30 μg of "
              "albumin/mg creatinine), estimated glomerular filtration rate (eGFR) "
              "<60 mL/min/1.73 m2, retinopathy, neuropathy, or ankle-brachial index <0.9, it "
              "may be reasonable to initiate moderate-intensity statin therapy to reduce "
              "ASCVD risk."),
         _fig("Figure 9", "20–39 y of age branch", "2a", 2683, _FIG9_YOUNG),
         _src("4.2.5", "supportive text 7", None, None, 2684,
              "In those individuals >30 years of age, calculation of 10- and 30-year risk by "
              "PREVENT-ASCVD equations should be used to guide treatment decisions according "
              "to the primary prevention algorithm (Figure 6, Section 4.2.3.7, “Primary "
              "Prevention in Adults 30 to 79 Years of Age With LDL-C Levels 70 to 189 mg/dL "
              "[1.8-4.9 mmol/L]”).", role="conflict"),
         _src("4.2.5", "synopsis", None, None, 2681,
              "Some young adults ≥30 years of age with diabetes may achieve borderline or "
              "intermediate-risk estimates by the PREVENT-ASCVD equations, and treatment "
              "should be guided by the primary prevention recommendations as in Section "
              "4.2.3.7, “Primary Prevention in Adults 30 to 79 Years of Age With LDL-C Levels "
              "70 to 189 mg/dL (1.8-4.9 mmol/L).”", role="conflict"),
         _fig("Figure 6", "Low 10-y Risk (<3%) branch", "2a", 2669, _FIG6_LOW)),
        ("Follows Figure 9: at ages 30-39 the PREVENT branch (10-year risk >= 3% or 30-year "
         "risk >= 10%) is COR 2a at any risk level, so DISCUSS; there is no GAP path through "
         "rows 8-9. Supportive text 7 and the synopsis point to the primary-prevention "
         "algorithm (Figure 6), which would give GAP at >= 5%; where the guideline reads two "
         "ways the oracle takes DISCUSS, as for ages 76-79.",
         "LDL-C 160-189 at low risk (10-year risk < 3%, 30-year risk < 10%, no row-5 "
         "feature): Figure 9 leads to healthy behavior counseling, while Figure 6's low-risk "
         "branch, where supportive text 7 and the synopsis send these patients, gives a "
         "moderate-intensity statin (COR 2a, as row 11a). The two readings differ, so DISCUSS "
         "by the same rule.",
         "Otherwise (no feature, 10-year risk < 3%, 30-year risk < 10%, LDL-C < 160) Figure "
         "9's NO branches lead to healthy behavior counseling (COR 1), not a statin start: "
         "NOT_INDICATED (row 7 or row 11b).",
         "Neither the row-5 features nor Figure 9's PREVENT branch has an LDL-C condition; "
         "they apply at any LDL-C below 190 (row 2 first).",
         "eGFR < 60 is encoded as a CKD stage >= 3 diagnosis code (PREVENT uses the eGFR "
         "value).",
         "Type 2 diabetes duration: from the earliest type 2 diabetes code start, in days / "
         "365.25. Diabetes due to cystic fibrosis (427089005) counts as diabetes but not "
         "toward the type 2 duration.",
         "Age 30 is included ('≥30' in Figure 9 and the synopsis)."),
        scope="Age 30-39, active diabetes, no ASCVD, LDL-C < 190 (rows 1-2 first).",
        data_limits=("Ankle-brachial index and type 1 diabetes do not occur in the data.",),
        not_sampled="Diabetes outside 40-75 is not sampled (base patients or twins)."),
    Row("6", "Older adults",
        "Age 76-79, no ASCVD, no diabetes, LDL-C < 190 mg/dL (primary prevention).", DISCUSS,
        (_src("4.2.8.3", "2", "2b", "B-NR", 2697,
              "In adults aged >75 years with an estimated life expectancy of at least 2.5 "
              "years, it may be reasonable to initiate moderate-intensity statin therapy "
              "after a clinician–patient discussion of potential benefits and risks to reduce "
              "ASCVD risk."),
         _src("Top take-home messages", "message 8", None, None, 2633,
              "After age 75 years, LDL-C–lowering pharmacotherapy can be considered in "
              "conjunction with lifestyle interventions to reduce ASCVD risk.")),
        ("Sec. 4.2.3.7 covers ages 30-79 but this COR 2b recommendation covers all adults "
         "> 75; DISCUSS is the conservative reading (SPEC 3.2).",
         "Life-expectancy condition not encoded."),
        scope="Age 76-79, no ASCVD, no diabetes, LDL-C < 190; includes CKD stage 3-4 (row 3b "
              "is limited to 40-75).",
        not_sampled="Primary prevention at 76-79 is not sampled."),
    Row("7", "LDL-C < 70", "Latest LDL-C < 70 mg/dL.", NOT_INDICATED,
        (_src("4.2.3.7", "12", "3: No Benefit", "B-NR", 2668,
              "In adults with a baseline untreated LDL-C <70 mg/dL (1.8 mmol/L) and "
              "non–HDL-C <100 mg/dL (2.6 mmol/L) and without additional ASCVD risk factors, "
              "initiation of LLT for primary prevention is unlikely to reduce ASCVD risk."),
         replace(_TAKE_HOME_8, role="conflict")),
        ("No recommendation to start a statin applies: sec. 4.2.3.7 and Figure 6 cover LDL-C "
         "70-189. Rec 12 covers only the subset with non-HDL-C < 100 and no additional ASCVD "
         "risk factors.",
         "Take-home message 8 recommends therapy for CKD stage 3-4 at 40-75 'regardless of "
         "LDL-C level', while sec. 4.2.8.8 rec 1 reads 'LDL-C of 70 to 189'. The oracle follows "
         "rec 1, so CKD stage 3-4 at 40-75 with LDL-C < 70 lands here.",
         "'Latest' LDL-C is used, although rec 12 reads 'baseline untreated LDL-C'."),
        scope="Age 30-75, no ASCVD, no diabetes at 40-75 (row 3), LDL-C < 70; includes CKD "
              "stage 3-4 at 40-75 and diabetes at 30-39 that row 5 did not match.",
        not_sampled="A row-7 base patient is sampled only if the label stays NOT_INDICATED with "
                    "LDL-C set to 70 (for a P1 eGFR, at every eGFR 60-140). This removes "
                    "patients whose label rests on the gap between rec 12 and the 70-189 "
                    "recommendations (10-year risk >= 3%, 30-year risk >= 10% at 30-59, CKD "
                    "stage 3-4 at 40-75)."),
    Row("8", "PREVENT >= 10%", "10-year PREVENT ASCVD risk >= 10%.", GAP,
        (_src("4.2.3.7", "7", "1", "A", 2667,
              "In adults at high (≥10%) 10-year estimated risk for ASCVD in whom LLT is "
              "initiated for primary prevention, high-intensity statin therapy is recommended "
              "to achieve an LDL-C reduction of ≥50% to reduce the risk of ASCVD."),
         _src("4.2.3.7", "supportive text 7", None, None, 2670,
              "High-intensity LLT is recommended as first-line LDL-C–lowering therapy for "
              "primary prevention of ASCVD in individuals at high risk (≥10%) for ASCVD, with "
              "an expected LDL-C lowering of ≥50%."),
         _fig("Figure 6", "High 10-y Risk (≥10%) branch", "1", 2669,
              "'High 10-y Risk (≥10%)' → 'Start high-intensity statin' (COR 1).")),
        ("Rec 7 is worded conditionally ('in whom LLT is initiated'); GAP because rec 5 (5 to "
         "< 10%) is unconditional and Figure 6 shows 'Start high-intensity statin' in a COR 1 "
         "box (SPEC 3.2). S-A and S-C relabel this row DISCUSS.",
         "Thresholds apply to the unrounded risk."),
        scope=_SCOPE_PRIMARY,
        not_sampled="S4 (rows 8-9)."),
    Row("9", "PREVENT 5 to < 10%", "10-year risk 5% to < 10%.", GAP,
        (_src("4.2.3.7", "5", "1", "A", 2667,
              "In adults at intermediate (5% to <10%) 10-year estimated risk for ASCVD, at "
              "least a moderate-intensity statin is recommended to achieve ≥30% to 49% LDL-C "
              "reduction and to reduce ASCVD risk; for those in the higher end of this risk "
              "range, a high-intensity statin is beneficial to further reduce LDL-C by ≥50% "
              "and reduce ASCVD risk."),),
        ("S-C relabels this row DISCUSS.",),
        scope=_SCOPE_PRIMARY,
        not_sampled="S4 (rows 8-9)."),
    Row("10", "PREVENT 3 to < 5%", "10-year risk 3% to < 5%.", DISCUSS,
        (_src("4.2.3.7", "4", "2a", "A", 2667,
              "In adults at borderline (3% to <5%) 10-year estimated risk for ASCVD in whom a "
              "decision is made to initiate statin therapy for primary prevention, a "
              "moderate-intensity statin is reasonable to achieve ≥30% to 49% LDL-C reduction "
              "and to reduce ASCVD risk."),
         _fig("Figure 6", "Borderline 10-y Risk (3%–<5%) branch", "2a", 2669,
              "'Borderline 10-y Risk (3%–<5%)' → 'Decide to start LLT?' → YES → 'Start "
              "moderate-intensity statin' (COR 2a); NO → 'CAC assessment' (COR 1).",
              role="context")),
        ("Conditional on a decision to treat.",
         "The CAC branch of Figure 6 (COR 1 assessment, not a statin start) is not encoded: no "
         "CAC data."),
        scope=_SCOPE_PRIMARY,
        not_sampled="DISCUSS: appears only as C5 twins."),
    Row("11a", "PREVENT < 3%, elevated",
        "10-year risk < 3%, and LDL-C 160-189 (ages 30-75) or 30-year risk >= 10% (ages "
        "30-59 only).", DISCUSS,
        (_src("4.2.3.7", "3", "2a", "C-LD", 2667,
              "In adults aged 30 to 59 years, at low (<3%) 10-year estimated risk for ASCVD but "
              "with an LDL-C of 160 to 189 mg/dL (4.1-4.9 mmol/L) or a 30-year ASCVD risk ≥10% "
              "(for those aged 30-59 years), a moderate-intensity statin is reasonable to "
              "reduce cumulative exposure to atherogenic lipoproteins."),
         _src("Table 1", "sec. 4.2.3.7, 2026 recommendation (new)", "2a", None, 2628,
              "COR 2a: In adults at low (<3%) 10-year estimated risk for ASCVD but with an "
              "LDL-C of 160 to 189 mg/dL (4.1-4.9 mmol/L) or a 30-year ASCVD risk ≥10% (for "
              "those aged 30-59 years), a moderate-intensity statin is reasonable to reduce "
              "cumulative exposure to atherogenic lipoproteins."),
         _fig("Figure 6", "Low 10-y Risk (<3%) branch", "2a", 2669, _FIG6_LOW)),
        ("Rec 3 opens with 'In adults aged 30 to 59 years' and repeats '(for those aged 30-59 "
         "years)' after the 30-year risk; Table 1 and Figure 6 carry no age limit for LDL-C "
         "160-189. The oracle reads the age limit as applying to the 30-year risk only, so "
         "ages 60-75 with LDL-C 160-189 are DISCUSS.",
         "30-year risk is computed only at ages 30-59."),
        scope=_SCOPE_PRIMARY,
        not_sampled="DISCUSS: appears only as C5 twins. Patients and twins aged 60-75 with "
                    "risk < 3% and LDL-C 160-189 are not used (their label depends on the "
                    "reading of rec 3's age limit)."),
    Row("11b", "PREVENT < 3%, otherwise",
        "All other patients at 10-year risk < 3% (and diabetes at 30-39 that row 5 did not "
        "match).", NOT_INDICATED,
        (_src("4.2.3.7", "2", "1", "A", 2667,
              "In adults aged 30 to 59 years, at low (<3%) 10-year estimated risk for ASCVD who "
              "have an LDL-C <160 mg/dL (4.1 mmol/L) and a 30-year risk estimate of <10%, "
              "counseling on health behaviors is recommended to reduce LDL-C and risk for "
              "ASCVD."),
         _src("Table 1", "sec. 4.2.3.7, 2026 recommendation (new)", "1", None, 2628,
              "COR 1: In adults at low (<3%) 10-year estimated risk for ASCVD who have an "
              "LDL-C <160 mg/dL (4.1 mmol/L) and a 30-year risk estimate of <10% (for those "
              "aged 30-59 years), counseling on health behaviors is recommended to reduce "
              "LDL-C and risk for ASCVD."),
         _fig("Figure 6", "Low 10-y Risk (<3%) branch", "1", 2669, _FIG6_LOW),
         _fig("Figure 9", "20–39 y of age branch", "1", 2683, _FIG9_YOUNG)),
        ("NOT_INDICATED: the NO branch of Figure 6's low-risk box leads to health behavior "
         "counseling (COR 1), with no recommendation to start a statin. At ages 60-75 the "
         "30-year risk is not used, so only LDL-C decides the branch.",
         "Diabetes at 30-39 reaching this row (LDL-C 70-159): Figure 9's NO branches "
         "(healthy behavior counseling, COR 1)."),
        scope="Age 30-75, LDL-C 70-159, 10-year risk < 3%, 30-year risk < 10% at ages 30-59, "
              "no ASCVD, no diabetes at 40-75, no CKD stage 3-4 at 40-75; plus diabetes at "
              "30-39 (LDL-C 70-159) with no row-5 feature, 10-year risk < 3% and 30-year risk "
              "< 10%.",
        not_sampled="S5 (with row 7)."),
)
ROW_BY_ID = {r.id: r for r in ROWS}


@dataclass(frozen=True)
class ReasonCategory:
    """A documented reason for not prescribing (SPEC 3.3): one taxonomy category."""
    id: str
    name: str                          # what the category covers
    structured: str                    # which structured entries count ("" if notes only)
    sources: tuple[Source, ...]
    encoding: tuple[str, ...] = ()
    data_limits: tuple[str, ...] = ()
    confirm: tuple[str, ...] = ()


REASONS: tuple[ReasonCategory, ...] = (
    ReasonCategory(
        "statin_intolerance", "Statin intolerance or statin-attributed muscle symptoms.",
        "Allergy or intolerance rows whose description names a statin ingredient "
        "(atorvastatin, rosuvastatin, simvastatin, pravastatin, lovastatin, fluvastatin, "
        "pitavastatin, cerivastatin), the word 'statin' or 'statins', or 'HMG-CoA reductase "
        "inhibitor', as whole words. A row active at index (no stop, or stop after index) "
        "counts whatever its start date; an ended row counts only if its stop date is 0-365 "
        "days before index.",
        (_src("4.2.11", "2", "1", "B-R", 2715,
              "In adults with statin-attributed muscle symptoms, the clinician–patient "
              "discussion should acknowledge patient side effect concerns, inform the patient "
              "of the heightened ASCVD risk associated with statin discontinuation, and "
              "provide alternative treatment options to reduce ASCVD risk."),
         _src("4.2.11", "1", "1", "C-LD", 2714,
              "In adults with statin-attributed muscle symptoms, assessment should include "
              "evaluation for secondary causes (Table 24), and in those with severe myalgias "
              "or weakness, objective clinical measures of muscle strength and measurement of "
              "CK are recommended to assess severity of the condition."),
         _src("4.2.5", "2", "1", "B-R", 2681,
              "In adults with diabetes who have statin-attributed side effects, initiation of "
              "ezetimibe and/or bempedoic acid or a PCSK9 mAb is recommended to lower LDL-C "
              "and reduce ASCVD risk.")),
        ("Whole-word matching, so nystatin, cilastatin and somatostatin do not count.",),
        ("Synthea v4.0.0 generates no statin allergy; record edits may add one.",)),
    ReasonCategory(
        "patient_declined", "Patient declined a statin.", "",
        (_src("4.2.3.7", "synopsis", None, None, 2668,
              "Factors that should be considered include short- and long-term risk, "
              "comorbidities, life expectancy, and patient preferences.", role="context"),),
        ("Category from Sarraju 2022 and Gobbel 2022; the guideline has no recommendation on "
         "refusal. Notes only.",)),
    ReasonCategory(
        "pregnancy", "Pregnancy, planned pregnancy or lactation (women aged 30-49 only).",
        "Pregnancy conditions (72892002, 47200007, 79586000) dated 0-365 days before index "
        "(start, or stop if only the stop falls in the window).",
        (_src("4.2.8.4", "1", "1", "C-LD", 2698,
              "Persons of childbearing age with hypercholesterolemia who are not at high risk "
              "for ASCVD and plan to become pregnant should stop statin therapy 1 to 2 months "
              "before attempting to become pregnant or as soon as pregnancy is discovered to "
              "avoid uncertain risks to the fetus."),
         _src("4.2.8.4", "supportive text 1", None, None, 2699,
              "At this time, it seems prudent to avoid statin therapy during pregnancy and "
              "while lactating, but to date, no significant teratogenicity has been "
              "observed."),
         _src("Table 25", "HMG-CoA reductase inhibitors (statins), comments", None, None, 2718,
              "pregnancy is not a contraindication, but statins should be avoided while "
              "lactating"),
         _src("Table 25", "HMG-CoA reductase inhibitors (statins), contraindications", None,
              None, 2718,
              "Acute liver failure; decompensated cirrhosis; lactation; severe underlying "
              "neuromuscular diseases (eg, dermatomyositis, muscular dystrophy)"),
         _src("4.2.8.4", "5", "2b", "C-LD", 2698,
              "In pregnant individuals with FH or a history of clinical ASCVD, it may be "
              "reasonable to continue statin therapy to lower LDL-C and ASCVD risk following "
              "an individualized benefit-risk discussion.", role="context")),
        ("Recognised whatever the patient's ASCVD risk, also in rows 1-2: the taxonomy "
         "classifies what is documented, not whether stopping is clinically right. Rec 1 is "
         "limited to persons 'not at high risk for ASCVD', and rec 5 (COR 2b) allows "
         "continuing a statin in pregnancy with FH or clinical ASCVD.",
         "Women aged 30-49 only, in the oracle and for planted reasons. Sampling is broader on "
         "purpose: any structured pregnancy entry within 365 days, or a pregnancy active at "
         "index, removes a patient at any age or sex, so no base patient carries a pregnancy "
         "entry that the oracle ignores."),
        ("No structured lactation code exists in Synthea v4.0.0 or the dev data; lactation "
         "can be documented only in notes.",)),
    ReasonCategory(
        "liver_failure", "Decompensated cirrhosis or acute liver failure.", "",
        (_src("Table 25", "HMG-CoA reductase inhibitors (statins), contraindications", None,
              None, 2718,
              "Acute liver failure; decompensated cirrhosis; lactation; severe underlying "
              "neuromuscular diseases (eg, dermatomyositis, muscular dystrophy)"),
         _src("5.1", "3", "2a", "B-R", 2721,
              "In adults at elevated ASCVD risk with chronic, stable liver disease (including "
              "metabolic dysfunction-associated steatotic liver disease), it is reasonable to "
              "treat with statin therapy to reduce ASCVD risk.", role="context"),
         _src("5.1", "supportive text 3", None, None, 2722,
              "Unlike in active liver disease, statins are not contraindicated in patients "
              "with chronic, stable liver disease, including metabolic dysfunction-associated "
              "steatotic liver disease (MASLD).", role="context")),
        ("Stable chronic liver disease (including MASLD) is not a reason.",
         "Notes only: no structured liver-failure entry is counted."),),
    ReasonCategory(
        "palliative", "Hospice, palliative care or limited life expectancy.",
        "Encounter 305336008 (admission to hospice), procedures 385763009 (hospice care) and "
        "306237005 (referral to palliative care service), condition Z51.5 (ICD-10-CM, "
        "encounter for palliative care), dated 0-365 days before index. Not counted: D9110 "
        "(palliative treatment of dental pain).",
        (_src("4.2.3.7", "11", "2b", "B-R", 2668,
              "In individuals with a life expectancy of <1 year, it may be reasonable to "
              "discontinue LLT that was prescribed for primary prevention purposes to avoid "
              "unnecessary medication use or adverse medication effects."),
         _src("4.2.8.3", "3", "2b", "B-R", 2697,
              "In patients with a life expectancy of <1 year, it may be reasonable to "
              "discontinue LDL-lowering therapy to avoid unnecessary medication use or "
              "adverse medication effects.")),
        ("The recommendations concern discontinuation; a documented care goal is taken as a "
         "reason not to start.",)),
)
REASON_BY_ID = {r.id: r for r in REASONS}
# Supporting statements per reason category (kept for callers that need only quotes).
REASON_SOURCES: dict[str, tuple[Source, ...]] = {r.id: r.sources for r in REASONS}

CONTEXT_SOURCES: tuple[Source, ...] = (
    _src("4.2.3.7", "1", "1", "A", 2667,
         "In adults being assessed for primary prevention of ASCVD, health behavior "
         "recommendations should be provided in addition to a benefit-risk discussion for "
         "consideration of LLT (Section 4.1.2, “Dietary Approaches in Dyslipidemia”).",
         role="context"),
    _src("5.1", "1", "1", "A", 2720,
         "In adults with dyslipidemia, an individualized clinician–patient discussion prior "
         "to initiating lipid-lowering medication is recommended to review the benefits and "
         "risks of pharmacotherapy to promote patient engagement and medication adherence.",
         role="context"),
    _src("Top take-home messages", "message 3", None, None, 2632,
         "Low-density lipoprotein (LDL)–lowering therapy can be considered in adults for "
         "primary prevention of ASCVD with a 10-year PREVENT-ASCVD risk estimate of 3% to <5% "
         "(borderline risk) and should be considered for those at 5% to <10% (intermediate "
         "risk) 10-year risk after a clinician–patient discussion.", role="context"),
)


@dataclass(frozen=True)
class Convention:
    """An oracle-wide rule that is not a table row (SPEC 3.2-3.4, 11)."""
    id: str
    title: str
    tag: str                           # "encoding", "data" or "convention"
    text: str
    sources: tuple[Source, ...] = ()


CONVENTIONS: tuple[Convention, ...] = (
    Convention(
        "reason_window", "Reason window", "convention",
        "A reason counts if dated 0-365 days before index (a structured statin allergy active "
        "at index counts whatever its start date). The window is a project convention: no "
        "guideline sentence sets it. A reason changes GAP to DOCUMENTED_REASON and nothing "
        "else. Deferral ('will revisit next visit') is not a reason.",
        (_src("4.2.8.4", "supportive text 2", None, None, 2700,
              "Considering the length of time for conception, pregnancy, and lactation, an "
              "individual may have to defer systemic LLT (especially statins) use for a period "
              "of 1 year, and possibly, several years.", role="context"),)),
    Convention(
        "latest_values", "'Latest' values", "encoding",
        "'Latest' means the latest value on or before index, with no lookback limit. The "
        "lipid panel is TC, HDL-C and LDL-C from the latest date on which all three were "
        "measured; a later LDL-C without TC and HDL-C is not used. Rec 12 of sec. 4.2.3.7 "
        "reads 'baseline untreated LDL-C'; the oracle uses the latest value even if it was "
        "measured during past statin use (generated statins do not lower later values; a "
        "phase 2 issue). If two values of a deciding input share the latest timestamp, the "
        "smallest row id is used and the patient is not sampled."),
    Convention(
        "thresholds", "Thresholds", "encoding",
        "LDL-C and risk thresholds apply to the recorded LDL-C and the unrounded PREVENT "
        "risk (LDL-C 189.6 is below 190; a risk of 4.99% is below 5%)."),
    Convention(
        "prevent_base", "PREVENT base equations only", "encoding",
        "The PREVENT base equations are used (10-year ASCVD; 30-year ASCVD at ages 30-59 "
        "only). The optional albuminuria, HbA1c and zip-code terms are not used. The "
        "recommendation on risk estimation does not name an equation variant; Table 13's note "
        "asks for all available information.",
        (_src("Table 13", "note", None, None, 2662,
              "Note that all available information should be included in risk estimates "
              "derived from the PREVENT-ASCVD equations, including albuminuria, HbA1c, and zip "
              "code for assessment of neighborhood-level social determinants of health.",
              role="context"),)),
    Convention(
        "open_closed", "Open- and closed-world inputs", "encoding",
        "Closed-world inputs (absence means 'no'): clinical ASCVD, diabetes, CKD stage, the "
        "row-5 features, diabetes duration, statin and BP treatment. Open-world inputs "
        "(absence means unknown): the lipid panel, SBP, smoking status, eGFR. An open-world "
        "input is deciding if setting it to every admissible value changes the label; "
        "CANNOT_DETERMINE names every absent or out-of-range deciding input. LDL-C has no "
        "valid range; if TC or HDL-C is outside its PREVENT range, only TC and HDL-C are "
        "unknown."),
    Convention(
        "prevent_inputs", "PREVENT input definitions", "encoding",
        "BP treatment includes oral furosemide (not intravenous). Smoking is the latest value "
        "of LOINC 72166-2. eGFR uses only the mL/min/{1.73_m2} series."),
)

# Recommendations that could bear on starting a statin but are not encoded (SPEC 3).
NOT_ENCODED: tuple[tuple[str, str], ...] = (
    ("4.2.3.6 recs 1, 3 and 5 (COR 1); 4.2.7",
     "Coronary artery calcium: use CAC when the decision is uncertain, start LLT if CAC > 0, "
     "consider incidental CAC. No CAC data."),
    ("4.2.3.3; 4.2.3.4 (COR 2a)",
     "Risk enhancers and reproductive risk markers personalize the discussion within the "
     "DISCUSS zone; they never create a COR 1 start. Synthea generates some (for example "
     "preeclampsia, rheumatoid arthritis); not used. hs-CRP (4.2.3.3 rec 2) is not generated."),
    ("4.2.4.4", "Familial hypercholesterolemia: not generated."),
    ("4.2.10", "Lipoprotein(a): not generated."),
    ("4.2.9 rec 5 (COR 1)",
     "Persistently elevated triglycerides at 40-75 without ASCVD or diabetes: estimate "
     "PREVENT risk to guide the discussion; the oracle uses PREVENT anyway, and triglycerides "
     "are not an input."),
    ("4.2.6 rec 8 (COR 2b); 4.2.8.6 rec 1 (COR 3: No Benefit)",
     "Heart failure with reduced ejection fraction: a moderate-intensity statin may be "
     "considered when it is due to ischemic heart disease and the patient is not already on a "
     "statin for ASCVD (rec 8; row 1 would answer GAP); starting LLT is not recommended "
     "without clinical ASCVD or another indication (4.2.8.6 rec 1). Not encoded. The generated "
     "data has heart failure diagnoses and ejection-fraction values, so patients with a heart "
     "failure diagnosis on or before index are not sampled. HFrEF is read only from a heart "
     "failure diagnosis, never from an ejection fraction alone: the CABG work-up records an "
     "ejection fraction of 30-49% without a heart failure diagnosis, and those patients are "
     "labeled by row 1."),
    ("4.2.8.10 rec 3 (COR 2b)",
     "Active cancer: starting a statin may be considered to prevent anthracycline-induced "
     "cardiotoxicity (cardioprotection, not ASCVD prevention). Not encoded: it would change only "
     "NOT_INDICATED or CANNOT_DETERMINE labels, and there it reads two ways (for example at a "
     "10-year risk below 3%): the recommendation names any adult with active cancer, the synopsis "
     "'patients at borderline or higher ASCVD risk when initiation of statin therapy was otherwise "
     "uncertain'; neither says how long after the last dose it applies. Patients with an "
     "anthracycline active at index or within 365 days before are not sampled."),
    ("4.2.4.3 rec 1; 4.2.5 rec 6; 4.2.8.3 rec 2; 4.2.8.10 rec 1",
     "Secondary causes before treating LDL-C >= 190; life-expectancy conditions."),
    ("Scope", "Dose intensity, LDL-C goals and non-statin therapy."),
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
REASON_CATEGORIES = tuple(r.id for r in REASONS)
PREGNANCY_REASON_AGES = (30, 49)


@dataclass(frozen=True)
class Reason:
    """A documented reason for not prescribing (SPEC 3.3)."""
    category: str      # one of REASON_CATEGORIES
    date: date
    source: str        # record row id or note id
    active_at_index: bool = False  # a structured statin allergy active at index

    def __post_init__(self):
        if self.category not in REASON_CATEGORIES:
            raise ValueError(f"unknown reason category {self.category!r}")


def reasons_from_facts(f) -> tuple[Reason, ...]:
    """Structured reasons (SPEC 3.3): allergy or intolerance rows that name a statin
    (dated by START if active at index, else by STOP; `facts.allergy_reason_date`);
    pregnancy conditions; hospice or palliative-care encounters, procedures or
    conditions (`facts.reason_entries`, dated 0-365 days before index). The window
    is applied in `applicable_reasons`."""
    out = [Reason("statin_intolerance", d, a.row_id, a.active) for a in f.allergies
           if (d := allergy_reason_date(a)) is not None]
    out += [Reason(e.category, e.date, e.row_id) for e in getattr(f, "reason_entries", ())]
    return tuple(out)


def reason_counts(r: Reason, index_date: date) -> bool:
    """Dated 0-365 days before index, or a statin allergy active at index."""
    return reason_in_window(r.date, index_date, r.active_at_index)


def applicable_reasons(reasons, index_date: date, sex: str, age: int) -> tuple[Reason, ...]:
    """Reasons that count at index (`reason_counts`); pregnancy only for women aged 30-49."""
    out = []
    for r in reasons:
        if not reason_counts(r, index_date):
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
# The label depends on LDL-C only through these bands (rows 2, 3b, 7, 11a).
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
        # Row 5 (ages 30-39): DISCUSS with a row-5 feature (rec 7), or with 10-year risk
        # >= 3% or 30-year risk >= 10% (Figure 9) at any LDL-C < 190, or with LDL-C
        # 160-189 (Figure 6 low-risk branch, COR 2a; Figure 9 reads NO there: two
        # readings, so DISCUSS). Otherwise healthy behavior counseling: NOT_INDICATED
        # through row 7 or row 11b. LDL-C >= 190 went to row 2 above.
        if x.row5_feature:
            put(np.True_, "5")
            return out
        if put((ldl >= 160) | (risk(10) >= RISK_BORDERLINE)
               | (risk(30) >= RISK_30Y_ELEVATED), "5"):
            return out
        if put(ldl < 70, "7"):
            return out
        put(np.True_, "11b")
        return out
    tr("6")
    if a >= 76:
        put(np.True_, "6")  # LDL-C < 190 here: row 2 came first
        return out
    if put(ldl < 70, "7"):
        return out
    r = risk(10)
    if put(r >= RISK_HIGH, "8") or put(r >= RISK_INTERMEDIATE, "9"):
        return out
    if put(r >= RISK_BORDERLINE, "10"):
        return out
    # Row 11a: LDL-C 160-189 at any age (Table 1, Figure 6); 30-year risk at 30-59 only.
    elevated = ldl >= 160
    if a <= 59:
        elevated = elevated | (risk(30) >= RISK_30Y_ELEVATED)
    if put(elevated, "11a"):
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
