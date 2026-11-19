import shutil
from datetime import date

import duckdb
import pytest

from conftest import (ASCVD, CKD3, DECEASED, FEMALE, FIXTURE, P1_ONLY, SEED, UNDER_30)
from gapcheck import load


def ids(con, table):
    return [r[0] for r in con.execute(f"select row_id from {table} order by row_id").fetchall()]


def test_cohort_excludes_deceased_and_out_of_age(loaded):
    pats = {r[0] for r in loaded.execute("select PATIENT from cohort").fetchall()}
    assert len(pats) == 8
    assert DECEASED not in pats and UNDER_30 not in pats
    c = loaded.execute("select * from load_counts").df().iloc[0]
    assert (c.n_patients, c.n_deceased, c.n_alive_under_30, c.n_cohort) == (10, 1, 1, 8)
    for t in load.CLINICAL_TABLES:
        n = loaded.execute(f"select count(*) from {t} where PATIENT not in "
                           "(select PATIENT from cohort)").fetchall()[0][0]
        assert n == 0, t


def test_index_date_and_age(loaded):
    row = loaded.execute("select index_date, age, sex from cohort where PATIENT = ?",
                         [FEMALE]).fetchall()[0]
    assert row == (date(2024, 12, 25), 31, "F")
    # under-30 patient: born 1996-08-13, last encounter 2026-02-03 -> 29
    assert load.COHORT_MIN_AGE == 30


def test_row_ids_unique_prefixed_and_pinned(loaded):
    for t, (prefix, _, _) in load.TABLES.items():
        got = ids(loaded, t)
        assert len(got) == len(set(got))
        assert all(i.startswith(prefix + "_") and len(i) == len(prefix) + 17 for i in got)
    # same ids as in the full dev database
    q = lambda s, p: loaded.execute(s, p).fetchall()[0][0]
    assert q("select row_id from patients where Id = ?", [ASCVD]) == "pat_36ba1c774e418830"
    assert q("select row_id from procedures where PATIENT = ? and CODE = '415070008'",
             [ASCVD]) == "proc_da4848236d62cad1"
    assert q("select row_id from conditions where PATIENT = ? and CODE = '433144002'",
             [CKD3]) == "cond_97c14f80bcfbb110"
    assert q("select row_id from observations where PATIENT = ? and CODE = '18262-6'",
             [P1_ONLY]) == "obs_4c0afd673dba6069"


def test_python_row_id_matches_sql(loaded):
    for t in ["conditions", "medications", "observations", "patients"]:
        cols = load.original_columns(loaded, t)
        df = loaded.execute(f"select * from {t} order by row_id limit 200").df()
        for r in df.itertuples(index=False):
            vals = [None if (v is None or v != v) else v for v in (getattr(r, c) for c in cols)]
            assert load.row_id(t, vals) == r.row_id


def test_content_unchanged(loaded):
    raw = duckdb.connect()
    for t in ["conditions", "observations", "medications"]:
        src = raw.execute(f"select * from read_csv('{FIXTURE / (t + '.csv')}', header=true, "
                          "all_varchar=true) where PATIENT in (select unnest(?)) ",
                          [[r[0] for r in loaded.execute("select PATIENT from cohort").fetchall()]]).df()
        cols = load.original_columns(loaded, t)
        assert list(src.columns) == cols
        got = loaded.execute(f"select {', '.join(chr(34) + c + chr(34) for c in cols)} from {t}").df()
        key = lambda d: sorted(map(tuple, d.fillna("<NULL>").astype(str).values.tolist()))
        assert key(src) == key(got)


def _copy_fixture(dst):
    shutil.copytree(FIXTURE, dst)
    return dst


def test_ids_stable_across_runs_order_and_deletion(tmp_path):
    a = load.load(FIXTURE, tmp_path / "a.duckdb", seed=SEED)
    b = load.load(FIXTURE, tmp_path / "b.duckdb", seed=SEED)
    for t in load.TABLES:
        assert ids(a, t) == ids(b, t)

    # reversed row order in the CSV files: same ids
    rev = _copy_fixture(tmp_path / "rev")
    for f in rev.glob("*.csv"):
        lines = f.read_text().splitlines(keepends=True)
        f.write_text(lines[0] + "".join(reversed(lines[1:])))
    c = load.load(rev, tmp_path / "c.duckdb", seed=SEED)
    for t in load.TABLES:
        assert ids(a, t) == ids(c, t)

    # delete one observation row: exactly that id disappears, no other changes
    dele = _copy_fixture(tmp_path / "del")
    f = dele / "observations.csv"
    lines = f.read_text().splitlines(keepends=True)
    victim = next(i for i, ln in enumerate(lines) if P1_ONLY in ln and "18262-6" in ln)
    f.write_text("".join(lines[:victim] + lines[victim + 1:]))
    d = load.load(dele, tmp_path / "d.duckdb", seed=SEED)
    assert set(ids(a, "observations")) - set(ids(d, "observations")) == {"obs_4c0afd673dba6069"}
    assert set(ids(d, "observations")) <= set(ids(a, "observations"))


def test_identical_rows_get_distinct_ids(tmp_path):
    dup = _copy_fixture(tmp_path / "dup")
    f = dup / "observations.csv"
    lines = f.read_text().splitlines(keepends=True)
    row = next(ln for ln in lines if P1_ONLY in ln and "18262-6" in ln)
    f.write_text("".join(lines) + row)
    con = load.load(dup, tmp_path / "dup.duckdb", seed=SEED)
    got = [r[0] for r in con.execute("select row_id from observations where PATIENT = ? and "
                                     "CODE = '18262-6'", [P1_ONLY]).fetchall()]
    vals = row.rstrip("\n").split(",")
    assert sorted(got) == sorted([load.row_id("observations", vals, 0),
                                  load.row_id("observations", vals, 1)])
    assert "obs_4c0afd673dba6069" in got


def test_refuses_existing_db(tmp_path):
    load.load(FIXTURE, tmp_path / "x.duckdb").close()
    with pytest.raises(FileExistsError):
        load.load(FIXTURE, tmp_path / "x.duckdb")


def test_generator_pins_utc_and_records_it():
    """Synthea writes date-only columns in the JVM zone and timestamps in UTC, and its
    patients depend on the zone; the pipeline reads every date as a UTC date. The generator
    script must pin UTC both ways and record it in the run manifest."""
    from pathlib import Path
    sh = (Path(__file__).parents[1] / "scripts" / "generate_synthea.sh").read_text()
    assert 'TIME_ZONE="UTC"' in sh and 'export TZ="$TIME_ZONE"' in sh
    assert '-Duser.timezone="$TIME_ZONE"' in sh
    assert '"time_zone": "%s"' in sh
    i_export, i_java = sh.index('export TZ="$TIME_ZONE"'), sh.index('"$JAVA" "${ARGS[@]}"')
    assert i_export < i_java
