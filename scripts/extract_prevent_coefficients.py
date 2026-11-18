"""Extract PREVENT base-model coefficients from the paper's supplement.

Usage:
    python -I scripts/extract_prevent_coefficients.py [SUPPLEMENT.xlsx] [--output PATH]

The supplement is "Supplemental Tables" (xlsx) of Khan SS et al., Circulation
2024;149:430-449 (PMC author manuscript NIHMS1953934). If no path is given,
the env var GAPCHECK_PREVENT_SUPPLEMENT is used. The file is only read
(openpyxl, read-only, cached values only).

Reads sheets "Table S12A Base 10yr" and "Table S12F Base 30yr" and writes the
ASCVD and total CVD coefficients for women and men. Columns are found from the
header labels, then checked against the sheet's own worked example: the risk
computed from the extracted coefficients must equal the sheet's risk in both
the intermediate-calculation table and the output table.
"""

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

import openpyxl

from gapcheck.prevent import derived_terms, log_odds

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO / "src" / "gapcheck" / "prevent_coefficients.json"
ENV_VAR = "GAPCHECK_PREVENT_SUPPLEMENT"

SHEETS = {"10yr": "Table S12A Base 10yr", "30yr": "Table S12F Base 30yr"}
TABLE_IDS = {"10yr": "S12A", "30yr": "S12F"}
OUTCOMES = {"ASCVD": "ascvd", "Total CVD": "total_cvd"}
SEXES = {"Women": "female", "Men": "male"}

# Row label in the beta-coefficient table -> term name used in gapcheck.prevent.
BETA_LABELS = {
    "Age per 10 years": "age",
    "Age per 10 years squared": "age_squared",
    "non-HDL-C per 1 mmol/L": "non_hdl",
    "HDL-C per 0.3 mmol/L": "hdl",
    "SBP <110 per 20 mmHg": "sbp_lt_110",
    "SBP ≥110 per 20 mmHg": "sbp_ge_110",
    "Diabetes": "diabetes",
    "Current smoking": "smoker",
    "BMI <30, per 5 kg/m2": "bmi_lt_30",
    "BMI 30+, per 5 kg/m2": "bmi_ge_30",
    "eGFR <60, per -15 ml": "egfr_lt_60",
    "eGFR 60+, per -15 ml": "egfr_ge_60",
    "Anti-hypertensive use": "bp_treated",
    "Statin use": "statin",
    "Treated SBP ≥110 mm Hg per 20 mm Hg": "bp_treated_x_sbp_ge_110",
    "Treated non-HDL-C": "statin_x_non_hdl",
    "Age per 10yr * non-HDL-C per 1 mmol/L": "age_x_non_hdl",
    "Age per 10yr * HDL-C per 0.3 mml/L": "age_x_hdl",
    "Age per 10yr * SBP ≥110 mm Hg per 20 mmHg": "age_x_sbp_ge_110",
    "Age per 10yr * diabetes": "age_x_diabetes",
    "Age per 10yr * current smoking": "age_x_smoker",
    "Age per 10yr * BMI 30+ per 5 kg/m2": "age_x_bmi_ge_30",
    "Age per 10yr * eGFR <60, per -15 ml": "age_x_egfr_lt_60",
    "Constant": "constant",
}
# BMI terms are blank for ASCVD and total CVD (used by heart failure only).
BMI_TERMS = {"bmi_lt_30", "bmi_ge_30", "age_x_bmi_ge_30"}

# Worked-example input rows (label prefix in column A -> input name).
INPUT_LABELS = {
    "Age": "age",
    "Total cholesterol": "tc",
    "High-density lipoprotein": "hdl",
    "Systolic blood pressure": "sbp",
    "Diabetes": "diabetes",
    "Current smoking": "smoker",
    "Body-mass index": "bmi",
    "Estimated GFR": "egfr",
    "Anti-hypertension medication": "bp_treated",
    "Statin use": "statin",
}


def text(v):
    return v.strip() if isinstance(v, str) else v


def find_row(rows, predicate, start=0):
    for i in range(start, len(rows)):
        if predicate(rows[i]):
            return i
    raise ValueError("expected row not found")


def column_map(outcome_row, sex_row):
    """Map (outcome, sex) -> column, from an outcome header row and a sex row."""
    cols = {}
    for c, v in enumerate(outcome_row):
        if text(v) in OUTCOMES:
            for offset, sex_label in enumerate(("Women", "Men")):
                if text(sex_row[c + offset]) != sex_label:
                    raise ValueError(f"expected {sex_label} under {v} at column {c + offset}")
                cols[(OUTCOMES[text(v)], SEXES[sex_label])] = c + offset
    if len(cols) != 2 * len(OUTCOMES):
        raise ValueError(f"outcome columns not found: {cols}")
    return cols


def read_inputs(rows):
    """Worked-example inputs and allowable ranges (rows below 'Input variables')."""
    head = find_row(rows, lambda r: text(r[0]) == "Input variables")
    assert text(rows[head][1]) == "Allowable range" and text(rows[head][2]) == "Input Values"
    values, ranges = {}, {}
    for r in rows[head + 1 : head + 1 + len(INPUT_LABELS)]:
        name = next(n for p, n in INPUT_LABELS.items() if str(r[0]).startswith(p))
        values[name] = r[2]
        ranges[name] = text(r[1])
    assert set(values) == set(INPUT_LABELS.values())
    return values, ranges


def read_sheet(ws):
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    width = max(len(r) for r in rows)
    rows = [r + (None,) * (width - len(r)) for r in rows]

    # Beta-coefficient table.
    top = find_row(rows, lambda r: text(r[0]) == "Risk Factors")
    cols = column_map(rows[top], rows[top + 1])
    coefficients = {key: {} for key in cols}
    labels = {}
    i = top + 2
    while rows[i][0] is not None and not str(rows[i][0]).startswith("*"):
        label = text(rows[i][0])
        name = BETA_LABELS[label]  # KeyError on an unknown row label
        labels[name] = label
        for key, c in cols.items():
            v = rows[i][c]
            if v is None:
                if name not in BMI_TERMS:
                    raise ValueError(f"blank coefficient {label} for {key}")
                continue
            if name in BMI_TERMS:
                raise ValueError(f"unexpected BMI coefficient for {key}")
            coefficients[key][name] = float(v)
        i += 1

    # Worked example: recompute and compare with both tables in the sheet.
    inputs, ranges = read_inputs(rows)
    terms = derived_terms(**{k: v for k, v in inputs.items() if k != "bmi"})
    mid = find_row(rows, lambda r: str(r[0]).startswith("Input variables and intermediate"))
    mid_cols = column_map(rows[mid], rows[mid + 1])
    risk_row = rows[find_row(rows, lambda r: text(r[0]) == "Risk", mid)]
    out = find_row(rows, lambda r: any(str(v).startswith("Output: Estimated") for v in r))
    out_cols = column_map(rows[out + 1], rows[out + 2])
    out_row = rows[out + 3]
    example = {}
    for key, beta in coefficients.items():
        x = log_odds(beta, terms)
        risk = math.exp(x) / (1 + math.exp(x))
        for where, value in (("intermediate", risk_row[mid_cols[key]]), ("output", out_row[out_cols[key]])):
            if abs(risk - value) > 1e-12:
                raise ValueError(f"worked example mismatch {key} ({where}): {risk} vs {value}")
        example[key] = risk_row[mid_cols[key]]
    return coefficients, labels, inputs, ranges, example


def extract(path):
    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    models = {o: {} for o in OUTCOMES.values()}
    term_labels, examples, allowable = {}, {}, {}
    for horizon, sheet in SHEETS.items():
        coefficients, labels, inputs, ranges, example = read_sheet(wb[sheet])
        for (outcome, sex), beta in coefficients.items():
            models[outcome].setdefault(horizon, {})[sex] = beta
        for name, label in labels.items():
            if name not in BMI_TERMS:
                term_labels.setdefault(name, label)
        allowable[TABLE_IDS[horizon]] = ranges
        examples[TABLE_IDS[horizon]] = {
            "inputs": inputs,
            "risk": {f"{o}_{s}": r for (o, s), r in example.items()},
        }
    wb.close()
    order = list(BETA_LABELS.values())
    term_labels = dict(sorted(term_labels.items(), key=lambda kv: order.index(kv[0])))
    return {
        "source": {
            "paper": (
                "Khan SS, et al. Development and Validation of the American Heart "
                "Association's PREVENT Equations. Circulation. 2024;149(6):430-449. "
                "doi:10.1161/CIRCULATIONAHA.123.067626"
            ),
            "supplement": "Supplemental Tables (xlsx), PMC author manuscript NIHMS1953934",
            "supplement_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "tables": {TABLE_IDS[h]: s for h, s in SHEETS.items()},
            "model": "PREVENT base model (no UACR, HbA1c or SDI)",
            "note": (
                "Coefficients copied from the beta-coefficient table of each sheet. "
                "Women/men columns checked against the sheet's worked example. BMI terms are "
                "blank for ASCVD and total CVD and are omitted. Cholesterol is converted "
                "with mmol/L = mg/dL x 0.02586."
            ),
            "extracted_by": "scripts/extract_prevent_coefficients.py",
        },
        "allowable_range_in_table": allowable,
        "terms": term_labels,
        "models": models,
        "worked_examples": examples,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("xlsx", nargs="?", default=os.environ.get(ENV_VAR))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not args.xlsx:
        parser.error(f"give the supplement path or set {ENV_VAR}")
    data = extract(args.xlsx)
    args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output.name}")


if __name__ == "__main__":
    sys.exit(main())
