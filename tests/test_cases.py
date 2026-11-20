"""Case sets on the fixture: determinism, oracle agreement, note scrub, id stability."""
import hashlib
import json
import re
from datetime import date

import pandas as pd
import pytest

from conftest import FIXTURE, SEED
from gapcheck import cases, edits, facts, load, notes, oracle, patches, phrases

N = 10


def _build(base_dir):
    con = load.load(FIXTURE, base_dir / "f.duckdb", seed=SEED)
    patches.apply_all(con, SEED)
    cases.ensure_p2(con, SEED)
    out = base_dir / "cs"
    m = cases.build_case_set(con, out, "dev", N, SEED, edits.load_phrases("dev"))
    con.close()
    return out, m


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("cases_a"))


@pytest.fixture(scope="module")
def case_list(built):
    return cases.read_cases(built[0])


def test_determinism(built, tmp_path_factory):
    out_b, m_b = _build(tmp_path_factory.mktemp("cases_b"))
    out_a, m_a = built
    assert m_a["files"] == m_b["files"]
    assert (out_a / "manifest.json").read_bytes() == (out_b / "manifest.json").read_bytes()
    for name, h in m_a["files"].items():
        assert hashlib.sha256((out_b / name).read_bytes()).hexdigest() == h


def test_manifest_lists_every_file_and_seed(built):
    out, m = built
    on_disk = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} - {"manifest.json"}
    assert set(m["files"]) == on_disk
    assert m["master_seed"] == SEED
    assert m["sub_seeds"] == {k: str(patches.sub_seed(SEED, k)) for k in cases.SUB_SEED_LABELS}
    assert m["phrases"]["version"] == edits.load_phrases("dev")["version"]


def test_every_condition_present(case_list):
    # The fixture's only C5 source was an S2 LDL pair; LDL twins now move up only and
    # need no LDL-C >= 190 at any time (SPEC 5.2), so the fixture has no C5 case.
    # c5_ldl is tested on hand-made records in test_edits.py.
    got = {c["condition"] for c in case_list}
    assert got == set(edits.CONDITIONS) - {"C5"}


def test_expected_label_equals_oracle_on_materialized_case(built, case_list):
    for c in case_list:
        r = cases.oracle_on_case(built[0], c["case_id"])
        assert (r.status, list(r.missing)) == (c["expected_status"], c["expected_missing"]), c


def test_directions(case_list):
    by = {c["case_id"]: c for c in case_list}
    for c in case_list:
        cond, st, base = c["condition"], c["expected_status"], c["base_status"]
        if cond == "C1":
            assert st == oracle.CANNOT_DETERMINE and c["variant"] in c["expected_missing"]
        elif cond == "C3a":
            assert base == oracle.GAP and st == oracle.DOCUMENTED_REASON
        elif cond == "C5":
            assert st != by[c["partner_case_id"]]["expected_status"]
            assert by[c["partner_case_id"]]["pair_id"] in (c["pair_id"], None)
        elif cond != "C0":
            assert st == base and c["expected_missing"] == []


SCRUB = {
    "lipid_panel": r"lipid panel|cholesterol|\bLDL|\bHDL|triglyceride\b",
    "sbp": r"blood pressure|systolic|diastolic",
    "smoking": r"smok",
}


def test_c1_note_scrub(built, case_list):
    """After a C1 deletion no note mentions the deleted input, and no row of it remains."""
    c1 = [c for c in case_list if c["condition"] == "C1"]
    assert c1
    for c in c1:
        con = cases.materialize(built[0], c["case_id"])
        texts = [r[0] for r in con.execute("select TEXT from notes").fetchall()]
        assert texts
        pat = re.compile(SCRUB[c["variant"]], re.I)
        assert not [t for t in texts if pat.search(t)], c["variant"]
        for table, codes in edits.DELETE_CODES[c["variant"]].items():
            n = con.execute(f"select count(*) from {table} where CODE in (select unnest(?))",
                            [list(codes)]).fetchall()[0][0]
            assert n == 0
        con.close()
    # the base notes of the same patients did mention it
    base = cases._case_set(str(built[0]))["notes"]
    for c in c1:
        if c["variant"] == "sbp":
            continue  # notes never name blood pressure (vital signs are not listed)
        t = base[base.PATIENT == c["base_patient"]].TEXT
        assert t.str.contains(SCRUB[c["variant"]], case=False, regex=True).any()


def test_id_stability(built, case_list):
    """An edit changes no other row id or note id; added rows get fresh ids."""
    cs = cases._case_set(str(built[0]))
    for c in case_list:
        tables, nt, dl = cases.case_tables(built[0], c["case_id"])
        for name in edits.ALL_TABLES:
            col = "Id" if name == "patients" else "PATIENT"
            b = cs["base"][name]
            b = b[b[col] == c["base_patient"]]
            dele = set(dl["delete"].get(name, ()))
            added = {r["row_id"] for r in dl["add"].get(name, [])}
            assert dele <= set(b.row_id)
            assert not added & set(b.row_id)
            assert set(tables[name].row_id) == (set(b.row_id) - dele) | added
            kept = b[~b.row_id.isin(dele)].set_index("row_id").sort_index()
            now = tables[name][~tables[name].row_id.isin(added)].set_index("row_id").sort_index()
            pd.testing.assert_frame_equal(kept, now, check_dtype=False)
        base_ids = cs["notes"][cs["notes"].PATIENT == c["base_patient"]].note_id
        assert list(nt.note_id) == list(base_ids)


def test_added_row_ids_follow_load_rule(built, case_list):
    """An edited row's id is the load rule applied to its content."""
    cols = {n: [k for k in df.columns if k not in ("row_id", "patch")]
            for n, df in cases._case_set(str(built[0]))["base"].items()}
    for c in case_list:
        _, _, dl = cases.case_tables(built[0], c["case_id"])
        for name, rows in dl["add"].items():
            for r in rows:
                vals = [r[k] for k in cols[name]]
                assert r["row_id"] == load.row_id(name, vals, 0)
                assert r["patch"] == edits.EDIT_PATCH


def test_notes_are_a_function_of_tables_and_ledger(built, case_list):
    for c in case_list:
        tables, nt, _ = cases.case_tables(built[0], c["case_id"])
        again = notes.notes_frame(notes.render_notes(tables, cases.case_plants(built[0],
                                                                               c["case_id"])))
        assert list(again.note_id) == list(nt.note_id)
        assert list(again.TEXT) == list(nt.TEXT)


def test_planting_rules(built, case_list):
    cs = cases._case_set(str(built[0]))
    coh = cs["cohort"].set_index("PATIENT")
    used = []
    for c in case_list:
        dl = cs["deltas"][c["case_id"]]
        want = {"C3a": 1, "C3b": 1, "C4": 1}.get(c["condition"],
                                                  1 if c["variant"] == "smoking" and
                                                  c["condition"] == "C5" else 0)
        assert len(dl["plants"]) == want
        _, nt, _ = cases.case_tables(built[0], c["case_id"])
        ix = date.fromisoformat(coh.loc[c["base_patient"], "index_date"])
        sex, age = coh.loc[c["base_patient"], "sex"], int(coh.loc[c["base_patient"], "age"])
        for p in dl["plants"]:
            used.append(p["phrase_id"])
            assert 0 <= (ix - date.fromisoformat(p["note_date"])).days <= 365
            text = nt[nt.note_id == p["note_id"]].TEXT.iloc[0]
            assert text.count(p["text"]) == 1
            base_text = cs["notes"][cs["notes"].note_id == p["note_id"]].TEXT.iloc[0]
            assert p["text"] not in base_text
            if p["reason_category"] == "pregnancy":
                assert sex == "F" and 30 <= age <= 49
            ph = {x["id"]: x for t in edits.load_phrases("dev")["types"].values() for x in t}
            if ph[p["phrase_id"]]["category"] == "pregnancy":
                assert sex == "F"
            assert phrases.may_plant(ph[p["phrase_id"]], sex)  # "She ..." only in women
            assert ph[p["phrase_id"]]["sex"] in (None, sex)
    assert len(used) == len(set(used))  # each phrase at most once per split


def test_ledger_rows_match_cases(built, case_list):
    led = pd.read_csv(built[0] / "edit_ledger.csv", dtype=str, keep_default_na=False)
    assert list(led.case_id) == [c["case_id"] for c in case_list]
    assert set(led.direction_check) == {"pass"}
    assert (led.expected_status.tolist() == [c["expected_status"] for c in case_list])


def test_c5_rules(built, case_list):
    by = {c["case_id"]: c for c in case_list}
    for c in case_list:
        if c["condition"] != "C5":
            continue
        con = cases.materialize(built[0], c["case_id"])
        f = facts.patient_facts(con, c["base_patient"])
        con.close()
        assert f.ldl != 190 and all(abs(f.ldl - x) >= 5 for x in (70, 160, 190))
        if c["risk10"] is not None:
            assert all(abs(c["risk10"] - x) >= 0.005 for x in (0.03, 0.05, 0.10))
        assert c["expected_status"] != by[c["partner_case_id"]]["expected_status"]
