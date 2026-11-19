"""The conventions page given to every LLM system (SPEC 6).

One Markdown page: status meanings, the decision table (plain-language rule,
label, class of recommendation) from `oracle.ROWS`, the encoding choices,
the input rules (SPEC 3.4), the reason rules (SPEC 3.3), the code lists in
compact form, the output schema and the evidence rules the verifier checks.

`render()` is deterministic; `sha256()` goes into every run manifest.
"""
from __future__ import annotations

import hashlib
from functools import cache

from . import codes as C
from . import oracle
from . import prevent
from . import schema as S
from .verifier import REQUIRED_INPUTS

STATUS_TEXT = {
    "ON_THERAPY": "An active statin prescription at the index date (not a claim about "
                  "adherence).",
    "GAP": "A class 1 recommendation to start a statin applies, no active statin, and no "
           "documented reason for not prescribing.",
    "DOCUMENTED_REASON": "Would be GAP, but a reason for not prescribing is documented.",
    "DISCUSS": "Only a class 2a or 2b recommendation applies. Never counted as a gap.",
    "NOT_INDICATED": "No class 1, 2a or 2b recommendation to start a statin applies.",
    "CANNOT_DETERMINE": "A deciding open-world input is missing or outside its valid range.",
}

INPUT_RULES = """\
- "At index" means dated on or before the index date. "Latest" means the latest value on \
or before the index date, with no lookback limit. If two values of one input share the \
latest timestamp, use the row with the smallest row id.
- Closed-world inputs (absence means "no"; never missing): clinical ASCVD (any qualifying \
code on or before index, stop dates ignored), diabetes, CKD stage and the row-5 features \
(active diagnosis codes at index), statin and BP treatment (start on or before index and no \
stop, or stop after index), diabetes duration (from the earliest diabetes start date). Age \
and sex are always present.
- Open-world inputs (absence means unknown): the lipid panel (TC, HDL-C and LDL-C from the \
latest date at which all three were measured), systolic BP, smoking status, eGFR (only the \
mL/min/{{1.73_m2}} series).
- An open-world input is deciding if setting it to different admissible values (with every \
other input as recorded) gives different statuses. Admissible values: smoking current or \
not; SBP 90-200; eGFR 15-150; TC 130-320 and HDL-C 20-100 mg/dL; any LDL-C up to TC minus \
HDL-C. An input made irrelevant by an earlier row is never deciding.
- CANNOT_DETERMINE applies when a deciding open-world input is absent or outside its \
valid range. List every absent open-world input in `missing` (whatever the status).
- PREVENT (base model, 10-year ASCVD risk; 30-year risk only at ages 30-59) needs: sex, age \
30-79, TC {tc}, HDL-C {hdl}, SBP {sbp}, BP treatment, statin use, diabetes, current smoking, \
eGFR {egfr}. Thresholds apply to the unrounded risk. BMI is not an input. Use the \
prevent_risk tool; do not compute PREVENT by hand."""

REASON_RULES = """\
- Categories: statin intolerance or statin-attributed muscle symptoms; patient declined; \
pregnancy or planned pregnancy (women aged 30-49 only); decompensated cirrhosis or acute \
liver failure (stable chronic liver disease is not a reason); hospice, palliative care or \
limited life expectancy.
- A reason needs an explicit refusal, intolerance, contraindication or care goal. Deferral \
("will revisit next visit") is not a reason. Negated statements and statements about \
relatives are not reasons.
- A reason counts if dated 0-365 days before the index date, in a note or in a structured \
entry (a statin allergy or intolerance row, a pregnancy condition, a hospice or \
palliative-care encounter, procedure or referral). A note reason needs no structured entry.
- A reason changes GAP to DOCUMENTED_REASON, and nothing else."""

ENCODING_RULES = """\
- First matching row wins, from row 0 down. Patients are aged 30-79 at index. HIV and \
ESRD, dialysis or kidney transplant are excluded from this set of patients.
- PREVENT 10% or more is GAP (row 8), like 5% to < 10% (row 9).
- Ages 76-79 in primary prevention are DISCUSS (row 6). Rows 1 and 2 have no age limit.
- Diabetes excludes prediabetes. Ischemic heart disease (414545008) counts as clinical ASCVD.
- Statins are matched by ingredient, including ezetimibe/simvastatin."""

EVIDENCE_RULES = """\
Every answer's evidence is checked by code:
1. Every DOCUMENTED source exists, is the right kind of row for its claim, and its value \
matches; a note quote must be an exact substring of that note.
2. For the row in `benefit_group`, every input in that row's list below has a DOCUMENTED, \
COMPUTED or ABSENT claim (risk_10y must be COMPUTED). A missing smoking status may instead \
be covered by two COMPUTED risk_10y items, smoker 0 and smoker 1 with all other inputs \
equal, whose risks fall in the same band (< 3%, 3 to < 5%, 5 to < 10%, >= 10%).
3. Every COMPUTED risk matches PREVENT recomputed from its `inputs` within 0.1 percentage \
point, and each input is backed by a claim in the answer: age, sex, lipid panel, SBP, eGFR \
and smoking by a DOCUMENTED claim with that value; statin, BP treatment and diabetes by a \
DOCUMENTED claim (1) or an ABSENT claim (0).
4. Every input in `missing` and every ABSENT claim is confirmed by re-running the query of \
its named list."""


def _rows_table() -> str:
    lines = ["| Row | Group | Rule | Label | COR |", "|---|---|---|---|---|"]
    for r in oracle.ROWS:
        if r.id == "X":
            continue
        cors = ", ".join(dict.fromkeys(s.cor for s in r.sources if s.cor)) or "-"
        if r.label == oracle.NOT_INDICATED and cors == "1":
            cors = "1 (counseling, no statin)"
        lines.append(f"| {r.id} | {r.group} | {r.rule} | {r.label} | {cors} |")
    return "\n".join(lines)


def _codes_line(name: str, d: dict, with_display: bool = True) -> str:
    if with_display:
        items = "; ".join(f"{c} {v.display}"
                          for c, v in sorted(d.items(), key=lambda kv: (kv[1].display, kv[0])))
    else:
        groups: dict[str, list[str]] = {}
        for c, v in sorted(d.items()):
            groups.setdefault(v.group or "other", []).append(c)
        items = "; ".join(f"{g}: {', '.join(cs)}" for g, cs in groups.items())
    return f"- {name}: {items}"


def _code_lists() -> str:
    L = [
        _codes_line("Clinical ASCVD, conditions", C.ASCVD_CONDITIONS),
        _codes_line("Clinical ASCVD, procedures", C.ASCVD_PROCEDURES),
        _codes_line("Statins (by ingredient)", C.STATINS, with_display=False),
        _codes_line("BP-lowering drugs (by class)", C.BP_DRUGS, with_display=False),
        _codes_line("Diabetes", C.DIABETES),
        "- Not diabetes: 714628002 Prediabetes",
        _codes_line("CKD stages (stage = highest active)", C.CKD_STAGES),
        _codes_line("Row-5 features: retinopathy", C.RETINOPATHY),
        _codes_line("Row-5 features: neuropathy", C.NEUROPATHY),
        _codes_line("Row-5 features: albuminuria or proteinuria", C.ALBUMINURIA),
        _codes_line("Pregnancy (structured reason)", C.PREGNANCY),
        _codes_line("Hospice or palliative care (structured reason)",
                    {**C.PALLIATIVE_ENCOUNTERS, **C.PALLIATIVE_PROCEDURES,
                     **C.PALLIATIVE_CONDITIONS}),
        "- Statin allergy or intolerance rows: allergy rows whose description names a statin.",
        "- Observations (LOINC): " + "; ".join(
            f"{name} {c.code}" for name, c in (("TC", C.TC), ("HDL-C", C.HDL), ("LDL-C", C.LDL),
                                              ("triglycerides", C.TG), ("SBP", C.SBP),
                                              ("DBP", C.DBP), ("BMI", C.BMI), ("eGFR", C.EGFR),
                                              ("smoking status", C.SMOKING))),
        "- Smoking status values: current = " + "; ".join(C.SMOKING_CURRENT)
        + "; not current = " + "; ".join(C.SMOKING_NOT_CURRENT),
    ]
    return "\n".join(L)


def _schema() -> str:
    lists = "\n".join(f"  - {k}: {v}" for k, v in S.ABSENT_LISTS.items())
    req = "\n".join(f"- Row {r}: {', '.join(v)}" for r, v in REQUIRED_INPUTS.items())
    return f"""\
Reply with one JSON object with exactly these keys:

- status: one of {", ".join(oracle.STATUSES)}
- benefit_group: the decision-table row you apply (a row id string), or null (use null with \
CANNOT_DETERMINE)
- risk_10y: PREVENT 10-year risk as a fraction (0.072 = 7.2%), or null if not computed
- missing: list of absent open-world inputs, from {", ".join(oracle.OPEN_INPUTS)}
- evidence: list of items {{"claim", "support", "source", "value"}}, plus "quote" for notes, \
"inputs" for COMPUTED, optional "date"
- note_for_clinician: two sentences

Evidence items:
- claim: one of {", ".join(S.CLAIMS)}
- support DOCUMENTED: source is a list of record row ids, or one note id with "quote" (an \
exact substring of the note), or ["{S.DEMOGRAPHICS}"] for age and sex. value: the value \
(lipid_panel as {{"tc": .., "hdl": .., "ldl": ..}}; smoking as the recorded text; \
reason as its category: {", ".join(oracle.REASON_CATEGORIES)}).
- support COMPUTED: source "{S.PREVENT_TOOL}", value the risk fraction, inputs the \
prevent_risk arguments ({", ".join(S.PREVENT_ARGS)}).
- support ABSENT: source is one named list, value null:
{lists}
- support INFERRED: source null; only for facts outside the inputs required below.

Required inputs per benefit_group (claims needed; "reason" means a reason claim or an \
ABSENT reasons_365d claim):
{req}"""


@cache
def render() -> str:
    R = prevent.RANGES
    rng = {k: f"{a}-{b}" for k, (a, b) in R.items()}
    status = "\n".join(f"- {k}: {v}" for k, v in STATUS_TEXT.items())
    return f"""\
# Conventions: statin initiation under the 2026 ACC/AHA dyslipidemia guideline

Source: 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on the Management \
of Dyslipidemia, J Am Coll Cardiol 2026;87(19):2624-2757. Scope: starting a statin, yes or \
no. Dose, LDL-C goals and non-statin therapy are out of scope.

## Status values

{status}

GAP is a prompt for review, not a finding of an error in care.

## Decision table (first matching row wins)

{_rows_table()}

## Encoding choices

{ENCODING_RULES}

## Inputs

{INPUT_RULES.format(**rng)}

## Documented reasons for not prescribing

{REASON_RULES}

## Code lists

{_code_lists()}

## Output schema

{_schema()}

## Evidence checks

{EVIDENCE_RULES}
"""


def sha256() -> str:
    return hashlib.sha256(render().encode("utf-8")).hexdigest()
