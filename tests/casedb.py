"""A hand-made one-patient case database in the layout of `cases.materialize`.

Primary-prevention man, 55 at index 2026-06-01: two lipid panels, 25 SBP
values (to test paging), smoking status, eGFR, an active BP drug, a stopped
statin, three notes. Tests change rows through `tables(**changes)`.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from gapcheck import codes as C

PATIENT = "pat-0001"
INDEX = "2026-06-01"
BIRTH = "1971-01-15"

COLS = {
    "patients": ["row_id", "patch", "Id", "BIRTHDATE", "DEATHDATE", "FIRST", "LAST", "MARITAL",
                 "RACE", "ETHNICITY", "GENDER"],
    "encounters": ["row_id", "patch", "Id", "START", "STOP", "PATIENT", "ENCOUNTERCLASS", "CODE",
                   "DESCRIPTION", "REASONCODE", "REASONDESCRIPTION"],
    "conditions": ["row_id", "patch", "START", "STOP", "PATIENT", "ENCOUNTER", "SYSTEM", "CODE",
                   "DESCRIPTION"],
    "observations": ["row_id", "patch", "DATE", "PATIENT", "ENCOUNTER", "CATEGORY", "CODE",
                     "DESCRIPTION", "VALUE", "UNITS", "TYPE"],
    "medications": ["row_id", "patch", "START", "STOP", "PATIENT", "PAYER", "ENCOUNTER", "CODE",
                    "DESCRIPTION", "REASONCODE", "REASONDESCRIPTION"],
    "procedures": ["row_id", "patch", "START", "STOP", "PATIENT", "ENCOUNTER", "SYSTEM", "CODE",
                   "DESCRIPTION", "REASONCODE", "REASONDESCRIPTION"],
    "allergies": ["row_id", "patch", "START", "STOP", "PATIENT", "ENCOUNTER", "CODE", "SYSTEM",
                  "DESCRIPTION", "TYPE", "CATEGORY"],
}


def obs(rid, date, code, desc, value, unit, category="laboratory", typ="numeric"):
    return {"row_id": rid, "DATE": f"{date}T10:00:00Z", "PATIENT": PATIENT, "ENCOUNTER": "e1",
            "CATEGORY": category, "CODE": code, "DESCRIPTION": desc, "VALUE": str(value),
            "UNITS": unit, "TYPE": typ}


def lipid(prefix, date, tc, hdl, ldl):
    return [obs(f"obs_{prefix}tc", date, C.TC.code, C.TC.display, tc, "mg/dL"),
            obs(f"obs_{prefix}hdl", date, C.HDL.code, C.HDL.display, hdl, "mg/dL"),
            obs(f"obs_{prefix}ldl", date, C.LDL.code, C.LDL.display, ldl, "mg/dL")]


def default_rows() -> dict[str, list[dict]]:
    o = lipid("a", "2024-05-01", 210, 50, 140) + lipid("b", "2026-05-01", 220, 45, 150)
    for i in range(25):
        o.append(obs(f"obs_sbp{i:02d}", f"20{10 + i % 16:02d}-0{1 + i % 9}-15", C.SBP.code,
                     C.SBP.display, 120 + i, "mm[Hg]", "vital-signs"))
    o.append(obs("obs_sbplast", "2026-05-01", C.SBP.code, C.SBP.display, 142, "mm[Hg]",
                  "vital-signs"))
    o.append(obs("obs_smk", "2026-05-01", C.SMOKING.code, C.SMOKING.display,
                 "Never smoked tobacco (finding)", None, "survey", "text"))
    o.append(obs("obs_egfr", "2026-05-01", C.EGFR.code, C.EGFR.display, 88, C.EGFR_UNIT_KEEP))
    o.append(obs("obs_late", "2026-07-01", C.LDL.code, C.LDL.display, 199, "mg/dL"))  # after index
    meds = [{"row_id": "med_lis", "START": "2020-01-01T00:00:00Z", "STOP": None,
             "PATIENT": PATIENT, "CODE": "314076", "DESCRIPTION": "lisinopril 10 MG Oral Tablet"},
            {"row_id": "med_sim", "START": "2015-01-01T00:00:00Z", "STOP": "2016-01-01T00:00:00Z",
             "PATIENT": PATIENT, "CODE": "312961", "DESCRIPTION": "Simvastatin 20 MG Oral Tablet"}]
    cond = [{"row_id": "cond_htn", "START": "2019-12-01", "STOP": None, "PATIENT": PATIENT,
             "CODE": "59621000", "DESCRIPTION": "Essential hypertension (disorder)"},
            {"row_id": "cond_pre", "START": "2018-01-01", "STOP": None, "PATIENT": PATIENT,
             "CODE": "714628002", "DESCRIPTION": "Prediabetes (finding)"}]
    proc = [{"row_id": f"proc_scr{i}", "START": f"20{10 + i}-03-01T00:00:00Z",
             "STOP": f"20{10 + i}-03-01T00:30:00Z", "PATIENT": PATIENT, "CODE": "171207006",
             "DESCRIPTION": "Depression screening (procedure)"} for i in range(4)]
    enc = [{"row_id": f"enc_{i}", "Id": f"e{i}", "START": d + "T09:00:00Z",
            "STOP": d + "T10:00:00Z", "PATIENT": PATIENT, "ENCOUNTERCLASS": "wellness",
            "CODE": "162673000", "DESCRIPTION": "General examination of patient (procedure)"}
           for i, d in enumerate(["2024-05-01", "2025-09-01", "2026-05-01"])]
    notes = [
        {"note_id": "note_a", "DATE": "2024-05-01", "ENCOUNTER": "e0", "PATIENT": PATIENT,
         "TEXT": "Date: 2024-05-01\nEncounter: wellness, General examination of patient\n\n"
                 "# History of Present Illness\nPatient has a history of: Essential hypertension.\n"},
        {"note_id": "note_b", "DATE": "2025-09-01", "ENCOUNTER": "e1", "PATIENT": PATIENT,
         "TEXT": "Date: 2025-09-01\nEncounter: wellness, General examination of patient\n\n"
                 "# Assessment and Plan\nStatin therapy discussed; patient declined a statin.\n"},
        {"note_id": "note_c", "DATE": "2026-05-01", "ENCOUNTER": "e2", "PATIENT": PATIENT,
         "TEXT": "Date: 2026-05-01\nEncounter: wellness, General examination of patient\n\n"
                 "# Assessment and Plan\nLaboratory tests done: lipid panel.\n"},
    ]
    pats = [{"row_id": "pat_1", "Id": PATIENT, "BIRTHDATE": BIRTH, "FIRST": "Test",
             "LAST": "Patient", "MARITAL": "M", "RACE": "white", "ETHNICITY": "nonhispanic",
             "GENDER": "M"}]
    return {"patients": pats, "encounters": enc, "conditions": cond, "observations": o,
            "medications": meds, "procedures": proc, "allergies": [], "notes": notes}


def tables(rows: dict[str, list[dict]] | None = None) -> dict[str, pd.DataFrame]:
    rows = rows or default_rows()
    out = {}
    for t, cols in COLS.items():
        df = pd.DataFrame(rows.get(t, []), columns=cols, dtype=object)
        out[t] = df.astype(object).where(df.notna(), None)
    out["notes"] = pd.DataFrame(rows.get("notes", []),
                                columns=["DATE", "ENCOUNTER", "PATIENT", "TEXT", "note_id"])
    return out


def connect(rows: dict[str, list[dict]] | None = None, sex: str = "M", birth: str = BIRTH,
            index: str = INDEX) -> duckdb.DuckDBPyConnection:
    from gapcheck.facts import age_at
    from datetime import date
    t = tables(rows)
    con = duckdb.connect()
    for name, df in t.items():
        cols = ", ".join(f'"{c}" varchar' for c in df.columns)
        con.execute(f"create table {name} ({cols})")
        if len(df):
            con.register("_df", df)
            con.execute(f"insert into {name} select * from _df")
            con.unregister("_df")
    age = age_at(date.fromisoformat(birth), date.fromisoformat(index))
    con.execute("create table cohort as select ? as PATIENT, cast(? as date) as index_date, "
                "cast(? as date) as birthdate, ? as sex, ? as age",
                [PATIENT, index, birth, sex, age])
    return con
