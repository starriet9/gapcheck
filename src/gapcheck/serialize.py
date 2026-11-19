"""Serialized record for L1 (SPEC 6, L1 row), and the build-stop check.

Sections, in order:
- demographics (index date, age, sex, birth date);
- conditions and procedures, one line per code (first start, last stop);
- medications, one line per code (first start, last stop, active at index);
- allergies, one line per row;
- the last three values (on or before index) of each observation code on the
  conventions page, with row ids;
- notes dated 0-365 days before index, in full.

Each code line gives row ids for citation: every row for codes on the
conventions page code lists, otherwise the first and the last row (a
repeated screening procedure can have hundreds of rows).

Procedures are listed with conditions because clinical ASCVD includes
revascularization procedures (row 1); the SPEC list names conditions only.

`build_stop(case_dir, case_id, text, ...)` raises `BuildStop` if a row the
oracle uses, or a planted sentence, is not in the text, or if the prompt is
over the context limit (tokens estimated as characters / 4).
"""
from __future__ import annotations

from . import codes as C
from .tools import CaseEnv

NOTE_WINDOW_DAYS = 365
OBS_PER_CODE = 3
OBS_ORDER = ("tc", "hdl", "ldl", "tg", "sbp", "dbp", "bmi", "egfr", "smoking")
CHARS_PER_TOKEN = 4


class BuildStop(RuntimeError):
    pass


def est_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def _d10(x) -> str | None:
    s = None if x is None else str(x)
    return None if not s or s in ("None", "nan") else s[:10]


LISTED_CODES = set().union(*(set(d) for d in C.CODE_LISTS.values()))


def _shown_ids(code: str, ids: list[str], active: list[str] | None = None) -> str:
    """All ids of a listed condition or procedure code; else first, last and active rows."""
    if len(ids) <= 2 or (active is None and code in LISTED_CODES):
        return ", ".join(ids)
    act = [i for i in (active or []) if i not in (ids[0], ids[-1])]
    extra = f", active {', '.join(act)}" if act else ""
    return f"{ids[0]} (first), {ids[-1]} (last){extra}; {len(ids)} rows"


def _per_code(env: CaseEnv, table: str) -> list[tuple]:
    rows = env.con.execute(
        f"select CODE, DESCRIPTION, START, STOP, row_id from {table} "
        "where substr(START, 1, 10) <= ? order by START, row_id", [env.ix]).fetchall()
    by: dict[str, dict] = {}
    for code, desc, st, sp, rid in rows:
        g = by.setdefault(code, {"code": code, "display": desc, "first": _d10(st),
                                 "stops": [], "ids": [], "active": False})
        sp10 = _d10(sp)
        g["stops"].append(sp10)
        g["ids"].append(rid)
        if sp10 is None or sp10 > env.ix:
            g["active"] = True
            g.setdefault("active_ids", []).append(rid)
    out = []
    for g in by.values():
        last = None if None in g["stops"] else max(g["stops"])
        out.append((g["first"], g["code"], g["display"], last, g["active"], g["ids"],
                    g.get("active_ids", [])))
    return sorted(out)


def serialize(env: CaseEnv) -> str:
    L = [f"# Patient record", "",
         f"Index date: {env.ix}",
         f"Demographics (source: demographics): age {env.age}, sex {env.sex}, "
         f"birth date {env.birthdate.isoformat()}", ""]

    L.append("## Conditions and procedures (one line per code: first start, last stop)")
    n = 0
    for table in ("conditions", "procedures"):
        for first, code, desc, last, _, ids, _ in _per_code(env, table):
            L.append(f"- {table[:-1]} {code} {desc} | first start {first} | "
                     f"last stop {last or 'none'} | rows {_shown_ids(code, ids)}")
            n += 1
    if not n:
        L.append("- none")
    L.append("")

    L.append("## Medications (one line per code: first start, last stop, active at index)")
    meds = _per_code(env, "medications")
    for first, code, desc, last, active, ids, act_ids in meds:
        L.append(f"- {code} {desc} | first start {first} | last stop {last or 'none'} | "
                 f"active at index: {'yes' if active else 'no'} | "
                 f"rows {_shown_ids(code, ids, act_ids)}")
    if not meds:
        L.append("- none")
    L.append("")

    L.append("## Allergies and intolerances")
    al = env.get_allergies()["items"]
    for a in al:
        L.append(f"- {a['code']} {a['display']} ({a['category']}) | start {a['start']} | "
                 f"stop {a['stop'] or 'none'} | row {a['row_id']}")
    if not al:
        L.append("- none")
    L.append("")

    L.append(f"## Observations (last {OBS_PER_CODE} values per code, newest first)")
    for key in OBS_ORDER:
        c = C.OBSERVATION_CODES[key]
        res = env.con.execute(
            "select DATE, VALUE, UNITS, row_id from observations where CODE = ? and "
            "substr(DATE, 1, 10) <= ? order by DATE desc, row_id limit ?",
            [c.code, env.ix, OBS_PER_CODE]).fetchall()
        vals = "; ".join(f"{d} {v}{(' ' + u) if u else ''} (row {rid})" for d, v, u, rid in res)
        L.append(f"- {c.code} {c.display}: {vals or 'no values'}")
    L.append("")

    L.append(f"## Notes dated within {NOTE_WINDOW_DAYS} days before index")
    notes = env.con.execute(
        "select note_id, substr(DATE, 1, 10) as d, TEXT from notes where "
        "substr(DATE, 1, 10) <= ? and cast(? as date) - cast(substr(DATE, 1, 10) as date) <= ? "
        "order by d, note_id", [env.ix, env.ix, NOTE_WINDOW_DAYS]).fetchall()
    for nid, d, text in notes:
        L += ["", f"### Note {nid} ({d})", text.rstrip("\n")]
    if not notes:
        L.append("- none")
    return "\n".join(L) + "\n"


def required_items(case_dir, case_id) -> tuple[list[str], list[str]]:
    """Row/note ids the oracle uses for this case, and the planted sentences."""
    from . import cases
    r = cases.oracle_on_case(case_dir, case_id)
    ids = [i for v in r.inputs_used.values() for i in v]
    ids += [x.source for x in r.reasons_in_window]
    texts = [p.text for p in cases.case_plants(case_dir, case_id)]
    return list(dict.fromkeys(ids)), texts


def check(text: str, ids, texts, prompt_tokens: int, context_limit: int, label: str = "") -> None:
    """Raise BuildStop if an id or sentence is not in `text`, or the prompt is too long."""
    lost = [i for i in ids if i not in text]
    lost_txt = [t for t in texts if t not in text]
    if lost or lost_txt:
        raise BuildStop(f"{label}: not in serialization: rows {lost[:5]}, "
                        f"planted sentences {len(lost_txt)}")
    if prompt_tokens > context_limit:
        raise BuildStop(f"{label}: prompt about {prompt_tokens} tokens > limit {context_limit}")


def build_stop(case_dir, case_id, text: str, prompt_tokens: int, context_limit: int) -> dict:
    """The build-stop check for one case of a case set. Returns a summary."""
    ids, texts = required_items(case_dir, case_id)
    check(text, ids, texts, prompt_tokens, context_limit, case_id)
    return {"used_ids": len(ids), "planted": len(texts), "prompt_tokens": prompt_tokens}
