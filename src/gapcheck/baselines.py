"""Rule baselines R0 and R1 (SPEC 6).

R0: the oracle on structured data only. Reasons come only from structured
    entries (`oracle.reasons_from_facts`): allergy/intolerance rows that name a
    statin (active at index, or ended within 365 days), pregnancy conditions,
    hospice or palliative-care entries.
R1: R0 plus keyword and regular-expression rules over notes dated 0-365 days
    before index, one rule set per reason category (SPEC 3.3), with simple
    negation handling and exclusion of family-member context.

R1's rules were written from the dev phrase split only
(`gapcheck.phrases`, split "dev") and are frozen before the test phrasings
are written (SPEC 8). R0 and R1 contain the oracle, so they are correct on
C0, C1, C2 and C5 by construction.

Both return the shared output schema (`gapcheck.schema`).

Notes are dicts {"note_id": str, "date": date | ISO str, "text": str}.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import oracle
from . import schema as S
from .oracle import CANNOT_DETERMINE, DOCUMENTED_REASON, GAP, Reason

RULES_VERSION = "r1-dev-1"

# ---------------------------------------------------------------- R1 rules
_I = re.IGNORECASE

STATIN_TERM = re.compile(
    r"\b(?:statins?|(?:atorva|simva|prava|rosuva|lova|pitava|fluva)(?:statins?)?|lipitor|crestor|"
    r"zocor|pravachol|livalo|mevacor|lescol|cholesterol\s+(?:med\w*|pills?|drugs?)|"
    r"lipid[- ]lowering)\b", _I)

# (pattern, needs a statin term in the same sentence)
RULES: dict[str, tuple[tuple[re.Pattern, bool], ...]] = {
    "statin_intolerance": tuple((re.compile(p, _I), ctx) for p, ctx in (
        (r"\bstatin[- ]?(?:intoleran\w*|myopath\w*|associated\s+muscle\w*|induced\s+\w+)", False),
        (r"\bSAMS\b", False),
        (r"\bintoler[ae]n\w*\s+(?:of|to)\s+statins?\b", False),
        (r"\b(?:not|unable\s+to|could\s+not|couldn't|cannot|can't)\s+tolerat\w*", True),
        (r"\bintoler[ae]n\w*", True),
        (r"\bmyalgi\w*", True),
        (r"\bmyopath\w*", True),
        (r"\brhabdo\w*", True),
        (r"\bmuscles?\s+(?:aches?|aching|pain|weakness|cramps?|cramping|symptoms|problems|soreness)",
         True),
        (r"\bmuscles\s+ache", True),
        (r"\b(?:leg|thigh|calf)\s+(?:cramps?|pain)", True),
        (r"\bcramping\b", True),
        (r"\b(?:CK\s+elevation|elevated\s+CK)\b", True),
        (r"\b(?:due\s+to|because\s+of|d/t|secondary\s+to)\s+(?:side|adverse)\s+effects", True),
        (r"\b(?:adverse|bad|allergic)\s+reaction", True),
    )),
    "patient_declined": tuple((re.compile(p, _I), True) for p in (
        r"\bdeclin\w*",
        r"\brefus\w*",
        r"\b(?:does|did|do)\s+not\s+(?:want|wish)\b",
        r"\b(?:doesn't|didn't|don't)\s+(?:want|wish)\b",
        r"\bnot\s+(?:willing|interested)\b",
        r"\bunwilling\b",
        r"\bopposed\s+to\b",
        r"\bdecided\s+against\b",
        r"\belects?\s+not\s+to\b",
        r"\bsaid\s+no\b",
    )),
    "pregnancy": tuple((re.compile(p, _I), False) for p in (
        r"\bpregn?an(?:t|cy|cies)\b",
        r"\bconce(?:ive|ption)\b",
        r"\btrying\s+for\s+a\s+baby\b",
        r"\bIUP\b",
        r"\bgestational\s+age\b",
        r"\bexpecting\s+(?:her|a|their)\s+(?:\w+\s+)?(?:child|baby)\b",
        r"\bIVF\b",
    )),
    "liver_failure": tuple((re.compile(p, _I), False) for p in (
        r"\bdecomp(?:ensat\w*)?\b",
        r"\b(?:liver|hepatic)\s+failure\b",
        r"\bend[- ]stage\s+liver\b",
        r"\bESLD\b",
        r"\bchild[- ]pugh\s+(?:class\s+)?C\b",
        r"\bascites\b",
        r"\bhepatic\s+encephalopathy\b",
        r"\bvaric(?:es|eal)\b",
        r"\bhepatorenal\b",
        r"\bfulminant\s+hepat\w*",
    )),
    "palliative": tuple((re.compile(p, _I), False) for p in (
        r"\bhospice\b",
        r"\bpall(?:i)?ative\b",
        r"\bcomfort[- ](?:care|measures|focused|oriented)\b",
        r"\b(?:limited|short|shortened|reduced)\s+life\s+expectancy\b",
        r"\blife\s+expectancy\s+(?:is\s+)?(?:short|limited|under|less\s+than|of\s+(?:days|weeks|months))\b",
        r"\bprognosis\s+(?:is\s+)?(?:of\s+)?(?:less\s+than|under|days|weeks|months)\b",
        r"\bterminal\s+(?:diagnosis|illness|cancer|disease)\b",
        r"\bend[- ]of[- ]life\b",
    )),
}
assert set(RULES) == set(oracle.REASON_CATEGORIES)

# Negation: a cue among the last NEG_WINDOW words before the keyword, in the
# same clause, with no comma, colon, parenthesis or "but" in between.
NEG_WINDOW = 5
NEG_CUE = re.compile(r"\b(?:no|not|denies|denied|deny|denys|without|negative|never|free\s+of|"
                     r"absence\s+of|ruled\s+out)\b", _I)
NEG_BREAK = re.compile(r"[,:(]|\bbut\b|\bhowever\b", _I)
# Family-member context: a cue anywhere before the keyword in the same clause.
FAMILY_CUE = re.compile(r"\b(?:mother|mom|father|dad|sister|brother|siblings?|son|daughter|aunt|"
                        r"uncle|grand(?:mother|father|parents?|ma|pa)|cousin|wife|husband|spouse|"
                        r"parents?|relatives?|family\s+(?:history|hx|members?)|fam\s+hx|FHx)\b", _I)
SENTENCE = re.compile(r"[^.!?\n]+")
CLAUSE = re.compile(r"[^;]+")


@dataclass(frozen=True)
class Hit:
    category: str
    start: int       # offset in the note text
    end: int
    quote: str       # exact substring text[start:end]


def _negated(before: str) -> bool:
    parts = NEG_BREAK.split(before)
    scope = parts[-1].split()[-NEG_WINDOW:] if parts else []
    return bool(NEG_CUE.search(" ".join(scope)))


def find_reasons(text: str) -> list[Hit]:
    """Reason mentions in one text: every non-negated, non-family keyword match."""
    hits: list[Hit] = []
    seen = set()
    for sm in SENTENCE.finditer(text):
        sent = sm.group(0)
        has_statin = bool(STATIN_TERM.search(sent))
        for cm in CLAUSE.finditer(sent):
            clause, c0 = cm.group(0), sm.start() + cm.start()
            for cat, rules in RULES.items():
                for pat, needs_statin in rules:
                    if needs_statin and not has_statin:
                        continue
                    for m in pat.finditer(clause):
                        before = clause[:m.start()]
                        if _negated(before) or FAMILY_CUE.search(before):
                            continue
                        key = (cat, c0 + m.start())
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append(Hit(cat, c0 + m.start(), c0 + m.end(), m.group(0)))
    hits.sort(key=lambda h: (h.start, h.category))
    return hits


def categories(text: str) -> set[str]:
    return {h.category for h in find_reasons(text)}


def _date(d) -> date:
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


@dataclass(frozen=True)
class NoteReason:
    reason: Reason
    quote: str


def note_reasons(notes, index_date: date) -> list[NoteReason]:
    """One reason per (note, category) for notes dated 0-365 days before index.

    The quote is the first matching keyword of that category in the note.
    """
    out = []
    for n in notes:
        d = _date(n["date"])
        if not 0 <= (index_date - d).days <= oracle.REASON_WINDOW_DAYS:
            continue
        first: dict[str, Hit] = {}
        for h in find_reasons(n["text"]):
            first.setdefault(h.category, h)
        for cat, h in first.items():
            out.append(NoteReason(Reason(cat, d, n["note_id"]), h.quote))
    return out


# ---------------------------------------------------------------- output
def _value(f, name):
    lp = f.lipid_panel
    if name == "lipid_panel":
        return None if lp is None else {"tc": float(lp.tc.value), "hdl": float(lp.hdl.value),
                                        "ldl": float(lp.ldl.value)}
    if name in ("sbp", "egfr"):
        o = getattr(f, name)
        return None if o is None else float(o.value)
    if name == "smoking":
        return None if f.smoking is None else str(f.smoking.obs.value)
    rows = {"statins": f.statins, "bp_drugs": f.bp_drugs, "hiv": f.hiv, "esrd": f.esrd,
            "ascvd": f.ascvd, "diabetes": f.diabetes, "ckd": f.ckd,
            "row5_features": f.retinopathy + f.neuropathy + f.albuminuria}[name]
    return [r.code for r in rows]


def _input_names(r: oracle.OracleResult) -> list[str]:
    rows = r.path if r.path else ("0", "X", "1") + r.row_set
    names = []
    for row in rows:
        for n in oracle.ROW_INPUTS.get(row, ()):
            names.extend(oracle.PREVENT_INPUTS if n == "prevent" else (n,))
    names.extend(r.deciding + r.missing + r.out_of_range)
    keep = set(names)
    return [n for n in S.INPUT_CLAIMS if n in keep]


def _prevent_args(f) -> dict:
    x = oracle.inputs_from_facts(f)
    return {"sex": "male" if x.sex == "M" else "female", "age": x.age, "tc": x.tc, "hdl": x.hdl,
            "sbp": x.sbp, "bp_treated": int(x.bp_treated), "statin": int(x.on_statin),
            "diabetes": int(x.diabetes), "smoker": int(bool(x.smoker)), "egfr": x.egfr}


_NOTE_TEXT = {
    "ON_THERAPY": "An active statin prescription is on record at the index date. "
                  "No initiation decision is needed.",
    "GAP": "A class 1 recommendation to start a statin applies (row {row}) and no statin or "
           "documented reason is on record. Review with the patient.",
    "DOCUMENTED_REASON": "A class 1 recommendation to start a statin applies (row {row}), but a "
                         "reason for not prescribing is documented ({reason}). Gap with a "
                         "documented reason, review.",
    "DISCUSS": "Only a class 2a or 2b recommendation applies (row {row}). Discuss; not counted "
               "as a gap.",
    "NOT_INDICATED": "No recommendation to start a statin applies (row {row}). No action on "
                     "statin initiation.",
    "CANNOT_DETERMINE": "A deciding input is missing or out of range: {missing}. The status "
                        "cannot be determined until it is recorded.",
}


def _output(f, r: oracle.OracleResult, note_quotes: dict[tuple, str]) -> dict:
    src = oracle._sources(f, _input_names(r))
    ev = []
    for name in _input_names(r):
        if name in ("age", "sex"):
            ev.append(S.item(name, S.DOCUMENTED, [S.DEMOGRAPHICS], getattr(f, name)))
        elif src.get(name):
            ev.append(S.item(name, S.DOCUMENTED, list(src[name]), _value(f, name)))
        else:
            ev.append(S.item(name, S.ABSENT, S.ABSENT_LIST_OF[name], None))
    args = _prevent_args(f)
    for claim, risk in (("risk_10y", r.risk10), ("risk_30y", r.risk30)):
        if risk is not None:
            ev.append(S.item(claim, S.COMPUTED, S.PREVENT_TOOL, float(risk), inputs=args))
    for rs in r.reasons_in_window:
        q = note_quotes.get((rs.source, rs.category))
        if q is None:
            ev.append(S.item("reason", S.DOCUMENTED, [rs.source], rs.category,
                             date=rs.date.isoformat()))
        else:
            ev.append(S.item("reason", S.DOCUMENTED, rs.source, rs.category, quote=q,
                             date=rs.date.isoformat()))
    if r.status == GAP:
        ev.append(S.item("reason", S.ABSENT, "reasons_365d", None))

    status = r.status
    bg = None if status == CANNOT_DETERMINE else r.row
    reason = "" if r.reason is None else f"{r.reason.category}, {r.reason.date.isoformat()}, {r.reason.source}"
    blockers = ", ".join(r.missing + r.out_of_range)
    note = _NOTE_TEXT[status].format(row=bg, reason=reason, missing=blockers)
    return S.validate({
        "status": status, "benefit_group": bg,
        "risk_10y": None if r.risk10 is None else float(r.risk10),
        "missing": list(r.absent), "evidence": ev, "note_for_clinician": note,
    })


def r0(f, variant: str = "main") -> dict:
    """R0: the oracle on structured data only (no notes)."""
    r = oracle.label(f, reasons=oracle.reasons_from_facts(f), variant=variant)
    if r.status == oracle.EXCLUDED:
        raise ValueError("patient is excluded from the cohort (HIV or ESRD)")
    return _output(f, r, {})


def r1(f, notes, variant: str = "main") -> dict:
    """R1: R0 plus note rules for reasons (notes dated 0-365 days before index)."""
    found = note_reasons(notes, f.index_date)
    reasons = oracle.reasons_from_facts(f) + tuple(n.reason for n in found)
    r = oracle.label(f, reasons=reasons, variant=variant)
    if r.status == oracle.EXCLUDED:
        raise ValueError("patient is excluded from the cohort (HIV or ESRD)")
    quotes = {(n.reason.source, n.reason.category): n.quote for n in found}
    return _output(f, r, quotes)


def dev_report(split: str = "dev") -> dict:
    """R1 on the phrase library: per fact type, how many phrases it flags.

    Reason phrases are correct if R1 finds their own category; distractor and
    smoking phrases are correct if R1 finds no reason at all.
    """
    from . import phrases as P
    out = {}
    for t, ps in P.load(split)["types"].items():
        rows = []
        for p in ps:
            cats = categories(p["text"])
            ok = (P.REASON_TYPES[t] in cats) if P.is_reason(t) else not cats
            rows.append({"id": p["id"], "found": sorted(cats), "correct": ok})
        out[t] = {"n": len(rows), "correct": sum(r["correct"] for r in rows),
                  "errors": [r for r in rows if not r["correct"]]}
    return out
