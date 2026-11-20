"""Post-generation patches (SPEC 4.1), each logged row by row.

Order: P0 -> P1 -> (indication groups, strata.py) -> P2. Every added or removed row is
written to `augmentation_ledger`: patch_id, patient, table_name, row_id,
action ('add' or 'remove'), reason, sub_seed.

Rows added by a patch carry the patch id in the `patch` column; generator
rows have NULL there. The `patch` column is internal: tools shown to a
model must not expose it.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from . import codes as C
from .load import original_columns, resort, row_id, sql_date

# P1 values: eGFR of healthy adults by age and sex, truncated to 60-140.
# Reference: CKD-EPI eGFR (mL/min/1.73 m2), mean and SD by age decade and sex,
# in 20,930 healthy adults (no hypertension, diabetes, kidney or cardiovascular
# disease, normal urine and blood tests): Ma Y, Zhan J, Xu G. Reference values
# of glomerular filtration rate for healthy adults in southern China: a
# cross-sectional survey. Ther Adv Chronic Dis 2021;12:20406223211035287,
# Table 1. Each decade is placed at its midpoint (">= 70" at 75); between
# midpoints mean and SD are interpolated linearly, outside them held at the
# end value. The paper does not name its CKD-EPI version; PREVENT uses
# CKD-EPI 2021.
# (The generator's own draw, uniform over men 77-160 and women 56-161 at
# every age, gave a 72-year-old 122.)
P1_REFERENCE = {  # (age, mean, SD)
    "M": ((25, 114.5, 10.4), (35, 109.3, 10.0), (45, 101.6, 9.8), (55, 95.2, 9.2),
          (65, 89.2, 8.8), (75, 79.6, 9.2)),
    "F": ((25, 121.1, 8.3), (35, 116.1, 7.9), (45, 106.8, 8.1), (55, 99.4, 7.8),
          (65, 91.4, 8.1), (75, 82.2, 7.9)),
}
# A patient keeps one place in that distribution (a level z, |z| <= 2.5 SD)
# across visits; each visit adds its own variation. Value at a visit:
# mean(age) + SD(age) * (r * z + sqrt(1 - r^2) * e), e standard normal, so
# values across patients keep the reference SD, and within one patient the
# visit-to-visit SD is about 0.53 SD, 4-5.5 mL/min/1.73 m2: close to the
# within-person variation of creatinine (CV 4.4%; Carobene et al., EuBIVAS,
# 2017) times 1.2 (the CKD-EPI 2021 creatinine exponent above the knot) at an
# eGFR of 90. r = 0.85 is a choice, not a published value.
P1_PERSON_CORR = 0.85
P1_LEVEL_LIMIT = 2.5
P1_TRUNCATE = (60.0, 140.0)  # a draw outside is redrawn (visit term e only)
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
    Columns: PATIENT, sex, birthdate, ENCOUNTER, DATE.
    """
    elig = p1_eligible(con)
    con.register("_p1_elig", elig)
    df = con.execute(f"""
        select o.PATIENT, e.sex, c.birthdate, o.ENCOUNTER, min(o.DATE) as DATE
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


def p1_reference(age: float, sex: str) -> tuple[float, float]:
    """(mean, SD) of healthy adults' eGFR at this age (P1_REFERENCE)."""
    ages, means, sds = zip(*P1_REFERENCE[sex])
    return float(np.interp(age, ages, means)), float(np.interp(age, ages, sds))


def p1_level(rng: np.random.Generator) -> float:
    """A patient's place in the reference distribution, in SD units (|z| <= 2.5)."""
    while True:
        z = float(rng.standard_normal())
        if abs(z) <= P1_LEVEL_LIMIT:
            return z


def p1_value(rng: np.random.Generator, age: float, sex: str, level: float) -> float:
    """One visit's eGFR, rounded to 0.1 and within P1_TRUNCATE."""
    mean, sd = p1_reference(age, sex)
    r = P1_PERSON_CORR
    lo, hi = P1_TRUNCATE
    for _ in range(10_000):
        v = mean + sd * (r * level + np.sqrt(1 - r * r) * float(rng.standard_normal()))
        if lo <= v <= hi:
            return round(float(v), 1)
    raise RuntimeError(f"P1: no value in {P1_TRUNCATE} at age {age:.1f}, level {level:.2f}")


def _age_years(when: str, birthdate) -> float:
    d = pd.Timestamp(str(when)[:10])
    return (d - pd.Timestamp(birthdate)).days / 365.25


def apply_p1(con: duckdb.DuckDBPyConnection, seed: int) -> tuple[int, int]:
    """Add imputed eGFR rows. Returns (patients, rows added).

    Per patient, a generator seeded by the P1 sub-seed and the patient id draws
    the level, then one value per visit in time order (age at the visit).
    """
    s = sub_seed(seed, "P1")
    targets = p1_targets(con)
    cols = original_columns(con, "observations")
    rows = []
    seen: dict[str, int] = {}
    for patient, grp in targets.groupby("PATIENT", sort=True):
        rng = _patient_rng(s, patient)
        level = p1_level(rng)
        for t in grp.itertuples(index=False):  # in time order
            v = p1_value(rng, _age_years(t.DATE, t.birthdate), t.sex, level)
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
# Indication groups (oracle rows) of S2-S4. Rows 8 and 9 are separate groups,
# so S4 is balanced within each risk bin.
P2_GROUPS = ("2", "3", "8", "9")
P2_MIN_AGE = 30
# Native statin rows written at a wellness visit start 7 days after the
# encounter (414 of 430 in the dev run: the stable ischemic heart disease
# module's delay). P2 rows copy that pattern.
P2_START_OFFSET_DAYS = 7
DISPENSE_DAYS = 30
P2_REASON = "statin added at a past wellness visit: seeded random half of an S2-S4 group (SPEC 4.1 P2)"
STATIN_DISTRIBUTION_FILE = Path(__file__).with_name("statin_distribution.json")


def load_statin_distribution() -> dict:
    return json.loads(STATIN_DISTRIBUTION_FILE.read_text())


def _band_match(band: str, age: int) -> bool:
    lo, hi = (int(v) for v in band.split("-"))
    return lo <= age <= hi


def _draw(rng: np.random.Generator, spec: dict, age: int, gender: str) -> str:
    """One lookup-table transition, as the generator's submodule does it."""
    row = next((r for r in spec["table"] if r["gender"] == gender and _band_match(r["age"], age)),
               None)
    if row is None:
        return spec["default"]
    p = np.array([row["p"].get(t, 0.0) for t in spec["order"]])
    if p.sum() <= 0:
        return spec["default"]
    return spec["order"][int(rng.choice(len(p), p=p / p.sum()))]


def draw_statin(rng: np.random.Generator, age: int, gender: str, dist: dict | None = None
                ) -> tuple[str, str]:
    """(RxNorm code, display) from the generator's statin submodule tables."""
    dist = dist or load_statin_distribution()
    ing = _draw(rng, dist["ingredient"], age, gender)
    prod = _draw(rng, dist["products"][ing], age, gender)
    c = dist["codes"][prod]
    return c["code"], c["display"]


def _rank_key(seed: int, group: str, patient: str) -> str:
    return hashlib.sha256(f"{seed}:{group}:{patient}".encode()).hexdigest()


def p2_pool(con: duckdb.DuckDBPyConnection, groups: pd.DataFrame) -> pd.DataFrame:
    """Patients of S2-S4 who can receive a P2 statin, with their candidate visits.

    groups: DataFrame with PATIENT and group (indication row before P2).
    Left out: 55822004 on or before index, an active statin at index (rows
    added by P2 itself are ignored, so the plan can be recomputed), and
    patients without a wellness encounter at age >= 30 whose start date plus
    P2_START_OFFSET_DAYS is on or before index.
    Returns one row per (patient, candidate encounter).
    """
    g = groups[groups.group.isin(P2_GROUPS)][["PATIENT", "group"]].drop_duplicates()
    con.register("_p2_g", g)
    df = con.execute(f"""
        select g.PATIENT, g."group", c.index_date, c.birthdate, p.GENDER, p.STATE,
               e.Id as ENCOUNTER, e.START as ESTART, e.PAYER
        from _p2_g g
        join cohort c on c.PATIENT = g.PATIENT
        join patients p on p.Id = g.PATIENT
        join encounters e on e.PATIENT = g.PATIENT
        where e.ENCOUNTERCLASS = '{C.WELLNESS_CLASS}'
          and {sql_date('START')} >= c.birthdate + interval {P2_MIN_AGE} year
          and {sql_date('START')} + interval {P2_START_OFFSET_DAYS} day <= c.index_date
          and not exists (select 1 from conditions d where d.PATIENT = g.PATIENT
                          and d.CODE in {_in_list(C.HYPERLIPIDEMIA_VETERAN)}
                          and {sql_date('START')} <= c.index_date)
          and not exists (select 1 from medications m where m.PATIENT = g.PATIENT
                          and m.patch is distinct from 'P2'
                          and m.CODE in {_in_list(C.STATINS)}
                          and {sql_date('START')} <= c.index_date
                          and (m.STOP is null or {sql_date('STOP')} > c.index_date))
        order by g.PATIENT, e.START, e.Id""").df()
    con.unregister("_p2_g")
    return df


def _sim_end(con: duckdb.DuckDBPyConnection) -> pd.Timestamp:
    """End of the simulation: the latest encounter STOP (dispense counts run to it)."""
    v = con.execute("select max(STOP) from encounters").fetchall()[0][0]
    return pd.Timestamp(v)


def _donors(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Native statin rows without a reason code (cost columns are copied from these)."""
    return con.execute(f"""
        select CODE, PAYER, BASE_COST, PAYER_COVERAGE from medications
        where patch is null and REASONCODE is null and CODE in {_in_list(C.STATINS)}
          and BASE_COST is not null and PAYER_COVERAGE is not null
        order by row_id""").df()


def p2_plan(con: duckdb.DuckDBPyConnection, seed: int, groups: pd.DataFrame
            ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rows P2 adds (same columns as `medications`) and the per-patient assignment.

    Within each indication group (rows 2, 3, 8, 9), the pool (see p2_pool) is
    ranked by sha256('<sub_seed>:<group>:<patient>') and the first half (n // 2)
    receives a statin. Each patient's draws (visit, drug and dose, cost donor)
    use a generator seeded by the sub-seed and the patient id only.
    """
    s = sub_seed(seed, "P2")
    pool = p2_pool(con, groups)
    dist = load_statin_distribution()
    donors = _donors(con)
    if donors.empty:
        raise RuntimeError("no native statin rows to copy cost columns from")
    end = _sim_end(con)
    cols = original_columns(con, "medications")

    g = groups[groups.group.isin(P2_GROUPS)][["PATIENT", "group"]].drop_duplicates()
    have = set(pool.PATIENT)
    assign = []
    for grp_id, grp in g.groupby("group", sort=True):
        cand = sorted((p for p in grp.PATIENT if p in have), key=lambda p: _rank_key(s, grp_id, p))
        chosen = set(cand[: len(cand) // 2])
        for p in sorted(grp.PATIENT):
            assign.append({"PATIENT": p, "group": grp_id, "in_pool": p in have,
                           "assigned": p in chosen})
    assign = pd.DataFrame(assign, columns=["PATIENT", "group", "in_pool", "assigned"])

    rows, seen = [], {}
    chosen = set(assign.PATIENT[assign.assigned])
    for patient, visits in pool[pool.PATIENT.isin(chosen)].groupby("PATIENT", sort=True):
        rng = _patient_rng(s, patient)
        v = visits.iloc[int(rng.integers(len(visits)))]
        start = pd.Timestamp(v.ESTART) + pd.Timedelta(days=P2_START_OFFSET_DAYS)
        bd = pd.Timestamp(v.birthdate)
        age = start.year - bd.year - ((start.month, start.day) < (bd.month, bd.day))
        code, display = draw_statin(rng, age, v.GENDER, dist)
        d = donors[(donors.CODE == code) & (donors.PAYER == v.PAYER)]
        if d.empty:
            d = donors[donors.CODE == code]
        if d.empty:
            d = donors
        donor = d.iloc[int(rng.integers(len(d)))]
        n = max(1, int((end - start).total_seconds() // (DISPENSE_DAYS * 86400)))
        rec = {c: None for c in cols}
        rec.update({"START": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "STOP": None,
                    "PATIENT": patient, "PAYER": v.PAYER, "ENCOUNTER": v.ENCOUNTER,
                    "CODE": code, "DESCRIPTION": display, "BASE_COST": donor.BASE_COST,
                    "PAYER_COVERAGE": donor.PAYER_COVERAGE, "DISPENSES": str(n),
                    "TOTALCOST": f"{float(donor.BASE_COST) * n:.2f}",
                    "REASONCODE": None, "REASONDESCRIPTION": None})
        vals = [rec[c] for c in cols]
        key = "\x1f".join("" if x is None else x for x in vals)
        k = seen.get(key, 0)
        seen[key] = k + 1
        rows.append({"row_id": row_id("medications", vals, k), "patch": "P2", **rec})
    add = pd.DataFrame(rows, columns=["row_id", "patch", *cols]).astype("string")
    return add, assign


def p2_applied(con: duckdb.DuckDBPyConnection) -> bool:
    return con.execute("select count(*) from augmentation_ledger where patch_id = 'P2'"
                       ).fetchall()[0][0] > 0


def apply_p2(con: duckdb.DuckDBPyConnection, seed: int, groups: pd.DataFrame) -> dict:
    """P2: give a statin to a seeded random half of each indication group of S2-S4 (SPEC 4.1).

    groups: PATIENT, group (indication row before P2; see strata.py).
    Adds rows to `medications` (patch='P2'), the ledger and table
    `p2_assignment`. Raises if P2 was already applied.
    """
    if p2_applied(con):
        raise RuntimeError("P2 already applied to this database")
    s = sub_seed(seed, "P2")
    add, assign = p2_plan(con, seed, groups)
    con.register("_p2_assign", assign)
    con.execute('create or replace table p2_assignment as select * from _p2_assign '
                'order by "group", PATIENT')
    con.unregister("_p2_assign")
    if not add.empty:
        clash = con.execute("select count(*) from medications where row_id in (select unnest(?))",
                            [add.row_id.tolist()]).fetchall()[0][0]
        if clash:
            raise RuntimeError(f"P2 row ids clash with {clash} existing rows")
        con.register("_p2_add", add)
        con.execute("insert into medications select * from _p2_add")
        con.execute(f"""
            insert into augmentation_ledger
            select 'P2', PATIENT, 'medications', row_id, 'add', '{P2_REASON}', '{s}'
            from _p2_add order by PATIENT, row_id""")
        con.unregister("_p2_add")
        resort(con, "medications")
    con.execute("update meta set value = value || ',P2' where key = 'patches'")
    con.execute("insert into meta values ('p2_sub_seed', ?)", [str(s)])
    by = assign.groupby("group")
    return {"p2_rows_added": len(add),
            "by_group": {k: {"group": len(v), "pool": int(v.in_pool.sum()),
                               "assigned": int(v.assigned.sum())} for k, v in by}}


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
