from datetime import date

import pandas as pd
import pytest

from conftest import (ALLERGY, ASCVD, CKD3, DIABETES, FEMALE, P1_ONLY, S2, STROKE)
from gapcheck import codes as C
from gapcheck import facts


@pytest.fixture
def F(patched):
    return {r.patient: facts.facts_at_index(r) for r in facts.iter_records(patched)}


def test_iter_records_matches_load_record(patched, F):
    assert len(F) == 8
    for p, f in F.items():
        assert facts.patient_facts(patched, p) == f


def test_p1_only_patient(F):
    f = F[P1_ONLY]
    assert (f.age, f.sex, f.index_date) == (31, "M", date(2026, 11, 10))
    lp = f.lipid_panel
    assert lp.timestamp == "2026-11-10T01:27:19Z"
    assert (lp.tc.value, lp.hdl.value, lp.ldl.value) == (169.5, 44.1, 96.0)
    assert lp.row_ids == ("obs_5e930bff4efe704a", "obs_9085801727323cd1", "obs_4c0afd673dba6069")
    assert not lp.tied
    assert f.smoking.current is False and f.smoking.recognized
    assert f.egfr_imputed and f.egfr.value == 129.7 and f.egfr.row_id == "obs_83772ad64e919c44"
    assert len(f.p1_rows) == 6
    assert f.missing == ()
    assert not (f.has_ascvd or f.has_diabetes or f.on_statin or f.on_bp_drug or f.excluded)


def test_ckd3_patient(F):
    f = F[CKD3]
    assert f.ckd_stage == 3 and {r.code for r in f.ckd} == {"431855005", "431856006", "433144002"}
    assert f.egfr.value == 54.2 and not f.egfr_imputed and f.egfr.unit == C.EGFR_UNIT_KEEP
    assert f.p1_rows == ()
    assert not f.has_diabetes  # has 127013003 but no diabetes code
    assert {r.code for r in f.albuminuria} == {"90781000119102", "157141000119108"}
    assert f.row5_features == {"t2dm_10y": False, "albuminuria": True, "ckd3plus": True,
                               "retinopathy": False, "neuropathy": False}
    assert [r.code for r in f.hyperlipidemia_55822004] == ["55822004"]
    assert [r.display for r in f.statins] == ["Simvastatin 10 MG Oral Tablet"]
    assert {r.group for r in f.bp_drugs} == {"CCB", "ACE inhibitor", "thiazide"}
    assert f.ldl == 110.7 and f.ldl_ever_ge190 and not f.s2_rule


def test_diabetes_patient(F):
    f = F[DIABETES]
    assert f.age == 40 and f.has_diabetes
    assert f.diabetes_onset == date(2017, 12, 28)
    assert f.diabetes_years == pytest.approx((date(2026, 10, 22) - date(2017, 12, 28)).days / 365.25)
    assert f.t2dm_years == f.diabetes_years
    assert [r.code for r in f.diabetes] == ["44054006"]  # prediabetes not counted
    assert f.egfr.value == 151.9 and not f.egfr_imputed
    assert f.lipid_panel.ldl.value == 121.2


def test_ascvd_patient(F):
    f = F[ASCVD]
    assert f.ascvd_types == {"IHD", "REVASC"}
    assert "proc_da4848236d62cad1" in {r.row_id for r in f.ascvd}
    assert f.on_statin and [r.code for r in f.statins] == ["312961"]
    assert [r.group for r in f.bp_drugs] == ["beta blocker"]
    assert f.sbp.value == 95.0
    assert f.lipid_panel.date == date(2024, 12, 12)


def test_stroke_stop_date_ignored(F):
    f = F[STROKE]
    assert f.ascvd_types == {"STROKE"} and f.ascvd[0].stop == date(2024, 11, 4)
    assert not f.on_statin
    assert f.ldl == 187.4 and f.ldl_history_max.value == 232.2 and f.ldl_ever_ge190


def test_s2_rule(F):
    f = F[S2]
    assert f.ldl == 204.0 and f.s2_rule
    assert len(f.ldl_ge190_earlier) == 2
    assert all(o.timestamp < f.lipid_panel.timestamp for o in f.ldl_ge190_earlier)
    assert f.egfr_imputed  # only the mL/min series before P0
    g = F[FEMALE]
    assert g.ldl == 229.6 and not g.s2_rule and g.ldl_ever_ge190


def test_female_pregnancy_ended(F):
    f = F[FEMALE]
    assert f.sex == "F" and f.pregnancy == ()


def test_allergies(F):
    f = F[ALLERGY]
    assert sorted((a.display, a.category, a.active, a.statin_related) for a in f.allergies) == [
        ("Allergic disposition (finding)", "environment", True, False),
        ("Aspirin", "medication", True, False)]


# ---------------------------------------------------------------- edited copies
def rec(patched, p):
    return facts.load_record(patched, p)


def test_deleting_open_world_inputs(patched):
    r = rec(patched, P1_ONLY)
    obs = r.observations
    lip = obs.CODE.isin([C.TC.code, C.HDL.code, C.LDL.code])
    f = facts.facts_at_index(r.replace(observations=obs[~lip]))
    assert f.lipid_panel is None and f.missing == ("lipid_panel",)
    f = facts.facts_at_index(r.replace(observations=obs[obs.CODE != C.SMOKING.code]))
    assert f.smoking is None and f.current_smoker is None and f.missing == ("smoking",)
    f = facts.facts_at_index(r.replace(observations=obs[obs.CODE.isin([C.SBP.code, C.EGFR.code]) == False]))
    assert f.missing == ("sbp", "egfr")
    # partial panel: TC and HDL without LDL is not a panel
    f = facts.facts_at_index(r.replace(observations=obs[obs.CODE != C.LDL.code]))
    assert f.lipid_panel is None and "lipid_panel" in f.missing
    # LDL removed at the latest panel only -> the previous full panel is used
    s = rec(patched, S2).observations
    latest = s[s.CODE == C.LDL.code].DATE.max()
    f = facts.facts_at_index(rec(patched, S2).replace(
        observations=s[~((s.CODE == C.LDL.code) & (s.DATE == latest))]))
    assert f.lipid_panel.timestamp < latest and f.lipid_panel.ldl.timestamp == f.lipid_panel.tc.timestamp


def test_edit_smoking_and_ties(patched):
    r = rec(patched, P1_ONLY)
    obs = r.observations.copy()
    i = obs[obs.CODE == C.SMOKING.code].DATE.idxmax()
    obs.loc[i, "VALUE"] = "Smokes tobacco daily (finding)"
    assert facts.facts_at_index(r.replace(observations=obs)).current_smoker is True
    # a second LDL value at the latest panel time: tie flagged, smallest row id wins
    ldl = r.observations[(r.observations.CODE == C.LDL.code)].iloc[[0]].copy()
    ldl["row_id"], ldl["VALUE"] = "obs_0000000000000000", "250.0"
    f = facts.facts_at_index(r.replace(observations=pd.concat([r.observations, ldl])))
    assert f.lipid_panel.tied and f.lipid_panel.ldl.n_tied == 2
    assert f.lipid_panel.ldl.row_id == "obs_0000000000000000" and f.ldl == 250.0


def test_unit_filter_and_medication_window(patched):
    r = rec(patched, P1_ONLY)
    obs = r.observations
    e = obs[obs.CODE == C.EGFR.code].iloc[[0]].copy()
    e["row_id"], e["DATE"], e["UNITS"], e["VALUE"] = "obs_ffff", "2026-11-10T23:00:00Z", "mL/min", "5.0"
    f = facts.facts_at_index(r.replace(observations=pd.concat([obs, e])))
    assert f.egfr.row_id == "obs_83772ad64e919c44"  # mL/min row ignored
    med = pd.DataFrame([{c: None for c in r.medications.columns}] * 3)
    med["row_id"] = ["med_a", "med_b", "med_c"]
    med["CODE"] = ["617311", "617311", "314076"]
    med["DESCRIPTION"] = ["atorvastatin 40 MG Oral Tablet"] * 2 + ["lisinopril 10 MG Oral Tablet"]
    med["START"] = ["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", "2026-11-11T00:00:00Z"]
    med["STOP"] = ["2026-11-10T00:00:00Z", "2026-11-11T00:00:00Z", None]
    f = facts.facts_at_index(r.replace(medications=med))
    assert [m.row_id for m in f.statins] == ["med_b"]  # stop on index is not active
    assert f.bp_drugs == ()                            # starts after index


def test_statin_allergy_edit(patched):
    r = rec(patched, ALLERGY)
    a = r.allergies.iloc[[1]].copy()
    a["row_id"], a["DESCRIPTION"], a["START"] = "alg_x", "Statin-induced myopathy", "2023-06-01"
    f = facts.facts_at_index(r.replace(allergies=pd.concat([r.allergies, a])))
    assert [x.row_id for x in f.allergies if x.statin_related] == ["alg_x"]
