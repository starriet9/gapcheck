"""Score a run (SPEC 7); with --also, run-to-run stability across runs 2 and 3.

Usage:
  python scripts/score_runs.py --cases data/cases/<set> --run data/runs/<run1> \
      [--also data/runs/<run2>,data/runs/<run3>] [--n-boot 10000] [--out score.json]
"""
import argparse
import json
from pathlib import Path

from gapcheck import score


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--also", default="")
    ap.add_argument("--n-boot", type=int, default=score.N_BOOT)
    ap.add_argument("--seed", type=int, default=score.BOOT_SEED)
    ap.add_argument("--out")
    a = ap.parse_args()
    extra = [x for x in a.also.split(",") if x]
    s = score.score(a.cases, a.run, extra, n_boot=a.n_boot, seed=a.seed)
    out = Path(a.out) if a.out else Path(a.run) / "score.json"
    out.write_text(json.dumps(s, indent=1) + "\n")
    for name, v in s["accuracy_main"]["systems"].items():
        print(name, "macro", round(100 * v["macro"]["est"], 1),
              [None if x is None else round(100 * x, 1) for x in v["macro"]["ci"]])
    for k, v in s["accuracy_main"]["differences"].items():
        print(k, round(100 * v["est"], 1), [round(100 * x, 1) for x in v["ci"]])


if __name__ == "__main__":
    main()
