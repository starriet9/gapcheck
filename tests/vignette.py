"""Hand-built PatientFacts for oracle and strata tests (no database needed)."""
from __future__ import annotations

import re
from datetime import date, timedelta

from gapcheck import codes as C
from gapcheck.facts import (AllergyRow, CodedRow, LipidPanel, Obs, PatientFacts, ReasonEntry,
                            Smoking)

INDEX = date(2026, 11, 1)
_n = [0]


def _rid(prefix: str) -> str:
    _n[0] += 1
    return f"{prefix}_{_n[0]:016x}"


def obs(value, d: date = INDEX, unit=None, patch=None, n_tied=1, ts=None) -> Obs:
    return Obs(_rid("obs"), ts or f"{d.isoformat()}T09:00:00Z", d, value, unit, patch, n_tied)


def coded(table: str, code: str, group: str = "", start: date = date(2015, 1, 1),
          stop: date | None = None) -> CodedRow:
    return CodedRow(table, _rid("cond" if table == "conditions" else table[:4]), code, "",
                    group, start, stop)


ASCVD_CODE = {"MI": "22298006", "STROKE": "230690007", "IHD": "414545008", "REVASC": "415070008"}


def make(age=50, sex="M", ldl=120.0, tc=200.0, hdl=50.0, sbp=130.0, smoker=False,
         egfr=90.0, egfr_p1=False, diabetes=False, dm_years=None, ckd=None, ascvd=(),
         statin=False, statin_stop: date | None = None, bp=False, hiv=False, esrd=False,
         retinopathy=False, neuropathy=False, albuminuria=False, pregnancy=False,
         vet=False, allergies=(), lipid=True, smoking_value=None,
         ldl_ever_max=None, ldl_earlier_ge190=(), ties=(), structured=(),
         index=INDEX) -> PatientFacts:
    """A PatientFacts built from plain values. None means the open-world input is absent.

    ties: names among ("lipid_panel", "sbp", "smoking", "egfr") whose latest
    timestamp holds two distinct values.
    structured: (category, days before index) pairs of structured reason entries.
    """
    t = lambda k: 2 if k in ties else 1
    panel = None
    if lipid and None not in (tc, hdl, ldl):
        panel = LipidPanel(f"{index.isoformat()}T09:00:00Z", index,
                           obs(float(tc), index, "mg/dL", n_tied=t("lipid_panel")),
                           obs(float(hdl), index, "mg/dL"), obs(float(ldl), index, "mg/dL"))
    sm = None
    if smoker is not None or smoking_value is not None:
        v = smoking_value or (C.SMOKING_CURRENT[0] if smoker else C.SMOKING_NOT_CURRENT[0])
        sm = Smoking(obs(v, index, n_tied=t("smoking")), v in C.SMOKING_CURRENT,
                     v in C.SMOKING_VALUES)
    eg = None
    if egfr is not None:
        eg = obs(float(egfr), index, C.EGFR_UNIT_KEEP, "P1" if egfr_p1 else None, t("egfr"))
    sbp_o = None if sbp is None else obs(float(sbp), index, "mm[Hg]", n_tied=t("sbp"))

    dm = ()
    onset = years = None
    if diabetes:
        years = 5.0 if dm_years is None else float(dm_years)
        onset = index - timedelta(days=round(years * 365.25))
        dm = (coded("conditions", "44054006", "type 2", onset),)
    ck = () if ckd is None else (coded("conditions", "433144002", str(ckd)),)
    asc = tuple(coded("conditions", ASCVD_CODE[a], a) for a in ascvd)
    st = (coded("medications", "617310", "atorvastatin", stop=statin_stop),) if statin else ()
    bpd = (coded("medications", "314076", "ACE inhibitor"),) if bp else ()
    ldl_max = None
    if ldl_ever_max is not None:
        ldl_max = obs(float(ldl_ever_max), index - timedelta(days=400))
    elif panel is not None:
        ldl_max = panel.ldl
    earlier = tuple(obs(float(v), index - timedelta(days=365 * (i + 1)))
                    for i, v in enumerate(ldl_earlier_ge190))
    present = {"lipid_panel": panel, "sbp": sbp_o, "smoking": sm, "egfr": eg}
    alg = []
    for a in allergies:  # (description, start) or (description, start, stop)
        desc, d, stop = (tuple(a) + (None,))[:3]
        alg.append(AllergyRow(_rid("alg"), "x", desc, "intolerance", "medication", d, stop,
                              stop is None or stop > index,
                              bool(re.search(C.STATIN_ALLERGY_PATTERN, desc, re.I))))
    alg = tuple(alg)
    return PatientFacts(
        patient=_rid("pat"), index_date=index, age=age, sex=sex,
        lipid_panel=panel, ldl_history_max=ldl_max, ldl_ge190_earlier=earlier,
        sbp=sbp_o, smoking=sm, egfr=eg, bmi=None,
        p1_rows=(eg.row_id,) if egfr_p1 and eg is not None else (),
        diabetes=dm, diabetes_onset=onset, diabetes_years=years,
        t2dm_years=years, ckd=ck, ckd_stage=ckd,
        retinopathy=(coded("conditions", "1551000119108"),) if retinopathy else (),
        neuropathy=(coded("conditions", "368581000119106"),) if neuropathy else (),
        albuminuria=(coded("conditions", "90781000119102"),) if albuminuria else (),
        ascvd=asc, hiv=(coded("conditions", "86406008"),) if hiv else (),
        esrd=(coded("conditions", "46177005"),) if esrd else (),
        pregnancy=(coded("conditions", "72892002"),) if pregnancy else (),
        hyperlipidemia_55822004=(coded("conditions", "55822004"),) if vet else (),
        statins=st if statin and (statin_stop is None or statin_stop > index) else (),
        bp_drugs=bpd, allergies=alg,
        missing=tuple(k for k, v in present.items() if v is None),
        reason_entries=tuple(ReasonEntry(cat, "procedures", _rid("proc"), "x", "", index -
                                         timedelta(days=days)) for cat, days in structured),
    )
