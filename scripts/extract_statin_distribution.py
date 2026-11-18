"""Copy the generator's statin prescribing tables into src/gapcheck/statin_distribution.json.

Usage: python scripts/extract_statin_distribution.py <synthea_source_dir> [--state Massachusetts]

Reads src/main/resources/modules/medications/statin.json and its lookup
tables (modules/lookup_tables/statin_*_distribution.csv) from a Synthea
v4.0.0 source tree and keeps the rows of one state. P2 (patches.py) draws a
drug and dose from this file the way the submodule does: ingredient by
(age band, gender, state), then product by the same keys; if no row
matches, the transition with default_probability 1 is taken.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "gapcheck" / "statin_distribution.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("synthea_dir")
    ap.add_argument("--state", default="Massachusetts")
    a = ap.parse_args()
    mod_dir = Path(a.synthea_dir) / "src" / "main" / "resources" / "modules"
    module_path = mod_dir / "medications" / "statin.json"
    raw = module_path.read_bytes()
    states = json.loads(raw)["states"]

    def table_rows(name: str) -> list[dict]:
        with open(mod_dir / "lookup_tables" / name, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["state"] == a.state]
        return [{"age": r["age"], "gender": r["gender"],
                 "p": {k: float(v) for k, v in r.items() if k not in ("age", "gender", "state")}}
                for r in rows]

    def transitions(state: dict) -> tuple[list, str | None, str | None]:
        opts = state["lookup_table_transition"]
        default = [o["transition"] for o in opts if o["default_probability"] == 1]
        return [o["transition"] for o in opts], default[0] if default else None, \
            opts[0]["lookup_table_name"]

    order, default, table = transitions(states["Prescribe_Ingredient"])
    out = {
        "source": {"module": "src/main/resources/modules/medications/statin.json",
                   "module_sha256": hashlib.sha256(raw).hexdigest(),
                   "synthea_version": "v4.0.0", "state": a.state,
                   "rule": "age bands are inclusive; first matching row in file order; "
                           "no matching row -> transition with default_probability 1"},
        "ingredient": {"order": order, "default": default, "table": table_rows(table)},
        "products": {},
        "codes": {},
    }
    for ing in order:
        o, d, t = transitions(states[ing])
        out["products"][ing] = {"order": o, "default": d, "table": table_rows(t)}
        for prod in o:
            c = states[prod]["codes"][0]
            out["codes"][prod] = {"code": c["code"], "display": c["display"]}
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT.name}: {len(out['codes'])} products")


if __name__ == "__main__":
    main()
