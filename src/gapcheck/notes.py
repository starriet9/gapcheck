"""Clinical notes rendered from the structured record (SPEC 4.1, notes row).

One note per encounter, in Synthea's section layout:
date and encounter; Chief Complaint; History of Present Illness (active
conditions as of the encounter date); Social History (smoking status as of
that date); Allergies; Medications (active on that date); Assessment and
Plan (this encounter's new diagnoses, procedures, prescriptions and the
names of laboratory panels done). No numeric results.

Planted sentences (from the edit ledger) are inserted into the HPI or the
plan at a slot drawn by the edit, so they never sit at a fixed position.

`render_notes` is a pure function of the tables and the plants: any
structured edit reaches every note it affects, and the same input always
gives the same characters. Note ids depend only on the encounter id.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

import pandas as pd

from . import codes as C
from .facts import age_at

NOTE_ID_KEY = "gapcheck-note-id-v1"
SECTIONS = ("hpi", "plan")  # where planted sentences may go

# Laboratory panel names, by LOINC code. Other laboratory codes are named by
# the rules in `panel_name`.
_PANELS = {
    "lipid panel": [C.TC.code, C.HDL.code, C.LDL.code, C.TG.code],
    "hemoglobin A1c": ["4548-4"],
    "eGFR": [C.EGFR.code],
    "urine microalbumin": ["14959-1"],
    "basic metabolic panel": ["2339-0", "6299-2", "38483-4", "49765-1", "2947-0", "6298-4",
                              "2069-3", "20565-8", "2345-7", "3094-0", "2160-0", "17861-6",
                              "2951-2", "2823-3", "2075-0", "2028-9"],
    "hepatic function panel": ["1742-6", "1920-8", "1751-7", "6768-6", "1975-2", "2885-2",
                               "10834-0"],
}
PANEL_OF_CODE = {c: name for name, cs in _PANELS.items() for c in cs}
MARITAL = {"M": "married", "S": "single", "D": "divorced", "W": "widowed"}


def note_id(encounter_id: str) -> str:
    h = hashlib.sha256(f"{NOTE_ID_KEY}\x1f{encounter_id}".encode()).hexdigest()
    return f"note_{h[:16]}"


@dataclass(frozen=True)
class Plant:
    """A sentence planted in one note (one row of the edit ledger)."""
    note_id: str
    section: str   # "hpi" or "plan"
    slot: int      # any non-negative int; position = slot mod (number of places)
    text: str
    phrase_id: str

    def __post_init__(self):
        if self.section not in SECTIONS:
            raise ValueError(f"section must be one of {SECTIONS}")


@dataclass(frozen=True)
class Note:
    note_id: str
    patient: str
    encounter: str
    date: date
    text: str


def panel_name(code: str, description: str) -> str:
    if code in PANEL_OF_CODE:
        return PANEL_OF_CODE[code]
    d = description.lower()
    if "urine" in d:
        return "urinalysis"
    if "automated count" in d or d.startswith(("hemoglobin [mass/volume] in blood", "hematocrit")):
        return "complete blood count"
    return "other laboratory tests"


# ---------------------------------------------------------------- helpers
def _d(x) -> date | None:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NA:
        return None
    s = str(x)
    return None if not s or s in ("nan", "<NA>", "None") else date.fromisoformat(s[:10])


def _s(x) -> str | None:
    if x is None or x is pd.NA or (isinstance(x, float) and pd.isna(x)):
        return None
    return str(x)


def _rows(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    if df is None or df.empty:
        return []
    keep = [c for c in cols if c in df.columns]
    return [dict(zip(keep, (_s(v) for v in r))) for r in df[keep].itertuples(index=False)]


def _clean(text: str) -> str:
    """Display text without the SNOMED semantic tag."""
    return re.sub(r"\s*\((finding|disorder|procedure|situation|regime/therapy|"
                  r"observable entity|morphologic abnormality|qualifier value)\)\s*$", "", text)


def _uniq(items) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def _join(items: list[str]) -> str:
    return "; ".join(items)


def _active(r: dict, d: date) -> bool:
    st, sp = _d(r.get("START")), _d(r.get("STOP"))
    return st is not None and st <= d and (sp is None or sp > d)


def _sentence(prefix: str, items: list[str], empty: str) -> str:
    return f"{prefix}{_join(items)}." if items else empty


# ---------------------------------------------------------------- render
def render_notes(tables: dict[str, pd.DataFrame], plants=()) -> list[Note]:
    """All notes of one patient, ordered by encounter START and encounter id.

    tables: patients (one row), encounters, conditions, observations,
    medications, procedures, allergies (Synthea columns; row_id optional).
    plants: `Plant` records; a plant whose note id is not rendered raises.
    """
    pat = tables["patients"].iloc[0]
    birth = date.fromisoformat(str(pat["BIRTHDATE"])[:10])
    first = re.sub(r"\d+$", "", str(pat.get("FIRST") or "")).strip()
    last = re.sub(r"\d+$", "", str(pat.get("LAST") or "")).strip()
    name = f"{first} {last}".strip() or "The patient"
    sex_word = {"M": "man", "F": "woman"}.get(str(pat.get("GENDER")), "person")
    race = _s(pat.get("RACE")) or ""
    eth = _s(pat.get("ETHNICITY")) or ""
    marital = MARITAL.get(_s(pat.get("MARITAL")) or "")

    enc = sorted(_rows(tables["encounters"], ["Id", "START", "ENCOUNTERCLASS", "DESCRIPTION",
                                               "REASONDESCRIPTION"]),
                 key=lambda r: (r["START"], r["Id"]))
    cond = sorted(_rows(tables["conditions"], ["START", "STOP", "ENCOUNTER", "DESCRIPTION",
                                               "row_id"]),
                  key=lambda r: (r["START"] or "", r["DESCRIPTION"] or "", r.get("row_id") or ""))
    meds = sorted(_rows(tables["medications"], ["START", "STOP", "ENCOUNTER", "DESCRIPTION",
                                                "row_id"]),
                  key=lambda r: (r["START"] or "", r["DESCRIPTION"] or "", r.get("row_id") or ""))
    procs = sorted(_rows(tables["procedures"], ["START", "ENCOUNTER", "DESCRIPTION", "row_id"]),
                   key=lambda r: (r["START"] or "", r["DESCRIPTION"] or "", r.get("row_id") or ""))
    algs = sorted(_rows(tables["allergies"], ["START", "STOP", "DESCRIPTION", "CATEGORY",
                                              "row_id"]),
                  key=lambda r: (r["START"] or "", r["DESCRIPTION"] or "", r.get("row_id") or ""))
    obs = _rows(tables["observations"], ["DATE", "ENCOUNTER", "CATEGORY", "CODE", "DESCRIPTION",
                                         "VALUE", "row_id"])
    smoking = sorted((o for o in obs if o["CODE"] == C.SMOKING.code),
                     key=lambda o: (o["DATE"], o.get("row_id") or ""))
    labs_by_enc: dict[str, list[str]] = {}
    for o in sorted((o for o in obs if o["CATEGORY"] == "laboratory"),
                    key=lambda o: (o["DATE"], o["CODE"])):
        labs_by_enc.setdefault(o["ENCOUNTER"], []).append(panel_name(o["CODE"], o["DESCRIPTION"]))

    by_note: dict[str, list[Plant]] = {}
    for p in plants:
        by_note.setdefault(p.note_id, []).append(p)
    out, seen = [], set()
    for e in enc:
        eid = e["Id"]
        nid = note_id(eid)
        seen.add(nid)
        d = _d(e["START"])
        age = age_at(birth, d)

        # HPI: conditions active on this date, not diagnosed at this encounter.
        hist = _uniq(_clean(c["DESCRIPTION"]) for c in cond
                     if c["ENCOUNTER"] != eid and _active(c, d))
        demo = " ".join(x for x in (f"{name} is a {age}-year-old", race, eth, sex_word) if x)
        hpi = [f"{demo}.",
               _sentence("Patient has a history of: ", hist, "No active chronic conditions.")]

        social = []
        if marital:
            social.append(f"Patient is {marital}.")
        sm = None
        for o in smoking:
            if _d(o["DATE"]) <= d:
                if sm is None or o["DATE"] != sm["DATE"]:
                    sm = o
            else:
                break
        if sm is not None:
            social.append(f"Tobacco smoking status: {_clean(sm['VALUE'])}.")
        if not social:
            social.append("No social history recorded.")

        alg = _uniq(_clean(a["DESCRIPTION"]) + (f" ({a['CATEGORY']})" if a["CATEGORY"] else "")
                    for a in algs if _active(a, d))
        med = _uniq(m["DESCRIPTION"] for m in meds if _active(m, d))

        new_dx = _uniq(_clean(c["DESCRIPTION"]) for c in cond if c["ENCOUNTER"] == eid)
        pr = _uniq(_clean(p["DESCRIPTION"]) for p in procs if p["ENCOUNTER"] == eid)
        rx = _uniq(m["DESCRIPTION"] for m in meds if m["ENCOUNTER"] == eid)
        labs = _uniq(labs_by_enc.get(eid, []))
        plan = []
        if new_dx:
            plan.append(f"Diagnosed at this visit: {_join(new_dx)}.")
        if pr:
            plan.append(f"Procedures performed: {_join(pr)}.")
        if rx:
            plan.append(f"Prescribed: {_join(rx)}.")
        if labs:
            plan.append(f"Laboratory tests done: {_join(labs)}.")
        if not plan:
            plan.append("No new diagnoses, procedures or prescriptions.")

        for p in sorted(by_note.get(nid, []), key=lambda p: (p.section, p.slot, p.phrase_id)):
            if p.section == "hpi":
                k = 1 + p.slot % len(hpi)       # after the first sentence
                hpi.insert(k, p.text)
            else:
                k = p.slot % (len(plan) + 1)
                plan.insert(k, p.text)

        cc = _clean(e["REASONDESCRIPTION"]) if e["REASONDESCRIPTION"] else "No complaints."
        lines = [
            f"Date: {d.isoformat()}",
            f"Encounter: {e['ENCOUNTERCLASS']}, {_clean(e['DESCRIPTION'] or '')}",
            "",
            "# Chief Complaint", cc, "",
            "# History of Present Illness", " ".join(hpi), "",
            "# Social History", " ".join(social), "",
            "# Allergies", "\n".join(f"- {a}" for a in alg) if alg else "No known allergies.", "",
            "# Medications", "\n".join(f"- {m}" for m in med) if med else "No active medications.", "",
            "# Assessment and Plan", "\n".join(plan),
        ]
        out.append(Note(nid, str(pat["Id"]), eid, d, "\n".join(lines) + "\n"))
    missing = set(by_note) - seen
    if missing:
        raise KeyError(f"planted sentence for an unknown note: {sorted(missing)[:3]}")
    return out


def notes_frame(notes: list[Note]) -> pd.DataFrame:
    return pd.DataFrame([{"note_id": n.note_id, "PATIENT": n.patient, "ENCOUNTER": n.encounter,
                          "DATE": n.date.isoformat(), "TEXT": n.text} for n in notes],
                        columns=["note_id", "PATIENT", "ENCOUNTER", "DATE", "TEXT"])
