"""Build a case set (SPEC 5.1, 5.2): base patients, twins, notes, ledger, manifest.

Usage:
  python scripts/build_cases.py <db> --split dev --n-per-stratum 10 [--name NAME]
      [--seed N] [--phrases FILE] [--out data/cases]
  python scripts/build_cases.py <db> --n-per-stratum 40 --count-only [--json FILE]

<db> is a gapcheck.duckdb with P0, P1 and P2 applied (scripts/build_db.py,
then scripts/gate.py). The master seed defaults to the database seed.
Writes <out>/<name>/ (default name: <split>_s<seed>_n<n>). --count-only
samples and builds every twin without writing anything or using phrases,
and prints the counts per condition.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from gapcheck import cases, edits, load, patches

ROOT = Path(__file__).resolve().parents[1]


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return p.name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--split", default="dev", choices=("dev", "test"))
    ap.add_argument("--n-per-stratum", type=int, required=True)
    ap.add_argument("--seed", type=int, help="master seed (default: database seed)")
    ap.add_argument("--name")
    ap.add_argument("--phrases", help="phrase file (default: gapcheck/phrases/<split>.json)")
    ap.add_argument("--out", default=str(ROOT / "data" / "cases"))
    ap.add_argument("--count-only", action="store_true")
    ap.add_argument("--json", help="with --count-only: write the counts to this file")
    a = ap.parse_args()

    db = Path(a.db)
    con = load.connect(db, read_only=True)
    if not patches.p2_applied(con):
        sys.exit("P2 is not applied; run scripts/gate.py on this database first")
    meta = dict(con.execute("select key, value from meta").fetchall())
    seed = a.seed if a.seed is not None else int(meta["seed"])
    t0 = time.time()
    if a.count_only:
        c = cases.counts(cases.plan(con, a.n_per_stratum, seed))
        c.update({"source_db": rel(db), "master_seed": seed, "n_per_stratum": a.n_per_stratum,
                  "seconds": round(time.time() - t0, 1)})
        text = json.dumps(c, indent=1, sort_keys=True)
        if a.json:
            Path(a.json).write_text(text + "\n")
        print(text)
        return
    lib = edits.load_phrases(a.split, a.phrases)
    pfile = Path(a.phrases) if a.phrases else edits.PHRASE_DIR / f"{a.split}.json"
    name = a.name or f"{a.split}_s{seed}_n{a.n_per_stratum}"
    m = cases.build_case_set(con, Path(a.out) / name, a.split, a.n_per_stratum, seed, lib,
                             phrase_file=pfile, source=rel(db))
    con.close()
    print(json.dumps({"case_set": rel(Path(a.out) / name), "seconds": round(time.time() - t0, 1),
                      **{k: m["counts"][k] for k in ("base_patients", "cases",
                                                     "cases_by_condition", "shortfalls",
                                                     "rejected")}}, indent=1))


if __name__ == "__main__":
    main()
