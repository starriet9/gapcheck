"""Check the code lists in gapcheck.codes against one Synthea CSV directory.

Usage: python scripts/check_codes.py <csv_dir> [<csv_dir> ...]

For each list: codes seen (patients), codes not seen, display mismatches,
and codes in the data that match the list's search pattern but are not
listed (candidates to review). Exit code 1 on a display mismatch.
"""
import re
import sys

import duckdb

from gapcheck import codes as C

# Search patterns for unlisted candidates, per list (case-insensitive).
PATTERNS = {
    "ascvd_conditions": r"infarct|angina|ischem|coronary|cerebrovasc|stroke|transient isch|"
                        r"claudic|peripheral (arter|vasc)|aneurysm|atheroscl",
    "ascvd_procedures": r"coronary|angioplast|stent|bypass|endarter|revasc|thrombect",
    "statins": r"statin|ezetim",
    "bp_drugs": r"thiazide|chlorthal|dipine|diltiazem|verapamil|pril\b|sartan|olol\b|metoprolol|"
                r"spironol|amiloride|triamterene|hydralazine|clonidine|guanfacine|azosin|"
                r"minoxidil|furosemide|bumetanide|torsemide|sacubitril",
    "diabetes": r"diabet",
    "ckd_stages": r"chronic kidney",
    "esrd_conditions": r"end-stage|end stage|transplant.*kidney|kidney.*transplant|renal transplant",
    "esrd_procedures": r"dialysis|transplant.*kidney|kidney.*transplant",
    "kidney_related_conditions": r"kidney|renal|nephr|albumin|proteinuria|dialysis",
    "hiv": r"immunodeficiency|\bhiv\b",
    "pregnancy": r"pregnan",
    "hyperlipidemia_veteran": r"lipid|cholesterol",
    "retinopathy": r"retinopath|blindness|macular",
    "neuropathy": r"neuropath",
    "albuminuria": r"albumin|proteinuria",
}


def check(csv_dir: str) -> int:
    con = duckdb.connect()
    for t in ("conditions", "procedures", "medications", "observations", "encounters"):
        con.execute(f"create view {t} as select * from read_csv('{csv_dir}/{t}.csv', "
                    "header=true, all_varchar=true)")
    bad = 0
    print(f"### {csv_dir}")
    for name, lst in C.CODE_LISTS.items():
        table = C.LIST_TABLE.get(name, "conditions")
        seen = {r[0]: (r[1], r[2]) for r in con.execute(
            f"select CODE, list(distinct DESCRIPTION), count(distinct PATIENT) from {table} "
            f"where CODE in (select unnest(?)) group by 1", [list(lst)]).fetchall()}
        n_pat = sum(v[1] for v in seen.values())
        print(f"{name} ({table}): {len(seen)}/{len(lst)} codes seen, patient-code pairs {n_pat}")
        for code, c in lst.items():
            if code in seen:
                descs = set(seen[code][0])
                allowed = {c.display, *c.alt_displays}
                if "display also in lower case" in c.note:
                    allowed |= {d for d in descs if d.lower() == c.display.lower()}
                if not descs <= allowed:
                    bad += 1
                    print(f"  DISPLAY MISMATCH {code}: listed {c.display!r}, data {sorted(descs)}")
        pat = PATTERNS.get(name)
        if pat:
            listed = set(lst)
            if name == "bp_drugs":
                listed |= set(C.BP_DRUGS_NOT_COUNTED)
            if name == "diabetes":
                listed |= set(C.PREDIABETES) | set(C.RETINOPATHY) | set(C.NEUROPATHY) \
                    | set(C.KIDNEY_RELATED_CONDITIONS)
            rows = con.execute(
                f"select CODE, any_value(DESCRIPTION), count(distinct PATIENT) n from {table} "
                f"where regexp_matches(DESCRIPTION, ?, 'i') group by 1 order by n desc",
                [pat]).fetchall()
            for code, desc, n in rows:
                if code not in listed:
                    print(f"  unlisted candidate {code} {desc!r} patients={n}")
    # observation codes, units and smoking answers
    for key, c in C.OBSERVATION_CODES.items():
        rows = con.execute("select DESCRIPTION, UNITS, count(*) from observations where CODE=? "
                           "group by all", [c.code]).fetchall()
        for desc, units, n in rows:
            flag = "" if desc == c.display else "  DISPLAY MISMATCH"
            bad += bool(flag)
            print(f"obs {key} {c.code} units={units!r} rows={n}{flag}")
    vals = con.execute("select VALUE, count(*) from observations where CODE=? group by 1",
                       [C.SMOKING.code]).fetchall()
    for v, n in vals:
        flag = "" if v in C.SMOKING_VALUES else "  UNLISTED SMOKING VALUE"
        bad += bool(flag)
        print(f"smoking value {v!r} rows={n}{flag}")
    return bad


if __name__ == "__main__":
    total = sum(check(d) for d in sys.argv[1:])
    sys.exit(1 if total else 0)
