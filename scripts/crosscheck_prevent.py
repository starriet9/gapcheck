"""Cross-check gapcheck.prevent against pyprevent 0.1.7 and preventr 0.12.2.

Usage: python -I scripts/crosscheck_prevent.py [--workdir DIR]

Writes the seeded inputs of gapcheck.prevent_samples (1,000 rows for 10-year
and 1,000 for 30-year ASCVD risk) to DIR/inputs.csv, runs
scripts/crosscheck_preventr.R with the project-local Rscript
(tools/bin/Rscript-local), and compares. Rows with SBP > 180 are skipped for
preventr, which rejects them. Prints the maximum absolute differences (risk on
a 0-1 scale) and writes them to DIR/summary.json. Default DIR:
data/prevent_crosscheck (not tracked). Exits 1 if any difference is 0.001
(0.1 percentage point) or more.
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

import pyprevent

from gapcheck.prevent import ascvd_risk
from gapcheck.prevent_samples import crosscheck_inputs

REPO = Path(__file__).resolve().parents[1]
RSCRIPT = REPO / "tools" / "bin" / "Rscript-local"
R_SCRIPT = REPO / "scripts" / "crosscheck_preventr.R"
FIELDS = ["sex", "age", "tc", "hdl", "sbp", "bp_treated", "statin", "diabetes", "smoker", "egfr", "bmi"]
TOLERANCE = 0.001
PREVENTR_MAX_SBP = 180

PYPREVENT = {10: pyprevent.calculate_10_yr_ascvd_risk, 30: pyprevent.calculate_30_yr_ascvd_risk}


def ours(row, horizon):
    return ascvd_risk(
        row["sex"], row["age"], row["tc"], row["hdl"], row["sbp"], row["bp_treated"],
        row["statin"], row["diabetes"], row["smoker"], row["egfr"], horizon=horizon,
    )


def pyprevent_risk(row, horizon):
    return PYPREVENT[horizon](
        sex=row["sex"], age=row["age"], total_cholesterol=row["tc"],
        hdl_cholesterol=row["hdl"], systolic_bp=row["sbp"],
        has_diabetes=bool(row["diabetes"]), current_smoker=bool(row["smoker"]),
        bmi=row["bmi"], egfr=row["egfr"], on_htn_meds=bool(row["bp_treated"]),
        on_cholesterol_meds=bool(row["statin"]),
    ) / 100  # percent -> 0-1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=REPO / "data" / "prevent_crosscheck")
    args = parser.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)
    inputs_csv = args.workdir / "inputs.csv"
    preventr_csv = args.workdir / "preventr.csv"

    cases = crosscheck_inputs()
    with inputs_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "horizon", *FIELDS])
        for i, (horizon, row) in enumerate(cases):
            w.writerow([i, horizon, *(repr(row[k]) if k != "sex" else row[k] for k in FIELDS)])

    subprocess.run([str(RSCRIPT), str(R_SCRIPT), str(inputs_csv), str(preventr_csv)], check=True)
    with preventr_csv.open(newline="") as f:
        preventr = {int(r["id"]): r for r in csv.DictReader(f)}

    stats = {}
    for h in (10, 30):
        stats[h] = {"rows": 0, "pyprevent": 0.0, "preventr_rows": 0, "preventr_skipped_sbp_gt_180": 0,
                    "preventr_full": 0.0, "preventr_rounded_3dp": 0.0}
    for i, (horizon, row) in enumerate(cases):
        s = stats[horizon]
        risk = ours(row, horizon)
        s["rows"] += 1
        s["pyprevent"] = max(s["pyprevent"], abs(risk - pyprevent_risk(row, horizon)))
        p = preventr[i]
        if row["sbp"] > PREVENTR_MAX_SBP:
            if p["ascvd_full"] != "NA":
                raise SystemExit(f"row {i}: preventr returned a value for SBP {row['sbp']}")
            s["preventr_skipped_sbp_gt_180"] += 1
            continue
        if p["ascvd_full"] == "NA":
            raise SystemExit(f"row {i}: preventr returned NA ({p['input_problems']})")
        s["preventr_rows"] += 1
        s["preventr_full"] = max(s["preventr_full"], abs(risk - float(p["ascvd_full"])))
        s["preventr_rounded_3dp"] = max(s["preventr_rounded_3dp"], abs(risk - float(p["ascvd_rounded"])))

    summary = {f"{h}yr": s for h, s in stats.items()}
    (args.workdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name, s in summary.items():
        print(
            f"{name}: rows {s['rows']}; max abs diff vs pyprevent {s['pyprevent']:.3g}; "
            f"vs preventr full {s['preventr_full']:.3g} ({s['preventr_rows']} rows, "
            f"{s['preventr_skipped_sbp_gt_180']} skipped SBP>180), "
            f"vs preventr rounded to 3 dp {s['preventr_rounded_3dp']:.3g}"
        )
    worst = max(max(s["pyprevent"], s["preventr_full"], s["preventr_rounded_3dp"]) for s in stats.values())
    return 0 if worst < TOLERANCE else 1


if __name__ == "__main__":
    sys.exit(main())
