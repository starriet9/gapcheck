"""Inputs at the index date, with the source row ids (SPEC 3.4).

This module is the single source of record facts for the oracle and for
the agent tools. It reads one patient's rows (`PatientRecord`, possibly an
edited copy) and returns a `PatientFacts`.

Rules:
- "At index": START or DATE (UTC date) on or before the index date.
- Closed-world inputs (absence means "no"): clinical ASCVD (any code on or
  before index, stop dates ignored); diabetes, CKD stage, row-5 features,
  pregnancy (active at index); statins and BP drugs (start <= index and no
  stop or stop > index); HIV, ESRD/dialysis/transplant and the 55822004
  diagnosis (any code on or before index).
- Open-world inputs (absence means unknown, listed in `missing`): lipid
  panel, SBP, smoking status, eGFR.
- "Latest" = latest timestamp on or before index, no lookback limit. If
  several rows of one code share that timestamp with different values,
  the row with the smallest row_id is used and `n_tied` counts the
  distinct values at that time.
- Lipid panel: TC, HDL-C and LDL-C from the latest timestamp at which all
  three are present.
- eGFR: only the mL/min/{1.73_m2} series (the other one is removed by P0).
- Diabetes onset: earliest START of any diabetes code on or before index.
- Diabetes evidence (`diabetes_evidence`, for the sampling rule
  `diabetes_evidence_without_diagnosis`): diabetic-complication conditions
  and glucose-lowering prescriptions starting on or before index, active or
  not; `diabetes_coded` says whether any diabetes code starts on or before
  index (active or not).
- Structured reasons (SPEC 3.3), `reason_entries`: pregnancy conditions
  (category "pregnancy") and hospice or palliative-care encounters,
  procedures or conditions (category "palliative", including the ICD-10-CM
  condition Z51.5) dated 0-365 days before index. The date is START if it
  falls in that window, else STOP if STOP falls in it. Statin allergy rows
  are in `allergies` (`statin_related`); `allergy_reason_date` and
  `reason_in_window` give their rule: an allergy active at index counts
  whatever its start date, an ended one only if its STOP is 0-365 days
  before index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterator

import duckdb
import pandas as pd

from . import codes as C

RECORD_TABLES = ("conditions", "observations", "medications", "procedures", "allergies")
OPEN_WORLD_INPUTS = ("lipid_panel", "sbp", "smoking", "egfr")


# ---------------------------------------------------------------- record
@dataclass(frozen=True, eq=False)
class PatientRecord:
    """One patient's rows (all Synthea columns plus row_id and patch)."""
    patient: str
    index_date: date
    birthdate: date
    sex: str  # "M" or "F", as in patients.GENDER
    conditions: pd.DataFrame
    observations: pd.DataFrame
    medications: pd.DataFrame
    procedures: pd.DataFrame
    allergies: pd.DataFrame
    encounters: pd.DataFrame | None = None  # used only for structured reasons

    def replace(self, **tables: pd.DataFrame) -> "PatientRecord":
        """Copy with some tables replaced (for edited twins)."""
        d = {k: getattr(self, k) for k in ("patient", "index_date", "birthdate", "sex",
                                           *RECORD_TABLES, "encounters")}
        d.update(tables)
        return PatientRecord(**d)


def _cohort_row(con: duckdb.DuckDBPyConnection, patient: str) -> tuple:
    rows = con.execute("select PATIENT, index_date, birthdate, sex from cohort where PATIENT = ?",
                       [patient]).fetchall()
    if not rows:
        raise KeyError(f"patient not in cohort: {patient}")
    return rows[0]


def load_record(con: duckdb.DuckDBPyConnection, patient: str) -> PatientRecord:
    """Every row of one patient from a loaded database."""
    p, ix, bd, sex = _cohort_row(con, patient)
    t = {name: con.execute(f"select * from {name} where PATIENT = ? order by row_id",
                           [patient]).df() for name in RECORD_TABLES + ("encounters",)}
    return PatientRecord(p, ix, bd, sex, **t)


def _relevant_filters() -> dict[str, str]:
    obs = sorted({c.code for c in C.OBSERVATION_CODES.values()})
    proc = sorted(set(C.ASCVD_PROCEDURES) | set(C.KIDNEY_RELATED_PROCEDURES)
                  | set(C.PALLIATIVE_PROCEDURES))
    enc = sorted(C.PALLIATIVE_ENCOUNTERS)
    return {
        "observations": "CODE in (" + ", ".join(f"'{c}'" for c in obs) + ")",
        "procedures": "CODE in (" + ", ".join(f"'{c}'" for c in proc) + ")",
        "encounters": "CODE in (" + ", ".join(f"'{c}'" for c in enc) + ")",
    }


def iter_records(con: duckdb.DuckDBPyConnection, patients: list[str] | None = None
                 ) -> Iterator[PatientRecord]:
    """Records of all cohort patients (or `patients`), with only the rows facts need.

    Observations and procedures are limited to the codes used here, so a
    record from this iterator gives the same facts as `load_record`.
    """
    coh = con.execute("select PATIENT, index_date, birthdate, sex from cohort order by PATIENT").df()
    if patients is not None:
        coh = coh[coh.PATIENT.isin(set(patients))]
    con.register("_want", coh[["PATIENT"]])
    filt = _relevant_filters()
    groups = {}
    for name in RECORD_TABLES + ("encounters",):
        where = filt.get(name, "true")
        df = con.execute(f"select * from {name} where PATIENT in (select PATIENT from _want) "
                         f"and {where} order by PATIENT, row_id").df()
        groups[name] = dict(tuple(df.groupby("PATIENT", sort=False)))
        groups[name + "_empty"] = df.iloc[0:0]
    con.unregister("_want")
    for r in coh.itertuples(index=False):
        t = {n: groups[n].get(r.PATIENT, groups[n + "_empty"])
             for n in RECORD_TABLES + ("encounters",)}
        yield PatientRecord(r.PATIENT, _to_date(r.index_date), _to_date(r.birthdate), r.sex, **t)


# ---------------------------------------------------------------- facts
@dataclass(frozen=True)
class Obs:
    row_id: str
    timestamp: str
    date: date
    value: float | str
    unit: str | None
    patch: str | None = None
    n_tied: int = 1  # distinct values of this code at this timestamp


@dataclass(frozen=True)
class LipidPanel:
    timestamp: str
    date: date
    tc: Obs
    hdl: Obs
    ldl: Obs

    @property
    def tied(self) -> bool:
        return max(self.tc.n_tied, self.hdl.n_tied, self.ldl.n_tied) > 1

    @property
    def row_ids(self) -> tuple[str, str, str]:
        return (self.tc.row_id, self.hdl.row_id, self.ldl.row_id)


@dataclass(frozen=True)
class Smoking:
    obs: Obs
    current: bool
    recognized: bool  # value is one of codes.SMOKING_VALUES


@dataclass(frozen=True)
class CodedRow:
    table: str
    row_id: str
    code: str
    display: str
    group: str
    start: date
    stop: date | None


@dataclass(frozen=True)
class AllergyRow:
    row_id: str
    code: str
    display: str
    type: str | None
    category: str | None
    start: date
    stop: date | None
    active: bool
    statin_related: bool


@dataclass(frozen=True)
class ReasonEntry:
    """A structured entry that documents a reason (SPEC 3.3)."""
    category: str      # oracle reason category: "pregnancy" or "palliative"
    table: str
    row_id: str
    code: str
    display: str
    date: date         # START, or STOP if only STOP is within the window


@dataclass(frozen=True)
class PatientFacts:
    patient: str
    index_date: date
    age: int
    sex: str
    # open-world
    lipid_panel: LipidPanel | None
    ldl_history_max: Obs | None
    ldl_ge190_earlier: tuple[Obs, ...]  # LDL >= 190 strictly before the latest panel
    sbp: Obs | None
    smoking: Smoking | None
    egfr: Obs | None
    bmi: Obs | None
    p1_rows: tuple[str, ...]  # every P1 eGFR row of the patient
    # closed-world
    diabetes: tuple[CodedRow, ...]
    diabetes_onset: date | None
    diabetes_years: float | None
    t2dm_years: float | None
    ckd: tuple[CodedRow, ...]
    ckd_stage: int | None
    retinopathy: tuple[CodedRow, ...]
    neuropathy: tuple[CodedRow, ...]
    albuminuria: tuple[CodedRow, ...]
    ascvd: tuple[CodedRow, ...]
    hiv: tuple[CodedRow, ...]
    esrd: tuple[CodedRow, ...]
    pregnancy: tuple[CodedRow, ...]
    hyperlipidemia_55822004: tuple[CodedRow, ...]
    statins: tuple[CodedRow, ...]
    bp_drugs: tuple[CodedRow, ...]
    allergies: tuple[AllergyRow, ...]
    missing: tuple[str, ...]
    reason_entries: tuple[ReasonEntry, ...] = ()
    diabetes_evidence: tuple[CodedRow, ...] = ()  # complications, glucose-lowering drugs
    diabetes_coded: bool = False  # any diabetes code on or before index (active or not)
    heart_failure: tuple[CodedRow, ...] = ()  # any code on or before index (not sampled)
    anthracyclines: tuple[CodedRow, ...] = ()  # any prescription starting on or before index

    # convenience flags
    @property
    def has_diabetes(self) -> bool:
        return bool(self.diabetes)

    @property
    def has_ascvd(self) -> bool:
        return bool(self.ascvd)

    @property
    def ascvd_types(self) -> frozenset[str]:
        return frozenset(r.group for r in self.ascvd)

    @property
    def on_statin(self) -> bool:
        return bool(self.statins)

    @property
    def on_bp_drug(self) -> bool:
        return bool(self.bp_drugs)

    @property
    def current_smoker(self) -> bool | None:
        return None if self.smoking is None else self.smoking.current

    @property
    def egfr_imputed(self) -> bool:
        return self.egfr is not None and self.egfr.patch == "P1"

    @property
    def ldl(self) -> float | None:
        return None if self.lipid_panel is None else float(self.lipid_panel.ldl.value)

    @property
    def ldl_ever_ge190(self) -> bool:
        return self.ldl_history_max is not None and float(self.ldl_history_max.value) >= 190

    @property
    def s2_rule(self) -> bool:
        """Latest LDL-C >= 190 and at least one earlier LDL-C >= 190 (SPEC 4.1)."""
        return self.ldl is not None and self.ldl >= 190 and bool(self.ldl_ge190_earlier)

    @property
    def anthracycline_365d(self) -> bool:
        """An anthracycline active at index or stopped within 365 days before it (not
        sampled, SPEC 5.1; sec. 4.2.8.10 rec 3 is not encoded)."""
        return any(r.stop is None or (self.index_date - r.stop).days <= 365
                   for r in self.anthracyclines)

    @property
    def diabetes_evidence_without_diagnosis(self) -> bool:
        """A diabetic complication or a glucose-lowering drug on or before index, and no
        diabetes code on or before index (a generator artifact; not sampled, SPEC 5.1)."""
        return bool(self.diabetes_evidence) and not self.diabetes_coded

    @property
    def excluded(self) -> bool:
        """HIV or ESRD/dialysis/transplant: excluded from the cohort (SPEC 3.1)."""
        return bool(self.hiv) or bool(self.esrd)

    @property
    def row5_features(self) -> dict[str, bool]:
        return {
            "t2dm_10y": self.t2dm_years is not None and self.t2dm_years >= 10,
            "albuminuria": bool(self.albuminuria),
            "ckd3plus": self.ckd_stage is not None and self.ckd_stage >= 3,
            "retinopathy": bool(self.retinopathy),
            "neuropathy": bool(self.neuropathy),
        }


# ---------------------------------------------------------------- helpers
def _to_date(x) -> date | None:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NaT:
        return None
    if isinstance(x, pd.Timestamp):
        return x.date()
    if isinstance(x, date):
        return x
    s = str(x)
    if not s or s == "nan" or s == "<NA>":
        return None
    return date.fromisoformat(s[:10])


def age_at(birthdate: date, d: date) -> int:
    return d.year - birthdate.year - ((d.month, d.day) < (birthdate.month, birthdate.day))


def _dates(s: pd.Series) -> list[date | None]:
    return [_to_date(v) for v in s]


def _with_dates(df: pd.DataFrame, start: str = "START", stop: str | None = "STOP") -> pd.DataFrame:
    df = df.copy()
    df["x_start"] = _dates(df[start])
    if stop is not None:
        df["x_stop"] = _dates(df[stop])
    return df


def _coded(df: pd.DataFrame, table: str, lst: dict[str, C.Code], index: date,
           active: bool) -> tuple[CodedRow, ...]:
    """Rows whose CODE is in `lst`, starting on or before index (and active at index)."""
    if df.empty:
        return ()
    d = df[df.CODE.isin(set(lst))]
    if d.empty:
        return ()
    has_stop = "STOP" in d.columns
    d = _with_dates(d, stop="STOP" if has_stop else None)
    keep = [s is not None and s <= index for s in d["x_start"]]
    if active and has_stop:
        keep = [k and (sp is None or sp > index) for k, sp in zip(keep, d["x_stop"])]
    d = d[keep]
    out = [CodedRow(table, r.row_id, r.CODE, r.DESCRIPTION, lst[r.CODE].group, r.x_start,
                    r.x_stop if has_stop else None)
           for r in d.itertuples(index=False)]
    return tuple(sorted(out, key=lambda x: (x.start, x.row_id)))


def _obs_rows(obs: pd.DataFrame, code: str, index: date, unit: str | None = None,
              numeric: bool = True) -> pd.DataFrame:
    if obs.empty:
        return obs
    d = obs[obs.CODE == code]
    if unit is not None:
        d = d[d.UNITS == unit]
    if d.empty:
        return d
    d = d.copy()
    d["x_date"] = _dates(d.DATE)
    d = d[[x is not None and x <= index for x in d["x_date"]]]
    if numeric:
        d["x_val"] = pd.to_numeric(d.VALUE, errors="coerce")
        d = d[d["x_val"].notna()]
    else:
        d["x_val"] = d.VALUE
    return d.sort_values(["DATE", "row_id"], ascending=[False, True])


def _obs_from_row(r, n_tied: int = 1) -> Obs:
    patch = r.patch if isinstance(r.patch, str) else None
    unit = r.UNITS if isinstance(r.UNITS, str) else None
    val = r.x_val if isinstance(r.x_val, str) else float(r.x_val)
    return Obs(r.row_id, r.DATE, r.x_date, val, unit, patch, n_tied)


def _latest(d: pd.DataFrame) -> Obs | None:
    if d.empty:
        return None
    top = d.iloc[0]
    at = d[d.DATE == top.DATE]
    return _obs_from_row(top, int(at["x_val"].nunique()))


def _lipids(obs: pd.DataFrame, index: date) -> tuple[LipidPanel | None, Obs | None, tuple[Obs, ...]]:
    parts = {k: _obs_rows(obs, getattr(C, k.upper()).code, index) for k in ("tc", "hdl", "ldl")}
    ldl = parts["ldl"]
    ldl_max = None
    if not ldl.empty:
        m = ldl.sort_values(["x_val", "DATE", "row_id"], ascending=[False, False, True]).iloc[0]
        ldl_max = _obs_from_row(m)
    common = set(parts["tc"].DATE) & set(parts["hdl"].DATE) & set(ldl.DATE) if not ldl.empty else set()
    if not common:
        return None, ldl_max, ()
    ts = max(common)
    picked = {k: _latest(v[v.DATE == ts]) for k, v in parts.items()}
    panel = LipidPanel(ts, _to_date(ts), picked["tc"], picked["hdl"], picked["ldl"])
    earlier = ldl[(ldl.DATE < ts) & (ldl["x_val"] >= 190)].sort_values(["DATE", "row_id"])
    return panel, ldl_max, tuple(_obs_from_row(r) for r in earlier.itertuples(index=False))


def _years(start: date | None, index: date) -> float | None:
    return None if start is None else (index - start).days / 365.25


REASON_WINDOW_DAYS = 365  # SPEC 3.3; a project convention, not a guideline number


def reason_in_window(d: date | None, index: date, active_at_index: bool = False) -> bool:
    """A reason dated `d` counts at `index`: dated 0-365 days before index, or
    (statin allergy rows active at index) dated any time on or before index."""
    if d is None:
        return False
    days = (index - d).days
    return days >= 0 and (active_at_index or days <= REASON_WINDOW_DAYS)


def allergy_reason_date(a: "AllergyRow") -> date | None:
    """Date of the reason a statin allergy row documents: START if the row is
    active at index (it counts whatever that date), else STOP (it counts only if
    STOP is 0-365 days before index). None if the row does not name a statin."""
    if not a.statin_related:
        return None
    return a.start if a.active else a.stop


def _reason_entries(rec: PatientRecord, ix: date) -> tuple[ReasonEntry, ...]:
    lists = [("conditions", C.PREGNANCY, "pregnancy"),
             ("conditions", C.PALLIATIVE_CONDITIONS, "palliative"),
             ("procedures", C.PALLIATIVE_PROCEDURES, "palliative"),
             ("encounters", C.PALLIATIVE_ENCOUNTERS, "palliative")]
    out = []
    for table, lst, cat in lists:
        df = getattr(rec, table)
        if df is None or df.empty:
            continue
        d = df[df.CODE.isin(set(lst))]
        if d.empty:
            continue
        d = _with_dates(d, stop="STOP" if "STOP" in d.columns else None)
        for r in d.itertuples(index=False):
            when = None
            for x in (r.x_start, getattr(r, "x_stop", None)):
                if x is not None and 0 <= (ix - x).days <= REASON_WINDOW_DAYS:
                    when = x
                    break
            if when is not None:
                out.append(ReasonEntry(cat, table, r.row_id, r.CODE, r.DESCRIPTION, when))
    return tuple(sorted(out, key=lambda e: (e.date, e.row_id)))


# ---------------------------------------------------------------- main
def facts_at_index(rec: PatientRecord) -> PatientFacts:
    ix = rec.index_date
    obs = rec.observations
    cond, med, proc = rec.conditions, rec.medications, rec.procedures

    panel, ldl_max, ldl_earlier = _lipids(obs, ix)
    sbp = _latest(_obs_rows(obs, C.SBP.code, ix))
    bmi = _latest(_obs_rows(obs, C.BMI.code, ix))
    egfr = _latest(_obs_rows(obs, C.EGFR.code, ix, unit=C.EGFR_UNIT_KEEP))
    sm = _latest(_obs_rows(obs, C.SMOKING.code, ix, numeric=False))
    smoking = None
    if sm is not None:
        smoking = Smoking(sm, sm.value in C.SMOKING_CURRENT, sm.value in C.SMOKING_VALUES)
    p1 = () if obs.empty else tuple(sorted(obs.row_id[obs.patch == "P1"]))

    # diabetes: active rows decide; onset from any diabetes row on or before index
    dm_active = _coded(cond, "conditions", C.DIABETES, ix, active=True)
    dm_any = _coded(cond, "conditions", C.DIABETES, ix, active=False)
    onset = min((r.start for r in dm_any), default=None) if dm_active else None
    t2 = [r.start for r in dm_any if r.group == "type 2"]
    t2_onset = min(t2) if dm_active and t2 and any(r.group == "type 2" for r in dm_active) else None

    ckd = _coded(cond, "conditions", C.CKD_STAGES, ix, active=True)
    ckd_stage = max((int(r.group) for r in ckd), default=None)

    ascvd = _coded(cond, "conditions", C.ASCVD_CONDITIONS, ix, active=False) + \
        _coded(proc, "procedures", C.ASCVD_PROCEDURES, ix, active=False)
    esrd = _coded(cond, "conditions", C.ESRD_CONDITIONS, ix, active=False) + \
        _coded(proc, "procedures", C.ESRD_PROCEDURES, ix, active=False)

    allergies = ()
    if not rec.allergies.empty:
        a = _with_dates(rec.allergies)
        pat = re.compile(C.STATIN_ALLERGY_PATTERN, re.I)
        rows = []
        for r in a.itertuples(index=False):
            if r.x_start is None or r.x_start > ix:
                continue
            rows.append(AllergyRow(
                r.row_id, r.CODE, r.DESCRIPTION,
                r.TYPE if isinstance(r.TYPE, str) else None,
                r.CATEGORY if isinstance(r.CATEGORY, str) else None,
                r.x_start, r.x_stop, r.x_stop is None or r.x_stop > ix,
                bool(pat.search(str(r.DESCRIPTION)))))
        allergies = tuple(sorted(rows, key=lambda x: (x.start, x.row_id)))

    present = {"lipid_panel": panel, "sbp": sbp, "smoking": smoking, "egfr": egfr}
    missing = tuple(k for k in OPEN_WORLD_INPUTS if present[k] is None)

    return PatientFacts(
        patient=rec.patient, index_date=ix, age=age_at(rec.birthdate, ix), sex=rec.sex,
        lipid_panel=panel, ldl_history_max=ldl_max, ldl_ge190_earlier=ldl_earlier,
        sbp=sbp, smoking=smoking, egfr=egfr, bmi=bmi, p1_rows=p1,
        diabetes=dm_active, diabetes_onset=onset, diabetes_years=_years(onset, ix),
        t2dm_years=_years(t2_onset, ix),
        ckd=ckd, ckd_stage=ckd_stage,
        retinopathy=_coded(cond, "conditions", C.RETINOPATHY, ix, active=True),
        neuropathy=_coded(cond, "conditions", C.NEUROPATHY, ix, active=True),
        albuminuria=_coded(cond, "conditions", C.ALBUMINURIA, ix, active=True),
        ascvd=tuple(sorted(ascvd, key=lambda x: (x.start, x.row_id))),
        hiv=_coded(cond, "conditions", C.HIV, ix, active=False),
        heart_failure=_coded(cond, "conditions", C.HEART_FAILURE, ix, active=False),
        anthracyclines=_coded(med, "medications", C.ANTHRACYCLINES, ix, active=False),
        esrd=tuple(sorted(esrd, key=lambda x: (x.start, x.row_id))),
        pregnancy=_coded(cond, "conditions", C.PREGNANCY, ix, active=True),
        hyperlipidemia_55822004=_coded(cond, "conditions", C.HYPERLIPIDEMIA_VETERAN, ix, active=False),
        statins=_coded(med, "medications", C.STATINS, ix, active=True),
        bp_drugs=_coded(med, "medications", C.BP_DRUGS, ix, active=True),
        allergies=allergies,
        missing=missing,
        reason_entries=_reason_entries(rec, ix),
        diabetes_evidence=tuple(sorted(
            _coded(cond, "conditions", C.DIABETIC_COMPLICATIONS, ix, active=False)
            + _coded(med, "medications", C.GLUCOSE_LOWERING_DRUGS, ix, active=False),
            key=lambda x: (x.start, x.row_id))),
        diabetes_coded=bool(dm_any),
    )


def patient_facts(con: duckdb.DuckDBPyConnection, patient: str) -> PatientFacts:
    return facts_at_index(load_record(con, patient))
