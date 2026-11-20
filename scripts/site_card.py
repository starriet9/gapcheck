"""Build the data of the page's care-gap alert card (site/card.json, schema 2).

Usage:
  python scripts/site_card.py [--pick site/card_pick.json] [--runs data/runs] \
      [--out site/card.json] [--commit-map FILE]

Each tab of the card is one real agent answer, named in site/card_pick.json
(tab, case id, run id, system; chosen by the author). For each tab this
writes, from files only:
  - the agent's final answer: status, decision-table row, the claims it made
    (record rows, computed risks, confirmed absences), the inputs it listed
    as missing, and its note to the clinician, verbatim;
  - every verifier round: the items it rejected, with the check and the
    reason, and how many claims passed;
  - whether the answer equals the code answer key;
  - the test edit: rows deleted, notes changed, sentence planted;
  - both risk values when the agent computed the risk twice (smoker and not);
  - for a tab with a base case, the answer on the unedited record;
  - where the run happened: model, CPU workstation or rented GPU, date,
    commit, and whether the prompts differ from the current ones.
A tab whose run, case or system record is not found stops the build: the card
never shows a case that was not run. `failures` entries in the pick file
(folded "Where it failed" on the page) are built the same way.

Run ids are the `run_id` of a run manifest under --runs (any depth). A run on
a copy of the case set is accepted if its manifest names the same case-set
manifest sha256. --commit-map is a TSV of old and new commit ids, for runs
recorded before a history rewrite.

Only synthetic (Synthea) records are read. The output holds sex, age, dates,
row ids, values and record descriptions; the only free text from a note is
the planted test sentence, and the only model text is note_for_clinician.
No patient id and no path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from gapcheck import cases as CS
from gapcheck import oracle

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "src" / "gapcheck" / "prompts"

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
    "reason": "Documented reason not to prescribe", "risk_10y": "10-year ASCVD risk",
    "risk_30y": "30-year ASCVD risk", "hiv": "HIV", "esrd": "ESRD, dialysis, transplant",
    "row5_features": "Diabetes complications", "age": "Age", "sex": "Sex",
}
LOWER = {"lipid_panel": "lipid panel", "sbp": "systolic BP", "smoking": "smoking status",
         "egfr": "eGFR"}
EDIT_NOUN = {"lipid_panel": "lipid result", "sbp": "systolic BP reading",
             "smoking": "smoking-status entry"}
# Why a missing input matters, when the agent's answer itself does not say more.
MISSING_WHY = {"lipid_panel": "Without it the 10-year risk cannot be computed.",
               "sbp": "Without it the 10-year risk cannot be computed.",
               "smoking": "Without it the 10-year risk cannot be computed.",
               "egfr": "Without it the 10-year risk cannot be computed."}
ACTION = {"DOCUMENTED": "row re-read", "COMPUTED": "risk recomputed from the cited inputs",
          "ABSENT": "query re-run, nothing found"}
# What matters, by the decision-table row the answer names (first lines of the card).
MATTERS = {"0": ["statins"], "1": ["ascvd", "lipid_panel", "statins"],
           "2": ["lipid_panel", "statins"], "3": ["diabetes", "lipid_panel", "statins"],
           "3b": ["ckd", "lipid_panel", "statins"], "4": ["diabetes", "age", "statins"],
           "5": ["diabetes", "risk_10y", "lipid_panel", "statins"],
           "6": ["age", "lipid_panel", "statins"], "7": ["lipid_panel", "statins"]}
MATTERS_RISK = ["risk_10y", "lipid_panel", "sbp", "statins"]          # rows 8-11b
MATTERS_NONE = ["ascvd", "diabetes", "ckd", "risk_10y", "lipid_panel", "sbp", "smoking",
                "statins"]                                            # no row (CANNOT_DETERMINE)
MAX_LINES = 4
P1_NOTE = ("The generated record had no eGFR, so patch P1 added one, drawn the way the generator "
           "draws eGFR. Any value from 60 to 140 gives the same answer.")
COLON = {"sex", "smoking", "bp_drugs", "reason", "ascvd", "diabetes", "ckd", "hiv", "esrd",
         "row5_features"}


# ---------------------------------------------------------------- small helpers
def num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:g}"


def pct(v) -> str:
    return f"{100 * float(v):.1f}%"


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def protocol_rules() -> dict[str, str]:
    """{row: rule text} from the decision table in PROTOCOL.md sec. 3."""
    out = {}
    for ln in (ROOT / "PROTOCOL.md").read_text().splitlines():
        m = re.match(r"\|\s*(\d+[ab]?)\s*\|\s*([^|]+?)\s*\|\s*([A-Z_, ]+?)\s*\|", ln)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def row_info(row: str | None) -> dict | None:
    if row is None or row not in oracle.ROW_BY_ID:
        return None
    r = oracle.ROW_BY_ID[row]
    cor = (next((s.cor for s in r.sources if s.role == "basis" and s.cor), None)
           if r.label in (oracle.GAP, oracle.DISCUSS) else None)
    return {"row": row, "group": r.group.replace(">=", "≥").replace("<=", "≤"),
            "class": cor, "rule": protocol_rules().get(row)}


def current_prompts() -> dict[str, str]:
    return {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(PROMPTS.glob("*.txt"))}


def load_commit_map(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    out = {}
    for ln in Path(path).read_text().splitlines():
        if ln.strip():
            old, new = ln.split("\t")[:2]
            out[old.strip()] = new.strip()
    return out


# ---------------------------------------------------------------- runs
def find_runs(runs_dir: Path) -> dict[str, dict]:
    """{run_id: {dir, manifest}} for every non-mock run manifest under runs_dir."""
    out = {}
    for m in sorted(runs_dir.rglob("manifest.json")):
        try:
            man = json.loads(m.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        be = man.get("backend") or {}
        if "run_id" not in man or be.get("backend") in (None, "mock"):
            continue
        out.setdefault(man["run_id"], {"dir": m.parent, "manifest": man})
    return out


def run_record(runs: dict, run_id: str, system: str, case_id: str, case_sha: str) -> tuple[dict, dict]:
    if run_id not in runs:
        raise SystemExit(f"card: run {run_id!r} not found under the runs directory")
    run = runs[run_id]
    man = run["manifest"]
    if (man.get("case_set") or {}).get("manifest_sha256") != case_sha:
        raise SystemExit(f"card: run {run_id} is on another case set")
    f = run["dir"] / f"{system}.jsonl"
    recs = {r["case_id"]: r for r in read_jsonl(f)} if f.exists() else {}
    rec = recs.get(case_id)
    if rec is None or rec.get("output") is None or not rec.get("schema_valid"):
        raise SystemExit(f"card: no valid {system} output for {case_id} in run {run_id}")
    return run, rec


def where(be: dict, override: str | None) -> str:
    if override:
        return override
    rev = (be.get("revision") or "") + " " + (be.get("gpu") or "")
    if re.search(r"\bCPU\b", rev):
        return "cpu"
    if re.search(r"CUDA|GPU|RTX|L40S|A100|H100|\bvLLM\b", rev, re.I):
        return "gpu"
    raise SystemExit(f"card: cannot tell CPU or GPU from the backend of {be.get('model')}; "
                     "set \"where\" in the pick file")


def model_display(be: dict) -> str:
    """'unsloth/Qwen3.8-Flash-Next-GGUF@<rev> UD-Q4_K_XL; ...' -> 'Qwen3.8-Flash-Next (4-bit GGUF)'."""
    rev = be.get("revision") or ""
    m = re.match(r"[^/]+/([^@ ]+?)(-GGUF)?@\S+\s*([^;]*)", rev)
    if not m:
        return be.get("model") or "unknown model"
    name, gguf, quant = m.group(1), m.group(2), m.group(3)
    bits = re.search(r"Q(\d)", quant or "")
    tail = " ".join(x for x in ((f"{bits.group(1)}-bit" if bits else ""), "GGUF" if gguf else "") if x)
    return f"{name} ({tail})" if tail else name


def run_meta(run: dict, override_where: str | None, cmap: dict[str, str]) -> dict:
    man = run["manifest"]
    be = man["backend"]
    git = man.get("git") or {}
    commit = git.get("commit")
    now = current_prompts()
    changed = sorted(k for k, v in (man.get("prompts_sha256") or {}).items() if now.get(k) not in (None, v))
    return {"run_id": man["run_id"], "model_display": model_display(be),
            "where": where(be, override_where), "model": be.get("model"),
            "revision": be.get("revision"), "temperature": be.get("temperature"),
            "commit": cmap.get(commit, commit), "dirty": git.get("dirty"),
            "finished_utc": man.get("finished_utc"),
            "date": (man.get("finished_utc") or man.get("started_utc") or "")[:10] or None,
            "prompts_changed_since": changed,
            "prompts_sha256": man.get("prompts_sha256"),
            "conventions_sha256": man.get("conventions_sha256")}


# ---------------------------------------------------------------- record rows
def lookup(tables: dict, row_id: str) -> tuple[str, dict] | None:
    for name, t in tables.items():
        if "row_id" in t.columns:
            hit = t[t.row_id == row_id]
            if len(hit):
                return name, hit.iloc[0].to_dict()
    return None


def clean_desc(d) -> str:
    return re.sub(r"\s*\((disorder|procedure|finding|situation|morphologic abnormality)\)$", "",
                  str(d)).strip()


def date_of(r: dict | None) -> str | None:
    if r is None:
        return None
    for k in ("DATE", "START"):
        v = r.get(k)
        if isinstance(v, str):
            return v[:10]
    return None


def row_view(tables: dict, row_id: str) -> dict:
    hit = lookup(tables, row_id)
    if hit is None:
        return {"id": row_id, "table": None, "description": None, "date": None, "value": None,
                "patch": None}
    table, r = hit
    val = r.get("VALUE")
    unit = r.get("UNITS")
    patch = r.get("patch")
    return {"id": row_id, "table": table, "description": clean_desc(r.get("DESCRIPTION", "")),
            "date": date_of(r),
            "value": (f"{num(val)} {unit}".strip() if isinstance(val, str) and
                      re.fullmatch(r"-?\d+(\.\d+)?", val) else (val if isinstance(val, str) else None)),
            "patch": patch if isinstance(patch, str) and patch else None}


def risk_inputs_text(inp: dict) -> str:
    parts = [f"age {inp['age']}", inp.get("sex", ""), f"TC {num(inp['tc'])}", f"HDL-C {num(inp['hdl'])}",
             f"SBP {num(inp['sbp'])}" + (" treated" if inp.get("bp_treated") else " untreated"),
             "smoker" if inp.get("smoker") else "non-smoker",
             "diabetes" if inp.get("diabetes") else "no diabetes",
             f"eGFR {num(inp['egfr'])}" if inp.get("egfr") is not None else "",
             "on a statin" if inp.get("statin") else "no statin"]
    return ", ".join(p for p in parts if p)


def claim_lines(name: str, item: dict, tables: dict, ev: dict, verified, split: bool) -> list[dict]:
    """Card lines for one claim (an ASCVD, diabetes or CKD claim citing a condition row and a
    procedure row gives one line for each when `split`)."""
    sup, src = item["support"], item["source"]
    rows = [row_view(tables, s) for s in src if s != "demographics"] if isinstance(src, list) else []
    base = {"name": name, "support": sup, "verified": verified, "action": ACTION.get(sup),
            "rows": rows, "source": src if isinstance(src, str) else None, "flag": None,
            "extra": None, "value": None, "when": None, "inputs": None,
            "sep": ": " if sup == "ABSENT" or name in COLON else " "}
    if sup == "ABSENT":
        val = "none in the last 365 days" if name == "reason" else "none on record"
        return [{**base, "text": LABEL.get(name, name), "value": val, "when": "re-queried"}]
    if sup == "COMPUTED":
        inp = item.get("inputs") or {}
        return [{**base, "text": LABEL.get(name, name), "value": pct(item["value"]),
                 "when": "recomputed", "inputs": risk_inputs_text(inp) if inp else None}]
    v = item.get("value")
    date = item.get("date") or (rows[0]["date"] if rows else None)
    if name == "lipid_panel":
        return [{**base, "text": "LDL-C", "value": f"{num(v['ldl'])} mg/dL",
                 "extra": f"TC {num(v['tc'])}, HDL-C {num(v['hdl'])}", "when": date}]
    if name == "sbp":
        on = ev.get("bp_drugs", {}).get("support") == "DOCUMENTED"
        return [{**base, "text": "Systolic BP", "value": f"{num(v)} mm Hg",
                 "extra": "on BP medication" if on else None, "when": date}]
    if name == "egfr":
        p1 = any(r["patch"] == "P1" for r in rows)
        return [{**base, "text": "eGFR", "value": f"{num(v)} mL/min/1.73 m²", "when": date,
                 "flag": {"kind": "p1", "text": P1_NOTE} if p1 else None}]
    if name == "smoking":
        return [{**base, "text": "Smoking", "value": str(v).replace(" (finding)", ""), "when": date}]
    if name == "age":
        return [{**base, "text": "Age", "value": str(v), "when": None}]
    if name == "sex":
        return [{**base, "text": "Sex", "value": {"M": "male", "F": "female"}.get(v, str(v))}]
    if name == "reason" and isinstance(src, str):
        return [{**base, "text": "Reason in a note", "value": item.get("quote") or str(v),
                 "when": date}]
    if rows and name in ("ascvd", "diabetes", "ckd") and split:
        seen, out = set(), []
        for r in sorted(rows, key=lambda r: (r["date"] or "", r["id"])):
            if r["table"] in seen:
                continue
            seen.add(r["table"])
            out.append({**base, "text": r["description"], "when": r["date"], "rows": [r],
                        "claim_rows": rows})
        return out[:2]
    if rows:
        more = f" + {len(rows) - 1} more rows" if len(rows) > 1 else ""
        return [{**base, "text": LABEL.get(name, name), "value": (rows[0]["description"] or "") + more,
                 "when": date}]
    return [{**base, "text": LABEL.get(name, name), "value": "recorded", "when": date}]


# ---------------------------------------------------------------- answer and verifier
def failure_view(f: dict, draft: dict) -> dict:
    ref = f.get("ref") or ""
    kind, name = "answer", None
    m = re.fullmatch(r"(missing|evidence)\[(\d+)\]", ref)
    if m and m.group(1) == "missing":
        kind, name = "missing", draft["missing"][int(m.group(2))]
    elif m:
        it = draft["evidence"][int(m.group(2))]
        kind, name = ("absent" if it["support"] == "ABSENT" else "claim"), it["claim"]
    return {"check": f["check"], "check_text": CHECKS.get(f["check"]), "kind": kind, "name": name,
            "label": LABEL.get(name, name) if name else None, "ref": ref or None,
            "detail": f.get("detail")}


def drafts_of(rec: dict, first: dict | None) -> list[dict]:
    """The answer each verifier round checked: round 1 is the A1 answer (same run), the last
    round the final output."""
    out = []
    v = rec.get("verifier") or []
    for i, _ in enumerate(v):
        if i == len(v) - 1 and not rec.get("forced_fallback"):
            out.append(rec["output"])
        elif i == 0 and first is not None:
            out.append(first["output"])
        else:
            out.append(None)
    return out


def verifier_rounds(rec: dict, first: dict | None) -> list[dict]:
    out = []
    for i, (r, draft) in enumerate(zip(rec.get("verifier") or [], drafts_of(rec, first))):
        claims = r.get("claims") or []
        out.append({"round": i + 1, "ok": r["ok"], "n_claims": len(claims),
                    "n_ok": sum(1 for c in claims if c.get("ok")),
                    "failures": [failure_view(f, draft) for f in r["failures"]] if draft else
                                [{"check": f["check"], "check_text": CHECKS.get(f["check"]),
                                  "kind": "answer", "name": None, "label": None,
                                  "ref": f.get("ref"), "detail": f.get("detail")}
                                 for f in r["failures"]]})
    return out


def answer_view(o: dict, tables: dict, verified: dict[int, bool] | None) -> dict:
    ev_items = o["evidence"]
    ev = {}
    for it in ev_items:
        ev.setdefault(it["claim"], it)
    risks = [{"value": it["value"], "smoker": (it.get("inputs") or {}).get("smoker")}
             for it in ev_items if it["claim"] == "risk_10y" and it["support"] == "COMPUTED"]
    info = row_info(o.get("benefit_group"))
    row = o.get("benefit_group")
    want = MATTERS.get(row, MATTERS_RISK if row in ("8", "9", "10", "11a", "11b") else MATTERS_NONE)
    lines = []
    for name in want:
        idx = next((i for i, it in enumerate(ev_items) if it["claim"] == name), None)
        if idx is None:
            continue
        it = ev_items[idx]
        if row is None and it["support"] == "ABSENT" and name != "statins":
            continue
        lines += claim_lines(name, it, tables, ev, verified.get(idx) if verified else None, split=True)
    statin = [ln for ln in lines if ln["name"] == "statins"]
    lines = [ln for ln in lines if ln["name"] != "statins"][: MAX_LINES - len(statin)] + statin
    claims = []
    for i, it in enumerate(ev_items):
        claims += claim_lines(it["claim"], it, tables, ev, verified.get(i) if verified else None,
                              split=False)
    return {"status": o["status"], "row": row, "row_info": info,
            "risk_10y": o.get("risk_10y"), "risks": risks,
            "missing": [{"name": m, "label": LABEL.get(m, m), "lower": LOWER.get(m, m),
                         "why": MISSING_WHY.get(m)} for m in o.get("missing", [])],
            "note_for_clinician": o.get("note_for_clinician"),
            "lines": lines, "claims": claims, "n_claims": len(ev_items)}


def same_as_key(o: dict, case: dict) -> bool:
    if o["status"] != case["expected_status"]:
        return False
    if case["expected_status"] == "CANNOT_DETERMINE":
        return set(case["expected_missing"]) <= set(o.get("missing", []))
    return True


def edit_view(case: dict, delta: dict) -> dict:
    cond = case["condition"]
    out = {"condition": cond, "variant": case["variant"], "direction": case.get("direction"),
           "kind": "none", "input": None, "input_label": None, "noun": None,
           "deleted_rows": sum(len(v) for v in delta["delete"].values()),
           "notes_changed": len(delta["notes"]) if delta["delete"] else 0,
           "planted": [{"text": p["text"], "note_date": p["note_date"]} for p in delta["plants"]]}
    if cond in ("C1", "C2a", "C2b"):
        out.update(kind="delete", input=case["variant"], input_label=LOWER.get(case["variant"]),
                   noun=EDIT_NOUN.get(case["variant"], case["variant"]))
    elif out["planted"]:
        out["kind"] = "plant"
    elif cond == "C5":
        out["kind"] = "move"
    return out


def build_answer(case_dir: str, case: dict, run: dict, rec: dict, first: dict | None) -> dict:
    tables, _, delta = CS.case_tables(case_dir, case["case_id"])
    v = rec.get("verifier") or []
    verified = ({c["index"]: bool(c["ok"]) for c in v[-1].get("claims", [])}
                if v and not rec.get("forced_fallback") else None)
    o = rec["output"]
    ans = answer_view(o, tables, verified)
    trace = rec.get("trace") or []
    ans.update(forced_fallback=bool(rec.get("forced_fallback")),
               called_prevent=any(t.get("type") == "tool" and t.get("name") == "prevent_risk"
                                  for t in trace),
               seconds=rec.get("seconds"))
    return {"answer": ans, "verifier": verifier_rounds(rec, first), "edit": edit_view(case, delta),
            "answer_key": {"status": case["expected_status"], "row": case["expected_row"],
                           "row_info": row_info(case["expected_row"]),
                           "missing": [{"name": m, "lower": LOWER.get(m, m)}
                                       for m in case["expected_missing"]],
                           "risk_10y": case.get("risk10"),
                           "same": same_as_key(o, case)}}


def patient_view(case_dir: str, case: dict) -> dict:
    from gapcheck import facts
    con = CS.materialize(case_dir, case["case_id"])
    f = facts.patient_facts(con, case["base_patient"])
    con.close()
    return {"sex": f.sex, "age": f.age, "index_date": f.index_date.isoformat()}


# ---------------------------------------------------------------- main
def build_item(spec: dict, case_dir: str, by_id: dict, runs: dict, case_sha: str,
               cmap: dict) -> dict:
    case = by_id.get(spec["case_id"])
    if case is None:
        raise SystemExit(f"card: {spec['case_id']} is not in the case set")
    system = spec.get("system", "A2")
    run, rec = run_record(runs, spec["run_id"], system, case["case_id"], case_sha)
    first = None
    if system == "A2":
        f = run["dir"] / "A1.jsonl"
        if f.exists():
            first = {r["case_id"]: r for r in read_jsonl(f)}.get(case["case_id"])
    item = {"id": spec["id"], "chosen_by": spec.get("chosen_by", "author"),
            "record": spec.get("record", "development"),
            "case_id": case["case_id"], "system": system,
            "patient": patient_view(case_dir, case),
            **build_answer(case_dir, case, run, rec, first),
            "run": run_meta(run, spec.get("where"), cmap)}
    st = item["answer"]["status"]
    if "label_by_status" in spec:
        item["label"] = spec["label_by_status"].get(st, spec["label_otherwise"])
        item["label_warn"] = st not in spec["label_by_status"]
    else:
        item["label"] = spec.get("label")
        item["label_warn"] = False
    if spec.get("base_case_id"):
        b = by_id[spec["base_case_id"]]
        if b["base_patient"] != case["base_patient"] or b["condition"] != "C0":
            raise SystemExit(f"card: {b['case_id']} is not the unedited record of {case['case_id']}")
        brun, brec = run_record(runs, spec["base_run_id"], system, b["case_id"], case_sha)
        bo = brec["output"]
        item["base"] = {"case_id": b["case_id"], "status": bo["status"],
                        "row": bo.get("benefit_group"), "risk_10y": bo.get("risk_10y"),
                        "same_as_key": same_as_key(bo, b), "run": run_meta(brun, spec.get("where"), cmap)}
    if spec.get("after_run_id"):
        arun, arec = run_record(runs, spec["after_run_id"], system, case["case_id"], case_sha)
        ao = arec["output"]
        item["after"] = {"status": ao["status"], "row": ao.get("benefit_group"),
                         "risk_10y": ao.get("risk_10y"), "same_as_key": same_as_key(ao, case),
                         "run": run_meta(arun, spec.get("after_where"), cmap)}
    return item


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", default="site/card_pick.json")
    ap.add_argument("--runs", default="data/runs")
    ap.add_argument("--out", default="site/card.json")
    ap.add_argument("--commit-map")
    a = ap.parse_args()
    pick = json.loads((ROOT / a.pick).read_text())
    case_dir = str(ROOT / pick["case_set"])
    by_id = {c["case_id"]: c for c in CS.read_cases(case_dir)}
    case_sha = hashlib.sha256((Path(case_dir) / "manifest.json").read_bytes()).hexdigest()
    runs = find_runs(ROOT / a.runs if not Path(a.runs).is_absolute() else Path(a.runs))
    cmap = load_commit_map(a.commit_map)
    tabs = [build_item(t, case_dir, by_id, runs, case_sha, cmap) for t in pick["tabs"]]
    fails = [build_item(t, case_dir, by_id, runs, case_sha, cmap) for t in pick.get("failures", [])]
    out = {"schema": 2, "generator": "scripts/site_card.py",
           "case_set": Path(case_dir).name, "case_set_manifest_sha256": case_sha,
           "checks": {str(k): v for k, v in CHECKS.items()},
           "tabs": tabs, "failures": fails}
    p = ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
    p.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    for t in tabs + fails:
        print(f"{t['id']}: {t['case_id']} {t['system']} {t['answer']['status']} "
              f"row {t['answer']['row']} (key {t['answer_key']['status']}, same {t['answer_key']['same']}); "
              f"run {t['run']['run_id']} on {t['run']['where']}, dirty {t['run']['dirty']}")
    print(f"wrote {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}")


if __name__ == "__main__":
    main()
