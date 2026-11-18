import pandas as pd
import pytest

from conftest import (ALLERGY, ASCVD, CKD3, DIABETES, FEMALE, P1_ONLY, S2, SEED, STROKE)
from gapcheck import codes as C
from gapcheck import load, patches

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


def test_p2_not_implemented(patched):
    with pytest.raises(NotImplementedError):
        patches.apply_p2(patched, SEED, pd.DataFrame())
