"""Counts after P0+P1 that need no PREVENT: eGFR coverage, P1, pre-oracle groups.

Usage: python scripts/pre_oracle_counts.py <run_dir>

Reads <run_dir>/gapcheck.duckdb, writes <run_dir>/results/pre_oracle_counts.json
and prints it. Groups follow the decision table order (rows 1, 2, 3, 3b, 4,
5, 6, then the LDL-C split), ignoring row 0, after the cohort exclusions
(HIV; ESRD, dialysis, transplant).
"""
import json
import sys
from collections import Counter
from pathlib import Path

from gapcheck import codes as C
from gapcheck import facts, load

RANGES = {"tc": (130, 320), "hdl": (20, 100), "sbp": (90, 200), "egfr": (15, 150)}


def group_of(f: facts.PatientFacts) -> str:
    if f.excluded:
        return "excluded_hiv_esrd"
    if f.has_ascvd:
        return "row1_ascvd"
    ldl = f.ldl
    if ldl is not None and ldl >= 190:
        return "row2_ldl_ge190"
    if f.has_diabetes and 40 <= f.age <= 75:
        return "row3_dm_40_75"
    if (f.ckd_stage in (3, 4)) and 40 <= f.age <= 75 and ldl is not None and 70 <= ldl < 190:
        return "row3b_ckd34_40_75"
    if f.has_diabetes and f.age >= 76:
        return "row4_dm_76_79"
    if f.has_diabetes and f.age <= 39:
        return "row5_dm_30_39"
    if ldl is None:
        return "lipid_panel_missing"
    if f.age >= 76:
        return "row6_primary_76_79"
    if ldl < 70:
        return "row7_ldl_lt70"
    return "rows8_11_prevent_needed"


def egfr_coverage(con) -> dict:
    q = lambda s: con.execute(s).fetchall()[0][0]
    n = q("select count(*) from cohort")
    native = q(f"select count(distinct PATIENT) from observations where CODE='{C.EGFR.code}' "
               "and patch is null")
    p0 = q("select count(distinct patient) from augmentation_ledger where patch_id='P0'")
    before = q(f"""select count(*) from (
        select distinct PATIENT from observations where CODE='{C.EGFR.code}' and patch is null
        union select distinct patient from augmentation_ledger where patch_id='P0')""")
    p1 = q("select count(distinct patient) from augmentation_ledger where patch_id='P1'")
    after = q(f"select count(distinct PATIENT) from observations where CODE='{C.EGFR.code}'")
    return {"cohort": n, "any_egfr_before_P0": before, "patients_with_P0_rows": p0,
            "native_mL_min_1_73_after_P0": native, "p1_patients": p1,
            "any_egfr_after_P1": after,
            "p0_rows_removed": q("select count(*) from augmentation_ledger where patch_id='P0'"),
            "p1_rows_added": q("select count(*) from augmentation_ledger where patch_id='P1'")}


def main() -> None:
    run = Path(sys.argv[1])
    con = load.connect(run / "gapcheck.duckdb", read_only=True)
    out = {"load": con.execute("select * from load_counts").df().iloc[0].astype(int).to_dict(),
           "egfr": egfr_coverage(con)}
    F = [facts.facts_at_index(r) for r in facts.iter_records(con)]
    con.close()

    groups = Counter()
    statin = Counter()
    vet = Counter()
    for f in F:
        g = group_of(f)
        groups[g] += 1
        statin[g] += f.on_statin
        vet[g] += bool(f.hyperlipidemia_55822004)
    out["groups"] = {g: {"n": groups[g], "on_statin": statin[g], "no_statin": groups[g] - statin[g],
                         "with_55822004": vet[g]} for g in sorted(groups)}

    inc = [f for f in F if not f.excluded]
    by_g = {}
    for f in inc:
        by_g.setdefault(group_of(f), []).append(f)

    def cnt(fs, pred):
        return sum(1 for f in fs if pred(f))

    asc = by_g.get("row1_ascvd", [])
    out["ascvd_types"] = {
        "any_flag": {t: cnt(asc, lambda f, t=t: t in f.ascvd_types) for t in C.ASCVD_TYPES},
        "exclusive_type_x_statin": {},
    }
    def asc_type(f):
        t = f.ascvd_types
        if t & {"MI", "ACS"}:
            return "MI/ACS"
        if "REVASC" in t:
            return "revasc_no_MI"
        if "IHD" in t:
            return "IHD_only"
        return "stroke_only"
    for f in asc:
        k = f"{asc_type(f)}|{'statin' if f.on_statin else 'no_statin'}"
        out["ascvd_types"]["exclusive_type_x_statin"][k] = \
            out["ascvd_types"]["exclusive_type_x_statin"].get(k, 0) + 1

    r2 = by_g.get("row2_ldl_ge190", [])
    out["s2"] = {
        "row2_latest_ldl_ge190": len(r2),
        "s2_rule_latest_and_earlier_ge190": cnt(r2, lambda f: f.s2_rule),
        "s2_rule_on_statin": cnt(r2, lambda f: f.s2_rule and f.on_statin),
        "s2_rule_without_55822004": cnt(r2, lambda f: f.s2_rule and not f.hyperlipidemia_55822004),
        "s2_rule_without_55822004_on_statin": cnt(
            r2, lambda f: f.s2_rule and not f.hyperlipidemia_55822004 and f.on_statin),
        "no_ascvd_ldl_ever_ge190_latest_lt190": cnt(
            inc, lambda f: not f.has_ascvd and f.ldl_ever_ge190 and f.ldl is not None and f.ldl < 190),
        "no_ascvd_latest_ldl_185_195": cnt(
            inc, lambda f: not f.has_ascvd and f.ldl is not None and 185 <= f.ldl <= 195),
    }

    pn = by_g.get("rows8_11_prevent_needed", []) + by_g.get("row5_dm_30_39", [])
    oor = {}
    for k, (lo, hi) in RANGES.items():
        def val(f, k=k):
            o = {"tc": f.lipid_panel and f.lipid_panel.tc, "hdl": f.lipid_panel and f.lipid_panel.hdl,
                 "sbp": f.sbp, "egfr": f.egfr}[k]
            return None if not o else float(o.value)
        vals = [val(f) for f in pn]
        oor[k] = {"missing": sum(v is None for v in vals),
                  "below": sum(v is not None and v < lo for v in vals),
                  "above": sum(v is not None and v > hi for v in vals)}
    oor["smoking_missing"] = cnt(pn, lambda f: f.smoking is None)
    oor["any_input_missing_or_out_of_range"] = cnt(pn, lambda f: any(
        x is None for x in (f.lipid_panel, f.sbp, f.smoking, f.egfr)) or not (
        130 <= f.lipid_panel.tc.value <= 320 and 20 <= f.lipid_panel.hdl.value <= 100
        and 90 <= f.sbp.value <= 200 and 15 <= f.egfr.value <= 150))

    out["not_sampled"] = {
        "hiv": cnt(F, lambda f: bool(f.hiv)),
        "esrd_dialysis_transplant": cnt(F, lambda f: bool(f.esrd)),
        "pregnancy_active_at_index": cnt(inc, lambda f: bool(f.pregnancy)),
        "dx_55822004_any": cnt(inc, lambda f: bool(f.hyperlipidemia_55822004)),
        "dx_55822004_in_rows2_to_11": cnt(
            inc, lambda f: bool(f.hyperlipidemia_55822004) and not f.has_ascvd),
        "primary_prevention_76_79": len(by_g.get("row4_dm_76_79", [])) + len(by_g.get("row6_primary_76_79", [])),
        "diabetes_outside_40_75": len(by_g.get("row4_dm_76_79", [])) + len(by_g.get("row5_dm_30_39", [])),
        "row3b_ckd": len(by_g.get("row3b_ckd34_40_75", [])),
        "native_egfr_gt150_all": cnt(inc, lambda f: f.egfr is not None and not f.egfr_imputed
                                     and f.egfr.value > 150),
        "native_egfr_gt150_prevent_needed": cnt(pn, lambda f: f.egfr is not None and not f.egfr_imputed
                                                and f.egfr.value > 150),
        "prevent_inputs_out_of_range_or_missing (rows 5, 8-11)": oor,
        "age_60_79_ldl_160_189_prevent_needed (upper bound; needs risk < 3%)": cnt(
            pn, lambda f: f.age >= 60 and 160 <= f.ldl < 190),
        "row7_ldl_lt70 (LDL<70 with risk>=5% needs PREVENT)": len(by_g.get("row7_ldl_lt70", [])),
        "p1_latest_egfr_imputed_all": cnt(inc, lambda f: f.egfr_imputed),
        "p1_latest_egfr_imputed_prevent_needed": cnt(pn, lambda f: f.egfr_imputed),
        "s3_s5_candidates_with_ldl_ever_ge190": cnt(
            by_g.get("row3_dm_40_75", []) + pn + by_g.get("row7_ldl_lt70", []),
            lambda f: f.ldl_ever_ge190),
    }
    out["ties_at_latest_timestamp"] = {
        "lipid_panel": cnt(inc, lambda f: f.lipid_panel is not None and f.lipid_panel.tied),
        "egfr": cnt(inc, lambda f: f.egfr is not None and f.egfr.n_tied > 1),
        "sbp": cnt(inc, lambda f: f.sbp is not None and f.sbp.n_tied > 1),
        "smoking": cnt(inc, lambda f: f.smoking is not None and f.smoking.obs.n_tied > 1),
    }
    out["inputs"] = {
        "current_smoker": cnt(inc, lambda f: f.current_smoker is True),
        "diabetes_active": cnt(inc, lambda f: f.has_diabetes),
        "on_statin": cnt(inc, lambda f: f.on_statin),
        "on_bp_drug": cnt(inc, lambda f: f.on_bp_drug),
        "on_bp_drug_only_furosemide_313988": cnt(
            inc, lambda f: f.on_bp_drug and {r.code for r in f.bp_drugs} == {"313988"}),
        "missing": {k: cnt(inc, lambda f, k=k: k in f.missing) for k in facts.OPEN_WORLD_INPUTS},
    }
    (run / "results").mkdir(exist_ok=True)
    (run / "results" / "pre_oracle_counts.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
