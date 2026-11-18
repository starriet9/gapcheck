"""Load a Synthea CSV directory into DuckDB and apply patches P0 and P1.

Usage: python scripts/build_db.py <run_dir> [--seed N]

<run_dir> holds output/csv (and run_manifest.json from
scripts/generate_synthea.sh, which gives the seed if --seed is omitted).
Writes <run_dir>/gapcheck.duckdb and <run_dir>/augmentation_ledger.csv.
"""
import argparse
import json
import time
from pathlib import Path

from gapcheck import load, patches


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--force", action="store_true", help="replace an existing database")
    a = ap.parse_args()
    run = Path(a.run_dir)
    seed = a.seed
    manifest = run / "run_manifest.json"
    if seed is None:
        seed = json.loads(manifest.read_text())["seed"]
    db = run / "gapcheck.duckdb"
    if db.exists() and a.force:
        db.unlink()
    t0 = time.time()
    con = load.load(run / "output" / "csv", db, seed=seed)
    counts = con.execute("select * from load_counts").df().iloc[0].to_dict()
    res = patches.apply_all(con, seed)
    n = patches.export_ledger(con, run / "augmentation_ledger.csv")
    con.execute("checkpoint")
    con.close()
    print(json.dumps({"seed": seed, **{k: int(v) for k, v in counts.items()}, **res,
                      "ledger_rows": n, "seconds": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
