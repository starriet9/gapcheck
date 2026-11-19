"""Build the data of the page's twin-patient card (site/card.json).

Usage:
  python scripts/site_card.py [--cases data/cases/<set>] [--runs data/runs] \
      [--out site/card.json]

The card has three states, all built from files, none typed by hand:
  1. a base patient's original record (condition C0),
  2. the same patient with the deciding input deleted (its C1 twin),
  3. a real agent answer that the code verifier caught and the agent repaired.

Selection rules (also written into card.json, so the page can show them):
  Pair (states 1-2): the C1 twin with the smallest case id among those whose
    lipid panel was deleted, with its base patient's C0 case: the rule of
    PROTOCOL.md sec. 10 ("Example record on the page"), applied to the
    development set. --gap-only adds "whose original record is labelled GAP"
    (a choice for the author; the protocol rule has no such condition).
  Caught failure (state 3): among A2 records in real (non-mock) runs from a
    clean commit, the smallest case id whose first verifier round failed and
    whose repair round passed (no forced fallback). If no clean-commit run
    has one, runs from a dirty tree are used and flagged.

States 1-2 show the record as the answer key reads it: the oracle on the
case, via the R0 baseline's output, which cites a record row for every input
(R0 is the oracle on structured data; C0 and C1 cases carry no note reasons,
so here the two agree). If a real model trace exists for the case, the
model's own answer is shown beside it. State 3 shows the model's claims.

Only synthetic (Synthea) records are read. The output holds row ids, values
and dates of one or two synthetic patients, no note text and no paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from gapcheck import baselines, cases as CS, facts, oracle

ROOT = Path(__file__).resolve().parents[1]
CASE_SET = "data/cases/dev_s20261120_utc_n10"
SYSTEM_PREF = ("A2", "A1", "L1")

RULE_PAIR = ("Pair: the C1 twin with the smallest case id among those whose lipid panel was "
             "deleted, shown with its original record, whatever any system answered (the "
             "rule PROTOCOL.md sets for the test set, applied here to the development set). "
             "Case ids are hashes, so their order is arbitrary.")
RULE_PAIR_GAP = ("Pair: the C1 twin with the smallest case id among those whose lipid panel "
                 "was deleted and whose original record is labelled GAP, shown with its "
                 "original record, whatever any system answered.")
RULE_CAUGHT = ("Caught failure: among real local-model A2 runs from a clean commit, the "
               "smallest case id whose first verifier round failed and whose single repair "
               "round passed.")

CHECKS = {  # SPEC 6.2, one line each
    1: "a cited record row must exist and hold the cited value",
    2: "every input the stated row needs must be covered",
    3: "a computed risk must match a recomputation from its cited inputs",
    4: "an input listed as missing, or claimed absent, must really be absent",
}
LABEL = {
    "lipid_panel": "Lipid panel", "sbp": "Systolic BP", "smoking": "Smoking status",
    "egfr": "eGFR", "statins": "Statin", "bp_drugs": "BP medication",
    "ascvd": "Clinical ASCVD", "diabetes": "Diabetes", "ckd": "CKD stage 3-4",
    "reason": "Reason not to prescribe (365 days)", "risk_10y": "10-year ASCVD risk",
    "risk_30y": "30-year ASCVD risk", "hiv": "HIV", "esrd": "ESRD, dialysis, transplant",
    "row5_features": "Diabetes complications", "age": "Age", "sex": "Sex",
}
MISSING_TEXT = {
    "lipid_panel": "Lipid panel: 10-year risk cannot be computed.",
    "sbp": "Systolic BP: 10-year risk cannot be computed.",
    "smoking": "Smoking status: 10-year risk cannot be computed.",
    "egfr": "eGFR: 10-year risk cannot be computed.",
}
STATUS_TEXT = {s: s.replace("_", " ") for s in oracle.STATUSES}


# ---------------------------------------------------------------- helpers
def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return p.name


def num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:g}"


def row_text(row: str | None) -> str | None:
    if row is None:
        return None
    r = oracle.ROW_BY_ID[row]
    # the class of a statin-start recommendation; NOT_INDICATED rows have none
    # (row 11b's COR 1 is for health behavior counseling, not a statin)
    cor = (next((s.cor for s in r.sources if s.role == "basis" and s.cor), None)
           if r.label in (oracle.GAP, oracle.DISCUSS) else None)
    group = r.group.replace(">=", "≥").replace("<=", "≤")
    return f"row {row}: {group}" + (f" (COR {cor})" if cor else "")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def real_runs(runs_dir: Path, case_set: str) -> list[dict]:
    """Every non-mock run on `case_set` under runs_dir (any depth)."""
    out = []
    for m in sorted(runs_dir.rglob("manifest.json")):
        try:
            man = json.loads(m.read_text())
        except json.JSONDecodeError:
            continue
        cs = (man.get("case_set") or {}).get("path", "")
        be = man.get("backend") or {}
        if Path(cs).name != Path(case_set).name or be.get("backend") in (None, "mock"):
            continue
        systems = {}
        for s in man.get("systems", []):
            f = m.parent / f"{s}.jsonl"
            if f.exists():
                systems[s] = {r["case_id"]: r for r in read_jsonl(f)}
        if not any(systems.values()):
            continue
        out.append({"dir": m.parent, "manifest": man, "systems": systems,
                    "clean": not (man.get("git") or {}).get("dirty", True)})
    return out


def model_display(be: dict) -> str:
    """'unsloth/Qwen3.8-Flash-Next-GGUF@<rev> UD-Q4_K_XL; ...' -> 'Qwen3.8-Flash-Next, 4-bit GGUF'."""
    rev = be.get("revision") or ""
    m = re.match(r"[^/]+/([^@ ]+?)(-GGUF)?@\S+\s*([^;]*)", rev)
    if not m:
        return be.get("model") or "unknown model"
    name, gguf, quant = m.group(1), m.group(2), m.group(3)
    bits = re.search(r"Q(\d)", quant or "")
    tail = ", ".join(x for x in ((f"{bits.group(1)}-bit" if bits else "") + (" GGUF" if gguf else ""),
                                 "CPU" if "CPU" in rev else "") if x.strip())
    return f"{name}, {tail}" if tail else name


def run_meta(run: dict) -> dict:
    man = run["manifest"]
    be = man["backend"]
    return {"run_id": man.get("run_id"), "model_display": model_display(be), "commit": (man.get("git") or {}).get("commit"),
            "dirty": (man.get("git") or {}).get("dirty"), "model": be.get("model"),
            "revision": be.get("revision"), "temperature": be.get("temperature"),
            "prompts_sha256": man.get("prompts_sha256"),
            "conventions_sha256": man.get("conventions_sha256"),
            "finished_utc": man.get("finished_utc")}


def traces_for(runs: list[dict], cid: str) -> list[tuple[dict, str, dict]]:
    """(run, system, record) for every real trace of a case, preferred first."""
    out = []
    for run in sorted(runs, key=lambda r: (not r["clean"], r["manifest"].get("run_id", ""))):
        for s in SYSTEM_PREF:
            rec = run["systems"].get(s, {}).get(cid)
            if rec is not None and rec.get("output") is not None:
                out.append((run, s, rec))
    return out


# ---------------------------------------------------------------- record lines
def lookup(tables: dict, row_id: str) -> dict | None:
    for t in tables.values():
        if "row_id" in t.columns:
            hit = t[t.row_id == row_id]
            if len(hit):
                return hit.iloc[0].to_dict()
    return None


def date_of(r: dict | None) -> str | None:
    if r is None:
        return None
    for k in ("DATE", "START"):
        if k in r and isinstance(r[k], str):
            return r[k][:10]
    return None


def value_text(name: str, value, tables: dict, sources) -> str:
    if name == "lipid_panel":
        return f"LDL-C {num(value['ldl'])} mg/dL (TC {num(value['tc'])}, HDL-C {num(value['hdl'])})"
    if name == "sbp":
        return f"{num(value)} mm Hg"
    if name == "egfr":
        return f"{num(value)} mL/min/1.73 m²"
    if name == "smoking":
        return str(value).replace(" (finding)", "")
    if name in ("risk_10y", "risk_30y"):
        return f"{100 * float(value):.1f}%"
    if name == "age":
        return str(value)
    if name == "sex":
        return {"M": "male", "F": "female"}.get(value, str(value))
    if isinstance(sources, list) and sources:
        r = lookup(tables, sources[0])
        if r is not None and "DESCRIPTION" in r:
            d = str(r["DESCRIPTION"]).split(" (")[0]
            more = f" + {len(sources) - 1} more rows" if len(sources) > 1 else ""
            return f"{d}{more}"
    return "recorded"


def line(name: str, item: dict | None, tables: dict) -> dict:
    """One 'What matters' line from an evidence item (None = not in the answer)."""
    if item is None:
        return {"name": name, "label": LABEL[name], "value": None, "support": None,
                "source": None, "date": None}
    sup, src = item["support"], item["source"]
    date = item.get("date")
    if sup == "DOCUMENTED" and isinstance(src, list) and src and src[0] != "demographics":
        date = date or date_of(lookup(tables, src[0]))
    if sup == "ABSENT":
        value = "none on record"
        source = src
    elif sup == "COMPUTED":
        value = value_text(name, item["value"], tables, src)
        source = "PREVENT equations"
    else:
        value = value_text(name, item["value"], tables, src)
        source = src
    return {"name": name, "label": LABEL.get(name, name), "value": value, "support": sup,
            "source": source, "date": date}


def evidence_map(output: dict) -> dict[str, dict]:
    m = {}
    for it in output["evidence"]:
        m.setdefault(it["claim"], it)
    return m


# ---------------------------------------------------------------- states
def agent_block(run: dict, system: str, rec: dict, expected: dict) -> dict:
    o = rec["output"]
    v = rec.get("verifier") or []
    return {
        "system": system, "run_id": run["manifest"].get("run_id"),
        "status": o["status"], "row": o.get("benefit_group"),
        "row_text": row_text(o.get("benefit_group")) if o.get("benefit_group") in oracle.ROW_BY_ID else None,
        "missing": o.get("missing", []),
        "same_as_answer_key": (o["status"] == expected["status"] and
                               (expected["status"] != "CANNOT_DETERMINE" or
                                set(expected["missing"]) <= set(o.get("missing", [])))),
        "verifier_rounds": [{"ok": r["ok"], "failed_checks": sorted({f["check"] for f in r["failures"]})}
                            for r in v],
        "forced_fallback": rec.get("forced_fallback"),
        "n_claims": len(o["evidence"]),
    }


def record_state(case_dir: str, case: dict, shown: list[str], runs: list[dict],
                 deleted: set[str] | None = None) -> dict:
    con = CS.materialize(case_dir, case["case_id"])
    f = facts.patient_facts(con, case["base_patient"])
    con.close()
    key = baselines.r0(f)
    tables, _, delta = CS.case_tables(case_dir, case["case_id"])
    ev = evidence_map(key)
    lines = []
    for name in shown:
        ln = line(name, ev.get(name), tables)
        if deleted and name in deleted:
            ln["deleted"] = True
        lines.append(ln)
    expected = {"status": case["expected_status"], "row": case["expected_row"],
                "missing": case["expected_missing"]}
    tr = traces_for(runs, case["case_id"])
    return {
        "case_id": case["case_id"], "condition": case["condition"], "variant": case["variant"],
        "patient": {"sex": f.sex, "age": f.age, "index_date": f.index_date.isoformat()},
        "answer_key": {**expected, "row_text": row_text(case["expected_row"]),
                       "risk_10y": key["risk_10y"]},
        "lines": lines,
        "all_lines": [line(n, it, tables) for n, it in ev.items()],
        "missing_text": [MISSING_TEXT[m] for m in case["expected_missing"] if m in MISSING_TEXT],
        "agent": agent_block(*tr[0], expected) if tr else None,
        "deleted_rows": sum(len(v) for v in delta["delete"].values()),
        "scrubbed_notes": len(delta["notes"]) if case["condition"] == "C1" else 0,
    }


def shown_inputs(key_output: dict) -> list[str]:
    """Up to five lines: the deciding inputs first, then what makes it a gap."""
    ev = evidence_map(key_output)
    order = ["lipid_panel", "risk_10y", "ascvd", "diabetes", "ckd", "sbp", "smoking",
             "egfr", "statins", "reason"]
    must = [n for n in ("statins", "reason") if n in ev]
    rest = [n for n in order if n in ev and n not in must]
    return (rest[: 5 - len(must)] + must)[:5]


def pick_pair(all_cases: list[dict], gap_only: bool) -> tuple[dict, dict]:
    c0 = {c["base_patient"]: c for c in all_cases if c["condition"] == "C0"}
    cands = [c for c in all_cases if c["condition"] == "C1" and c["variant"] == "lipid_panel"
             and c["base_patient"] in c0
             and (not gap_only or c0[c["base_patient"]]["expected_status"] == "GAP")]
    if not cands:
        raise SystemExit("no lipid-panel C1 twin that fits the rule in this case set")
    best = min(cands, key=lambda c: c["case_id"])
    return c0[best["base_patient"]], best


def pick_caught(runs: list[dict]) -> tuple[dict, dict, dict, bool] | None:
    for want_clean in (True, False):
        hits = []
        for run in runs:
            if run["clean"] != want_clean:
                continue
            for cid, rec in run["systems"].get("A2", {}).items():
                v = rec.get("verifier") or []
                if len(v) >= 2 and not v[0]["ok"] and v[-1]["ok"] and not rec.get("forced_fallback"):
                    first = run["systems"].get("A1", {}).get(cid)
                    if first is not None:
                        hits.append((cid, run["manifest"].get("run_id", ""), run, rec, first))
        if hits:
            cid, _, run, rec, first = min(hits, key=lambda h: (h[0], h[1]))
            return run, rec, first, not want_clean
    return None


def caught_state(case_dir: str, case: dict, run: dict, rec: dict, first: dict,
                 dirty: bool) -> dict:
    tables, _, delta = CS.case_tables(case_dir, case["case_id"])
    con = CS.materialize(case_dir, case["case_id"])
    f = facts.patient_facts(con, case["base_patient"])
    con.close()
    v1, v_last = rec["verifier"][0], rec["verifier"][-1]
    o1, o2 = first["output"], rec["output"]
    failed = []
    for fl in v1["failures"]:
        ref = fl["ref"]
        item = None
        if ref.startswith("missing["):
            name = o1["missing"][int(ref[8:-1])]
            item = {"kind": "missing", "name": name, "label": LABEL.get(name, name)}
        elif ref.startswith("evidence["):
            it = o1["evidence"][int(ref[9:-1])]
            item = {"kind": "claim", "name": it["claim"], "label": LABEL.get(it["claim"], it["claim"]),
                    "support": it["support"],
                    "source": it["source"] if isinstance(it["source"], str) else ", ".join(it["source"])}
        else:
            item = {"kind": "answer", "name": None, "label": "the answer as a whole"}
        failed.append({**item, "check": fl["check"], "check_text": CHECKS[fl["check"]],
                       "detail": fl["detail"]})
    ev1, ev2 = evidence_map(o1), evidence_map(o2)
    names = [n for n in ("ascvd", "lipid_panel", "risk_10y", "diabetes", "statins", "reason")
             if n in ev1][:3]
    fixed = []
    for fl in failed:
        n = fl["name"]
        if n and n in ev2:
            fixed.append(line(n, ev2[n], tables))
    return {
        "case_id": case["case_id"], "condition": case["condition"], "variant": case["variant"],
        "patient": {"sex": f.sex, "age": f.age, "index_date": f.index_date.isoformat()},
        "answer_key": {"status": case["expected_status"], "row": case["expected_row"],
                       "missing": case["expected_missing"], "row_text": row_text(case["expected_row"])},
        "first": {"status": o1["status"], "row": o1.get("benefit_group"),
                  "row_text": row_text(o1.get("benefit_group")), "missing": o1.get("missing", []),
                  "lines": [line(n, ev1[n], tables) for n in names]},
        "failed": failed,
        "repaired": {"status": o2["status"], "row": o2.get("benefit_group"),
                     "row_text": row_text(o2.get("benefit_group")), "missing": o2.get("missing", []),
                     "lines": fixed, "all_checks_pass": v_last["ok"],
                     "n_claims": len(o2["evidence"])},
        "planted": [{"text": p["text"], "type": p["phrase_type"], "note_date": p["note_date"]}
                    for p in delta["plants"]],
        "run": run_meta(run), "from_dirty_tree": dirty,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=CASE_SET)
    ap.add_argument("--runs", default="data/runs")
    ap.add_argument("--out", default="site/card.json")
    ap.add_argument("--gap-only", action="store_true",
                    help="pair rule: only twins whose original record is labelled GAP")
    a = ap.parse_args()
    case_dir = str(ROOT / a.cases) if not Path(a.cases).is_absolute() else a.cases
    all_cases = sorted(CS.read_cases(case_dir), key=lambda c: c["case_id"])
    by_id = {c["case_id"]: c for c in all_cases}
    runs = real_runs(ROOT / a.runs if not Path(a.runs).is_absolute() else Path(a.runs), a.cases)

    c0, c1 = pick_pair(all_cases, a.gap_only)
    con = CS.materialize(case_dir, c0["case_id"])
    f0 = facts.patient_facts(con, c0["base_patient"])
    con.close()
    shown = shown_inputs(baselines.r0(f0))
    s1 = record_state(case_dir, c0, shown, runs)
    s2 = record_state(case_dir, c1, shown, runs, deleted={"lipid_panel"})
    was = {ln["name"]: ln for ln in s1["lines"]}
    for ln in s2["lines"]:
        if ln.get("deleted"):  # show what was there, struck through
            ln.update(value=was[ln["name"]]["value"], support=was[ln["name"]]["support"],
                      source=was[ln["name"]]["source"], date=was[ln["name"]]["date"])
        elif ln["name"] == "risk_10y" and ln["value"] is None:  # not deleted, not computable
            ln["value"], ln["blocked"] = "cannot be computed", True
    caught = pick_caught(runs)
    s3 = caught_state(case_dir, by_id[caught[1]["case_id"]], *caught) if caught else None

    man = Path(case_dir) / "manifest.json"
    used_runs = {}
    for st in (s1, s2):
        if st["agent"]:
            r = next(r for r in runs if r["manifest"].get("run_id") == st["agent"]["run_id"])
            used_runs[st["agent"]["run_id"]] = run_meta(r)
    if s3:
        used_runs[s3["run"]["run_id"]] = s3["run"]
    out = {
        "schema": 1,
        "label": "Development record, local model. Not a result.",
        "footer": "Synthetic patient. Designed test set, not prevalence. Research prototype, not medical advice.",
        "rules": {"pair": RULE_PAIR_GAP if a.gap_only else RULE_PAIR, "caught": RULE_CAUGHT},
        "provenance": {
            "case_set": Path(case_dir).name,
            "case_set_manifest_sha256": hashlib.sha256(man.read_bytes()).hexdigest(),
            "generator": "scripts/site_card.py",
            "runs": [used_runs[k] for k in sorted(used_runs)],
            "real_runs_seen": sorted(r["manifest"].get("run_id", "") for r in runs),
        },
        "checks": {str(k): v for k, v in CHECKS.items()},
        "states": {"original": s1, "edited": s2, "caught": s3},
    }
    p = ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {rel(p)}: pair {c0['case_id']} / {c1['case_id']}; caught "
          f"{s3['case_id'] if s3 else None}; real runs seen {len(runs)}")


if __name__ == "__main__":
    main()
