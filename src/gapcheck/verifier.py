"""Code verifier (SPEC 6.2): provenance and arithmetic, nothing else.

It never evaluates the decision table (it does not import the oracle) and
never reads the edit ledger. It reads only the case record through
`tools.CaseEnv` and the answer itself.

Checks:
1. Every DOCUMENTED source exists and its value or quoted text matches.
   Row ids must be rows of the kind the claim is about (for example a
   statin claim must cite a statin prescription row); note sources need the
   quote as an exact substring of the note.
2. For the row the answer states in `benefit_group`, every input in that
   row's static list (`REQUIRED_INPUTS`) has a DOCUMENTED, COMPUTED or ABSENT
   claim. A required binary input that is missing (smoking status) may
   instead be covered by two prevent_risk results (smoker 0 and 1, all other
   inputs equal) whose 10-year risks fall in the same label band
   (`LABEL_BANDS`: < 3%, 3 to < 5%, >= 5%). Every status other than
   CANNOT_DETERMINE needs a benefit_group ("missing row" otherwise);
   ON_THERAPY needs row 0, and row 0 a DOCUMENTED statin claim. Rows 8 and 9
   share one required-input list, so either may be named when the risk spans
   both (for example smoking missing).
3. Every COMPUTED risk is recomputed from its cited inputs and matches
   within 0.1 percentage point, and each input is backed by a claim in the
   answer: age, sex, lipid panel, SBP, eGFR and smoking by a DOCUMENTED claim
   with that value; statin, BP treatment and diabetes flags by a DOCUMENTED
   (1) or ABSENT (0) claim. The smoker input of a two-call pair needs no
   backing.
4. Every input in `missing` and every ABSENT claim is confirmed by re-running
   the fixed query of its named list (`CaseEnv.named_list`).

Not checked (both would leak the answer): whether a cited value is the
latest one, and whether an absent input was left out of `missing`. ABSENT
"reasons_365d" is checked against structured entries only; no fixed query
reads free text.

Failures are (check, ref) pairs, ref = "evidence[i]", "missing[i]" or None
(check 2). The model is told only these (`feedback`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import codes as C
from . import schema as S
from .tools import CLAIM_CODES, REASON_CATEGORY_OF_CODE, CaseEnv, ToolError, compute_prevent

# Status names (plain strings; the oracle module is not imported).
CANNOT_DETERMINE, ON_THERAPY = "CANNOT_DETERMINE", "ON_THERAPY"
RISK_TOLERANCE = 0.001   # 0.1 percentage point, risks are fractions
VALUE_TOLERANCE = 0.05   # recorded values have one decimal
INPUT_TOLERANCE = 0.5    # a PREVENT input may be the cited value rounded
# Two-call rule (check 2): 10-year risk cuts at which the main-analysis label
# changes (3%: rows 10 and 11a/11b; 5%: rows 9 and 10). 10% is not one: rows 8
# and 9 are both GAP, so two results at 8% and 12% give the same label. A
# static list of cut points, like REQUIRED_INPUTS; the decision table itself is
# never evaluated. The 30-year risk (rows 11a/11b) is not compared.
LABEL_BANDS = (0.03, 0.05)

# Static required-input table (SPEC 6.2 check 2): what a claim of each row
# must cover. Row 0 first, so every other row needs the statin list; then the
# rows before it in the table that could have matched. "risk_10y" needs a
# COMPUTED claim; "reason" applies to GAP rows (a reason, or its absence).
_PREVENT = ("age", "sex", "lipid_panel", "sbp", "bp_drugs", "statins", "diabetes", "smoking",
            "egfr", "risk_10y")
_BASE = ("statins", "ascvd")
_PRIMARY = _BASE + ("lipid_panel", "diabetes", "age")


def _u(*parts) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x for p in parts for x in p))


REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "0": ("statins",),
    "1": _u(_BASE, ("reason",)),
    "2": _u(_BASE, ("lipid_panel", "reason")),
    "3": _u(_PRIMARY, ("reason",)),
    "3b": _u(_PRIMARY, ("ckd", "reason")),
    "4": _PRIMARY,
    "5": _u(_PRIMARY, ("row5_features",), _PREVENT),
    "6": _PRIMARY,
    "7": _PRIMARY,
    "8": _u(_PRIMARY, ("ckd",), _PREVENT, ("reason",)),
    "9": _u(_PRIMARY, ("ckd",), _PREVENT, ("reason",)),
    "10": _u(_PRIMARY, ("ckd",), _PREVENT),
    "11a": _u(_PRIMARY, ("ckd",), _PREVENT),
    "11b": _u(_PRIMARY, ("ckd",), _PREVENT),
}
CLOSED_WORLD = ("statins", "ascvd", "diabetes", "ckd", "row5_features", "bp_drugs", "hiv",
                "esrd", "reason")
BINARY_OPEN = ("smoking",)
FLAG_CLAIM = {"statin": "statins", "bp_treated": "bp_drugs", "diabetes": "diabetes"}
OBS_CODE = {"tc": C.TC.code, "hdl": C.HDL.code, "ldl": C.LDL.code}
_STATIN_ALLERGY = re.compile(C.STATIN_ALLERGY_PATTERN, re.I)


@dataclass(frozen=True)
class Failure:
    check: int
    ref: str | None
    detail: str  # for logs only; never shown to the model

    def to_dict(self) -> dict:
        return {"check": self.check, "ref": self.ref, "detail": self.detail}


@dataclass
class Report:
    failures: list[Failure]
    claim_status: list[dict]  # per evidence item: {"index", "support", "ok", "checks"}

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {"ok": self.ok, "failures": [f.to_dict() for f in self.failures],
                "claims": self.claim_status}


def feedback(report: Report) -> str:
    """What the model is told: check numbers and claim indices only."""
    parts = []
    for f in report.failures:
        parts.append(f"check {f.check}" + (f" ({f.ref})" if f.ref else ""))
    return "; ".join(dict.fromkeys(parts))


# ---------------------------------------------------------------- helpers
def _close(a, b, tol=VALUE_TOLERANCE) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _strip_tag(s: str) -> str:
    return re.sub(r"\s*\([a-z /]+\)\s*$", "", str(s)).strip().lower()


def _smoking_current(v) -> bool | None:
    if isinstance(v, bool) or v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        t = _strip_tag(v)
        if t in {_strip_tag(x) for x in C.SMOKING_CURRENT} or t in ("current", "yes", "true"):
            return True
        if t in {_strip_tag(x) for x in C.SMOKING_NOT_CURRENT} or t in ("not current", "no",
                                                                       "false", "never",
                                                                       "former"):
            return False
    return None


def _sex(v) -> str | None:
    t = str(v).strip().lower()
    return {"m": "M", "male": "M", "f": "F", "female": "F"}.get(t)


def _band(r: float) -> int:
    return sum(r >= c for c in LABEL_BANDS)


# ---------------------------------------------------------------- check 1
def _check_documented(it: dict, env: CaseEnv) -> str | None:
    """None if the DOCUMENTED item checks out, else a reason."""
    claim, src, val = it["claim"], it["source"], it["value"]
    if isinstance(src, str):  # a note with a quote
        text = env.note_text(src)
        if text is None:
            return "note not found"
        if it["quote"] not in text:
            return "quote not in note"
        return None
    if claim in ("risk_10y", "risk_30y"):
        return "a risk must be COMPUTED"
    if S.DEMOGRAPHICS in src:
        if claim not in ("age", "sex") or src != [S.DEMOGRAPHICS]:
            return "demographics cites age or sex only"
        if claim == "age":
            return None if val is None or _close(val, env.age, 0) else "age differs"
        return None if val is None or _sex(val) == env.sex else "sex differs"
    if claim in ("age", "sex"):
        return "age and sex come from demographics"
    rows = []
    for rid in src:
        r = env.row(rid)
        if r is None:
            return f"row {rid} not found"
        rows.append(r)
    # what kind of row each claim may cite
    for table, cols in rows:
        code = str(cols.get("CODE"))
        if claim == "reason" and table == "allergies":
            if not _STATIN_ALLERGY.search(str(cols.get("DESCRIPTION"))):
                return "allergy row does not mention a statin"
            continue
        if code not in CLAIM_CODES.get(claim, {}).get(table, set()):
            return f"row {cols.get('row_id')} is not a {claim} row"
        if claim == "egfr" and cols.get("UNITS") != C.EGFR_UNIT_KEEP:
            return "eGFR row in the wrong unit series"
    return _check_value(claim, val, rows)


def _obs_values(rows, code) -> list:
    return [cols.get("VALUE") for t, cols in rows if t == "observations" and cols.get("CODE") == code]


def _check_value(claim: str, val, rows) -> str | None:
    if val is None:
        return None
    if claim == "lipid_panel":
        if isinstance(val, dict):
            for k, v in val.items():
                if k not in OBS_CODE:
                    return f"unknown lipid key {k}"
                if not any(_close(v, x) for x in _obs_values(rows, OBS_CODE[k])):
                    return f"{k} value differs"
            return None
        if _is_num(val):
            vals = [cols.get("VALUE") for _, cols in rows]
            return None if any(_close(val, x) for x in vals) else "value differs"
        return "lipid value must be a dict or a number"
    if claim in ("sbp", "egfr"):
        code = C.SBP.code if claim == "sbp" else C.EGFR.code
        return None if any(_close(val, x) for x in _obs_values(rows, code)) else "value differs"
    if claim == "smoking":
        want = _smoking_current(val)
        got = [_smoking_current(x) for x in _obs_values(rows, C.SMOKING.code)]
        if isinstance(val, str) and any(_strip_tag(val) == _strip_tag(x)
                                        for x in _obs_values(rows, C.SMOKING.code)):
            return None
        return None if want is not None and want in got else "value differs"
    if claim == "reason":
        cats = set()
        for t, cols in rows:
            cats.add("statin_intolerance" if t == "allergies"
                     else REASON_CATEGORY_OF_CODE.get(str(cols.get("CODE"))))
        return None if not isinstance(val, str) or val in cats else "reason category differs"
    # closed-world lists: codes, displays or a CKD stage
    keys = set()
    for _, cols in rows:
        keys |= {str(cols.get("CODE")), str(cols.get("DESCRIPTION")).lower()}
    items = val if isinstance(val, list) else [val]
    for v in items:
        if isinstance(v, bool):
            continue  # a plain yes/no
        if _is_num(v):
            if claim == "ckd":
                stages = {int(C.CKD_STAGES[c].group) for c in keys if c in C.CKD_STAGES}
                if int(v) not in stages:
                    return "CKD stage differs"
                continue
            return "value differs"
        if str(v) not in keys and str(v).lower() not in keys:
            return "value differs"
    return None


# ---------------------------------------------------------------- check 3
def _backing(ev: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for it in ev:
        by.setdefault(it["claim"], []).append(it)
    return by


def _input_backed(arg: str, v, by: dict) -> bool:
    doc = [it for it in by.get({"tc": "lipid_panel", "hdl": "lipid_panel"}.get(arg, arg), [])
           if it["support"] == S.DOCUMENTED]
    if arg in FLAG_CLAIM:
        items = by.get(FLAG_CLAIM[arg], [])
        if int(v) == 1:
            return any(it["support"] == S.DOCUMENTED for it in items)
        return any(it["support"] == S.ABSENT for it in items)
    if arg == "sex":
        return any(_sex(it["value"]) == _sex(v) for it in by.get("sex", [])
                   if it["support"] == S.DOCUMENTED)
    if arg == "age":
        return any(_close(it["value"], v, INPUT_TOLERANCE) for it in by.get("age", [])
                   if it["support"] == S.DOCUMENTED)
    if arg == "smoker":
        return any(_smoking_current(it["value"]) == bool(v) for it in by.get("smoking", [])
                   if it["support"] == S.DOCUMENTED)
    if arg in ("tc", "hdl"):
        for it in doc:
            x = it["value"]
            if isinstance(x, dict) and _close(x.get(arg), v, INPUT_TOLERANCE):
                return True
        return False
    return any(_close(it["value"], v, INPUT_TOLERANCE) for it in doc)  # sbp, egfr


def _smoking_pairs(ev: list[dict]) -> set[int]:
    """Indices of COMPUTED risk_10y items in a two-call smoking pair with equal bands."""
    comp = [(i, it) for i, it in enumerate(ev)
            if it["support"] == S.COMPUTED and it["claim"] == "risk_10y"]
    out = set()
    for i, a in comp:
        for j, b in comp:
            if i >= j:
                continue
            ia, ib = a["inputs"], b["inputs"]
            if {int(bool(ia["smoker"])), int(bool(ib["smoker"]))} != {0, 1}:
                continue
            if any(ia[k] != ib[k] for k in S.PREVENT_ARGS if k != "smoker"):
                continue
            try:
                ra, rb = compute_prevent(ia)["risk_10y"], compute_prevent(ib)["risk_10y"]
            except ToolError:
                continue
            if _band(ra) == _band(rb):
                out |= {i, j}
    return out


def _check_computed(i: int, it: dict, by: dict, pairs: set[int]) -> str | None:
    try:
        r = compute_prevent(it["inputs"])
    except ToolError as e:
        return f"inputs invalid: {e}"
    key = "risk_30y" if it["claim"] == "risk_30y" else "risk_10y"
    if r[key] is None or abs(r[key] - float(it["value"])) > RISK_TOLERANCE:
        return "risk does not match recomputation"
    for arg, v in it["inputs"].items():
        if arg == "smoker" and i in pairs:
            continue
        if not _input_backed(arg, v, by):
            return f"input {arg} not backed by a claim"
    return None


# ---------------------------------------------------------------- main
def verify(out: dict, env: CaseEnv) -> Report:
    """Run checks 1-4 on a schema-valid answer."""
    S.validate(out)
    ev = out["evidence"]
    by = _backing(ev)
    pairs = _smoking_pairs(ev)
    fails: list[Failure] = []
    claims = [{"index": i, "claim": it["claim"], "support": it["support"], "ok": True,
               "checks": []} for i, it in enumerate(ev)]

    def fail(check, i, detail):
        ref = None if i is None else (i if isinstance(i, str) else f"evidence[{i}]")
        fails.append(Failure(check, ref, detail))
        if isinstance(i, int):
            claims[i]["ok"] = False
            claims[i]["checks"].append(check)

    for i, it in enumerate(ev):
        s = it["support"]
        if s == S.DOCUMENTED:
            why = _check_documented(it, env)
            if why:
                fail(1, i, why)
        elif s == S.COMPUTED:
            why = _check_computed(i, it, by, pairs)
            if why:
                fail(3, i, why)
        elif s == S.ABSENT:
            if env.named_list(it["source"]):
                fail(4, i, f"named list {it['source']} is not empty")

    for k, name in enumerate(out["missing"]):
        if env.named_list(S.ABSENT_LIST_OF[name]):
            fail(4, f"missing[{k}]", f"{name} is on record")

    bg = out["benefit_group"]
    st = out["status"]
    if bg is None and st != CANNOT_DETERMINE:
        fail(2, None, "missing row: a definite status needs a benefit_group")
    elif st == ON_THERAPY and bg != "0":
        fail(2, None, "ON_THERAPY needs benefit_group 0")
    if bg == "0" and not any(it["support"] == S.DOCUMENTED for it in by.get("statins", [])):
        fail(2, None, "row 0 needs a DOCUMENTED active-statin claim")
    if bg is not None:
        if bg not in REQUIRED_INPUTS:
            fail(2, None, f"benefit_group {bg} has no required-input list")
        else:
            for name in REQUIRED_INPUTS[bg]:
                items = by.get(name, [])
                if name == "risk_10y":
                    ok = any(it["support"] == S.COMPUTED for it in items)
                else:
                    ok = any(it["support"] in (S.DOCUMENTED, S.COMPUTED, S.ABSENT)
                             for it in items)
                if not ok and name in BINARY_OPEN and name in out["missing"] and pairs:
                    ok = True
                if not ok:
                    fail(2, None, f"required input {name} not covered")
    return Report(fails, claims)


def unsupported(report: Report) -> tuple[int, int]:
    """(failing, total) DOCUMENTED and ABSENT claims under checks 1 or 4 (SPEC 7, outcome 6)."""
    rel = [c for c in report.claim_status if c["support"] in (S.DOCUMENTED, S.ABSENT)]
    bad = [c for c in rel if set(c["checks"]) & {1, 4}]
    return len(bad), len(rel)
