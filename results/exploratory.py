"""Exploratory measures E1-E3 for L1, A1 and A2 of a run (not pre-specified: no hypothesis,
no verdict), with the frozen scorer's scored cases, validity and correctness rules (main
variant) and cluster-bootstrap resamples (src/gapcheck/score.py). No scored number changes.
E1 row agreement: valid, not forced outputs whose benefit_group is the oracle row. E2 latest
entry: DOCUMENTED lipid_panel, sbp, smoking and egfr claims citing only rows of that input
dated T, the timestamp the oracle reads. E3: C1 and C3a cases correct together with the same
patient's C0 case.

usage: python results/exploratory.py --cases <case set> --run <run dir> [--out <file>]
       python results/exploratory.py --inputs-only --cases <case set> [--out <file>]
The second form reads no run and prints only counts of the case set: E1 cases left out per
condition and row-0 cases; per E2 input, cases with no T and cases whose oracle fact is tied.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from gapcheck import cases as CS, codes as C, facts, schema as S, score  # noqa: E402

SYSTEMS = ("L1", "A1", "A2")                      # R0 and R1 are skipped
E1_CONDITIONS = ("C0", "C2a", "C2b", "C3a", "C3b", "C4", "C5")
CLAIMS = facts.OPEN_WORLD_INPUTS                  # lipid_panel, sbp, smoking, egfr
KINDS = ("no_entry", "not_a_row", "latest", "mixed", "another_date")
PAIRED = ("C1", "C3a")
# The observation codes the oracle reads per input, as facts.facts_at_index and facts._lipids
# select them (codes.py constants; TG is not part of the panel); eGFR also by unit below.
INPUT_CODES = {"lipid_panel": (C.TC.code, C.HDL.code, C.LDL.code), "sbp": (C.SBP.code,),
               "smoking": (C.SMOKING.code,), "egfr": (C.EGFR.code,)}


def fail(msg: str) -> None:   # a shape the definitions do not cover: ids only, no content
    raise SystemExit(f"exploratory.py: {msg}")


def rate(design: score.Design, mask: np.ndarray, hit: np.ndarray, W: np.ndarray) -> dict:
    """Case-level rate (score._boot_prop) with the one-point-interval flag."""
    return _flag(score._boot_prop(design, mask, hit, W))


def claim_rate(design: score.Design, k: np.ndarray, n: np.ndarray, W: np.ndarray) -> dict:
    """Claim-level rate, summed per cluster as score.secondary does for unsupported claims."""
    k_cl, n_cl = np.zeros(design.n.shape[0]), np.zeros(design.n.shape[0])
    np.add.at(k_cl, design.case_cl, k)
    np.add.at(n_cl, design.case_cl, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = (W @ k_cl) / (W @ n_cl)
    return _flag({**score._prop(int(k.sum()), int(n.sum())),
                  "ci": score._ci(boot) if n.sum() > 0 else [None, None]})


def _flag(r: dict) -> dict:
    r["interval_degenerate"] = r["ci"][0] is not None and r["ci"][0] == r["ci"][1]
    return r


def output_of(rec: dict | None, where: str) -> dict | None:
    """The output of a valid record (score.status_of), else None; stops on a shape not covered."""
    if score.status_of(rec) is None:
        return None
    o = rec["output"]
    if "benefit_group" not in o or not isinstance(o.get("evidence"), list):
        fail(f"{where}: valid output without benefit_group or an evidence list")
    if any(not isinstance(it, dict) or not {"claim", "support", "source"} <= set(it)
           for it in o["evidence"]):
        fail(f"{where}: evidence item without claim, support or source")
    return o


def case_inputs(case_dir: str, case: dict) -> dict:
    """Per E2 input: T (the oracle's timestamp, or None), the input's rows {row_id: DATE},
    the rows the oracle's fact uses, and whether that fact is tied (n_tied > 1)."""
    cid, pid = case["case_id"], case["base_patient"]
    con = CS.materialize(case_dir, cid)
    try:
        # DEFS: cases.oracle_on_case reads the patient from the case's delta; materialize puts
        # that patient in `cohort`. Stop if it is not the case's base_patient.
        if [r[0] for r in con.execute("select PATIENT from cohort").fetchall()] != [pid]:
            fail(f"{cid}: record patient is not base_patient")
        obs = facts.load_record(con, pid).observations     # the rows patient_facts reads
        f = facts.patient_facts(con, pid)
    finally:
        con.close()
    fact = {"lipid_panel": f.lipid_panel, "sbp": f.sbp, "egfr": f.egfr,
            "smoking": None if f.smoking is None else f.smoking.obs}
    out = {}
    for name in CLAIMS:
        # DEFS: the input's rows by code (and eGFR unit) only, as defs.md says: no on-or-before-
        # index or numeric filter, so such a row is an input row (not dated T: counted by date)
        d = obs[obs.CODE.isin(INPUT_CODES[name])]
        if name == "egfr":
            d = d[d.UNITS == C.EGFR_UNIT_KEEP]
        if d.row_id.duplicated().any():
            fail(f"{cid}: duplicate row_id among the {name} rows")
        x, lp = fact[name], name == "lipid_panel"
        out[name] = {"T": None if x is None else x.timestamp, "rows": dict(zip(d.row_id, d.DATE)),
                     "used": set() if x is None else set(x.row_ids) if lp else {x.row_id},
                     "tied": x is not None and (x.tied if lp else x.n_tied > 1)}
    return out


def kind_of(item: dict, inp: dict) -> str:
    """E2 kind of one item: the first of KINDS that applies."""
    if inp["T"] is None:
        return "no_entry"
    src = item["source"]
    if not isinstance(src, list) or not src:
        return "not_a_row"
    # DEFS: a cited id that is not a string is "not one of the input's rows".
    if any(not isinstance(x, str) or x not in inp["rows"] for x in src):
        return "not_a_row"
    # dated T: the row's DATE string is T, the DATE string facts.py keeps (no DATE: not dated T)
    at_T = [isinstance(inp["rows"][x], str) and inp["rows"][x] == inp["T"] for x in src]
    return "latest" if all(at_T) else "mixed" if any(at_T) else "another_date"


def e1(design, cases, recs, ok, W) -> dict:
    cond = np.array([c["condition"] for c in cases])
    exp = [c["expected_row"] for c in cases]
    elig = np.array([e is not None for e in exp], dtype=bool)
    # DEFS: by_condition never lists C1 (the oracle row of a C1 case is None). A C1 case with a
    # row would enter `pooled` but no condition; stop instead.
    if (elig & (cond == "C1")).any():
        fail("a C1 case has an expected_row")
    forced = np.array([bool((recs.get(c["case_id"]) or {}).get("forced_fallback")) for c in cases])
    agree = np.zeros(len(cases), dtype=bool)
    for i, c in enumerate(cases):
        o = output_of(recs.get(c["case_id"]), c["case_id"])
        agree[i] = elig[i] and o is not None and not forced[i] \
            and o["benefit_group"] == c["expected_row"]
    two = {f"{'correct' if a else 'wrong'}_{'agree' if b else 'disagree'}":
           int((elig & (ok == a) & (agree == b)).sum())
           for a in (True, False) for b in (True, False)}
    return {"by_condition": {k: rate(design, elig & (cond == k), agree, W)
                             for k in E1_CONDITIONS if (elig & (cond == k)).any()},
            "pooled": rate(design, elig, agree, W), "two_by_two": two,
            "excluded": {k: int((~elig & (cond == k)).sum()) for k in sorted(set(cond))},
            "row0": sum(1 for e in exp if e == "0"), "forced": int((elig & forced).sum())}


def e2(design, cases, recs, inputs, W) -> dict:
    k, n, tie = np.zeros(len(cases)), np.zeros(len(cases)), 0
    by_kind, by_claim = dict.fromkeys(KINDS, 0), {c: {"n": 0, "latest": 0} for c in CLAIMS}
    for i, c in enumerate(cases):
        o = output_of(recs.get(c["case_id"]), c["case_id"])
        if o is None:                     # items of invalid outputs do not exist
            continue
        for it in o["evidence"]:          # forced fallbacks keep their evidence: counted
            if it["support"] != S.DOCUMENTED or it["claim"] not in CLAIMS:
                continue
            inp = inputs[c["case_id"]][it["claim"]]
            kind = kind_of(it, inp)
            by_kind[kind] += 1
            by_claim[it["claim"]]["n"] += 1
            n[i] += 1
            if kind == "latest":
                k[i] += 1
                by_claim[it["claim"]]["latest"] += 1
                # DEFS: any cited row dated T that the oracle's fact does not use, also a
                # same-value duplicate (n_tied counts distinct values, so it may be 1)
                tie += any(x not in inp["used"] for x in it["source"])
    return {"rate": claim_rate(design, k, n, W), "by_kind": by_kind,
            "latest_not_oracle_row": tie, "by_claim": by_claim}


def pairs_of(cases: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """(edited, C0) case indices per C1 and C3a, linked by base_patient as score.case_flags
    links them; stops if there is no C0 case or its expected status is the same."""
    c0 = {}
    for j, c in enumerate(cases):
        if c["condition"] == "C0":
            # DEFS: "the C0 case" of a patient must be unique (case_flags would keep the last)
            if c["base_patient"] in c0:
                fail(f"two C0 cases for the base patient of {c['case_id']}")
            c0[c["base_patient"]] = j
    out = {x: [] for x in PAIRED}
    for i, c in enumerate(cases):
        if c["condition"] in PAIRED:
            j = c0.get(c["base_patient"])
            if j is None or cases[j]["expected_status"] == c["expected_status"]:
                fail(f"{c['case_id']}: no C0 case, or the same expected_status as its C0 case")
            out[c["condition"]].append((i, j))
    return out


def e3(design, cases, ok, pairs, W) -> dict:
    cond = np.array([c["condition"] for c in cases])
    out = {}
    for x in PAIRED:                      # DEFS: both always reported (n 0 if no such case)
        hit = np.zeros(len(cases), dtype=bool)
        two = {f"c0_{a}_edited_{b}": 0 for a in ("correct", "wrong") for b in ("correct", "wrong")}
        for i, j in pairs[x]:
            hit[i] = ok[i] and ok[j]
            two["c0_%s_edited_%s" % ("correct" if ok[j] else "wrong",
                                     "correct" if ok[i] else "wrong")] += 1
        out[x] = {"rate": rate(design, cond == x, hit, W), "two_by_two": two}
    return out


def exploratory(case_dir: str, run_dir: str) -> dict:
    cases = score.scored_cases(case_dir, run_dir)
    design = score.Design(cases)
    W = design.weights(score.N_BOOT, score.BOOT_SEED)      # the resamples score.score() uses
    run, pairs = score.load_run(run_dir), pairs_of(cases)
    inputs = {c["case_id"]: case_inputs(case_dir, c) for c in cases}
    out = {"case_set": Path(case_dir).name, "run": Path(run_dir).name,
           "n_boot": int(W.shape[0]), "seed": score.BOOT_SEED, "systems": {}}
    for name in (s for s in SYSTEMS if s in run):
        recs = run[name]
        ok = np.array([score.correct(c, recs.get(c["case_id"])) for c in cases], dtype=bool)
        out["systems"][name] = {"E1": e1(design, cases, recs, ok, W),
                                "E2": e2(design, cases, recs, inputs, W),
                                "E3": e3(design, cases, ok, pairs, W)}
    if not out["systems"]:
        fail(f"no L1, A1 or A2 records in {run_dir}")
    return out


def inputs_only(case_dir: str) -> dict:
    """Counts of the case set alone (no run); nothing else from any record is printed.
    DEFS: all cases of the set (there is no run manifest to narrow them)."""
    cases = CS.read_cases(case_dir)
    pairs = pairs_of(cases)               # runs the E3 stops on the whole case set
    e2c = {x: {"no_entry": 0, "tied": 0} for x in CLAIMS}
    for c in cases:
        for x, inp in case_inputs(case_dir, c).items():
            e2c[x]["no_entry"] += inp["T"] is None
            e2c[x]["tied"] += bool(inp["tied"])
    excl = {k: sum(1 for c in cases if c["condition"] == k and c["expected_row"] is None)
            for k in sorted({c["condition"] for c in cases})}
    return {"case_set": Path(case_dir).name, "n_cases": len(cases), "E2": e2c,
            "E1": {"excluded": excl, "row0": sum(1 for c in cases if c["expected_row"] == "0")},
            "E3": {x: len(pairs[x]) for x in PAIRED}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", required=True)
    ap.add_argument("--run")
    ap.add_argument("--inputs-only", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.inputs_only == bool(a.run):
        ap.error("give --run, or --inputs-only without --run")
    res = inputs_only(a.cases) if a.inputs_only else exploratory(a.cases, a.run)
    text = json.dumps(res, indent=1, sort_keys=True) + "\n"
    if a.out:
        Path(a.out).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
