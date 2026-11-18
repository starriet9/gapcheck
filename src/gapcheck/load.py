"""Load Synthea CSV files into DuckDB with stable row ids.

Tables: patients, encounters, conditions, observations, medications,
procedures, allergies (original Synthea columns, all VARCHAR, content
unchanged), each with two extra leading columns:

- row_id: "<prefix>_<16 hex>", a keyed sha256 of the table name, the row's
  content and its occurrence number among rows of identical content (which
  includes the patient id). It does not depend on file order or on other
  rows, so it is the same across runs, deleting a row changes no other id,
  and rows added by patches get ids of the same form.
- patch: NULL for generator rows, else the patch id that added the row.

Plus `cohort` (one row per included patient: index date, age, sex) and
`meta`. Only the cohort is loaded: living patients (no DEATHDATE) aged
30-79 at the index date, which is the date of their last encounter START.

Dates: every Synthea timestamp is UTC ISO 8601 ("2026-11-01T07:01:57Z"), so
the first 10 characters are the UTC date. This module and facts.py use that
rule everywhere.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

import duckdb

ROW_ID_KEY = "gapcheck-row-id-v1"
ROW_ID_HEX = 16
SEP = "\x1f"

# table -> (row id prefix, patient column, date column used for ordering)
TABLES: dict[str, tuple[str, str, str | None]] = {
    "patients": ("pat", "Id", None),
    "encounters": ("enc", "PATIENT", "START"),
    "conditions": ("cond", "PATIENT", "START"),
    "observations": ("obs", "PATIENT", "DATE"),
    "medications": ("med", "PATIENT", "START"),
    "procedures": ("proc", "PATIENT", "START"),
    "allergies": ("alg", "PATIENT", "START"),
}
CLINICAL_TABLES = [t for t in TABLES if t != "patients"]
COHORT_MIN_AGE, COHORT_MAX_AGE = 30, 79


def content_string(table: str, values: Sequence[str | None]) -> str:
    """The string hashed for a row: key, table and column values (NULL -> '')."""
    return SEP.join([ROW_ID_KEY, table, *("" if v is None else str(v) for v in values)])


def row_id(table: str, values: Sequence[str | None], occurrence: int = 0) -> str:
    """Row id for one row given its original column values in CSV order.

    `occurrence` is 0 for the first row with this exact content in the
    table, 1 for the next identical row, and so on.
    """
    h0 = hashlib.sha256(content_string(table, values).encode()).hexdigest()
    h = hashlib.sha256(f"{h0}{SEP}{occurrence}".encode()).hexdigest()
    return f"{TABLES[table][0]}_{h[:ROW_ID_HEX]}"


def _row_id_sql(table: str, columns: list[str]) -> tuple[str, str]:
    """SQL for the content hash (h0) and the row id given h0 and an occurrence column k."""
    parts = [f"'{ROW_ID_KEY}'", f"'{table}'"] + [f'coalesce("{c}", \'\')' for c in columns]
    h0 = "sha256(" + " || chr(31) || ".join(parts) + ")"
    prefix = TABLES[table][0]
    rid = f"'{prefix}_' || substr(sha256(h0 || chr(31) || cast(k as varchar)), 1, {ROW_ID_HEX})"
    return h0, rid


def sql_date(col: str) -> str:
    """SQL expression for the UTC date of a Synthea date or timestamp column."""
    return f'cast(substr("{col}", 1, 10) as date)'


def connect(db_path: str | Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path), read_only=read_only)
    con.execute("SET TimeZone = 'UTC'")
    return con


def add_row_ids(con: duckdb.DuckDBPyConnection, table: str, source: str) -> None:
    """Create `table` from relation `source` (original columns) with row_id and patch."""
    cols = [r[0] for r in con.execute(f"describe {source}").fetchall()]
    h0, rid = _row_id_sql(table, cols)
    _, pcol, dcol = TABLES[table]
    order = f'"{pcol}"' + (f', "{dcol}"' if dcol else "") + ", row_id"
    collist = ", ".join(f'"{c}"' for c in cols)
    con.execute(f"""
        create or replace table {table} as
        with a as (select {h0} as h0, * from {source}),
             b as (select row_number() over (partition by h0) - 1 as k, * from a)
        select {rid} as row_id, cast(null as varchar) as patch, {collist}
        from b order by {order}""")
    n, nd = con.execute(f"select count(*), count(distinct row_id) from {table}").fetchall()[0]
    if n != nd:
        raise RuntimeError(f"row id collision in {table}: {n} rows, {nd} ids")


def resort(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Rewrite a table in (patient, date, row_id) order, e.g. after a patch."""
    _, pcol, dcol = TABLES[table]
    order = f'"{pcol}"' + (f', "{dcol}"' if dcol else "") + ", row_id"
    con.execute(f"create or replace table {table} as select * from {table} order by {order}")


def load(csv_dir: str | Path, db_path: str | Path, seed: int | None = None) -> duckdb.DuckDBPyConnection:
    """Load one Synthea CSV directory into a new DuckDB file and return the connection."""
    csv_dir = Path(csv_dir)
    db_path = Path(db_path)
    if db_path.exists():
        raise FileExistsError(db_path)
    con = connect(db_path)

    def read(t: str) -> str:
        return (f"read_csv('{csv_dir / (t + '.csv')}', header=true, all_varchar=true, "
                "auto_detect=true)")

    con.execute(f"create temp table src_patients as select * from {read('patients')}")
    con.execute(f"create temp table src_encounters as select * from {read('encounters')}")
    con.execute(f"""
        create temp table cohort_all as
        with idx as (
            select PATIENT, max({sql_date('START')}) as index_date
            from src_encounters group by PATIENT)
        select p.Id as PATIENT, i.index_date, cast(p.BIRTHDATE as date) as birthdate,
               p.GENDER as sex, p.DEATHDATE is not null as deceased,
               date_diff('year', cast(p.BIRTHDATE as date), i.index_date)
                 - case when strftime(i.index_date, '%m%d') < strftime(cast(p.BIRTHDATE as date), '%m%d')
                        then 1 else 0 end as age
        from src_patients p left join idx i on i.PATIENT = p.Id""")
    con.execute(f"""
        create table cohort as
        select PATIENT, index_date, birthdate, sex, age from cohort_all
        where not deceased and index_date is not null
          and age between {COHORT_MIN_AGE} and {COHORT_MAX_AGE}
        order by PATIENT""")
    con.execute("""
        create table load_counts as
        select count(*) as n_patients,
               count(*) filter (where deceased) as n_deceased,
               count(*) filter (where not deceased and index_date is null) as n_alive_no_encounter,
               count(*) filter (where not deceased and age < 30) as n_alive_under_30,
               count(*) filter (where not deceased and age > 79) as n_alive_over_79,
               (select count(*) from cohort) as n_cohort
        from cohort_all""")

    con.execute("create temp view v_patients as select * from src_patients "
                "where Id in (select PATIENT from cohort)")
    add_row_ids(con, "patients", "v_patients")
    con.execute("create temp view v_encounters as select * from src_encounters "
                "where PATIENT in (select PATIENT from cohort)")
    add_row_ids(con, "encounters", "v_encounters")
    for t in ["conditions", "observations", "medications", "procedures", "allergies"]:
        con.execute(f"create temp view v_{t} as select * from {read(t)} "
                    "where PATIENT in (select PATIENT from cohort)")
        add_row_ids(con, t, f"v_{t}")
    con.execute("drop table src_encounters")
    con.execute("drop table src_patients")

    con.execute("create table meta (key varchar, value varchar)")
    con.execute("insert into meta values ('row_id_key', ?), ('seed', ?), ('index_rule', ?)",
                [ROW_ID_KEY, None if seed is None else str(seed),
                 "last encounter START date (UTC)"])
    con.execute("""
        create table augmentation_ledger (
            patch_id varchar, patient varchar, table_name varchar, row_id varchar,
            action varchar, reason varchar, sub_seed varchar)""")
    return con


def original_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Synthea columns of a loaded table (without row_id and patch)."""
    cols = [r[0] for r in con.execute(f"describe {table}").fetchall()]
    return [c for c in cols if c not in ("row_id", "patch")]
