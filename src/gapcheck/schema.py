"""Output schema shared by every system (SPEC 6.1): R0, R1, L1, A1, A2.

An output is a plain dict (JSON-serializable):

    status             one of oracle.STATUSES
    benefit_group      oracle row id the system applies (oracle.ROW_BY_ID), or None
    risk_10y           PREVENT 10-year ASCVD risk as a fraction (0-1), or None
    missing            list of absent open-world inputs (subset of OPEN_INPUTS)
    evidence           list of evidence items (below)
    note_for_clinician two sentences

Evidence item:

    claim    what the item is about: one of CLAIMS (an input name, "risk_10y",
             "risk_30y" or "reason")
    support  DOCUMENTED | COMPUTED | ABSENT | INFERRED
    source   DOCUMENTED: list of record row ids, or one note id (with `quote`),
                         or [DEMOGRAPHICS] for age and sex
             COMPUTED:   "prevent_risk"
             ABSENT:     one named list (ABSENT_LISTS)
             INFERRED:   None (allowed only outside the inputs required by
                         benefit_group; the verifier checks that)
    value    the value claimed (number, string, list or dict), or None
    quote    DOCUMENTED from a note only: the exact substring of the note
    inputs   COMPUTED only: the prevent_risk arguments (PREVENT_ARGS -> value)
    date     optional ISO date of the cited entry (reasons)

`validate(out)` checks shape and vocabulary only; whether sources exist and
quotes match is the verifier's job (SPEC 6.2).
"""
from __future__ import annotations

from .oracle import OPEN_INPUTS, ROW_BY_ID, STATUSES

DOCUMENTED, COMPUTED, ABSENT, INFERRED = "DOCUMENTED", "COMPUTED", "ABSENT", "INFERRED"
SUPPORTS = (DOCUMENTED, COMPUTED, ABSENT, INFERRED)
DEMOGRAPHICS = "demographics"
PREVENT_TOOL = "prevent_risk"

# Named lists an ABSENT claim may cite. The first five are named in SPEC 6.1;
# the others cover the remaining closed-world inputs and the open-world
# observation series, so every required input can be cited.
ABSENT_LISTS = {
    "statins": "active statin prescriptions at index",
    "ascvd_codes": "clinical ASCVD codes on or before index",
    "diabetes_codes": "active diabetes codes at index",
    "ckd_codes": "active CKD stage codes at index",
    "reasons_365d": "documented reasons for not prescribing, 0-365 days before index",
    "bp_drugs": "active BP-lowering prescriptions at index",
    "row5_feature_codes": "active retinopathy, neuropathy or albuminuria codes at index",
    "hiv_codes": "HIV codes on or before index",
    "esrd_codes": "ESRD, dialysis or kidney transplant codes on or before index",
    "lipid_panel": "lipid panels (TC, HDL-C, LDL-C) on or before index",
    "sbp": "systolic BP values on or before index",
    "smoking": "smoking status values on or before index",
    "egfr": "eGFR values (mL/min/{1.73_m2}) on or before index",
}
# Input name (oracle.ROW_INPUTS / PREVENT_INPUTS) -> ABSENT list.
ABSENT_LIST_OF = {
    "statins": "statins", "ascvd": "ascvd_codes", "diabetes": "diabetes_codes",
    "ckd": "ckd_codes", "bp_drugs": "bp_drugs", "row5_features": "row5_feature_codes",
    "hiv": "hiv_codes", "esrd": "esrd_codes", "lipid_panel": "lipid_panel", "sbp": "sbp",
    "smoking": "smoking", "egfr": "egfr", "reason": "reasons_365d",
}
INPUT_CLAIMS = ("age", "sex", "statins", "hiv", "esrd", "ascvd", "diabetes", "ckd",
                "row5_features", "bp_drugs", "lipid_panel", "sbp", "smoking", "egfr")
CLAIMS = INPUT_CLAIMS + ("risk_10y", "risk_30y", "reason")
PREVENT_ARGS = ("sex", "age", "tc", "hdl", "sbp", "bp_treated", "statin", "diabetes",
                "smoker", "egfr")
KEYS = ("status", "benefit_group", "risk_10y", "missing", "evidence", "note_for_clinician")
ITEM_KEYS = {"claim", "support", "source", "value", "quote", "inputs", "date"}


def item(claim: str, support: str, source=None, value=None, **extra) -> dict:
    """One evidence item (validated)."""
    it = {"claim": claim, "support": support, "source": source, "value": value, **extra}
    validate_item(it)
    return it


def validate_item(it: dict) -> None:
    if not isinstance(it, dict) or not {"claim", "support", "source", "value"} <= set(it):
        raise ValueError(f"evidence item needs claim, support, source, value: {it!r}")
    if set(it) - ITEM_KEYS:
        raise ValueError(f"unknown evidence keys {sorted(set(it) - ITEM_KEYS)}")
    if it["claim"] not in CLAIMS:
        raise ValueError(f"unknown claim {it['claim']!r}")
    s, src = it["support"], it["source"]
    if s not in SUPPORTS:
        raise ValueError(f"unknown support {s!r}")
    if s == DOCUMENTED:
        if isinstance(src, str):
            if not isinstance(it.get("quote"), str) or not it["quote"]:
                raise ValueError(f"DOCUMENTED source \"{src}\" is a string, which means a note id and "
                                 f"needs a non-empty quote; cite record rows as a list of row ids, "
                                 f"e.g. [\"{src}\"]")
        elif not (isinstance(src, list) and src and all(isinstance(x, str) and x for x in src)):
            raise ValueError(f"DOCUMENTED source must be row ids or a note id: {src!r}")
    elif s == COMPUTED:
        if src != PREVENT_TOOL or not isinstance(it.get("inputs"), dict):
            raise ValueError("COMPUTED needs source 'prevent_risk' and an inputs dict")
        if set(it["inputs"]) != set(PREVENT_ARGS):
            raise ValueError(f"COMPUTED inputs must be {PREVENT_ARGS}")
        if not isinstance(it["value"], (int, float)) or not 0 <= it["value"] <= 1:
            raise ValueError("COMPUTED value must be a risk fraction")
    elif s == ABSENT:
        if src not in ABSENT_LISTS:
            raise ValueError(f"ABSENT source must be a named list: {src!r}")
    elif src is not None:
        raise ValueError("INFERRED has no source")
    if "quote" in it and s != DOCUMENTED:
        raise ValueError("quote only on DOCUMENTED items")


def validate(out: dict) -> dict:
    """Raise ValueError if `out` breaks the schema; return it unchanged."""
    if not isinstance(out, dict) or set(out) != set(KEYS):
        raise ValueError(f"output keys must be {KEYS}: {sorted(out) if isinstance(out, dict) else out!r}")
    if out["status"] not in STATUSES:
        raise ValueError(f"unknown status {out['status']!r}")
    bg = out["benefit_group"]
    if bg is not None and bg not in ROW_BY_ID:
        raise ValueError(f"unknown benefit_group {bg!r}")
    r = out["risk_10y"]
    if r is not None and (not isinstance(r, (int, float)) or not 0 <= r <= 1):
        raise ValueError(f"risk_10y must be a fraction or None: {r!r}")
    m = out["missing"]
    if not isinstance(m, list) or any(x not in OPEN_INPUTS for x in m) or len(set(m)) != len(m):
        raise ValueError(f"missing must be a list of distinct open-world inputs: {m!r}")
    if not isinstance(out["evidence"], list):
        raise ValueError("evidence must be a list")
    for it in out["evidence"]:
        validate_item(it)
    if not isinstance(out["note_for_clinician"], str):
        raise ValueError("note_for_clinician must be a string")
    return out
