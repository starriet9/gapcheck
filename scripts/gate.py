"""Day-0 gate (SPEC 5.1): P2, oracle and strata on one database; aggregate report.

Usage: python scripts/gate.py <db> [--seed N] [--name NAME]

<db> is a gapcheck.duckdb built by scripts/build_db.py (P0 and P1 applied).
Steps: facts before P2 -> indication groups -> P2 (applied in place once;
on a database that already has P2, the plan is recomputed and checked
against the stored rows) -> facts of P2 patients -> oracle labels -> strata.

Writes:
- reports/gate_<name>.md (tracked; aggregate counts only)
- <db dir>/augmentation_ledger.csv (re-exported with P2 rows)
- <db dir>/results/strata.csv and results/gate_<name>.json (patient level; not tracked)
"""
import argparse
import json
import math
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from gapcheck import facts, load, oracle, patches, strata

ROOT = Path(__file__).resolve().parents[1]
GATE_MIN_ELIGIBLE = 50
GATE_MIN_ON_OFF = 15
TEST_N = 40           # base patients per stratum in the test set (SPEC 5.1)
TEST_HALF = TEST_N // 2
TEST_S4_CELL = TEST_N // 4  # S4: per risk bin and statin status


def pre_p2(rec: facts.PatientRecord) -> facts.PatientRecord:
    m = rec.medications
    if m.empty or "patch" not in m or not (m.patch == "P2").any():
        return rec
    return rec.replace(medications=m[m.patch != "P2"])


def capacity_untreated_s1(types: Counter) -> int:
    """Untreated S1 patients usable with at most half stroke-only."""
    other = sum(v for k, v in types.items() if k != "stroke_only")
    return other + min(types.get("stroke_only", 0), other)


def needed_size(count: int, cohort: int, threshold: int) -> tuple[int | None, int | None]:
    """Cohort size for an expected count of `threshold`, and with a 2-SD Poisson margin."""
    if count == 0:
        return None, None
    rate = count / cohort
    point = threshold / rate
    margin = (1 + math.sqrt(1 + threshold)) ** 2 / rate
    return math.ceil(point), math.ceil(margin)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--seed", type=int, help="patch seed (default: meta.seed)")
    ap.add_argument("--name", help="set name for the report (default: db folder name)")
    a = ap.parse_args()
    db = Path(a.db)
    run = db.parent
    name = a.name or run.name
    T: dict[str, float] = {}

    con = load.connect(db)
    meta = dict(con.execute("select key, value from meta").fetchall())
    seed = a.seed if a.seed is not None else int(meta["seed"])
    cohort_n = con.execute("select count(*) from cohort").fetchall()[0][0]

    t0 = time.time()
    recs = list(facts.iter_records(con))
    T["read_records"] = time.time() - t0
    t0 = time.time()
    F_pre = {r.patient: facts.facts_at_index(pre_p2(r)) for r in recs}
    T["facts_pre_p2"] = time.time() - t0
    del recs
    t0 = time.time()
    S = {p: strata.assign(f) for p, f in F_pre.items()}
    T["oracle_indication_and_strata"] = time.time() - t0

    groups = pd.DataFrame([{"PATIENT": p, "group": s.indication_row} for p, s in S.items()
                           if s.indication_row in patches.P2_GROUPS])
    t0 = time.time()
    if patches.p2_applied(con):
        add, assign = patches.p2_plan(con, seed, groups)
        stored = sorted(r[0] for r in con.execute(
            "select row_id from medications where patch = 'P2'").fetchall())
        if sorted(add.row_id) != stored:
            sys.exit("stored P2 rows differ from the recomputed plan; rebuild the database")
        p2_info = {"p2_rows_added": len(add), "already_applied": True}
    else:
        p2_info = patches.apply_p2(con, seed, groups)
        con.execute("checkpoint")
    T["p2"] = time.time() - t0
    assign = con.execute("select * from p2_assignment").df()
    n_ledger = patches.export_ledger(con, run / "augmentation_ledger.csv")

    t0 = time.time()
    p2_patients = sorted(assign.PATIENT[assign.assigned])
    F_post = dict(F_pre)
    for r in facts.iter_records(con, p2_patients):
        F_post[r.patient] = facts.facts_at_index(r)
    T["facts_p2_patients"] = time.time() - t0
    con.close()

    # Final labels (main variant) for every eligible patient, plus consistency checks.
    t0 = time.time()
    labels = {}
    problems = []
    for p, s in S.items():
        if s.stratum is None:
            continue
        lab = oracle.label(F_post[p])
        labels[p] = lab.status
        on = F_post[p].on_statin
        expect = oracle.ON_THERAPY if on else s.indication_status
        if lab.status != expect:
            problems.append((p, lab.status, expect))
    T["oracle_labels_eligible"] = time.time() - t0
    if problems:
        sys.exit(f"{len(problems)} eligible patients with an unexpected label, e.g. {problems[:3]}")

    # ---------------------------------------------------------- counts
    on_statin = {p: F_post[p].on_statin for p in S}
    def group_key(s):
        if s.indication_row:
            return s.indication_row
        if s.indication_status == oracle.CANNOT_DETERMINE:
            return oracle.CANNOT_DETERMINE
        return "/".join(s.row_set)  # label known, row varies with an unusable input

    rows = Counter(group_key(s) for s in S.values())
    rows_on = Counter(group_key(s) for p, s in S.items() if on_statin[p])
    per = {}
    for st in strata.STRATA:
        cand = [s for s in S.values() if s.candidate == st]
        elig = [s for s in cand if s.stratum == st]
        ex_any = Counter(e for s in cand for e in s.blocking())
        ex_only = Counter(s.blocking()[0] for s in cand if len(s.blocking()) == 1)
        on = sum(on_statin[s.patient] for s in elig)
        d = {"candidates": len(cand), "eligible": len(elig), "on": on, "off": len(elig) - on,
             "on_native": sum(s.on_statin_native for s in elig),
             "exclusions_any": dict(ex_any), "exclusions_only": dict(ex_only),
             "lipid_not_deciding": sum("lipid_panel" not in s.deciding for s in elig)}
        if st == "S1":
            types_off = Counter(s.ascvd_type for s in elig if not on_statin[s.patient])
            types_on = Counter(s.ascvd_type for s in elig if on_statin[s.patient])
            d["ascvd_types_off"] = dict(types_off)
            d["ascvd_types_on"] = dict(types_on)
            d["off_capacity_stroke_rule"] = capacity_untreated_s1(types_off)
        if st == "S4":
            d["bins"] = {b: {"on": sum(1 for s in elig if s.s4_bin == b and on_statin[s.patient]),
                             "off": sum(1 for s in elig if s.s4_bin == b and not on_statin[s.patient])}
                         for b in ("5-<10", ">=10")}
        per[st] = d

    cohort_ex = Counter(e for s in S.values() for e in s.exclusions)

    # P1-eGFR impact (PREVENT-needed: indication rows 5, 8-11).
    pn_rows = {"5", "8", "9", "10", "11a", "11b"}
    pn = [s for s in S.values() if s.indication_row in pn_rows]
    p1_impact = {
        "prevent_needed": len(pn),
        "p1_latest": sum(s.egfr_p1 for s in pn),
        "p1_row_dependent": sum(bool(s.p1_row_dependent) for s in pn),
        "p1_label_dependent_main": sum(bool(s.p1_label_dependent) for s in pn),
        "by_row": {r: {"n": sum(1 for s in pn if s.indication_row == r),
                       "p1": sum(1 for s in pn if s.indication_row == r and s.egfr_p1),
                       "dependent": sum(1 for s in pn if s.indication_row == r
                                        and s.p1_row_dependent)}
                   for r in ["5", "8", "9", "10", "11a", "11b"]},
    }
    for st in ("S4", "S5"):
        d = per[st]
        only = d["exclusions_only"].get("p1_egfr_dependent", 0)
        p1_impact[st] = {"eligible": d["eligible"],
                         "removed_by_p1_rule_any": d["exclusions_any"].get("p1_egfr_dependent", 0),
                         "removed_only_by_p1_rule": only,
                         "eligible_without_p1_rule": d["eligible"] + only}
    s4_lab_only = sum(1 for s in S.values() if s.candidate == "S4"
                      and s.blocking() == ("p1_egfr_dependent",) and not s.p1_label_dependent)
    p1_impact["S4"]["kept_if_rule_were_main_label_only"] = s4_lab_only

    # Gate (SPEC 5.1) and sampling needs.
    gate = {}
    for st, d in per.items():
        off = d["off_capacity_stroke_rule"] if st == "S1" else d["off"]
        checks = {"eligible>=50": d["eligible"] >= GATE_MIN_ELIGIBLE}
        if st != "S5":
            checks["on>=15"] = d["on"] >= GATE_MIN_ON_OFF
            checks["off>=15"] = off >= GATE_MIN_ON_OFF
        gate[st] = {"checks": checks, "pass": all(checks.values())}
    gate_pass = all(g["pass"] for g in gate.values())

    need = []
    for st, d in per.items():
        off = d["off_capacity_stroke_rule"] if st == "S1" else d["off"]
        items = [("eligible", d["eligible"], GATE_MIN_ELIGIBLE, TEST_N)]
        if st != "S5":
            items += [("on", d["on"], GATE_MIN_ON_OFF, TEST_HALF),
                      ("off" + (" (stroke rule)" if st == "S1" else ""), off,
                       GATE_MIN_ON_OFF, TEST_HALF)]
        if st == "S4":
            for b, c in d["bins"].items():
                items += [(f"{b} on", c["on"], None, TEST_S4_CELL),
                          (f"{b} off", c["off"], None, TEST_S4_CELL)]
        for what, n, tg, ts in items:
            row = {"stratum": st, "count": what, "n": n}
            for label, thr in (("gate", tg), ("test", ts)):
                pt, mg = needed_size(n, cohort_n, thr) if thr else (None, None)
                row[f"{label}_threshold"] = thr
                row[f"{label}_cohort_point"] = pt
                row[f"{label}_cohort_margin"] = mg
            need.append(row)

    out = {"set": name, "seed": seed, "cohort": cohort_n, "p2": p2_info,
           "ledger_rows": n_ledger, "indication_rows": dict(rows),
           "indication_rows_on_statin": dict(rows_on), "strata": per,
           "cohort_exclusions": dict(cohort_ex), "p1_impact": p1_impact, "gate": gate,
           "gate_pass": gate_pass, "size_needs": need,
           "seconds": {k: round(v, 1) for k, v in T.items()}}
    (run / "results").mkdir(exist_ok=True)
    (run / "results" / f"gate_{name}.json").write_text(json.dumps(out, indent=1))
    rows_df = pd.DataFrame([{**asdict(s), "stratum": s.stratum, "blocking": ";".join(s.blocking()),
                             "exclusions": ";".join(s.exclusions), "deciding": ";".join(s.deciding),
                             "on_statin": on_statin[s.patient], "label": labels.get(s.patient)}
                            for s in S.values()])
    rows_df.to_csv(run / "results" / "strata.csv", index=False)
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"gate_{name}.md").write_text(render(out))
    print(json.dumps({"set": name, "gate_pass": gate_pass, "seconds": out["seconds"],
                      "eligible": {k: (v["eligible"], v["on"], v["off"]) for k, v in per.items()}}))


def render(o: dict) -> str:
    L = []
    w = L.append
    w(f"# Day-0 gate: {o['set']}")
    w("")
    w("Generated by `scripts/gate.py` (SPEC 5.1). Aggregate counts only. Synthetic patients "
      "(Synthea v4.0.0); a designed test set, not prevalence.")
    w("")
    w(f"- Seed: {o['seed']}. Cohort (alive, aged 30-79 at index): {o['cohort']:,}.")
    p2 = o["p2"]
    w(f"- P2 statin rows added: {p2['p2_rows_added']}"
      + (" (already applied; plan recomputed and matched)" if p2.get("already_applied") else "")
      + f". Ledger rows after P2: {o['ledger_rows']:,}.")
    w(f"- **Gate: {'PASS' if o['gate_pass'] else 'FAIL'}** "
      "(every stratum >= 50 eligible; S1-S4 >= 15 on and >= 15 off a statin).")
    w("")
    w("## Strata")
    w("")
    w("| Stratum | Candidates | Eligible | On statin | Off statin | Gate |")
    w("|---|---:|---:|---:|---:|---|")
    for st, d in o["strata"].items():
        g = o["gate"][st]
        off = str(d["off"])
        if st == "S1":
            off += f" (usable with stroke rule: {d['off_capacity_stroke_rule']})"
        fails = [k for k, v in g["checks"].items() if not v]
        w(f"| {st} | {d['candidates']} | {d['eligible']} | {d['on']} | {off} | "
          f"{'pass' if g['pass'] else 'FAIL: ' + ', '.join(fails)} |")
    w("")
    w("Candidates: patients whose indication group (first matching row, row 0 ignored, before "
      "P2) defines the stratum: S1 row 1, S2 row 2, S3 row 3, S4 rows 8-9, S5 rows 7 and 11b. "
      "On statin: row 0 after P2 (native in S1; P2 in S2-S4).")
    w("")
    s4 = o["strata"]["S4"]["bins"]
    w("S4 by 10-year risk: " + "; ".join(f"{b}: {c['on']} on, {c['off']} off" for b, c in s4.items())
      + ".")
    s1 = o["strata"]["S1"]
    fmt = lambda c: ", ".join(f"{k} {v}" for k, v in sorted(c.items())) or "none"
    w("")
    w("Eligible patients whose lipid panel is not deciding (a lipid-panel deletion (C1) or an "
      "LDL-C threshold twin (C5) cannot change their label): "
      + "; ".join(f"{k} {d['lipid_not_deciding']}" for k, d in o["strata"].items()) + ".")
    w("")
    w(f"S1 untreated by ASCVD type: {fmt(s1['ascvd_types_off'])}. Treated: "
      f"{fmt(s1['ascvd_types_on'])}. With at most half stroke-only, "
      f"{s1['off_capacity_stroke_rule']} untreated S1 patients are usable.")
    w("")
    w("## Not sampled")
    w("")
    w("Per stratum: patients of the candidate group removed by each rule (a patient can have "
      "several; 'only' = removed by this rule alone).")
    w("")
    w("| Rule | " + " | ".join(o["strata"]) + " |")
    w("|---|" + "---:|" * len(o["strata"]))
    for e, (_, desc) in strata.EXCLUSIONS.items():
        cells = []
        for st, d in o["strata"].items():
            a, b = d["exclusions_any"].get(e, 0), d["exclusions_only"].get(e, 0)
            cells.append(f"{a} ({b} only)" if a else "0")
        if any(c != "0" for c in cells):
            w(f"| {desc} | " + " | ".join(cells) + " |")
    w("")
    w("Whole cohort, by rule (any group):")
    w("")
    w("| Rule | Patients |")
    w("|---|---:|")
    for e, (_, desc) in strata.EXCLUSIONS.items():
        w(f"| {desc} | {o['cohort_exclusions'].get(e, 0)} |")
    w("")
    w("## Indication groups (whole cohort, before P2)")
    w("")
    w("| Row | Patients | On statin after P2 |")
    w("|---|---:|---:|")
    order = list(oracle.ROW_BY_ID)
    order += sorted(k for k in o["indication_rows"] if k not in order)
    for r in order:
        if r in o["indication_rows"]:
            w(f"| {r} | {o['indication_rows'][r]} | {o['indication_rows_on_statin'].get(r, 0)} |")
    w("")
    w("## P1 eGFR rule")
    w("")
    p = o["p1_impact"]
    w(f"PREVENT-needed patients (indication rows 5, 8-11): {p['prevent_needed']}; latest eGFR "
      f"imputed by P1: {p['p1_latest']}; indication row changes somewhere in eGFR 60-140: "
      f"{p['p1_row_dependent']} (main label changes: {p['p1_label_dependent_main']}).")
    w("")
    w("| Row | Patients | P1 latest eGFR | Row depends on P1 eGFR |")
    w("|---|---:|---:|---:|")
    for r, c in p["by_row"].items():
        w(f"| {r} | {c['n']} | {c['p1']} | {c['dependent']} |")
    w("")
    for st in ("S4", "S5"):
        c = p[st]
        w(f"- {st}: eligible {c['eligible']}; removed by the rule {c['removed_by_p1_rule_any']} "
          f"({c['removed_only_by_p1_rule']} by it alone); eligible without the rule "
          f"{c['eligible_without_p1_rule']}.")
    w(f"- S4 patients removed only because the risk bin (row 8 vs 9) moves, not the main label: "
      f"{p['S4']['kept_if_rule_were_main_label_only']}.")
    w("")
    w("## Cohort size needed")
    w("")
    w("Cohort size at which the expected count reaches the threshold ('point'), and with a "
      "two-SD Poisson margin ('margin'), scaled from this set. Gate thresholds: 50 eligible, "
      "15 on, 15 off. Test sampling: 40 per stratum, 20 on and 20 off, S4 10 per risk bin and "
      "statin status.")
    w("")
    w("| Stratum | Count | n here | Gate point | Gate margin | Test point | Test margin |")
    w("|---|---|---:|---:|---:|---:|---:|")
    f = lambda v: "-" if v is None else f"{v:,}"
    for r in o["size_needs"]:
        w(f"| {r['stratum']} | {r['count']} | {r['n']} | {f(r['gate_cohort_point'])} | "
          f"{f(r['gate_cohort_margin'])} | {f(r['test_cohort_point'])} | "
          f"{f(r['test_cohort_margin'])} |")
    worst = max((r["test_cohort_margin"] or 0) for r in o["size_needs"])
    worst_g = max((r["gate_cohort_margin"] or 0) for r in o["size_needs"])
    w("")
    w(f"Largest need: gate with margin {worst_g:,} cohort adults; test sampling with margin "
      f"{worst:,} cohort adults.")
    w("")
    w("## Run time (seconds)")
    w("")
    for k, v in o["seconds"].items():
        w(f"- {k}: {v}")
    w("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
