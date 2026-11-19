"""Score a run (PROTOCOL.md secs. 7-10); with --also, run-to-run stability across runs 2
and 3.

Usage:
  python scripts/score_runs.py --cases data/cases/<set> --run data/runs/<run1> \
      [--also data/runs/<run2>,data/runs/<run3>] [--n-boot 10000] [--out score.json] \
      [--results results.json]

Writes the full score (default <run>/score.json) and the page-facing results
(default <run>/results.json: headline numbers, hypotheses H1-H3, flags and
the fixed sentences; keys in the docstring of gapcheck.score), and prints the
headline.
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
    a = ap.parse_args()
    extra = [x for x in a.also.split(",") if x]
    s = score.score(a.cases, a.run, extra, n_boot=a.n_boot, seed=a.seed)
    out = Path(a.out) if a.out else Path(a.run) / "score.json"
    out.write_text(json.dumps(s, indent=1) + "\n")
    res = Path(a.results) if a.results else out.with_name("results.json")
    res.write_text(json.dumps(score.results(s), indent=1, ensure_ascii=False) + "\n")
    for name, v in s["accuracy_main"]["systems"].items():
        print(name, "macro", _r(v["macro"]["est"]), [_r(x) for x in v["macro"]["ci"]])
    for k, v in s["accuracy_main"]["differences"].items():
        print(k, _r(v["est"]), [_r(x) for x in v["ci"]])
    h = s["headline"]
    for key in ("answered_anyway", "changed_when_must_not"):
        for name, v in h[key].items():
            print(key, name, f"{v['k']} of {v['n']}", _r(v["rate"]), [_r(x) for x in v["ci"]])
    for k, v in s["hypotheses"].items():
        if v is not None:
            print(k, v["verdict"])
    print("wrote", out, "and", res)


if __name__ == "__main__":
    main()
