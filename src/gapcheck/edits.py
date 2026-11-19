"""Record edits: conditions C0-C5 of SPEC 5.2, each logged as one ledger row.

A twin is a base patient's record with rows deleted and/or added and,
for C3, C4 and the C5 smoking twin, one sentence planted in one note. The
expected label is the oracle on the edited record (planted reasons are
passed to the oracle from the ledger, since the oracle does not read
notes), checked against the condition's expected direction.

Conditions (who gets one, per base patient):
- C0 clean: every base patient.
- C1 deciding input deleted (S2, S4, S5; not on a statin): every row of one
  open-world input that is deciding (lipid panel, SBP or smoking status;
  one drawn at random if several). Must give CANNOT_DETERMINE with it in
  `missing`.
- C2a non-deciding input deleted (S1, S3, every patient on a statin): the
  same deletion for one present input drawn at random. Must not change.
- C2b smoking status deleted where PREVENT decides and smoking is not
  deciding (S4, S5 at rows 8, 9, 11b). Must not change.
- C3a reason sentence in a note (GAP patients) -> DOCUMENTED_REASON.
- C3b the same kind of sentence (NOT_INDICATED patients). Must not change.
- C4 distractor sentence (GAP and NOT_INDICATED patients). Must not change.
- C5 threshold twins (S2, S4, S5 not on a statin, and near-threshold
  sources), up to one pair per type:
  ldl   two twins with the latest LDL-C set to 185 and 195 (TC moved by the
        same amount), for latest LDL-C within LDL_PAIR_WINDOW of 190;
  risk  base + one twin: latest SBP or TC (with LDL-C by the same amount)
        moved so the 10-year risk lands in [3.5%, 4.5%], crossing 5% (S4)
        or 3% (S5);
  smoking base + one twin: every smoking row in the 3 years before index
        set to current, plus a smoking sentence in a note;
  near  near-threshold sources (off statin, row 9, 10 or 11b, 10-year risk
        within 1.5 points of 5% or 3%; not base cases themselves): two
        twins, SBP or TC moved so risk sits 0.5-1.0 point below and above
        the cut.
  Pairs are used only if their labels differ; every C5 twin is at least
  0.5 point from each risk cut it uses and 5 mg/dL from each LDL-C cut.

Every twin must also keep its label and `missing` for every eGFR 60-140
when the latest eGFR is a P1 value (the base patients' rule, SPEC 4.1).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import codes as C
from . import oracle, prevent, strata
from .facts import PatientFacts, PatientRecord, facts_at_index
from .load import TABLES, row_id
from .notes import Plant, note_id

EDIT_TABLES = ("conditions", "observations", "medications", "procedures", "allergies")
ALL_TABLES = ("patients", "encounters") + EDIT_TABLES
EDIT_PATCH = "edit"  # value of the internal `patch` column on rows added by an edit

CONDITIONS = ("C0", "C1", "C2a", "C2b", "C3a", "C3b", "C4", "C5")
MUST_CHANGE, MUST_NOT_CHANGE, NONE = "must_change", "must_not_change", "none"
DIRECTION = {"C0": NONE, "C1": MUST_CHANGE, "C2a": MUST_NOT_CHANGE, "C2b": MUST_NOT_CHANGE,
             "C3a": MUST_CHANGE, "C3b": MUST_NOT_CHANGE, "C4": MUST_NOT_CHANGE,
             "C5": MUST_CHANGE}

# Rows removed when an open-world input is deleted (C1, C2): every row of
# these codes, whatever its date.
DELETE_CODES: dict[str, dict[str, tuple[str, ...]]] = {
    "lipid_panel": {"observations": (C.TC.code, C.HDL.code, C.LDL.code, C.TG.code)},
    "sbp": {"observations": (C.SBP.code, C.DBP.code, "8478-0")},  # BP panel incl. mean BP
    "smoking": {"observations": (C.SMOKING.code,),
                "conditions": ("449868002",)},  # "Smokes tobacco daily (finding)"
}
DELETABLE = ("lipid_panel", "sbp", "smoking")

# Reasons (SPEC 3.3) and distractors: phrase-library fact types.
REASON_PHRASE_TYPE = {"statin_intolerance": "reason_intolerance",
                      "patient_declined": "reason_declined", "pregnancy": "reason_pregnancy",
                      "liver_failure": "reason_liver", "palliative": "reason_palliative"}
REASON_ORDER = tuple(REASON_PHRASE_TYPE)
DISTRACTOR_KINDS = ("negation", "relative", "deferral", "stable_liver")
PHRASE_TYPES = tuple(REASON_PHRASE_TYPE.values()) + tuple(
    f"distractor_{k}" for k in DISTRACTOR_KINDS) + ("smoking_current",)
NOTE_WINDOW_DAYS = 365

# C5 parameters.
RISK_MARGIN = 0.005          # 0.5 percentage point from every risk cut
LDL_MARGIN = 5.0             # mg/dL from every LDL-C cut
LDL_CUTS = (70.0, 160.0, 190.0)
RISK_CUTS = (oracle.RISK_BORDERLINE, oracle.RISK_INTERMEDIATE, oracle.RISK_HIGH)
RISK_TARGET = (0.035, 0.045)  # 3-5% band, 0.5 point from both cuts
LDL_PAIR = (185.0, 195.0)
LDL_PAIR_WINDOW = 35.0       # latest LDL-C within 190 +/- this
MAX_SBP_MOVE = 30            # mm Hg
MAX_TC_MOVE = 50             # mg/dL
SMOKING_WINDOW_YEARS = 3
PREVENT_LABEL_ROWS = ("8", "9", "11b")
NEAR_STRATUM = "NT"  # near-threshold C5 source (not a base case)


# ---------------------------------------------------------------- phrases
PHRASE_DIR = Path(__file__).with_name("phrases")


def load_phrases(split: str = "dev", path: str | Path | None = None) -> dict:
    """Phrase library of one split (gapcheck.phrases), or a file in the same format."""
    from . import phrases
    if path is None:
        return phrases.load(split)
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    phrases.validate(doc, split)
    return doc


class PhraseShortage(RuntimeError):
    pass


class PhrasePool:
    """Draw phrases without replacement (each phrase at most once per split).

    Order within a type is a seeded shuffle. A draw takes the first unused
    phrase that the patient may receive: sentences of category "pregnancy"
    (reasons and distractors) only in women.
    """

    def __init__(self, lib: dict, seed: int):
        self.version = lib.get("version")
        self.pools = {}
        for t in PHRASE_TYPES:
            items = sorted(lib["types"].get(t, []), key=lambda p: p["id"])
            self.pools[t] = sorted(items, key=lambda p: _h(f"{seed}:{t}:{p['id']}"))
        self.used: set[str] = set()

    def draw(self, ptype: str, sex: str) -> dict:
        for ph in self.pools[ptype]:
            if ph["id"] in self.used or (ph["category"] == "pregnancy" and sex != "F"):
                continue
            self.used.add(ph["id"])
            return ph
        raise PhraseShortage(f"phrase type {ptype}: no unused phrase left for sex {sex}")


# ---------------------------------------------------------------- seeds
def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def rng_for(seed: int, *parts) -> np.random.Generator:
    return np.random.default_rng(int(_h(":".join(str(p) for p in (seed, *parts)))[:16], 16))


# ---------------------------------------------------------------- records
@dataclass(eq=False)
class Base:
    """A base patient: all rows of the 7 tables, cohort fields, facts and label."""
    patient: str
    index_date: date
    birthdate: date
    sex: str
    tables: dict[str, pd.DataFrame]
    stratum: str | None = None
    statin: bool | None = None
    facts: PatientFacts | None = None
    result: oracle.OracleResult | None = None
    near_cut: float | None = None  # near-threshold C5 sources only

    def record(self, tables: dict[str, pd.DataFrame] | None = None) -> PatientRecord:
        t = tables or self.tables
        return PatientRecord(self.patient, self.index_date, self.birthdate, self.sex,
                             **{k: t[k] for k in EDIT_TABLES}, encounters=t["encounters"])

    def encounters_in_window(self, days: int = NOTE_WINDOW_DAYS) -> list[tuple[str, date]]:
        e = self.tables["encounters"]
        out = []
        for eid, st in zip(e["Id"], e["START"]):
            d = date.fromisoformat(str(st)[:10])
            if 0 <= (self.index_date - d).days <= days:
                out.append((str(st), str(eid), d))
        return [(eid, d) for _, eid, d in sorted(out)]


def finish_base(b: Base) -> Base:
    b.facts = facts_at_index(b.record())
    b.result = oracle.label(b.facts)
    return b


@dataclass(frozen=True)
class PlantSpec:
    phrase_type: str
    note_id: str
    note_date: date
    section: str
    slot: int
    reason_category: str | None = None
    phrase_id: str | None = None
    text: str | None = None

    def plant(self) -> Plant:
        if self.text is None:
            raise ValueError("phrase not assigned")
        return Plant(self.note_id, self.section, self.slot, self.text, self.phrase_id)


@dataclass
class Twin:
    patient: str
    condition: str
    variant: str
    deletes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    adds: dict[str, list[dict]] = field(default_factory=dict)
    replaces: dict[str, str] = field(default_factory=dict)  # added row id -> deleted row id
    plants: list[PlantSpec] = field(default_factory=list)
    result: oracle.OracleResult | None = None
    facts: PatientFacts | None = None
    base_status: str | None = None
    check: bool | None = None
    p1_stable: bool | None = None
    pair: str | None = None          # pair id (C5)
    partner: str | None = None       # partner's (condition, variant) key, "C0/clean" for base
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.condition}/{self.variant}"

    @property
    def direction(self) -> str:
        return DIRECTION[self.condition]

    def reasons(self) -> tuple[oracle.Reason, ...]:
        return tuple(oracle.Reason(p.reason_category, p.note_date, p.note_id)
                     for p in self.plants if p.reason_category)


def apply(base: Base, tw: Twin) -> dict[str, pd.DataFrame]:
    """Edited tables of a twin (base tables are not modified)."""
    out = {}
    for t in ALL_TABLES:
        df = base.tables[t]
        dele = set(tw.deletes.get(t, ()))
        adds = tw.adds.get(t, [])
        if not dele and not adds:
            out[t] = df
            continue
        if dele:
            have = set(df.row_id)
            if not dele <= have:
                raise KeyError(f"deleting rows not in {t}: {sorted(dele - have)[:3]}")
            df = df[~df.row_id.isin(dele)]
        if adds:
            df = pd.concat([df, pd.DataFrame(adds, columns=df.columns)], ignore_index=True)
        dcol = TABLES[t][2]
        df = df.sort_values(([dcol] if dcol else []) + ["row_id"], kind="stable")
        out[t] = df.reset_index(drop=True)
    return out


def _content(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in ("row_id", "patch")]


def _val(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA else str(v)


def _replace_rows(base: Base, tw: Twin, table: str, changes: dict[str, dict]) -> None:
    """Replace rows (by row id) with copies whose columns are changed; new row ids."""
    df = base.tables[table]
    cols = _content(df)
    keep = df[~df.row_id.isin(set(changes) | set(tw.deletes.get(table, ())))]
    counts: dict[str, int] = {}
    for r in keep[cols].itertuples(index=False):
        k = "\x1f".join("" if _val(v) is None else _val(v) for v in r)
        counts[k] = counts.get(k, 0) + 1
    adds = tw.adds.setdefault(table, [])
    have = set(df.row_id)
    for old_id in sorted(changes):
        old = df[df.row_id == old_id].iloc[0]
        rec = {c: _val(old[c]) for c in cols}
        rec.update(changes[old_id])
        vals = [rec[c] for c in cols]
        k = "\x1f".join("" if v is None else v for v in vals)
        occ = counts.get(k, 0)
        counts[k] = occ + 1
        rid = row_id(table, vals, occ)
        if rid in have:
            raise RuntimeError(f"edited row id clashes with an existing row: {rid}")
        adds.append({"row_id": rid, "patch": EDIT_PATCH, **rec})
        tw.replaces[rid] = old_id
    tw.deletes[table] = tuple(sorted(set(tw.deletes.get(table, ())) | set(changes)))


def _delete_input(base: Base, tw: Twin, name: str) -> None:
    for table, codes in DELETE_CODES[name].items():
        df = base.tables[table]
        ids = tuple(sorted(df.row_id[df.CODE.isin(codes)]))
        if ids:
            tw.deletes[table] = tuple(sorted(set(tw.deletes.get(table, ())) | set(ids)))


# ---------------------------------------------------------------- evaluation
def p1_stable(f: PatientFacts, reasons=()) -> bool:
    """Label, row and `missing` the same for every eGFR 60-140 (only checked for a P1 eGFR)."""
    if not f.egfr_imputed or f.on_statin:
        return True  # row 0 does not read eGFR
    x = oracle.inputs_from_facts(f)
    window = oracle.applicable_reasons(tuple(reasons) + oracle.reasons_from_facts(f),
                                       f.index_date, f.sex, f.age)
    ok = oracle.usable(x)
    if all(ok.values()):
        if len(set(strata.rows_over_egfr(x))) != 1:
            return False
    ref = None
    vals = strata.P1_EGFR_VALUES if not all(ok.values()) else strata.P1_EGFR_VALUES[[0, -1]]
    for e in vals:
        r = oracle.evaluate(replace(x, egfr=float(e)), has_reason=bool(window))
        k = (r["status"], r["row"], r["missing"])
        if ref is None:
            ref = k
        elif k != ref:
            return False
    return True


def evaluate_twin(base: Base, tw: Twin, partner_status: str | None = None) -> Twin:
    """Oracle on the edited record, direction check and P1-eGFR stability."""
    rec = base.record(apply(base, tw))
    f = facts_at_index(rec)
    reasons = tw.reasons()
    res = oracle.label(f, reasons=tuple(reasons) + oracle.reasons_from_facts(f))
    tw.facts, tw.result, tw.base_status = f, res, base.result.status
    tw.p1_stable = p1_stable(f, reasons)
    b = base.result
    if tw.condition == "C0":
        ok = True
    elif tw.condition == "C1":
        ok = res.status == oracle.CANNOT_DETERMINE and tw.variant in res.missing
    elif tw.condition == "C3a":
        ok = b.status == oracle.GAP and res.status == oracle.DOCUMENTED_REASON
    elif tw.condition == "C5":
        ok = partner_status is not None and res.status != partner_status
    else:
        ok = res.status == b.status and res.missing == b.missing
    tw.check = bool(ok)
    return tw


def margins_ok(f: PatientFacts, res: oracle.OracleResult) -> bool:
    """C5 rule: >= 0.5 point from every risk cut used, >= 5 mg/dL from every LDL-C cut."""
    if f.ldl is None or any(abs(f.ldl - c) < LDL_MARGIN for c in LDL_CUTS):
        return False
    if res.risk10 is not None:
        if any(abs(res.risk10 - c) < RISK_MARGIN for c in RISK_CUTS):
            return False
        if f.age <= 59 and res.risk10 < oracle.RISK_BORDERLINE and res.risk30 is not None \
                and abs(res.risk30 - oracle.RISK_30Y_ELEVATED) < RISK_MARGIN:
            return False
    return True


def silence_zone(f: PatientFacts, res: oracle.OracleResult) -> bool:
    """Label resting on guideline silence (age 60-79, risk < 3%, LDL-C 160-189; SPEC 5.1)."""
    return res.row == "11b" and f.age >= 60 and f.ldl is not None and 160 <= f.ldl < 190


# ---------------------------------------------------------------- builders
def _pick_note(base: Base, rng: np.random.Generator) -> tuple[str, date]:
    win = base.encounters_in_window()
    if not win:
        raise ValueError(f"no note within {NOTE_WINDOW_DAYS} days of index for {base.patient}")
    eid, d = win[int(rng.integers(len(win)))]
    return note_id(eid), d


def _plant_spec(base: Base, rng, ptype: str, category: str | None = None) -> PlantSpec:
    nid, d = _pick_note(base, rng)
    section = ("hpi", "plan")[int(rng.integers(2))]
    return PlantSpec(ptype, nid, d, section, int(rng.integers(1_000_000)), category)


def c0(base: Base) -> Twin:
    return evaluate_twin(base, Twin(base.patient, "C0", "clean"))


def c1(base: Base, seed: int) -> list[Twin]:
    if base.stratum not in ("S2", "S4", "S5") or base.statin:
        return []
    dec = [n for n in DELETABLE if n in base.result.deciding]
    if not dec:
        return []
    rng = rng_for(seed, base.patient, "C1")
    name = dec[int(rng.integers(len(dec)))]
    tw = Twin(base.patient, "C1", name, detail={"deciding": list(dec)})
    _delete_input(base, tw, name)
    return [evaluate_twin(base, tw)]


def _present(base: Base) -> list[str]:
    f = base.facts
    have = {"lipid_panel": f.lipid_panel is not None, "sbp": f.sbp is not None,
            "smoking": f.smoking is not None}
    return [n for n in DELETABLE if have[n]]


def c2a(base: Base, seed: int) -> list[Twin]:
    if not (base.stratum in ("S1", "S3") or base.statin):
        return []
    cand = [n for n in _present(base) if n not in base.result.deciding]
    if not cand:
        return []
    rng = rng_for(seed, base.patient, "C2a")
    name = cand[int(rng.integers(len(cand)))]
    tw = Twin(base.patient, "C2a", name)
    _delete_input(base, tw, name)
    return [evaluate_twin(base, tw)]


def c2b(base: Base, seed: int) -> list[Twin]:
    r = base.result
    if base.stratum not in ("S4", "S5") or base.statin or r.row not in PREVENT_LABEL_ROWS:
        return []
    if base.facts.smoking is None or "smoking" in r.deciding:
        return []
    tw = Twin(base.patient, "C2b", "smoking")
    _delete_input(base, tw, "smoking")
    return [evaluate_twin(base, tw)]


def reason_categories(base: Base) -> tuple[str, ...]:
    """Reason categories that may be planted for this patient (pregnancy: women 30-49)."""
    lo, hi = oracle.PREGNANCY_REASON_AGES
    preg = base.sex == "F" and lo <= base.facts.age <= hi
    return tuple(c for c in REASON_ORDER if c != "pregnancy" or preg)


def c3(base: Base, seed: int, category: str) -> list[Twin]:
    st = base.result.status
    cond = {oracle.GAP: "C3a", oracle.NOT_INDICATED: "C3b"}.get(st)
    if cond is None:
        return []
    if category not in reason_categories(base):
        raise ValueError(f"category {category} not allowed for {base.patient}")
    rng = rng_for(seed, base.patient, cond)
    tw = Twin(base.patient, cond, category,
              plants=[_plant_spec(base, rng, REASON_PHRASE_TYPE[category], category)])
    return [evaluate_twin(base, tw)]


def c4(base: Base, seed: int, kind: str) -> list[Twin]:
    if base.result.status not in (oracle.GAP, oracle.NOT_INDICATED):
        return []
    rng = rng_for(seed, base.patient, "C4")
    tw = Twin(base.patient, "C4", kind, plants=[_plant_spec(base, rng, f"distractor_{kind}")])
    return [evaluate_twin(base, tw)]


def _c5_eligible(base: Base) -> bool:
    return base.stratum in ("S2", "S4", "S5") and not base.statin \
        and base.result.status != oracle.CANNOT_DETERMINE


def _fmt(v: float) -> str:
    return f"{v:.1f}"


def _c5_ok(tw: Twin) -> bool:
    return bool(tw.check and tw.p1_stable and margins_ok(tw.facts, tw.result)
                and not silence_zone(tw.facts, tw.result))


def c5_ldl(base: Base, seed: int) -> list[Twin]:
    f = base.facts
    lp = f.lipid_panel
    if not _c5_eligible(base) or lp is None or lp.tied:
        return []
    ldl, tc = float(lp.ldl.value), float(lp.tc.value)
    if abs(ldl - 190.0) > LDL_PAIR_WINDOW:
        return []
    twins = []
    for target in LDL_PAIR:
        d = target - ldl
        if not 130.0 <= tc + d <= 320.0:
            return []
        tw = Twin(base.patient, "C5", f"ldl{int(target)}",
                  detail={"type": "ldl", "ldl_from": ldl, "ldl_to": target, "tc_from": tc,
                          "tc_to": round(tc + d, 1),
                          "ldl_ge190_elsewhere": len(f.ldl_ge190_earlier)})
        _replace_rows(base, tw, "observations", {lp.ldl.row_id: {"VALUE": _fmt(target)},
                                                 lp.tc.row_id: {"VALUE": _fmt(tc + d)}})
        twins.append(tw)
    lo, hi = twins
    evaluate_twin(base, lo)
    evaluate_twin(base, hi, partner_status=lo.result.status)
    evaluate_twin(base, lo, partner_status=hi.result.status)
    if not (_c5_ok(lo) and _c5_ok(hi)):
        return []
    pair = f"{base.patient}:ldl"
    lo.pair = hi.pair = pair
    lo.partner, hi.partner = hi.key, lo.key
    return [lo, hi]


def _risk_candidates(base: Base, kind: str, target=RISK_TARGET) -> list[tuple[float, dict]]:
    """Moves of SBP or TC (with LDL-C) that put 10-year risk in `target`, nearest first."""
    f = base.facts
    x = oracle.inputs_from_facts(f)
    sex = "male" if x.sex == "M" else "female"
    if kind == "sbp":
        v0 = x.sbp
        vals = np.arange(max(90, int(np.ceil(v0 - MAX_SBP_MOVE))),
                         min(200, int(np.floor(v0 + MAX_SBP_MOVE))) + 1, dtype=float)
        r = prevent.ascvd_risk_array(sex, x.age, x.tc, x.hdl, vals, int(x.bp_treated),
                                     int(x.on_statin), int(x.diabetes), int(x.smoker), x.egfr)
        moves = [(abs(v - v0), v, {"sbp": v}) for v in vals]
    else:
        d = np.arange(-MAX_TC_MOVE, MAX_TC_MOVE + 1, dtype=float)
        tc, ldl = x.tc + d, x.ldl + d
        band = [b for b in oracle.LDL_BANDS if b[0] <= x.ldl < b[1]][0]
        okm = (tc >= 130) & (tc <= 320) & (ldl >= band[0]) & (ldl < band[1])
        okm &= np.min(np.abs(ldl[:, None] - np.array(LDL_CUTS)[None, :]), axis=1) >= LDL_MARGIN
        r = prevent.ascvd_risk_array(sex, x.age, tc, x.hdl, x.sbp, int(x.bp_treated),
                                     int(x.on_statin), int(x.diabetes), int(x.smoker), x.egfr)
        r = np.where(okm, r, np.nan)
        moves = [(abs(dd), dd, {"tc": t, "ldl": l}) for dd, t, l in zip(d, tc, ldl)]
    r = np.broadcast_to(r, (len(moves),))
    keep = [(m, v, ch) for (m, v, ch), rr in zip(moves, r)
            if not np.isnan(rr) and target[0] <= rr <= target[1] and m > 0]
    return [(v, ch) for _, v, ch in sorted(keep, key=lambda t: (t[0], t[1]))]


def _risk_twin(base: Base, kind: str, variant: str, target, partner_status=None):
    """Nearest SBP or TC move into `target` that passes the C5 rules, or None."""
    f = base.facts
    lp = f.lipid_panel
    for v, ch in _risk_candidates(base, kind, target):
        if kind == "sbp":
            tw = Twin(base.patient, "C5", variant,
                      detail={"type": "risk", "input": "sbp", "from": float(f.sbp.value),
                              "to": float(v)})
            _replace_rows(base, tw, "observations", {f.sbp.row_id: {"VALUE": _fmt(v)}})
        else:
            tw = Twin(base.patient, "C5", variant,
                      detail={"type": "risk", "input": "tc", "tc_from": float(lp.tc.value),
                              "tc_to": round(float(ch["tc"]), 1),
                              "ldl_from": float(lp.ldl.value),
                              "ldl_to": round(float(ch["ldl"]), 1)})
            _replace_rows(base, tw, "observations",
                          {lp.tc.row_id: {"VALUE": _fmt(ch["tc"])},
                           lp.ldl.row_id: {"VALUE": _fmt(ch["ldl"])}})
        evaluate_twin(base, tw, partner_status=partner_status)
        if partner_status is None:
            # first member of a two-twin pair: only its own rules here
            if tw.p1_stable and margins_ok(tw.facts, tw.result) \
                    and not silence_zone(tw.facts, tw.result) \
                    and tw.result.status != oracle.CANNOT_DETERMINE:
                return tw
        elif _c5_ok(tw):
            return tw
    return None


def _risk_ready(base: Base) -> bool:
    f = base.facts
    return not (f.sbp is None or f.sbp.n_tied > 1 or f.lipid_panel is None or f.lipid_panel.tied)


def c5_risk(base: Base, seed: int) -> list[Twin]:
    r = base.result
    if not _c5_eligible(base) or r.row not in PREVENT_LABEL_ROWS or not _risk_ready(base):
        return []
    found = {}
    for kind in ("sbp", "tc"):
        tw = _risk_twin(base, kind, f"risk_{kind}", RISK_TARGET, partner_status=r.status)
        if tw is not None:
            found[kind] = tw
    if not found:
        return []
    kinds = sorted(found)
    kind = kinds[int(rng_for(seed, base.patient, "C5", "risk").integers(len(kinds)))]
    tw = found[kind]
    tw.detail["feasible"] = kinds
    tw.detail["risk10_from"] = r.risk10
    tw.detail["risk10_to"] = tw.result.risk10
    tw.pair, tw.partner = f"{base.patient}:risk", "C0/clean"
    return [tw]


# Near-threshold source patients (SPEC 5.2 "and near-threshold patients"; 5.1
# "DISCUSS-zone patients appear only as C5 twins"): not base cases themselves;
# each gives two twins, one on each side of the cut.
NEAR_CUTS = (oracle.RISK_INTERMEDIATE, oracle.RISK_BORDERLINE)
NEAR_ROWS = ("9", "10", "11b")
NEAR_SOURCE_BAND = 0.015     # source risk within 1.5 points of the cut
NEAR_TWIN_BAND = (0.005, 0.010)  # each twin 0.5-1.0 point beyond the cut


def c5_near(base: Base, seed: int, cut: float) -> list[Twin]:
    """Two twins of a near-threshold source: risk moved to just below and just above `cut`."""
    if base.statin or not _risk_ready(base) or base.result.row not in NEAR_ROWS:
        return []
    lo_t = (cut - NEAR_TWIN_BAND[1], cut - NEAR_TWIN_BAND[0])
    hi_t = (cut + NEAR_TWIN_BAND[0], cut + NEAR_TWIN_BAND[1])
    found = {}
    for kind in ("sbp", "tc"):
        lo = _risk_twin(base, kind, f"near{round(cut * 100)}_below_{kind}", lo_t)
        if lo is None:
            continue
        hi = _risk_twin(base, kind, f"near{round(cut * 100)}_above_{kind}", hi_t,
                        partner_status=lo.result.status)
        if hi is None:
            continue
        evaluate_twin(base, lo, partner_status=hi.result.status)
        if _c5_ok(lo) and _c5_ok(hi):
            found[kind] = (lo, hi)
    if not found:
        return []
    kinds = sorted(found)
    kind = kinds[int(rng_for(seed, base.patient, "C5", "near").integers(len(kinds)))]
    lo, hi = found[kind]
    pair = f"{base.patient}:near"
    for a, b in ((lo, hi), (hi, lo)):
        a.pair, a.partner = pair, b.key
        a.detail.update({"source_risk10": base.result.risk10, "risk10_to": a.result.risk10,
                         "cut": cut, "feasible": kinds, "source_row": base.result.row})
    return [lo, hi]


def c5_smoking(base: Base, seed: int) -> list[Twin]:
    r = base.result
    if not _c5_eligible(base) or r.row not in PREVENT_LABEL_ROWS:
        return []
    ix = base.index_date
    start = ix.replace(year=ix.year - SMOKING_WINDOW_YEARS) if not (ix.month == 2 and ix.day == 29) \
        else date(ix.year - SMOKING_WINDOW_YEARS, 2, 28)
    o = base.tables["observations"]
    sm = o[o.CODE == C.SMOKING.code]
    ids = [rid for rid, d in zip(sm.row_id, sm.DATE)
           if start <= date.fromisoformat(str(d)[:10]) <= ix]
    if not ids:
        return []
    cur = C.SMOKING_CURRENT[0]
    changes = {rid: {"VALUE": cur} for rid in ids
               if str(sm.VALUE[sm.row_id == rid].iloc[0]) != cur}
    if not changes:
        return []
    rng = rng_for(seed, base.patient, "C5", "smoking")
    tw = Twin(base.patient, "C5", "smoking", plants=[_plant_spec(base, rng, "smoking_current")],
              detail={"type": "smoking", "rows_set_current": len(changes),
                      "window_start": start.isoformat()})
    _replace_rows(base, tw, "observations", changes)
    evaluate_twin(base, tw, partner_status=r.status)
    if not _c5_ok(tw):
        return []
    tw.detail["risk10_from"] = r.risk10
    tw.detail["risk10_to"] = tw.result.risk10
    tw.pair, tw.partner = f"{base.patient}:smoking", "C0/clean"
    return [tw]


def balanced_choice(counts: dict[str, int], allowed, rng: np.random.Generator) -> str:
    """The allowed option used least so far (ties broken at random)."""
    allowed = list(allowed)
    m = min(counts[a] for a in allowed)
    best = [a for a in allowed if counts[a] == m]
    pick = best[int(rng.integers(len(best)))]
    counts[pick] += 1
    return pick


def build_twins(bases: list[Base], seed: int) -> tuple[list[Twin], dict]:
    """All twins of all base patients (no phrases yet). Bases in sampling order.

    Returns (twins, rejected): `rejected` counts twins dropped by a failed
    check, by condition (C5 pairs that fail their rules are not counted).
    """
    twins: list[Twin] = []
    rejected: dict[str, int] = {}
    c3_counts = {c: {k: 0 for k in REASON_ORDER} for c in ("C3a", "C3b")}
    c4_counts = {k: 0 for k in DISTRACTOR_KINDS}
    for b in bases:
        if b.stratum == NEAR_STRATUM:
            mine = c5_near(b, seed, b.near_cut)
            cond = None
        else:
            mine = [c0(b)] + c1(b, seed) + c2a(b, seed) + c2b(b, seed)
            cond = {oracle.GAP: "C3a", oracle.NOT_INDICATED: "C3b"}.get(b.result.status)
        if cond:
            cat = balanced_choice(c3_counts[cond], reason_categories(b),
                                  rng_for(seed, b.patient, cond, "category"))
            mine += c3(b, seed, cat)
            kind = balanced_choice(c4_counts, DISTRACTOR_KINDS,
                                   rng_for(seed, b.patient, "C4", "kind"))
            mine += c4(b, seed, kind)
        if b.stratum != NEAR_STRATUM:
            mine += c5_ldl(b, seed) + c5_risk(b, seed) + c5_smoking(b, seed)
        for tw in mine:
            if tw.check and tw.p1_stable:
                twins.append(tw)
            else:
                rejected[tw.condition] = rejected.get(tw.condition, 0) + 1
    return twins, rejected


def assign_phrases(twins: list[Twin], pool: PhrasePool, sex: dict[str, str]) -> None:
    """Fill every plant with a phrase of its type, in twin order (sex: patient -> M/F)."""
    for tw in twins:
        new = []
        for p in tw.plants:
            ph = pool.draw(p.phrase_type, sex[tw.patient])
            new.append(replace(p, phrase_id=ph["id"], text=ph["text"]))
        tw.plants = new
