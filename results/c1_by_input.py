"""C1 accuracy by deleted input, for each system of a run.

PROTOCOL.md sec. 4: "C2b and C1-by-smoking are small; their intervals will be wide and are
reported as such." The frozen scorer (src/gapcheck/score.py) reports C1 as one condition. This
script splits C1 by the deleted input (the case's `variant`: lipid_panel, sbp, smoking) using
the scorer's own functions: the same scored cases, the same correctness rule (status
CANNOT_DETERMINE and the deleted input in `missing`), and the same cluster-bootstrap resamples
(N_BOOT, BOOT_SEED). It changes no scored number. Written after the test seed and before any
run 1 output (RUNS.md sec. 3a).

usage: python results/c1_by_input.py --cases <case set> --run <run dir> [--out <file>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from gapcheck import score  # noqa: E402


def by_input(case_dir: str, run_dir: str, n_boot: int = score.N_BOOT,
             seed: int = score.BOOT_SEED) -> dict:
    cases = score.scored_cases(case_dir, run_dir)
    design = score.Design(cases)
    W = design.weights(n_boot, seed)          # the resamples score.score() uses
    run = score.load_run(run_dir)
    cond = np.array([c["condition"] for c in cases])
    var = np.array([c["variant"] for c in cases], dtype=object)
    inputs = sorted(set(var[cond == "C1"]))
    out = {"case_set": Path(case_dir).name, "run": Path(run_dir).name, "n_boot": int(W.shape[0]),
           "seed": seed, "measure": "C1 accuracy by deleted input", "systems": {}}
    for name, recs in sorted(run.items()):
        ok = np.array([score.correct(c, recs.get(c["case_id"])) for c in cases], dtype=bool)
        rows = {}
        for v in inputs:
            r = score._boot_prop(design, (cond == "C1") & (var == v), ok, W)
            lo, hi = r["ci"]
            r["interval_degenerate"] = lo is not None and lo == hi
            rows[v] = r
        out["systems"][name] = rows
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--n-boot", type=int, default=score.N_BOOT)
    ap.add_argument("--seed", type=int, default=score.BOOT_SEED)
    ap.add_argument("--out")
    a = ap.parse_args()
    res = by_input(a.cases, a.run, a.n_boot, a.seed)
    text = json.dumps(res, indent=1) + "\n"
    if a.out:
        Path(a.out).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
