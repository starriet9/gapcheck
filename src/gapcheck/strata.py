"""Strata of base patients and every reason a patient is not sampled (SPEC 5.1).

Order (SPEC 4.1): P0 -> P1 -> indication group -> P2 -> oracle label.
The indication group is the oracle's first matching row among X and 1-11,
with row 0 ignored, computed on the record before P2. A stratum is an
indication group; "on a statin" is the row-0 result after P2.

Every sampling rule here (ties, P1 eGFR, PREVENT inputs) is evaluated on the
indication group, not on the final label, so that treated and untreated
patients of a stratum are selected by the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from . import oracle, prevent

STRATA = ("S1", "S2", "S3", "S4", "S5")
STRATUM_OF_ROW = {"1": "S1", "2": "S2", "3": "S3", "8": "S4", "9": "S4", "7": "S5", "11b": "S5"}
# Rows whose label (or, for row 7, whose exclusion rule) needs PREVENT.
PREVENT_ROWS = frozenset({"5", "7", "8", "9", "10", "11a", "11b"})
P1_EGFR_RANGE = (60.0, 140.0)
P1_EGFR_VALUES = np.arange(60.0, 140.0 + 0.5, 1.0)
PRIMARY = ("S2", "S3", "S4", "S5")

# name -> (strata it applies to, description). Order is the report order.
EXCLUSIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "hiv": (STRATA, "HIV (row X)"),
    "esrd_dialysis_transplant": (STRATA, "ESRD, dialysis or kidney transplant (row X)"),
    "cannot_determine": (STRATA, "indication group is CANNOT_DETERMINE"),
    "row3b_ckd": (STRATA, "row 3b (CKD stage 3-4, 40-75)"),
    "structured_reason_365d": (STRATA, "a structured reason within 365 days of index "
                                       "(pregnancy, hospice or palliative care, statin "
                                       "allergy) or pregnancy active at index"),
    "dx_55822004": (PRIMARY, "diagnosis 55822004 (S2-S5)"),
    "primary_prevention_76_79": (PRIMARY, "primary prevention at age 76-79"),
    "diabetes_outside_40_75": (PRIMARY, "diabetes outside age 40-75"),
    "ldl_lt70_risk_ge5": (PRIMARY, "LDL-C < 70 with 10-year risk >= 5%"),
    "age60_79_risk_lt3_ldl160_189": (PRIMARY, "age 60-79, risk < 3%, LDL-C 160-189"),
    "prevent_input_missing_or_out_of_range": (
        PRIMARY, "a PREVENT input missing or out of range where PREVENT is needed"),
    "native_egfr_gt150": (PRIMARY, "native (generator) eGFR > 150 where PREVENT is needed"),
    "p1_egfr_dependent": (STRATA, "latest eGFR is a P1 value and the indication row "
                                  "is not the same for every eGFR 60-140"),
    "tie_in_deciding_input": (STRATA, "two values at the latest timestamp of a deciding input"),
    "s2_no_earlier_ldl_ge190": (("S2",), "S2: no LDL-C >= 190 before the latest panel"),
    "s2_meets_row3": (("S2",), "S2: also meets row 3 (diabetes, 40-75; lipid panel not deciding)"),
    "ldl_ever_ge190": (("S3", "S4", "S5"), "S3-S5: LDL-C >= 190 at some time"),
}


@dataclass(frozen=True)
class StrataRow:
    patient: str
    age: int
    sex: str
    indication_row: str | None
    indication_status: str
    row_set: tuple[str, ...]         # rows over unusable inputs ((row,) normally)
    candidate: str | None            # stratum of the indication row, before exclusions
    exclusions: tuple[str, ...]      # every rule that applies to this patient
    risk10: float | None
    deciding: tuple[str, ...]        # deciding inputs of the indication group
    egfr_p1: bool                    # latest eGFR is a P1 value
    p1_row_dependent: bool | None    # indication row changes over eGFR 60-140 (P1 only)
    p1_label_dependent: bool | None  # main label changes over eGFR 60-140 (P1 only)
    on_statin_native: bool           # before P2
    ascvd_type: str | None
    s4_bin: str | None               # "5-<10" or ">=10"

    @property
    def stratum(self) -> str | None:
        """Stratum if eligible (no exclusion applies), else None."""
        return self.candidate if self.candidate and not self.blocking() else None

    def blocking(self) -> tuple[str, ...]:
        """Exclusions that apply to this patient's candidate stratum."""
        if self.candidate is None:
            return self.exclusions
        return tuple(e for e in self.exclusions if self.candidate in EXCLUSIONS[e][0])


def has_structured_reason(f) -> bool:
    """Any structured reason dated 0-365 days before index, whatever age and sex."""
    return any(0 <= (f.index_date - r.date).days <= oracle.REASON_WINDOW_DAYS
               for r in oracle.reasons_from_facts(f))


def ascvd_type(f) -> str | None:
    t = f.ascvd_types
    if not t:
        return None
    if t & {"MI", "ACS"}:
        return "MI/ACS"
    if "REVASC" in t:
        return "revasc_no_MI"
    if "IHD" in t:
        return "IHD_only"
    return "stroke_only"


def rows_over_egfr(x: oracle.OracleInputs, values=P1_EGFR_VALUES) -> np.ndarray:
    """Indication row ids over eGFR values (other inputs as recorded)."""
    if not oracle.usable(replace(x, egfr=90.0)) == dict.fromkeys(oracle.OPEN_INPUTS, True):
        # Another input is unusable: evaluate one value at a time.
        return np.array([oracle.evaluate(replace(x, egfr=float(e)), ignore_row0=True)["row"]
                         for e in values], dtype=object)
    codes = oracle._row_codes(x, x.tc, x.hdl, x.ldl, x.sbp, float(x.smoker), values,
                              ignore_row0=True)
    codes = np.broadcast_to(codes, values.shape)
    return np.array([oracle._ROW_IDS[int(c)] for c in codes], dtype=object)


def assign(f) -> StrataRow:
    """Stratum candidate and every applicable exclusion for one patient (pre-P2 facts)."""
    ind = oracle.indication(f)
    x = oracle.inputs_from_facts(f)
    row = ind.row if ind.status != oracle.CANNOT_DETERMINE else None
    cand = STRATUM_OF_ROW.get(row)
    if row is None and ind.status != oracle.CANNOT_DETERMINE:
        # Label known but the row varies with an unusable input (e.g. rows 8 and 9).
        cands = {STRATUM_OF_ROW.get(r) for r in ind.row_set}
        cand = cands.pop() if len(cands) == 1 else None
    ex: list[str] = []
    if f.hiv:
        ex.append("hiv")
    if f.esrd:
        ex.append("esrd_dialysis_transplant")
    if ind.status == oracle.CANNOT_DETERMINE:
        ex.append("cannot_determine")
    if row == "3b":
        ex.append("row3b_ckd")
    if f.pregnancy or has_structured_reason(f):
        ex.append("structured_reason_365d")
    if f.hyperlipidemia_55822004:
        ex.append("dx_55822004")
    if not f.has_ascvd and f.age >= 76:
        ex.append("primary_prevention_76_79")
    if f.has_diabetes and not 40 <= f.age <= 75:
        ex.append("diabetes_outside_40_75")

    ok = oracle.usable(x)
    prevent_ok = all(ok.values()) and x.lipid_present
    # P1 eGFR: the indication row must not depend on the imputed value.
    row_dep = lab_dep = None
    p1 = f.egfr_imputed
    if p1 and row is not None and row in PREVENT_ROWS and prevent_ok:
        rows = rows_over_egfr(x)
        row_dep = len(set(rows)) > 1
        lab_dep = len({oracle.row_status(r) for r in rows if r is not None}) > 1 or None in rows
    elif p1 and (row is None or row not in PREVENT_ROWS):
        row_dep = lab_dep = False  # rows before 5 do not read eGFR
    # (PREVENT rows with an unusable input are excluded by another rule.)
    if row_dep:
        ex.append("p1_egfr_dependent")

    if row == "7":
        # LDL-C < 70 with 10-year risk >= 5%; with a P1 eGFR, at any eGFR 60-140.
        if prevent_ok:
            eg = P1_EGFR_VALUES if p1 else np.array([x.egfr])
            r = prevent.ascvd_risk_array("male" if x.sex == "M" else "female", x.age, x.tc,
                                         x.hdl, x.sbp, int(x.bp_treated), int(x.on_statin),
                                         int(x.diabetes), int(x.smoker), eg)
            if np.max(r) >= oracle.RISK_INTERMEDIATE:
                ex.append("ldl_lt70_risk_ge5")
    if row == "11b" and f.age >= 60 and x.ldl is not None and 160 <= x.ldl < 190:
        ex.append("age60_79_risk_lt3_ldl160_189")
    if (row in PREVENT_ROWS or (row is None and set(ind.row_set) & PREVENT_ROWS)) \
            and not prevent_ok:
        ex.append("prevent_input_missing_or_out_of_range")
    needs_prevent = row in PREVENT_ROWS or (row is None and bool(set(ind.row_set) & PREVENT_ROWS))
    if needs_prevent and f.egfr is not None and not f.egfr_imputed and float(f.egfr.value) > 150:
        ex.append("native_egfr_gt150")

    tied = {"lipid_panel": f.lipid_panel is not None and f.lipid_panel.tied,
            "sbp": f.sbp is not None and f.sbp.n_tied > 1,
            "smoking": f.smoking is not None and f.smoking.obs.n_tied > 1,
            "egfr": f.egfr is not None and f.egfr.n_tied > 1}
    if any(tied[k] for k in ind.deciding):
        ex.append("tie_in_deciding_input")
    if cand == "S2" and not f.s2_rule:
        ex.append("s2_no_earlier_ldl_ge190")
    if cand == "S2" and f.has_diabetes and 40 <= f.age <= 75:
        ex.append("s2_meets_row3")
    if f.ldl_ever_ge190 and row != "2":
        ex.append("ldl_ever_ge190")

    s4_bin = None
    if cand == "S4" and row is not None:
        s4_bin = ">=10" if row == "8" else "5-<10"
    return StrataRow(
        patient=f.patient, age=f.age, sex=f.sex, indication_row=row,
        indication_status=ind.status, row_set=ind.row_set, candidate=cand, exclusions=tuple(ex),
        risk10=ind.risk10, deciding=ind.deciding, egfr_p1=p1, p1_row_dependent=row_dep,
        p1_label_dependent=lab_dep, on_statin_native=f.on_statin, ascvd_type=ascvd_type(f),
        s4_bin=s4_bin)
