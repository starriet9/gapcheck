import numpy as np
import pandas as pd
import pytest

from conftest import (ALLERGY, ASCVD, CKD3, DIABETES, FEMALE, P1_ONLY, S2, SEED, STROKE)
from gapcheck import codes as C
from gapcheck import facts, load, oracle, patches

P1_PATIENTS = {P1_ONLY, ASCVD, STROKE, S2, FEMALE, ALLERGY}


def q(con, sql, params=()):
    return con.execute(sql, list(params)).fetchall()


def test_p0_removes_only_ml_min_series(loaded):
    before = q(loaded, "select row_id from observations where CODE = ? and UNITS = ?",
               [C.EGFR.code, C.EGFR_UNIT_DROP])
    keep_before = q(loaded, "select row_id from observations where CODE = ? and UNITS = ? "
                    "order by row_id", [C.EGFR.code, C.EGFR_UNIT_KEEP])
    assert len(before) == 19  # 4 (CKD3) + 15 (S2)
    n = patches.apply_p0(loaded)
    assert n == 19
    units = {r[0] for r in q(loaded, "select distinct UNITS from observations where CODE = ?",
                             [C.EGFR.code])}
    assert units == {C.EGFR_UNIT_KEEP}
    assert q(loaded, "select row_id from observations where CODE = ? and UNITS = ? order by row_id",
             [C.EGFR.code, C.EGFR_UNIT_KEEP]) == keep_before
    led = q(loaded, "select row_id, action, table_name, sub_seed from augmentation_ledger "
                    "where patch_id = 'P0'")
    assert {r[0] for r in led} == {r[0] for r in before}
    assert all(r[1:] == ("remove", "observations", None) for r in led)


def test_unit_strings_in_fixture(loaded):
    got = {r[0] for r in q(loaded, "select distinct UNITS from observations where CODE = ?",
                           [C.EGFR.code])}
    assert got == {"mL/min", "mL/min/{1.73_m2}"}


def test_p1_eligibility(loaded):
    patches.apply_p0(loaded)
    elig = set(patches.p1_eligible(loaded).PATIENT)
    assert elig == P1_PATIENTS
    assert CKD3 not in elig      # kidney diagnosis and native eGFR
    assert DIABETES not in elig  # native eGFR


def test_p1_rows(patched):
    add = patched.execute("select * from observations where patch = 'P1'").df()
    assert set(add.PATIENT) == P1_PATIENTS
    native = patched.execute("select * from observations where CODE = ? and patch is null limit 1",
                             [C.EGFR.code]).df().iloc[0]
    for col in ["CATEGORY", "CODE", "DESCRIPTION", "UNITS", "TYPE"]:
        assert (add[col] == native[col]).all(), col
    assert add.VALUE.str.fullmatch(r"\d+\.\d").all()
    sex = dict(q(patched, "select PATIENT, sex from cohort"))
    v = add.VALUE.astype(float)
    lo = add.PATIENT.map(lambda p: 77.0 if sex[p] == "M" else 60.0)
    assert (v >= lo).all() and (v <= 140.0).all()
    # one row per adult wellness encounter with an SBP row, at the BP panel time
    exp = patched.execute(f"""
        select o.PATIENT, o.ENCOUNTER, min(o.DATE) as DATE from observations o
        join encounters e on e.Id = o.ENCOUNTER join cohort c on c.PATIENT = o.PATIENT
        where o.CODE = '{C.SBP.code}' and e.ENCOUNTERCLASS = 'wellness'
          and o.PATIENT in (select unnest(?))
          and cast(substr(o.DATE, 1, 10) as date) >= c.birthdate + interval 18 year
        group by all""", [sorted(P1_PATIENTS)]).df()
    key = lambda d: sorted(map(tuple, d[["PATIENT", "ENCOUNTER", "DATE"]].values.tolist()))
    assert key(add) == key(exp)


def test_p1_pinned_to_dev_run(patched):
    # Same ids and values as in the full dev database: the draw depends only
    # on the seed and the patient, not on other patients.
    rows = q(patched, "select row_id, DATE, VALUE from observations where PATIENT = ? "
                      "and patch = 'P1' order by DATE desc", [P1_ONLY])
    assert len(rows) == 6
    assert rows[0] == ("obs_83772ad64e919c44", "2026-11-10T01:27:19Z", "129.7")
    assert rows[1] == ("obs_87e6be1d6ef338c0", "2023-11-07T01:27:19Z", "127.8")
    assert q(patched, "select count(*), min(cast(VALUE as double)) from observations "
                      "where PATIENT = ? and patch = 'P1'", [FEMALE])[0] == (6, 91.9)


def test_p1_deterministic_and_seeded(tmp_path):
    from conftest import FIXTURE
    out = []
    for name, seed in [("a", SEED), ("b", SEED), ("c", SEED + 1)]:
        con = load.load(FIXTURE, tmp_path / f"{name}.duckdb", seed=seed)
        patches.apply_all(con, seed)
        out.append(con.execute("select row_id, VALUE from observations where patch = 'P1' "
                               "order by row_id").fetchall())
        con.close()
    assert out[0] == out[1]
    assert [v for _, v in out[0]] != [v for _, v in out[2]]


def test_ledger_complete(patched, tmp_path):
    led = patched.execute("select * from augmentation_ledger").df()
    assert list(led.columns) == ["patch_id", "patient", "table_name", "row_id", "action",
                                 "reason", "sub_seed"]
    assert led.row_id.is_unique
    added = set(led.row_id[led.action == "add"])
    removed = set(led.row_id[led.action == "remove"])
    in_table = {r[0] for r in q(patched, "select row_id from observations")}
    assert added <= in_table
    assert not (removed & in_table)
    assert added == {r[0] for r in q(patched, "select row_id from observations where patch is not null")}
    s = str(patches.sub_seed(SEED, "P1"))
    assert (led.sub_seed[led.patch_id == "P1"] == s).all()
    assert led.sub_seed[led.patch_id == "P0"].isna().all()
    # ledger patient matches the row's patient
    pat = dict(q(patched, "select row_id, PATIENT from observations where patch = 'P1'"))
    assert all(pat[r.row_id] == r.patient for r in led[led.action == "add"].itertuples())
    n = patches.export_ledger(patched, tmp_path / "ledger.csv")
    assert n == len(led) == len(pd.read_csv(tmp_path / "ledger.csv"))


def test_p1_rows_sorted_in_place(patched):
    # patched rows sit among native rows (table is ordered by patient, date, row_id)
    df = patched.execute("select PATIENT, DATE, row_id, patch from observations").df()
    assert df.equals(df.sort_values(["PATIENT", "DATE", "row_id"]).reset_index(drop=True))


# ---------------------------------------------------------------- P2
# Fixture groups: S2 and FEMALE are row 2; DIABETES is row 3; CKD3 (55822004,
# simvastatin) is put in row 3 to check it is left out; P1_ONLY and ALLERGY are
# put in row 9 for the test.
P2_GROUPS = pd.DataFrame({"PATIENT": [S2, FEMALE, DIABETES, CKD3, P1_ONLY, ALLERGY],
                          "group": ["2", "2", "3", "3", "9", "9"]})


def test_p2_pool_and_half(patched):
    info = patches.apply_p2(patched, SEED, P2_GROUPS)
    assert info["by_group"] == {"2": {"group": 2, "pool": 2, "assigned": 1},
                                "3": {"group": 2, "pool": 1, "assigned": 0},
                                "9": {"group": 2, "pool": 1, "assigned": 0}}
    a = patched.execute("select * from p2_assignment").df().set_index("PATIENT")
    assert not a.loc[CKD3, "in_pool"]  # 55822004 and an active statin
    # P1_ONLY (31) has no wellness visit at age >= 30 at least 7 days before index
    assert not a.loc[P1_ONLY, "in_pool"]
    visits = q(patched, """select count(*) from encounters e join cohort c on c.PATIENT = e.PATIENT
                           where e.PATIENT = ? and e.ENCOUNTERCLASS = 'wellness'
                             and cast(substr(e.START, 1, 10) as date) >= c.birthdate + interval 30 year
                             and cast(substr(e.START, 1, 10) as date) + interval 7 day <= c.index_date""",
               [P1_ONLY])[0][0]
    assert visits == 0
    assert info["p2_rows_added"] == 1
    with pytest.raises(RuntimeError):
        patches.apply_p2(patched, SEED, P2_GROUPS)


def test_p2_rows_like_native(patched):
    patches.apply_p2(patched, SEED, P2_GROUPS)
    add = patched.execute("select * from medications where patch = 'P2'").df()
    assigned = set(patched.execute("select PATIENT from p2_assignment where assigned").df().PATIENT)
    assert set(add.PATIENT) == assigned and len(add) == len(assigned)
    filled = [c for c in add.columns if c not in ("STOP", "REASONCODE", "REASONDESCRIPTION")]
    assert add[filled].notna().all().all()
    assert add[["STOP", "REASONCODE", "REASONDESCRIPTION"]].isna().all().all()
    dist = patches.load_statin_distribution()
    known = {(v["code"], v["display"]) for v in dist["codes"].values()}
    for r in add.itertuples():
        assert (r.CODE, r.DESCRIPTION) in known and r.CODE in C.STATINS
        enc = patched.execute("select START, ENCOUNTERCLASS, PAYER from encounters where Id = ?",
                              [r.ENCOUNTER]).fetchall()[0]
        assert enc[1] == "wellness" and enc[2] == r.PAYER
        assert pd.Timestamp(r.START) - pd.Timestamp(enc[0]) == pd.Timedelta(days=7)
        ix, bd = patched.execute("select index_date, birthdate from cohort where PATIENT = ?",
                                 [r.PATIENT]).fetchall()[0]
        start = pd.Timestamp(r.START).date()
        assert start <= ix and facts.age_at(bd, start) >= 30
        assert float(r.TOTALCOST) == pytest.approx(float(r.BASE_COST) * int(r.DISPENSES), abs=0.01)
        assert int(r.DISPENSES) >= 1
    led = patched.execute("select * from augmentation_ledger where patch_id = 'P2'").df()
    assert set(led.row_id) == set(add.row_id) and (led.action == "add").all()
    assert (led.sub_seed == str(patches.sub_seed(SEED, "P2"))).all()
    df = patched.execute("select PATIENT, START, row_id from medications").df()
    assert df.equals(df.sort_values(["PATIENT", "START", "row_id"]).reset_index(drop=True))


def test_p2_patients_are_on_therapy(patched):
    patches.apply_p2(patched, SEED, P2_GROUPS)
    assigned = patched.execute("select PATIENT from p2_assignment where assigned").df().PATIENT
    for p in assigned:
        f = facts.patient_facts(patched, p)
        assert f.on_statin and oracle.label(f).status == oracle.ON_THERAPY


def test_p2_deterministic_and_seeded(tmp_path):
    from conftest import FIXTURE
    plans = []
    for name, seed in [("a", SEED), ("b", SEED)] + [(f"s{i}", SEED + i) for i in range(1, 6)]:
        con = load.load(FIXTURE, tmp_path / f"{name}.duckdb", seed=seed)
        patches.apply_all(con, seed)
        add, assign = patches.p2_plan(con, seed, P2_GROUPS)
        plans.append((tuple(add.row_id), tuple(add.CODE), tuple(assign.assigned)))
        con.close()
    assert plans[0] == plans[1]
    assert len(set(plans[1:])) > 1


def test_draw_statin_follows_generator_tables():
    dist = patches.load_statin_distribution()
    rng = np.random.default_rng(1)
    young = {patches.draw_statin(rng, 30, "F", dist)[0] for _ in range(50)}
    assert young <= {"617310"}  # 26-35 F: atorvastatin 20 mg only
    draws = [patches.draw_statin(rng, 50, "M", dist)[0] for _ in range(4000)]
    ator = {v["code"] for k, v in dist["codes"].items() if "Atorvastatin" in k}
    assert sum(c in ator for c in draws) / len(draws) == pytest.approx(0.614, abs=0.03)
