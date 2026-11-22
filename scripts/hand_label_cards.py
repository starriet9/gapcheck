"""Blind case cards for the hand-labeling check of the oracle (30 development cases).

The author labels 30 development cases by hand, without seeing the oracle's
answer, to check that the code applies the decision table correctly to real
records. Each card is built from the case's record exactly as the systems get
it: `CaseEnv.from_case` (base record with the case's edits applied, and the
notes rendered for that case, via `cases.materialize`) and the systems' own
tools (`get_demographics`, `get_observations`, `get_allergies`,
`prevent_risk`). Nothing comes from the oracle's fact extraction
(`gapcheck.facts`); the risk numbers (`prevent_risk`) and the statin, BP-drug
and diabetes code lists (`gapcheck.codes`) are shared with the oracle.

Selection (PROTOCOL.md sec. 3), at most one case per base patient:
- all cases sorted by sha256("20261122:handlabel:<case_id>") (hex, ascending);
  walking that list, a case is taken if fewer than 5 cases of its expected
  status are taken, its base patient is not taken yet, and its base patient is
  not shown on the project page (any case named in site/card_pick.json); stop
  at 5 per status.
  If a status cannot reach 5, the script stops and prints the counts (the rule
  is never relaxed);
- the 30 cases shown in the order of sha256("20261122:handlabel-order:<case_id>"),
  numbered 1-30.

Outputs (in --out):
- cards.json  {"meta": {...}, "cards": [card, ...]}. No case ids, no labels,
              nothing that tells what an edit changed.
- key.json    {"meta": {...}, "cards": [{"n", "case_id", "expected_status",
              "expected_row", "condition"}]}. Keep it closed until every card
              is labeled.

A card holds:
- age at index, sex, index date;
- diagnoses (conditions) dated on or before index, one entry per code, with
  start and stop dates; Synthea's social and administrative findings and
  situations (SOCIAL_ADMIN) in a separate list `other_findings`;
- measurements on or before index, every value with its date, newest first,
  and the latest value: TC, HDL-C, LDL-C, systolic BP, smoking status, eGFR,
  HbA1c;
- medications, one entry per code, with start/stop dates and whether active
  at index; procedures, one entry per code, latest first;
- allergies and intolerances (the get_allergies tool result);
- pregnancy-related and hospice/palliative entries (text match, every table);
- the notes dated 0-365 days before index, in full, newest first, and the
  number of older notes (every note in full if there are at most
  FULL_NOTES_MAX notes);
- PREVENT from the systems' prevent_risk tool, with inputs chosen from the
  record by the input rules of the conventions page (`prevent_block`).

Usage:
  python scripts/hand_label_cards.py --out DIR
  python scripts/hand_label_cards.py --out DIR --case-dir data/cases/dev_s20261120_utc_n10
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from gapcheck import codes as C
from gapcheck import oracle
from gapcheck.cases import ROOT, read_cases
from gapcheck.tools import CaseEnv

DEFAULT_CASE_DIR = "data/cases/dev_s20261120_utc_n10"
SELECT_SALT = "20261122:handlabel"
ORDER_SALT = "20261122:handlabel-order"
PER_STATUS = 5
NOTE_WINDOW_DAYS = 365   # notes shown in full (the reason window, conventions page)
FULL_NOTES_MAX = 10      # at most this many notes on record: every note in full
MAX_PERIODS = 8          # periods listed per code (newest first); older ones counted
ALL_ROWS = 10 ** 9       # page size that returns every row of a tool in one page

# Measurements shown, in this order: card key -> LOINC code.
MEASUREMENTS = {
    "total_cholesterol": C.TC.code,
    "hdl_c": C.HDL.code,
    "ldl_c": C.LDL.code,
    "systolic_bp": C.SBP.code,
    "smoking_status": C.SMOKING.code,
    "egfr": C.EGFR.code,
    "hba1c": "4548-4",
}

# Synthea's social and administrative findings and situations (exact condition
# descriptions). They go to `other_findings`; every other condition is a
# diagnosis, so an entry not on this list is never hidden.
SOCIAL_ADMIN = frozenset({
    "Educated to high school level (finding)",
    "Full-time employment (finding)",
    "Has a criminal record (finding)",
    "Homeless (finding)",
    "Housing unsatisfactory (finding)",
    "Lack of access to transportation (finding)",
    "Limited social contact (finding)",
    "Medication review due (situation)",
    "Not in labor force (finding)",
    "Only received primary school education (finding)",
    "Part-time employment (finding)",
    "Received higher education (finding)",
    "Refugee (person)",
    "Reports of violence in the environment (finding)",
    "Risk activity involvement (finding)",
    "Serving in military service (finding)",
    "Social isolation (finding)",
    "Stress (finding)",
    "Transport problem (finding)",
    "Unemployed (finding)",
    "Victim of intimate partner abuse (finding)",
})

# Text searches over conditions, procedures and encounters (DESCRIPTION).
PREGNANCY_TEXT = re.compile(
    r"pregnan|prenatal|antenatal|postpartum|post-partum|obstetric|lactat|breast ?fe|miscarr"
    r"|gestat|eclampsia|trimester|of labor\b|delivery|fetal|fetus|caesarean|cesarean|amniotic"
    r"|ectopic|childbirth|abortion", re.I)
HOSPICE_TEXT = re.compile(r"hospice|palliat|end of life|end-of-life|terminal|comfort care", re.I)
SEARCH_TABLES = ("conditions", "procedures", "encounters")


# ---------------------------------------------------------------- selection
def _h(salt: str, case_id: str) -> str:
    return hashlib.sha256(f"{salt}:{case_id}".encode()).hexdigest()


def select(cases: list[dict], exclude_patients=frozenset()) -> list[dict]:
    """PER_STATUS cases per expected status, at most one per base patient and none of
    `exclude_patients`, in display order (module docstring)."""
    picked, patients = [], set(exclude_patients)
    taken = {s: 0 for s in oracle.STATUSES}
    for c in sorted(cases, key=lambda c: _h(SELECT_SALT, c["case_id"])):
        st = c["expected_status"]
        if taken[st] < PER_STATUS and c["base_patient"] not in patients:
            picked.append(c)
            patients.add(c["base_patient"])
            taken[st] += 1
        if len(picked) == PER_STATUS * len(taken):
            break
    short = {s: n for s, n in taken.items() if n < PER_STATUS}
    if short:
        raise SystemExit(f"cannot take {PER_STATUS} cases per status with one case per base "
                         f"patient; taken: {taken}")
    return sorted(picked, key=lambda c: _h(ORDER_SALT, c["case_id"]))


# ---------------------------------------------------------------- record helpers
def _d10(x) -> str | None:
    s = None if x is None else str(x)
    return None if not s or s in ("None", "nan", "NaT") else s[:10]


def _active(stop10: str | None, ix: str) -> bool:
    """Active at index: no stop date, or a stop date after index (the tools' rule)."""
    return stop10 is None or stop10 > ix


def _rows(env: CaseEnv, table: str, where: str = "", params=()) -> list[tuple]:
    """(CODE, DESCRIPTION, start, stop) of rows that start on or before index."""
    return env.con.execute(
        f"select CODE, DESCRIPTION, START, STOP from {table} "
        f"where substr(START, 1, 10) <= ? {where} order by START, row_id",
        [env.ix, *params]).fetchall()


def _grouped(env: CaseEnv, rows: list[tuple], periods: bool = True,
             active: bool = True) -> list[dict]:
    """One entry per (code, description), with the number of rows and the first start.

    periods: the newest MAX_PERIODS [start, stop] periods (and the number of older
    ones); otherwise the last start date. active: whether any row is active at index
    (active entries first). Entries are ordered by first start date."""
    by: dict[tuple, dict] = {}
    for code, desc, st, sp in rows:
        st10, sp10 = _d10(st), _d10(sp)
        g = by.setdefault((code, desc), {"display": desc, "code": code, "act": False,
                                         "first_start": st10, "rows": 0, "ps": []})
        g["rows"] += 1
        g["act"] |= _active(sp10, env.ix)
        g["ps"].append([st10, sp10])
    out = []
    for g in by.values():
        ps = sorted(g["ps"], key=lambda p: p[0] or "", reverse=True)
        e = {"display": g["display"], "code": g["code"]}
        if active:
            e["active_at_index"] = g["act"]
        e["first_start"] = g["first_start"]
        if not periods:
            e["last_start"] = ps[0][0]
        e["rows"] = g["rows"]
        if periods:
            e["periods"] = ps[:MAX_PERIODS]
            if len(ps) > MAX_PERIODS:
                e["older_periods"] = len(ps) - MAX_PERIODS
        out.append(e)
    return sorted(out, key=lambda e: (active and not e["active_at_index"],
                                      e["first_start"] or "", e["display"] or "",
                                      e["code"] or ""))


def _text_matches(env: CaseEnv, rx: re.Pattern) -> list[dict]:
    """Entries of conditions, procedures and encounters whose description matches `rx`."""
    out = []
    for table in SEARCH_TABLES:
        rows = [r for r in _rows(env, table) if r[1] and rx.search(r[1])]
        cond = table == "conditions"
        for g in _grouped(env, rows, periods=cond, active=cond):
            out.append({"table": table, **g})
    return out


# ---------------------------------------------------------------- card sections
def measurements(env: CaseEnv) -> dict:
    """Every value on or before index (the get_observations tool), newest first.

    `unit` is the series unit when every value has the same one; otherwise each value
    has its own. A value has `time` (UTC, as recorded) only when another value of the
    series has the same date. `latest` is the first value (tool order: newest timestamp,
    then smallest row id), or null."""
    out = {}
    for key, code in MEASUREMENTS.items():
        r = env.get_observations(code)
        items = r["items"]
        units = {i["unit"] for i in items}
        days = [_d10(i["date"]) for i in items]
        vals = []
        for i, d in zip(items, days):
            v = {"date": d}
            if days.count(d) > 1:
                v["time"] = str(i["date"])[11:]
            v["value"] = i["value"]
            if len(units) > 1:
                v["unit"] = i["unit"]
            vals.append(v)
        latest = None
        if items:
            latest = {"value": items[0]["value"], "unit": items[0]["unit"], "date": days[0]}
        out[key] = {"code": code, "display": r["display"],
                    "unit": next(iter(units)) if len(units) == 1 else None,
                    "latest": latest, "values": vals}
    return out


def notes(env: CaseEnv) -> tuple[list[dict], int]:
    """Notes on or before index, newest first: those dated 0-NOTE_WINDOW_DAYS days before
    index in full (every note if at most FULL_NOTES_MAX), and the number of older ones."""
    ix = env.index_date
    rows = env._notes(None)  # the search_notes/read_note query: dated on or before index
    shown, older = [], 0
    for _, d, text in rows:
        days = (ix - date.fromisoformat(d)).days
        if len(rows) <= FULL_NOTES_MAX or days <= NOTE_WINDOW_DAYS:
            shown.append({"date": d, "days_before_index": days, "text": text})
        else:
            older += 1
    return shown, older


def _latest(values: list[dict], unit: str | None = None) -> dict | None:
    """Latest value (tool order: newest timestamp first, then smallest row id)."""
    for v in values:
        if unit is None or v["unit"] == unit:
            return v
    return None


def _series(meas: dict, key: str) -> list[dict]:
    """Values of one measurement, each with its unit."""
    m = meas[key]
    return [{"unit": m["unit"], **v} for v in m["values"]]


def _lipid_panel(meas: dict) -> dict | None:
    """TC, HDL-C and LDL-C from the latest date at which all three were measured."""
    keys = ("total_cholesterol", "hdl_c", "ldl_c")
    dates = [{v["date"] for v in meas[k]["values"]} for k in keys]
    common = set.intersection(*dates)
    if not common:
        return None
    d = max(common)
    pick = {k: _latest([v for v in meas[k]["values"] if v["date"] == d]) for k in keys}
    return {"date": d, "tc": pick["total_cholesterol"]["value"],
            "hdl": pick["hdl_c"]["value"], "ldl": pick["ldl_c"]["value"]}


def _active_coded(env: CaseEnv, table: str, code_list: dict) -> list[str]:
    """Descriptions of rows active at index whose code is on a conventions code list."""
    out = []
    for code, desc, _, sp in _rows(env, table):
        if code in code_list and _active(_d10(sp), env.ix) and desc not in out:
            out.append(desc)
    return out


def prevent_block(env: CaseEnv, meas: dict) -> dict:
    """PREVENT from the systems' prevent_risk tool.

    Inputs follow the input rules of the conventions page given to the systems:
    latest value on or before index (ties: smallest row id); the lipid panel from
    the latest date with TC, HDL-C and LDL-C; eGFR only in mL/min/{1.73_m2}; statin,
    BP treatment and diabetes from the conventions code lists, active at index.
    With no smoking status on record, the tool is called twice (smoker 0 and 1), as
    the systems are told to do. With another input missing, nothing is computed.
    """
    panel = _lipid_panel(meas)
    sbp = _latest(_series(meas, "systolic_bp"))
    egfr = _latest(_series(meas, "egfr"), C.EGFR_UNIT_KEEP)
    smk = _latest(_series(meas, "smoking_status"))
    statins = _active_coded(env, "medications", C.STATINS)
    bp = _active_coded(env, "medications", C.BP_DRUGS)
    dm = _active_coded(env, "conditions", C.DIABETES)
    smoker = None
    if smk is not None:
        if smk["value"] in C.SMOKING_CURRENT:
            smoker = 1
        elif smk["value"] in C.SMOKING_NOT_CURRENT:
            smoker = 0
    inputs_from = {
        "lipid_panel": panel,
        "sbp": None if sbp is None else {"date": sbp["date"], "value": sbp["value"]},
        "egfr": None if egfr is None else {"date": egfr["date"], "value": egfr["value"],
                                           "unit": egfr["unit"]},
        "smoking": None if smk is None else {"date": smk["date"], "value": smk["value"]},
        "statin_active_at_index": statins,
        "bp_drug_active_at_index": bp,
        "diabetes_active_at_index": dm,
    }
    # Absent from the record (smoking also when the latest value is none of the
    # conventions' smoking values). Not the oracle's `missing`, which names only
    # absent inputs that decide the status.
    absent = [k for k, v in (("lipid_panel", panel), ("sbp", sbp), ("smoking", smoker),
                             ("egfr", egfr)) if v is None]
    out = {"inputs_from": inputs_from, "inputs_not_on_record": absent}
    if [k for k in absent if k != "smoking"]:
        out["computed"] = False
        out["results"] = []
        return out
    args = {"sex": {"M": "male", "F": "female"}[env.sex], "age": int(env.age),
            "tc": panel["tc"], "hdl": panel["hdl"], "sbp": sbp["value"],
            "bp_treated": int(bool(bp)), "statin": int(bool(statins)),
            "diabetes": int(bool(dm)), "smoker": smoker, "egfr": egfr["value"]}
    tries = [smoker] if smoker is not None else [0, 1]
    results = []
    for s in tries:
        res = env.call("prevent_risk", {**args, "smoker": s})
        results.append({"smoker": s, "tool_result": res})
    out["computed"] = all("error" not in r["tool_result"] for r in results)
    out["results"] = results
    return out


def card(n: int, env: CaseEnv) -> dict:
    demo = env.get_demographics()
    cond = _rows(env, "conditions")
    meas = measurements(env)
    shown, older = notes(env)
    allergies = [{k: v for k, v in a.items() if k != "row_id"}
                 for a in env.get_allergies()["items"]]
    return {
        "n": n,
        "age": demo["age"],
        "sex": demo["sex"],
        "index_date": demo["index_date"],
        "diagnoses": _grouped(env, [r for r in cond if r[1] not in SOCIAL_ADMIN]),
        "other_findings": _grouped(env, [r for r in cond if r[1] in SOCIAL_ADMIN],
                                   periods=False),
        "measurements": meas,
        "medications": _grouped(env, _rows(env, "medications")),
        "procedures": sorted(_grouped(env, _rows(env, "procedures"), periods=False,
                                      active=False),
                             key=lambda e: e["last_start"] or "", reverse=True),
        "allergies": allergies,
        "pregnancy": _text_matches(env, PREGNANCY_TEXT),
        "hospice_palliative": _text_matches(env, HOSPICE_TEXT),
        "notes": shown,
        "older_notes_count": older,
        "prevent": prevent_block(env, meas),
    }


# ---------------------------------------------------------------- main
def _rel(p: Path) -> str:
    p = p.resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.name


INLINE_CHARS = 200  # a list or object whose one-line form is at most this long stays on one line


def _has_dict(o) -> bool:
    return isinstance(o, dict) or (isinstance(o, list) and any(_has_dict(x) for x in o))


def _fmt(o, ind: int = 0) -> str:
    """JSON with short lists and objects, and lists without objects, on one line."""
    one = json.dumps(o, ensure_ascii=False, separators=(", ", ": "))
    if not isinstance(o, (dict, list)) or not o:
        return one
    if len(one) <= INLINE_CHARS or not _has_dict(o):
        return one
    pad, sub = " " * ind, " " * (ind + 1)
    if isinstance(o, dict):
        body = [f"{sub}{json.dumps(k, ensure_ascii=False)}: {_fmt(v, ind + 1)}"
                for k, v in o.items()]
        return "{\n" + ",\n".join(body) + f"\n{pad}}}"
    body = [f"{sub}{_fmt(v, ind + 1)}" for v in o]
    return "[\n" + ",\n".join(body) + f"\n{pad}]"


def _dump(obj) -> str:
    return _fmt(obj) + "\n"


PAGE_PICK = ROOT / "site" / "card_pick.json"


def page_patients(cases: list[dict], pick: Path = PAGE_PICK) -> set[str]:
    """Base patients of every case named on the project page (site/card_pick.json)."""
    if not pick.exists():
        return set()
    named = set(re.findall(r"case_[0-9a-f]{12}", pick.read_text()))
    return {c["base_patient"] for c in cases if c["case_id"] in named}


def build(case_dir: Path) -> tuple[dict, dict]:
    cases = read_cases(case_dir)
    excluded = page_patients(cases)
    picked = select(cases, excluded)
    cards, key = [], []
    for n, c in enumerate(picked, 1):
        env = CaseEnv.from_case(case_dir, c["case_id"], page_size=ALL_ROWS)
        try:
            cards.append(card(n, env))
        finally:
            env.close()
        key.append({"n": n, "case_id": c["case_id"], "expected_status": c["expected_status"],
                    "expected_row": c["expected_row"], "condition": c["condition"]})
    meta = {"case_set": _rel(case_dir), "cards": len(cards),
            "patients_excluded_as_shown_on_page": len(excluded),
            "built_by": "scripts/hand_label_cards.py",
            "note_window_days": NOTE_WINDOW_DAYS}
    return {"meta": meta, "cards": cards}, {"meta": meta, "cards": key}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case-dir", default=DEFAULT_CASE_DIR,
                    help="case set directory (repository-relative or absolute)")
    ap.add_argument("--out", required=True, help="output directory")
    a = ap.parse_args(argv)
    case_dir = Path(a.case_dir)
    if not case_dir.is_absolute() and not case_dir.exists():
        case_dir = ROOT / case_dir
    cards, key = build(case_dir)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    text = _dump(cards)
    (out / "cards.json").write_text(text, encoding="utf-8")
    key["meta"] = {**key["meta"],
                   "selection": f"all cases sorted by sha256('{SELECT_SALT}:<case_id>') "
                                f"ascending; take a case if its expected status has fewer "
                                f"than {PER_STATUS} taken, its base patient is not taken and is not shown on "
                                f"the project page (site/card_pick.json); "
                                f"stop at {PER_STATUS} per status; display order by "
                                f"sha256('{ORDER_SALT}:<case_id>')",
                   "cases_jsonl_sha256": hashlib.sha256(
                       (case_dir / "cases.jsonl").read_bytes()).hexdigest(),
                   "cards_json_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    (out / "key.json").write_text(_dump(key), encoding="utf-8")
    print(f"wrote {len(cards['cards'])} cards: cards.json ({len(text.encode())} bytes), key.json")


if __name__ == "__main__":
    main()
