"""Agent tools over one materialized case (SPEC 6, A1 row).

Every tool is a fixed, parameterized DuckDB query on the case database made
by `cases.materialize` (no free SQL). Results are compact JSON-ready dicts
with record row ids, and long lists are paginated. The internal `patch`
column is never shown.

`CaseEnv` also holds the fixed queries behind the ABSENT named lists
(`named_list`), used by the verifier (SPEC 6.2 check 4). They read the same
facts module as the oracle (`facts.facts_at_index`), so "absent" means the
same thing everywhere.

Tools (name -> JSON schema in `TOOL_SPECS`):
  get_demographics()
  search_conditions(query | codes, page)        conditions, procedures, encounters
  get_observations(code, since, page)
  get_medications(active_only, page)
  get_allergies()
  search_notes(query, since, page)
  read_note(note_id)
  prevent_risk(sex, age, tc, hdl, sbp, bp_treated, statin, diabetes, smoker, egfr, sources)
  guideline_lookup(row)
"""
from __future__ import annotations

import json
import math
import time
from datetime import date
from pathlib import Path

from . import codes as C
from . import facts as F
from . import prevent
from . import schema as S

PAGE_SIZE = 20
SNIPPET_CHARS = 80
MAX_SNIPPETS = 3
NOTE_TABLE_COLUMNS = ("note_id", "DATE", "TEXT")
REASON_WINDOW_DAYS = 365          # SPEC 3.3
PREGNANCY_REASON_AGES = (30, 49)  # SPEC 3.3


class ToolError(ValueError):
    """Bad arguments; returned to the model as {"error": ...}."""


def dumps(obj) -> str:
    """Compact, stable JSON (the form tool results are sent in)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _d10(x) -> str | None:
    if x is None:
        return None
    s = str(x)
    return None if s in ("", "None", "nan", "NaT") else s[:10]


def _iso(x, name: str) -> str | None:
    if x is None or x == "":
        return None
    try:
        return date.fromisoformat(str(x)[:10]).isoformat()
    except ValueError as e:
        raise ToolError(f"{name} must be an ISO date (YYYY-MM-DD)") from e


def _page(rows: list, page, size: int) -> dict:
    try:
        page = 1 if page is None else int(page)
    except (TypeError, ValueError) as e:
        raise ToolError("page must be an integer >= 1") from e
    if page < 1:
        raise ToolError("page must be an integer >= 1")
    n = len(rows)
    pages = max(1, math.ceil(n / size))
    return {"total": n, "page": page, "pages": pages,
            "items": rows[(page - 1) * size: page * size]}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return v
    return int(x) if x.is_integer() else x


# ---------------------------------------------------------------- schemas
def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


_PAGE = {"type": "integer", "minimum": 1, "description": "Page number (default 1)."}
_SINCE = {"type": "string", "description": "Only entries on or after this date (YYYY-MM-DD)."}
_FLAG = {"type": "integer", "enum": [0, 1]}

TOOL_SPECS: dict[str, dict] = {
    "get_demographics": {
        "description": "Index date, age at index, sex and birth date. Cite age and sex with "
                       "the source 'demographics'.",
        "parameters": _obj({}),
    },
    "search_conditions": {
        "description": "Diagnoses (conditions), procedures and encounters dated on or before "
                       "the index date that match a text query (case-insensitive substring of "
                       "the description) or a list of codes. Returns row ids, codes, start and "
                       "stop dates, and whether the entry is active at index.",
        "parameters": _obj({"query": {"type": "string"},
                            "codes": {"type": "array", "items": {"type": "string"}},
                            "page": _PAGE}),
    },
    "get_observations": {
        "description": "Values of one observation code (LOINC) dated on or before the index "
                       "date, newest first, with row ids, timestamps and units.",
        "parameters": _obj({"code": {"type": "string"}, "since": _SINCE, "page": _PAGE},
                           ["code"]),
    },
    "get_medications": {
        "description": "Medication rows that started on or before the index date, with row "
                       "ids, codes, start and stop dates and whether each is active at index. "
                       "active_only (default true) keeps only rows active at index.",
        "parameters": _obj({"active_only": {"type": "boolean"}, "page": _PAGE}),
    },
    "get_allergies": {
        "description": "Allergy and intolerance rows that started on or before the index date.",
        "parameters": _obj({}),
    },
    "search_notes": {
        "description": "Clinical notes dated on or before the index date, newest first. With a "
                       "query (case-insensitive substring), only notes that contain it, with "
                       "exact text snippets around each match. Without a query, a list of "
                       "notes.",
        "parameters": _obj({"query": {"type": "string"}, "since": _SINCE, "page": _PAGE}),
    },
    "read_note": {
        "description": "Full text of one note.",
        "parameters": _obj({"note_id": {"type": "string"}}, ["note_id"]),
    },
    "prevent_risk": {
        "description": "PREVENT base-model 10-year ASCVD risk (and 30-year risk at ages 30-59) "
                       "as fractions. Give every input and, in `sources`, the record row ids "
                       "(or 'demographics', or an ABSENT list name) each input comes from.",
        "parameters": _obj({
            "sex": {"type": "string", "enum": ["male", "female"]},
            "age": {"type": "number"}, "tc": {"type": "number"}, "hdl": {"type": "number"},
            "sbp": {"type": "number"}, "bp_treated": _FLAG, "statin": _FLAG,
            "diabetes": _FLAG, "smoker": _FLAG, "egfr": {"type": "number"},
            "sources": {"type": "object", "additionalProperties": {
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}},
        }, list(S.PREVENT_ARGS)),
    },
    "guideline_lookup": {
        "description": "One decision-table row: rule, label and the quoted guideline sources.",
        "parameters": _obj({"row": {"type": "string"}}, ["row"]),
    },
}
TOOL_NAMES = tuple(TOOL_SPECS)


def tool_specs(names=TOOL_NAMES) -> list[dict]:
    """Neutral tool definitions [{name, description, parameters}] for a backend."""
    return [{"name": n, **TOOL_SPECS[n]} for n in names]


# ---------------------------------------------------------------- PREVENT
def compute_prevent(args: dict) -> dict:
    """PREVENT risks from prevent_risk arguments (shared by the tool and the verifier)."""
    a = dict(args)
    sex = a.get("sex")
    sex = {"M": "male", "F": "female"}.get(sex, sex)
    if sex not in ("male", "female"):
        raise ToolError("sex must be 'male' or 'female'")
    flags = {}
    for k in ("bp_treated", "statin", "diabetes", "smoker"):
        v = a.get(k)
        if v not in (0, 1, True, False):
            raise ToolError(f"{k} must be 0 or 1")
        flags[k] = int(v)
    nums = {}
    for k in ("age", "tc", "hdl", "sbp", "egfr"):
        v = a.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ToolError(f"{k} must be a number")
        nums[k] = float(v)
    try:
        r10 = prevent.ascvd_risk(sex, nums["age"], nums["tc"], nums["hdl"], nums["sbp"],
                                 flags["bp_treated"], flags["statin"], flags["diabetes"],
                                 flags["smoker"], nums["egfr"], horizon=10)
    except prevent.PreventRangeError as e:
        raise ToolError(str(e)) from e
    out = {"risk_10y": r10, "risk_30y": None}
    lo, hi = prevent.AGE_RANGE_30_YEAR
    if lo <= nums["age"] <= hi:
        out["risk_30y"] = prevent.ascvd_risk(
            sex, nums["age"], nums["tc"], nums["hdl"], nums["sbp"], flags["bp_treated"],
            flags["statin"], flags["diabetes"], flags["smoker"], nums["egfr"], horizon=30)
    return out


# ---------------------------------------------------------------- environment
class CaseEnv:
    """Tools and fixed queries over one case database (one patient)."""

    def __init__(self, con, page_size: int = PAGE_SIZE):
        self.con = con
        self.page_size = page_size
        row = con.execute("select PATIENT, index_date, birthdate, sex, age from cohort").fetchall()
        if len(row) != 1:
            raise ValueError("case database must hold exactly one patient")
        self.patient, ix, bd, self.sex, self.age = row[0]
        self.index_date = ix if isinstance(ix, date) else date.fromisoformat(str(ix)[:10])
        self.birthdate = bd if isinstance(bd, date) else date.fromisoformat(str(bd)[:10])
        self.ix = self.index_date.isoformat()
        self.calls: list[dict] = []
        self._facts = None

    @classmethod
    def from_case(cls, case_dir: str | Path, case_id: str, **kw) -> "CaseEnv":
        from . import cases
        return cls(cases.materialize(case_dir, case_id), **kw)

    def close(self) -> None:
        self.con.close()

    # ---------------------------------------------------------- dispatch
    def call(self, name: str, args: dict | None) -> dict:
        """Run one tool; errors come back as {"error": ...}. Every call is logged."""
        t0 = time.perf_counter()
        args = dict(args or {})
        try:
            if name not in TOOL_SPECS:
                raise ToolError(f"unknown tool {name!r}")
            allowed = set(TOOL_SPECS[name]["parameters"]["properties"])
            extra = set(args) - allowed
            if extra:
                raise ToolError(f"unknown arguments: {sorted(extra)}")
            result = getattr(self, name)(**args)
        except ToolError as e:
            result = {"error": str(e)}
        except TypeError as e:
            result = {"error": f"bad arguments: {e}"}
        self.calls.append({"tool": name, "args": args, "result": result,
                           "seconds": round(time.perf_counter() - t0, 4)})
        return result

    def facts(self) -> F.PatientFacts:
        if self._facts is None:
            self._facts = F.patient_facts(self.con, self.patient)
        return self._facts

    def _q(self, sql: str, params=()) -> list[tuple]:
        return self.con.execute(sql, list(params)).fetchall()

    # ---------------------------------------------------------- tools
    def get_demographics(self) -> dict:
        return {"source": S.DEMOGRAPHICS, "index_date": self.ix, "age": int(self.age),
                "sex": self.sex, "birthdate": self.birthdate.isoformat()}

    def search_conditions(self, query: str | None = None, codes: list[str] | None = None,
                          page: int = 1) -> dict:
        q = (query or "").strip()
        cs = [str(c).strip() for c in (codes or []) if str(c).strip()]
        if not q and not cs:
            raise ToolError("give a query or a list of codes")
        rows = []
        for table, has_stop in (("conditions", True), ("procedures", True), ("encounters", True)):
            res = self._q(
                f"select row_id, CODE, DESCRIPTION, START, STOP from {table} "
                "where substr(START, 1, 10) <= ? and "
                "((? <> '' and lower(DESCRIPTION) like '%' || lower(?) || '%') "
                " or CODE in (select unnest(?::varchar[]))) "
                "order by START, row_id", [self.ix, q, q, cs])
            for rid, code, desc, st, sp in res:
                st10, sp10 = _d10(st), _d10(sp)
                rows.append({"row_id": rid, "table": table, "code": code, "display": desc,
                             "start": st10, "stop": sp10,
                             "active_at_index": sp10 is None or sp10 > self.ix})
        rows.sort(key=lambda r: (r["start"] or "", r["row_id"]))
        return _page(rows, page, self.page_size)

    def get_observations(self, code: str, since: str | None = None, page: int = 1) -> dict:
        code = str(code).strip()
        if not code:
            raise ToolError("code is required")
        s = _iso(since, "since") or "0000-00-00"
        res = self._q("select row_id, DATE, DESCRIPTION, VALUE, UNITS from observations "
                      "where CODE = ? and substr(DATE, 1, 10) <= ? and substr(DATE, 1, 10) >= ? "
                      "order by DATE desc, row_id", [code, self.ix, s])
        desc = res[0][2] if res else None
        items = [{"row_id": rid, "date": dt, "value": _num(v), "unit": u}
                 for rid, dt, _, v, u in res]
        out = {"code": code, "display": desc}
        out.update(_page(items, page, self.page_size))
        return out

    def get_medications(self, active_only: bool = True, page: int = 1) -> dict:
        res = self._q("select row_id, CODE, DESCRIPTION, START, STOP from medications "
                      "where substr(START, 1, 10) <= ? order by START, row_id", [self.ix])
        items = []
        for rid, code, desc, st, sp in res:
            sp10 = _d10(sp)
            active = sp10 is None or sp10 > self.ix
            if active_only and not active:
                continue
            items.append({"row_id": rid, "code": code, "display": desc, "start": _d10(st),
                          "stop": sp10, "active_at_index": active})
        return _page(items, page, self.page_size)

    def get_allergies(self) -> dict:
        res = self._q("select row_id, CODE, DESCRIPTION, CATEGORY, TYPE, START, STOP "
                      "from allergies where substr(START, 1, 10) <= ? order by START, row_id",
                      [self.ix])
        items = []
        for rid, code, desc, cat, typ, st, sp in res:
            sp10 = _d10(sp)
            items.append({"row_id": rid, "code": code, "display": desc, "category": cat,
                          "type": typ, "start": _d10(st), "stop": sp10,
                          "active_at_index": sp10 is None or sp10 > self.ix})
        return {"total": len(items), "items": items}

    def _notes(self, since: str | None) -> list[tuple[str, str, str]]:
        s = _iso(since, "since") or "0000-00-00"
        return self._q("select note_id, substr(DATE, 1, 10) as d, TEXT from notes "
                       "where substr(DATE, 1, 10) <= ? and substr(DATE, 1, 10) >= ? "
                       "order by d desc, note_id", [self.ix, s])

    @staticmethod
    def _encounter_line(text: str) -> str | None:
        for line in text.splitlines():
            if line.startswith("Encounter: "):
                return line[len("Encounter: "):]
        return None

    def search_notes(self, query: str | None = None, since: str | None = None,
                     page: int = 1) -> dict:
        q = (query or "").strip()
        items = []
        for nid, d, text in self._notes(since):
            item = {"note_id": nid, "date": d, "encounter": self._encounter_line(text)}
            if q:
                low, ql = text.lower(), q.lower()
                pos, snips = 0, []
                while len(snips) < MAX_SNIPPETS:
                    i = low.find(ql, pos)
                    if i < 0:
                        break
                    a = max(0, i - SNIPPET_CHARS)
                    b = min(len(text), i + len(q) + SNIPPET_CHARS)
                    snips.append(text[a:b])
                    pos = i + len(q)
                if not snips:
                    continue
                item["snippets"] = snips
            items.append(item)
        return _page(items, page, self.page_size)

    def read_note(self, note_id: str) -> dict:
        res = self._q("select note_id, substr(DATE, 1, 10), TEXT from notes "
                      "where note_id = ? and substr(DATE, 1, 10) <= ?", [str(note_id), self.ix])
        if not res:
            raise ToolError(f"no note {note_id!r}")
        nid, d, text = res[0]
        return {"note_id": nid, "date": d, "text": text}

    def prevent_risk(self, sources: dict | None = None, **inputs) -> dict:
        missing = [k for k in S.PREVENT_ARGS if k not in inputs]
        if missing:
            raise ToolError(f"missing inputs: {missing}")
        r = compute_prevent(inputs)
        out = {"inputs": {k: inputs[k] for k in S.PREVENT_ARGS},
               "risk_10y": round(r["risk_10y"], 6),
               "risk_10y_percent": round(100 * r["risk_10y"], 2)}
        if r["risk_30y"] is not None:
            out["risk_30y"] = round(r["risk_30y"], 6)
            out["risk_30y_percent"] = round(100 * r["risk_30y"], 2)
        out["sources"] = sources or {}
        return out

    def guideline_lookup(self, row: str) -> dict:
        from .oracle import ROW_BY_ID  # table data only
        r = ROW_BY_ID.get(str(row).strip())
        if r is None or r.id == "X":
            raise ToolError("unknown row; rows are " + ", ".join(
                k for k in ROW_BY_ID if k != "X"))
        return {"row": r.id, "group": r.group, "rule": r.rule, "label": r.label,
                "encoding": r.encoding,
                "sources": [{"section": s.section, "rec": s.rec, "cor": s.cor, "loe": s.loe,
                             "quote": s.quote} for s in r.sources]}

    # ---------------------------------------------------------- fixed queries
    def named_list(self, name: str) -> list[str]:
        """Row ids that make an ABSENT named list non-empty (empty list = absent)."""
        f = self.facts()
        ids = lambda rows: [r.row_id for r in rows]  # noqa: E731
        if name == "statins":
            return ids(f.statins)
        if name == "ascvd_codes":
            return ids(f.ascvd)
        if name == "diabetes_codes":
            return ids(f.diabetes)
        if name == "ckd_codes":
            return ids(f.ckd)
        if name == "bp_drugs":
            return ids(f.bp_drugs)
        if name == "row5_feature_codes":
            return ids(f.retinopathy + f.neuropathy + f.albuminuria)
        if name == "hiv_codes":
            return ids(f.hiv)
        if name == "esrd_codes":
            return ids(f.esrd)
        if name == "lipid_panel":
            return [] if f.lipid_panel is None else list(f.lipid_panel.row_ids)
        if name in ("sbp", "egfr"):
            o = getattr(f, name)
            return [] if o is None else [o.row_id]
        if name == "smoking":
            return [] if f.smoking is None else [f.smoking.obs.row_id]
        if name == "reasons_365d":
            return self.structured_reasons()
        raise KeyError(name)

    def structured_reasons(self) -> list[str]:
        """Structured reason entries dated 0-365 days before index (SPEC 3.3)."""
        f = self.facts()

        def inwin(d):
            return d is not None and 0 <= (self.index_date - d).days <= REASON_WINDOW_DAYS
        out = [a.row_id for a in f.allergies if a.statin_related and inwin(a.start)]
        lo, hi = PREGNANCY_REASON_AGES
        for e in f.reason_entries:
            if e.category == "pregnancy" and not (f.sex == "F" and lo <= f.age <= hi):
                continue
            if inwin(e.date):
                out.append(e.row_id)
        return out

    # ---------------------------------------------------------- record lookups
    def row(self, row_id: str) -> tuple[str, dict] | None:
        """(table, columns) of one record row, or None."""
        for table in ("conditions", "procedures", "medications", "observations", "allergies",
                      "encounters"):
            df = self.con.execute(f"select * from {table} where row_id = ?", [row_id]).df()
            if len(df):
                r = df.iloc[0].to_dict()
                r.pop("patch", None)
                return table, r
        return None

    def note_text(self, note_id: str) -> str | None:
        res = self._q("select TEXT from notes where note_id = ?", [note_id])
        return res[0][0] if res else None


# Code lists per claim, for checking what a cited row is (verifier check 1).
CLAIM_CODES: dict[str, dict[str, set[str]]] = {
    "statins": {"medications": set(C.STATINS)},
    "bp_drugs": {"medications": set(C.BP_DRUGS)},
    "ascvd": {"conditions": set(C.ASCVD_CONDITIONS), "procedures": set(C.ASCVD_PROCEDURES)},
    "diabetes": {"conditions": set(C.DIABETES)},
    "ckd": {"conditions": set(C.CKD_STAGES)},
    "row5_features": {"conditions": set(C.RETINOPATHY) | set(C.NEUROPATHY) | set(C.ALBUMINURIA)
                      | set(C.CKD_STAGES) | set(C.DIABETES)},
    "hiv": {"conditions": set(C.HIV)},
    "esrd": {"conditions": set(C.ESRD_CONDITIONS), "procedures": set(C.ESRD_PROCEDURES)},
    "lipid_panel": {"observations": {C.TC.code, C.HDL.code, C.LDL.code}},
    "sbp": {"observations": {C.SBP.code}},
    "egfr": {"observations": {C.EGFR.code}},
    "smoking": {"observations": {C.SMOKING.code}},
    "reason": {"conditions": set(C.PREGNANCY) | set(C.PALLIATIVE_CONDITIONS),
               "procedures": set(C.PALLIATIVE_PROCEDURES),
               "encounters": set(C.PALLIATIVE_ENCOUNTERS)},  # + statin allergy rows
}
REASON_CATEGORY_OF_CODE = {**{c: "pregnancy" for c in C.PREGNANCY},
                           **{c: "palliative" for c in C.PALLIATIVE_CONDITIONS},
                           **{c: "palliative" for c in C.PALLIATIVE_PROCEDURES},
                           **{c: "palliative" for c in C.PALLIATIVE_ENCOUNTERS}}
