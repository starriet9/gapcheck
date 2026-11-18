"""Post-generation patches (SPEC 4.1), each logged row by row.

Order: P0 -> P1 -> (indication groups) -> P2. Every added or removed row is
written to `augmentation_ledger`: patch_id, patient, table_name, row_id,
action ('add' or 'remove'), reason, sub_seed.

Rows added by a patch carry the patch id in the `patch` column; generator
rows have NULL there. The `patch` column is internal: tools shown to a
model must not expose it.
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from . import codes as C
from .load import original_columns, resort, row_id, sql_date

# P1 value range: the generator's own draw for people without kidney damage
# (biometrics.yml metabolic.basic_panel.creatinine_clearance.normal; drawn
# uniformly by LifecycleModule via Person.rand(range)), truncated to 60-140.
P1_NORMAL_RANGE = {"M": (77.0, 160.0), "F": (56.0, 161.0)}
P1_TRUNCATE = (60.0, 140.0)
# Only wellness encounters from this age on get a P1 row (no eGFR rows in
# childhood; SPEC does not say, see PIPELINE notes).
P1_MIN_AGE = 18

P0_REASON = "eGFR series in mL/min removed (SPEC 4.1 P0)"
P1_REASON = "eGFR imputed at adult wellness BP panel: no eGFR and no kidney-related diagnosis (SPEC 4.1 P1)"


def sub_seed(seed: int, label: str) -> int:
    """Sub-seed for a patch or edit step: first 8 bytes of sha256('<seed>:<label>')."""
    return int(hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()[:16], 16)


def _patient_rng(seed: int, patient: str) -> np.random.Generator:
    h = int(hashlib.sha256(f"{seed}:{patient}".encode()).hexdigest()[:16], 16)
    return np.random.default_rng(h)


def _in_list(codes: dict) -> str:
    return "(" + ", ".join(f"'{c}'" for c in codes) + ")"


# ---------------------------------------------------------------- P0
def apply_p0(con: duckdb.DuckDBPyConnection) -> int:
    """Remove every eGFR row in mL/min. Returns the number of rows removed."""
    where = f"CODE = '{C.EGFR.code}' and UNITS = '{C.EGFR_UNIT_DROP}'"
    con.execute(f"""
        insert into augmentation_ledger
        select 'P0', PATIENT, 'observations', row_id, 'remove', '{P0_REASON}', null
        from observations where {where} order by PATIENT, row_id""")
    n = con.execute(f"select count(*) from observations where {where}").fetchall()[0][0]
    con.execute(f"delete from observations where {where}")
    return n


# ---------------------------------------------------------------- P1
def p1_eligible(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Cohort patients with no eGFR row left and no kidney-related diagnosis or procedure."""
    return con.execute(f"""
        select c.PATIENT, c.sex from cohort c
        where not exists (select 1 from observations o
                          where o.PATIENT = c.PATIENT and o.CODE = '{C.EGFR.code}')
          and not exists (select 1 from conditions d where d.PATIENT = c.PATIENT
                          and d.CODE in {_in_list(C.KIDNEY_RELATED_CONDITIONS)}
                          and {sql_date('START')} <= c.index_date)
          and not exists (select 1 from procedures p where p.PATIENT = c.PATIENT
                          and p.CODE in {_in_list(C.KIDNEY_RELATED_PROCEDURES)}
                          and {sql_date('START')} <= c.index_date)
        order by c.PATIENT""").df()


def p1_targets(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """One row per (eligible patient, adult wellness encounter with an SBP row).

    DATE is the time of the BP panel (the encounter's first SBP row).
    """
    elig = p1_eligible(con)
    con.register("_p1_elig", elig)
    df = con.execute(f"""
        select o.PATIENT, e.sex, o.ENCOUNTER, min(o.DATE) as DATE
        from observations o
        join encounters x on x.Id = o.ENCOUNTER
        join _p1_elig e on e.PATIENT = o.PATIENT
        join cohort c on c.PATIENT = o.PATIENT
        where o.CODE = '{C.SBP.code}' and x.ENCOUNTERCLASS = '{C.WELLNESS_CLASS}'
          and {sql_date('DATE')} <= c.index_date
          and {sql_date('DATE')} >= c.birthdate + interval {P1_MIN_AGE} year
        group by all order by o.PATIENT, DATE, o.ENCOUNTER""").df()
    con.unregister("_p1_elig")
    return df


def p1_value(rng: np.random.Generator, sex: str) -> float:
    lo, hi = P1_NORMAL_RANGE[sex]
    lo, hi = max(lo, P1_TRUNCATE[0]), min(hi, P1_TRUNCATE[1])
    return round(float(rng.uniform(lo, hi)), 1)


def apply_p1(con: duckdb.DuckDBPyConnection, seed: int) -> tuple[int, int]:
    """Add imputed eGFR rows. Returns (patients, rows added)."""
    s = sub_seed(seed, "P1")
    targets = p1_targets(con)
    cols = original_columns(con, "observations")
    rows = []
    seen: dict[str, int] = {}
    for patient, grp in targets.groupby("PATIENT", sort=True):
        rng = _patient_rng(s, patient)
        for t in grp.itertuples(index=False):
            v = p1_value(rng, t.sex)
            rec = {c: None for c in cols}
            rec.update({"DATE": t.DATE, "PATIENT": patient, "ENCOUNTER": t.ENCOUNTER,
                        "CATEGORY": "laboratory", "CODE": C.EGFR.code,
                        "DESCRIPTION": C.EGFR.display, "VALUE": f"{v:.1f}",
                        "UNITS": C.EGFR_UNIT_KEEP, "TYPE": "numeric"})
            vals = [rec[c] for c in cols]
            key = "\x1f".join("" if x is None else x for x in vals)
            k = seen.get(key, 0)
            seen[key] = k + 1
            rows.append({"row_id": row_id("observations", vals, k), "patch": "P1", **rec})
    if not rows:
        return 0, 0
    add = pd.DataFrame(rows, columns=["row_id", "patch", *cols]).astype("string")
    clash = con.execute("select count(*) from observations where row_id in (select unnest(?))",
                        [add.row_id.tolist()]).fetchall()[0][0]
    if clash:
        raise RuntimeError(f"P1 row ids clash with {clash} existing rows")
    con.register("_p1_add", add)
    con.execute("insert into observations select * from _p1_add")
    con.execute(f"""
        insert into augmentation_ledger
        select 'P1', PATIENT, 'observations', row_id, 'add', '{P1_REASON}', '{s}'
        from _p1_add order by PATIENT, row_id""")
    con.unregister("_p1_add")
    resort(con, "observations")
    return add.PATIENT.nunique(), len(add)


# ---------------------------------------------------------------- P2
def apply_p2(con: duckdb.DuckDBPyConnection, seed: int, groups: pd.DataFrame) -> None:
    """P2: statin assignment within S2-S4 (SPEC 4.1). Not implemented yet.

    TODO: needs the oracle's indication groups (first matching row among
    1-11, ignoring row 0), passed in `groups` (PATIENT, group). Within each
    of S2-S4, a seeded random half (sub_seed(seed, 'P2')) gets a statin:
    start at a past wellness encounter at age >= 30, active at index; drug
    and dose drawn from the generator's statin submodule
    (medications/statin.json); every medications column filled as in native
    rows; rows marked patch='P2' and written to the ledger.
    """
    raise NotImplementedError("P2 needs the oracle indication groups")


# ---------------------------------------------------------------- ledger
def export_ledger(con: duckdb.DuckDBPyConnection, path: str | Path) -> int:
    """Write augmentation_ledger to CSV (sorted). Returns the number of rows."""
    df = con.execute("select * from augmentation_ledger "
                     "order by patch_id, patient, table_name, row_id").df()
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    return len(df)


def apply_all(con: duckdb.DuckDBPyConnection, seed: int) -> dict:
    """Run P0 then P1 on a freshly loaded database."""
    n0 = apply_p0(con)
    p1_pat, p1_rows = apply_p1(con, seed)
    con.execute("insert into meta values ('patches', 'P0,P1'), ('p1_sub_seed', ?)",
                [str(sub_seed(seed, "P1"))])
    return {"p0_rows_removed": n0, "p1_patients": p1_pat, "p1_rows_added": p1_rows}
