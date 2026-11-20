"""Score a run (PROTOCOL.md secs. 7-9); with --also, run-to-run stability across runs 2
and 3.

Usage:
  python scripts/score_runs.py --cases data/cases/<set> --run data/runs/<run1> \
      [--also data/runs/<run2>,data/runs/<run3>] [--n-boot 10000] [--out score.json] \
      [--results results.json] [--gpu-hours H --usd-per-hour R]

Writes the full score (default <run>/score.json) and the page-facing results
(default <run>/results.json: the primary outcome and the page's counts,
hypotheses H1-H3, the verifier's first-round rejections, cost per case and
flags; keys in the docstring of gapcheck.score), and prints the main numbers.
Cost per case (secondary outcome 8) is written only when both --gpu-hours and
--usd-per-hour are given (the values entered in RUNS.md sec. 5 for the run).
"""
import argparse
import json
from pathlib import Path

from gapcheck import score


def _r(x):
    return None if x is None else round(100 * x, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--also", default="")
    ap.add_argument("--n-boot", type=int, default=score.N_BOOT)
    ap.add_argument("--seed", type=int, default=score.BOOT_SEED)
    ap.add_argument("--out")
    ap.add_argument("--results")
    ap.add_argument("--gpu-hours", type=float, help="GPU-hours of the run (RUNS.md sec. 5)")
    ap.add_argument("--usd-per-hour", type=float, help="hourly GPU rate in USD (RUNS.md sec. 5)")
    a = ap.parse_args()
    if (a.gpu_hours is None) != (a.usd_per_hour is None):
        ap.error("--gpu-hours and --usd-per-hour go together")
    extra = [x for x in a.also.split(",") if x]
    s = score.score(a.cases, a.run, extra, n_boot=a.n_boot, seed=a.seed)
    if a.gpu_hours is not None:
        s["cost"] = score.cost_per_case(a.gpu_hours, a.usd_per_hour, s["n_cases"])
    out = Path(a.out) if a.out else Path(a.run) / "score.json"
    out.write_text(json.dumps(s, indent=1) + "\n")
    res = Path(a.results) if a.results else out.with_name("results.json")
    res.write_text(json.dumps(score.results(s), indent=1, ensure_ascii=False) + "\n")
    for name, v in s["accuracy_main"]["systems"].items():
        print(name, "macro", _r(v["macro"]["est"]), [_r(x) for x in v["macro"]["ci"]])
    for k, v in s["accuracy_main"]["differences"].items():
        print(k, _r(v["est"]), [_r(x) for x in v["ci"]])
    h = s["headline"]
    for key in ("c1_correct", "answered_anyway", "changed_when_must_not"):
        for name, v in h[key].items():
            print(key, name, f"{v['k']} of {v['n']}", _r(v["rate"]), [_r(x) for x in v["ci"]])
    vr = s.get("verifier_A2")
    if vr:
        r = vr["rejected"]
        print(f"verifier A2: {r['k']} of {r['n']} first drafts rejected; by check "
              f"{vr['by_check']}; repaired {vr['repaired']}, forced {vr['forced']}")
    if s.get("cost"):
        c = s["cost"]
        print(f"cost: ${c['usd_per_case']:.4f} and {c['gpu_seconds_per_case']:.0f} GPU-seconds "
              f"per case ({c['gpu_hours']} GPU-hours at ${c['usd_per_hour']}/h, {c['n_cases']} cases)")
    for k, v in s["hypotheses"].items():
        if v is not None:
            print(k, v["verdict"])
    print("wrote", out, "and", res)


if __name__ == "__main__":
    main()
