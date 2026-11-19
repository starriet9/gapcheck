"""Agent tools on a hand-made case database."""
import json

import pytest

import casedb
from gapcheck import codes as C
from gapcheck import prevent
from gapcheck import tools as T


@pytest.fixture
def env():
    e = T.CaseEnv(casedb.connect(), page_size=10)
    yield e
    e.close()


def test_every_tool_has_a_schema():
    for name, spec in T.TOOL_SPECS.items():
        p = spec["parameters"]
        assert p["type"] == "object" and p["additionalProperties"] is False
        assert set(p["required"]) <= set(p["properties"])
        assert hasattr(T.CaseEnv, name)
    assert [t["name"] for t in T.tool_specs(("prevent_risk",))] == ["prevent_risk"]


def test_get_demographics(env):
    d = env.call("get_demographics", {})
    assert d == {"source": "demographics", "index_date": "2026-06-01", "age": 55, "sex": "M",
                 "birthdate": "1971-01-15"}


def test_search_conditions_by_query_and_codes(env):
    r = env.call("search_conditions", {"query": "HYPERTENSION"})
    assert [i["row_id"] for i in r["items"]] == ["cond_htn"]
    assert r["items"][0]["active_at_index"] is True and r["items"][0]["table"] == "conditions"
    r = env.call("search_conditions", {"codes": ["171207006"]})
    assert r["total"] == 4 and {i["table"] for i in r["items"]} == {"procedures"}
    assert all(i["active_at_index"] is False for i in r["items"])
    assert env.call("search_conditions", {"codes": sorted(C.DIABETES)})["total"] == 0
    assert "error" in env.call("search_conditions", {})
    # encounters are searchable too
    assert env.call("search_conditions", {"query": "general examination"})["total"] == 3


def test_get_observations_paging_and_index(env):
    r = env.call("get_observations", {"code": C.SBP.code})
    assert r["total"] == 26 and r["pages"] == 3 and len(r["items"]) == 10
    assert r["items"][0] == {"row_id": "obs_sbplast", "date": "2026-05-01T10:00:00Z",
                             "value": 142, "unit": "mm[Hg]"}
    dates = [i["date"] for i in r["items"]]
    assert dates == sorted(dates, reverse=True)
    p3 = env.call("get_observations", {"code": C.SBP.code, "page": 3})
    assert len(p3["items"]) == 6
    seen = {i["row_id"] for p in (1, 2, 3)
            for i in env.call("get_observations", {"code": C.SBP.code, "page": p})["items"]}
    assert len(seen) == 26
    # after the index date: not shown
    ldl = env.call("get_observations", {"code": C.LDL.code})
    assert [i["value"] for i in ldl["items"]] == [150, 140]
    since = env.call("get_observations", {"code": C.LDL.code, "since": "2025-01-01"})
    assert since["total"] == 1
    assert "error" in env.call("get_observations", {"code": C.LDL.code, "since": "May 2025"})
    assert "error" in env.call("get_observations", {"code": C.SBP.code, "page": 0})


def test_get_medications(env):
    act = env.call("get_medications", {})
    assert [i["row_id"] for i in act["items"]] == ["med_lis"]
    allm = env.call("get_medications", {"active_only": False})
    assert {i["row_id"]: i["active_at_index"] for i in allm["items"]} == {
        "med_sim": False, "med_lis": True}


def test_get_allergies(env):
    assert env.call("get_allergies", {}) == {"total": 0, "items": []}


def test_search_and_read_notes(env):
    r = env.call("search_notes", {"query": "declined"})
    assert [i["note_id"] for i in r["items"]] == ["note_b"]
    text = env.call("read_note", {"note_id": "note_b"})["text"]
    for s in r["items"][0]["snippets"]:
        assert s in text  # snippets are exact substrings
    lst = env.call("search_notes", {})
    assert [i["note_id"] for i in lst["items"]] == ["note_c", "note_b", "note_a"]
    assert lst["items"][0]["encounter"].startswith("wellness")
    assert env.call("search_notes", {"since": "2025-01-01"})["total"] == 2
    assert "error" in env.call("read_note", {"note_id": "note_zzz"})


def test_prevent_risk(env):
    args = {"sex": "male", "age": 55, "tc": 220, "hdl": 45, "sbp": 142, "bp_treated": 1,
            "statin": 0, "diabetes": 0, "smoker": 0, "egfr": 88}
    r = env.call("prevent_risk", {**args, "sources": {"sbp": ["obs_sbplast"]}})
    want = prevent.ascvd_risk("male", 55, 220, 45, 142, 1, 0, 0, 0, 88)
    assert abs(r["risk_10y"] - want) < 1e-6 and r["risk_30y"] is not None
    assert r["sources"] == {"sbp": ["obs_sbplast"]}
    assert "error" in env.call("prevent_risk", {**args, "sbp": 250})
    assert "error" in env.call("prevent_risk", {k: v for k, v in args.items() if k != "egfr"})
    old = env.call("prevent_risk", {**args, "age": 65})
    assert "risk_30y" not in old  # 30-year risk only at ages 30-59


def test_guideline_lookup(env):
    r = env.call("guideline_lookup", {"row": "9"})
    assert r["label"] == "GAP" and r["sources"][0]["cor"] == "1"
    assert "error" in env.call("guideline_lookup", {"row": "X"})
    assert "error" in env.call("guideline_lookup", {"row": "12"})


def test_bad_calls_and_log(env):
    assert "error" in env.call("nope", {})
    assert "error" in env.call("get_allergies", {"x": 1})
    assert len(env.calls) == 2 and env.calls[0]["tool"] == "nope"
    json.dumps(env.calls)  # JSON-ready


def test_named_lists(env):
    assert env.named_list("statins") == []          # simvastatin stopped in 2016
    assert env.named_list("bp_drugs") == ["med_lis"]
    assert env.named_list("diabetes_codes") == []   # prediabetes is not diabetes
    assert env.named_list("lipid_panel") == ["obs_btc", "obs_bhdl", "obs_bldl"]
    assert env.named_list("sbp") == ["obs_sbplast"]
    assert env.named_list("reasons_365d") == []     # the note reason is not structured


def test_no_patch_column_shown(env):
    env.con.execute("update observations set patch = 'P1' where row_id = 'obs_egfr'")
    r = env.call("get_observations", {"code": C.EGFR.code})
    assert "patch" not in json.dumps(r) and "P1" not in json.dumps(r)
    assert "patch" not in env.row("obs_egfr")[1]
