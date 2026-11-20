"""List candidate failure cases for the author to read: a starting point, not a rule.

The protocol has no selection rule for the examples shown with the results:
they are chosen by the author and labelled as such, and all outputs are
released (PROTOCOL.md sec. 10). This script gives a reproducible first list of
incorrect cases to read, so that the choice starts from the weakest
conditions rather than from wherever the author happened to look.

Usage:
  python scripts/select_failures.py --cases data/cases/<set> --run data/runs/<run1> \
      [--out FILE]

Writes <run>/failures.json by default. No model output is read except the
status that decides correctness.

Candidates. Rank the conditions by A2 accuracy in run 1, lowest first
(ties in the order C0, C1, C2a, C2b, C3a, C3b, C4, C5). From each of the
first three conditions that have an incorrect A2 case, take the incorrect
case with the smallest case id. If fewer than three conditions have one, the
remaining places are filled by the same rule for A1, then L1. If there are
still fewer than three, the output says how many there were.
  Details:
  - Accuracy and correctness are those of `gapcheck.score` (main analysis,
    the cases the run manifest lists); a case with no record or no valid
    output is incorrect.
  - "Smallest case id" is the smallest string; ids are "case_" + 12 hex
    digits, so this is hexadecimal order.
  - Each system contributes at most one case per condition. A case id
    already taken for an earlier system is skipped, and the next smallest
    incorrect case of that condition is taken instead (so a fallback system
    may show a condition already shown for A2, but never the same case).

Example record (a candidate too). Among C1 cases whose deleted input is the lipid panel
(variant "lipid_panel"), the one with the smallest case id, shown with its
base record (the same patient's C0 case), whatever the systems answered.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gapcheck import score
from gapcheck.edits import CONDITIONS

N_FAILURES = 3
SYSTEM_ORDER = ("A2", "A1", "L1")
EXAMPLE_VARIANT = "lipid_panel"
RULE_FAILURES = (
    "Rank the conditions by A2 accuracy in run 1, lowest first (ties in the order C0, C1, "
    "C2a, C2b, C3a, C3b, C4, C5). From each of the first three conditions that have an "
    "incorrect A2 case, show the incorrect case with the smallest case id. If fewer than "
    "three conditions have one, the remaining places are filled by the same rule for A1, "
    "then L1.")
RULE_EXAMPLE = ("The C1 case with the smallest case id among those whose lipid panel was "
                "deleted, shown with its base record, whatever the systems answered.")


def condition_accuracy(case_list: list[dict], recs: dict[str, dict]) -> dict[str, dict]:
    """{condition: {k, n, est}} for one system (est None if the condition has no case)."""
    out = {k: {"k": 0, "n": 0, "est": None} for k in CONDITIONS}
    for c in case_list:
        a = out[c["condition"]]
        a["n"] += 1
        a["k"] += int(score.correct(c, recs.get(c["case_id"])))
    for a in out.values():
        if a["n"]:
            a["est"] = a["k"] / a["n"]
    return out


def _answer(rec: dict | None) -> dict:
    if rec is None:
        return {"record": False, "schema_valid": False, "status": None, "benefit_group": None,
                "missing": None, "error": None, "forced_fallback": False}
    o = rec.get("output") if rec.get("schema_valid") else None
    return {"record": True, "schema_valid": bool(rec.get("schema_valid")),
            "status": o.get("status") if o else None,
            "benefit_group": o.get("benefit_group") if o else None,
            "missing": o.get("missing") if o else None,
            "error": rec.get("error"), "forced_fallback": bool(rec.get("forced_fallback"))}


def _case_view(c: dict) -> dict:
    return {"case_id": c["case_id"], "condition": c["condition"], "variant": c["variant"],
            "base_patient": c["base_patient"], "stratum": c["stratum"],
            "expected_status": c["expected_status"],
            "expected_missing": c.get("expected_missing", [])}


def failure_cases(case_list: list[dict], run: dict[str, dict[str, dict]],
                  n: int = N_FAILURES, systems=SYSTEM_ORDER) -> list[dict]:
    """Candidate failure cases, weakest condition first."""
    picked: list[dict] = []
    taken: set[str] = set()
    for s in systems:
        if len(picked) >= n:
            break
        if s not in run:
            continue
        recs = run[s]
        acc = condition_accuracy(case_list, recs)
        for k in score.rank_conditions(acc):
            if len(picked) >= n:
                break
            wrong = sorted(c["case_id"] for c in case_list
                           if c["condition"] == k and not score.correct(c, recs.get(c["case_id"])))
            wrong = [cid for cid in wrong if cid not in taken]
            if not wrong:
                continue
            cid = wrong[0]
            c = next(x for x in case_list if x["case_id"] == cid)
            taken.add(cid)
            picked.append({"place": len(picked) + 1, "system": s, "condition": k,
                           "condition_accuracy": acc[k], **_case_view(c),
                           "answer": _answer(recs.get(cid))})
    return picked


def example_case(case_list: list[dict], run: dict[str, dict[str, dict]] | None = None
                 ) -> dict | None:
    """A candidate example record: smallest-id C1 case with the lipid panel deleted."""
    c1 = sorted((c for c in case_list
                 if c["condition"] == "C1" and c["variant"] == EXAMPLE_VARIANT),
                key=lambda c: c["case_id"])
    if not c1:
        return None
    c = c1[0]
    base = [x for x in case_list if x["condition"] == "C0" and x["base_patient"] == c["base_patient"]]
    out = {**_case_view(c), "base_case_id": base[0]["case_id"] if base else None,
           "base_expected_status": base[0]["expected_status"] if base else None}
    if run is not None:
        out["answers"] = {s: _answer(recs.get(c["case_id"])) for s, recs in sorted(run.items())}
    return out


def select(case_list: list[dict], run: dict[str, dict[str, dict]]) -> dict:
    f = failure_cases(case_list, run)
    return {"rules": {"failure_cases": RULE_FAILURES, "example_record": RULE_EXAMPLE},
            "systems_in_run": sorted(run),
            "failure_cases": f, "n_failure_cases": len(f),
            "fewer_than_three": len(f) < N_FAILURES,
            "example_record": example_case(case_list, run)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    case_list = score.scored_cases(a.cases, a.run)
    run = score.load_run(a.run)
    out = select(case_list, run)
    out.update({"case_set": Path(a.cases).name, "run": Path(a.run).name})
    p = Path(a.out) if a.out else Path(a.run) / "failures.json"
    p.write_text(json.dumps(out, indent=1) + "\n")
    for f in out["failure_cases"]:
        print(f["place"], f["system"], f["condition"], f["case_id"],
              f"{f['condition_accuracy']['k']}/{f['condition_accuracy']['n']}")
    if out["fewer_than_three"]:
        print(f"only {out['n_failure_cases']} failure case(s)")
    ex = out["example_record"]
    print("example", ex["case_id"] if ex else None, "base", ex["base_case_id"] if ex else None)
    print("wrote", p)


if __name__ == "__main__":
    main()
