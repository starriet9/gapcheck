"""Extract a few patients from a Synthea CSV directory into a small test fixture.

Usage: python scripts/make_fixture.py <csv_dir> <out_dir> <patient id> [...]

Keeps every patients, conditions, medications and allergies row of the
chosen patients; observations and procedures only for the codes the
pipeline uses; encounters that are wellness visits, referenced by a kept
row, or the patient's last encounter (so the index date is unchanged).
Values are copied unchanged (CSV quoting may differ from Synthea's).
"""
import sys
from pathlib import Path

import duckdb

from gapcheck import codes as C

OBS = sorted({c.code for c in C.OBSERVATION_CODES.values()} | {"38483-4", "2160-0"})
PROC = sorted(set(C.ASCVD_PROCEDURES) | set(C.KIDNEY_RELATED_PROCEDURES))


def main() -> None:
    src, out, pats = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()

    def rd(t):
        return f"read_csv('{src / (t + '.csv')}', header=true, all_varchar=true)"

    con.execute("create table want as select unnest(?) as PATIENT", [pats])
    sel = {
        "patients": f"select * from {rd('patients')} where Id in (select PATIENT from want)",
        "conditions": f"select * from {rd('conditions')} where PATIENT in (select PATIENT from want)",
        "medications": f"select * from {rd('medications')} where PATIENT in (select PATIENT from want)",
        "allergies": f"select * from {rd('allergies')} where PATIENT in (select PATIENT from want)",
        "observations": f"select * from {rd('observations')} where PATIENT in (select PATIENT from want) "
                        f"and CODE in (select unnest(?))",
        "procedures": f"select * from {rd('procedures')} where PATIENT in (select PATIENT from want) "
                      f"and CODE in (select unnest(?))",
    }
    for t, q in sel.items():
        params = [OBS] if t == "observations" else [PROC] if t == "procedures" else []
        con.execute(f"create table f_{t} as {q}", params)
    con.execute(f"""
        create table f_encounters as
        with e as (select * from {rd('encounters')} where PATIENT in (select PATIENT from want)),
             refs as (select ENCOUNTER from f_conditions union select ENCOUNTER from f_medications
                      union select ENCOUNTER from f_allergies union select ENCOUNTER from f_observations
                      union select ENCOUNTER from f_procedures),
             last as (select PATIENT, max(START) as START from e group by 1)
        select e.* from e
        where ENCOUNTERCLASS = 'wellness' or Id in (select ENCOUNTER from refs)
           or (PATIENT, START) in (select (PATIENT, START) from last)""")
    for t in ["patients", "encounters", "conditions", "observations", "medications",
              "procedures", "allergies"]:
        con.execute(f"copy (select * from f_{t}) to '{out / (t + '.csv')}' (header, delimiter ',')")
        n = con.execute(f"select count(*) from f_{t}").fetchall()[0][0]
        print(t, n)


if __name__ == "__main__":
    main()
