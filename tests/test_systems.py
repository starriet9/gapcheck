"""Systems L1, A1, A2 with scripted mock models; serialization; R0/R1 on a case set."""
import copy
import json

import numpy as np
import pytest

import casedb
from conftest import FIXTURE, SEED
from gapcheck import cases, edits, load, oracle, patches, score, serialize, systems, verifier
from gapcheck import schema as S
from gapcheck.backends import BackendError, MockBackend, Response, ToolCall
from gapcheck.mock_agents import ABSTAIN, ReferenceAgent
from gapcheck.tools import CaseEnv


@pytest.fixture
def env():
    e = CaseEnv(casedb.connect())
    yield e
    e.close()


ST = systems.Settings(max_steps=6, repair_steps=4)


def _fake_row(a):
    a = copy.deepcopy(a)
    i = next(k for k, it in enumerate(a["evidence"]) if it["claim"] == "sbp")
    a["evidence"][i]["source"] = ["obs_doesnotexist"]
    return a


def _inferred_lipids(a):
    a = copy.deepcopy(a)
    a["evidence"] = [S.item("lipid_panel", S.INFERRED, None, it["value"])
                     if it["claim"] == "lipid_panel" else it for it in a["evidence"]]
    return a


def test_parse_answer():
    good = json.dumps(ABSTAIN)
    assert systems.parse_answer(good)[0] == ABSTAIN
    assert systems.parse_answer("```json\n" + good + "\n```")[0] == ABSTAIN
    assert systems.parse_answer("Here it is: " + good)[0] == ABSTAIN
    assert systems.parse_answer("")[1] == "empty reply"
    assert systems.parse_answer("{not json}")[1].startswith("invalid JSON")
    assert systems.parse_answer(json.dumps({**ABSTAIN, "status": "MAYBE"}))[1].startswith("schema")


def test_correct_agent_passes_verifier(env):
    b = MockBackend(ReferenceAgent())
    a1, a2 = systems.run_agent(env, b, ST)
    ref = systems.run_rule("R1", env)["output"]
    assert a1["output"] == ref and a2["output"] == ref
    assert a2["verifier"][0]["ok"] and len(a2["verifier"]) == 1 and not a2["forced_fallback"]
    assert a1["tool_calls"] >= 10 and a1["model_calls"] == 5
    assert a1["tokens"]["input_tokens"] > 0
    tools_used = {t["name"] for t in a1["trace"] if t["type"] == "tool"}
    assert {"get_demographics", "get_observations", "search_notes", "prevent_risk"} <= tools_used
    assert not any("error" in t["result"] for t in a1["trace"] if t["type"] == "tool")


def test_nonexistent_row_fails_check1_then_repair(env):
    b = MockBackend(ReferenceAgent(mutate=_fake_row, repair=lambda a: a))
    a1, a2 = systems.run_agent(env, b, ST)
    first = a2["verifier"][0]
    assert not first["ok"] and {f["check"] for f in first["failures"]} == {1}
    assert a2["verifier"][1]["ok"] and not a2["forced_fallback"]
    assert a1["output"]["evidence"] != a2["output"]["evidence"]  # A1 = pre-verification
    repair_msg = [t for t in a2["trace"] if t["type"] == "user" and t["kind"] == "repair"][0]
    assert repair_msg["content"].startswith("The evidence checks failed: check 1 (evidence[")
    assert "obs_doesnotexist" not in repair_msg["content"]


def test_inferred_deciding_input_fails_check2(env):
    b = MockBackend(ReferenceAgent(mutate=_inferred_lipids, repair=lambda a: a))
    _, a2 = systems.run_agent(env, b, ST)
    assert 2 in {f["check"] for f in a2["verifier"][0]["failures"]}


def test_forced_fallback(env):
    b = MockBackend(ReferenceAgent(mutate=_fake_row))
    a1, a2 = systems.run_agent(env, b, ST)
    assert a2["forced_fallback"] and len(a2["verifier"]) == 2
    out = a2["output"]
    assert out["status"] == oracle.CANNOT_DETERMINE and out["benefit_group"] is None
    assert out["missing"] == a1["output"]["missing"]
    S.validate(out)
    assert a1["output"]["status"] == oracle.DOCUMENTED_REASON and not a1["forced_fallback"]


def test_reask_then_schema_invalid(env):
    ok = MockBackend(ReferenceAgent(text_override=["I think GAP."]))
    a1, _ = systems.run_agent(env, ok, ST, verify=False)
    assert a1["schema_valid"]
    assert [t["kind"] for t in a1["trace"] if t["type"] == "user"] == ["reask"]
    bad = MockBackend(["no json here", "still none"])
    a1, a2 = systems.run_agent(env, bad, ST)
    assert not a1["schema_valid"] and a1["output"] is None and a1["error"]
    assert not a2["schema_valid"] and not a2["forced_fallback"]


def test_tool_limit(env):
    spin = MockBackend(lambda m, t: Response("", [ToolCall("x", "get_allergies", {})]))
    a1, _ = systems.run_agent(env, spin, systems.Settings(max_steps=2), verify=False)
    assert not a1["schema_valid"]
    errs = [t for t in a1["trace"] if t["type"] == "tool" and t["result"] == {"error": "tool limit reached"}]
    assert errs and a1["tool_calls"] >= 3


def test_l1_only_prevent_tool(env):
    b = MockBackend(ReferenceAgent())
    r = systems.run_l1(env, b, systems.Settings())
    assert r["schema_valid"] and r["output"] == systems.run_rule("R1", env)["output"]
    assert b.calls[0]["tools"] == ["prevent_risk"]
    user = b.calls[0]["messages"][1]["content"]
    assert user.startswith(systems.prompt("task_l1")) and "# Patient record" in user
    # a tool other than prevent_risk is refused
    calls = iter([Response("", [ToolCall("a", "get_allergies", {})]), Response(json.dumps(ABSTAIN))])
    r = systems.run_l1(env, MockBackend(lambda m, t: next(calls)), systems.Settings())
    tool = [t for t in r["trace"] if t["type"] == "tool"][0]
    assert "not available" in tool["result"]["error"]


def test_l1_two_rounds_max(env):
    args = {"sex": "male", "age": 55, "tc": 220, "hdl": 45, "sbp": 142, "bp_treated": 1,
            "statin": 0, "diabetes": 0, "smoker": 0, "egfr": 88}
    spin = MockBackend(lambda m, t: Response("", [ToolCall("p", "prevent_risk", args)]))
    r = systems.run_l1(env, spin, systems.Settings())
    real = [t for t in r["trace"] if t["type"] == "tool" and "risk_10y" in t["result"]]
    assert len(real) == 2 and not r["schema_valid"]


def test_system_prompt_has_conventions():
    m = systems.system_message()["content"]
    assert m.startswith(systems.prompt("system")) and "## Decision table" in m
    assert set(systems.prompt_hashes()) == set(systems.PROMPTS)


# ---------------------------------------------------------------- serialization
def test_serialization(env):
    t = serialize.serialize(env)
    assert "Index date: 2026-06-01" in t
    assert "note_b" in t and "note_c" in t and "note_a" not in t  # 365-day window
    assert "obs_sbplast" in t and t.count("Systolic Blood Pressure") == 1
    sbp_line = next(x for x in t.splitlines() if x.startswith("- 8480-6"))
    assert sbp_line.count("(row ") == 3
    assert "obs_late" not in t  # after index
    assert "first start 2010-03-01" in t and "4 rows" in t  # long unlisted code: first/last
    assert "active at index: yes" in t
    f = env.facts()
    r = oracle.label(f)
    ids = [i for v in r.inputs_used.values() for i in v]
    serialize.check(t, ids, [], serialize.est_tokens(t), 10_000)
    with pytest.raises(serialize.BuildStop):
        serialize.check(t, ids + ["obs_nothere"], [], 10, 10_000)
    with pytest.raises(serialize.BuildStop):
        serialize.check(t, ids, ["a planted sentence"], 10, 10_000)
    with pytest.raises(serialize.BuildStop):
        serialize.check(t, ids, [], 20_000, 10_000)


# ---------------------------------------------------------------- case set (fixture)
@pytest.fixture(scope="module")
def case_set(tmp_path_factory):
    d = tmp_path_factory.mktemp("sys_cases")
    con = load.load(FIXTURE, d / "f.duckdb", seed=SEED)
    patches.apply_all(con, SEED)
    cases.ensure_p2(con, SEED)
    out = d / "cs"
    cases.build_case_set(con, out, "dev", 10, SEED, edits.load_phrases("dev"))
    con.close()
    return out


def test_rules_on_case_set(case_set):
    """R0 and R1 contain the oracle: correct on C0, C1, C2a, C2b, C5 by construction.
    Their outputs pass verifier checks 1, 3 and 4, and every case passes the L1 build stop."""
    cl = cases.read_cases(case_set)
    recs = {"R0": {}, "R1": {}}
    for c in cl:
        env = CaseEnv.from_case(case_set, c["case_id"])
        for s in recs:
            r = systems.run_rule(s, env)
            rep = verifier.verify(r["output"], env)
            assert not {f.check for f in rep.failures} & {1, 3, 4}, (s, c, rep.failures)
            recs[s][c["case_id"]] = r
        t = serialize.serialize(env)
        serialize.build_stop(case_set, c["case_id"], t, serialize.est_tokens(t), 10**9)
        env.close()
    for s, rr in recs.items():
        for c in cl:
            if c["condition"] in ("C0", "C1", "C2a", "C2b", "C5"):
                assert score.correct(c, rr[c["case_id"]]), (s, c)
    assert all(score.correct(c, recs["R1"][c["case_id"]]) for c in cl if c["condition"] == "C3a")
    assert not any(score.correct(c, recs["R0"][c["case_id"]]) for c in cl
                   if c["condition"] == "C3a")


def test_reference_agent_on_case_set(case_set):
    cl = cases.read_cases(case_set)[:6]
    for c in cl:
        env = CaseEnv.from_case(case_set, c["case_id"])
        a1, a2 = systems.run_agent(env, MockBackend(ReferenceAgent()), ST)
        assert a1["output"] == systems.run_rule("R1", env)["output"]
        assert a2["schema_valid"]
        env.close()


def test_oracle_complete_passes_verifier_on_case_set(case_set):
    """Verifier fairness: the oracle's label with complete, correct evidence passes on
    every case with no forced fallback, and scores 100%."""
    from gapcheck.mock_agents import OracleCompleteAgent
    cl = cases.read_cases(case_set)
    for c in cl:
        env = CaseEnv.from_case(case_set, c["case_id"])
        a1, a2 = systems.run_agent(env, MockBackend(OracleCompleteAgent()), ST)
        assert a2["schema_valid"] and not a2["forced_fallback"], (c, a2["verifier"])
        assert len(a2["verifier"]) == 1 and a2["verifier"][0]["ok"], (c, a2["verifier"])
        assert score.correct(c, a2) and score.correct(c, a1), c
        calls = [t for t in a2["trace"] if t["type"] == "tool"]
        assert all(t["name"] == "prevent_risk" and "error" not in t["result"] for t in calls)
        comp = [it for it in a2["output"]["evidence"]
                if it["support"] == S.COMPUTED and it["claim"] == "risk_10y"]
        assert len(comp) == len(calls)
        if c["condition"] == "C2b" and a2["output"]["benefit_group"]:
            assert len(calls) == 2 and "smoking" in a2["output"]["missing"]
        env.close()


def test_oracle_complete_needs_a_case():
    from gapcheck.mock_agents import OracleCompleteAgent
    env = CaseEnv(casedb.connect())
    with pytest.raises(RuntimeError):
        OracleCompleteAgent().bind(env)
    env.close()


# ---------------------------------------------------------------- real-server failures
class _RejectWhen:
    """Reference agent whose server rejects a request once `when(messages)` holds."""

    def __init__(self, when, **kw):
        self.inner = ReferenceAgent(**kw)
        self.when = when

    def bind(self, env):
        self.inner.bind(env)

    def __call__(self, messages, tools):
        if self.when(messages):
            raise BackendError(400, "request exceeds the available context size")
        return self.inner(messages, tools)


def _in_repair(messages):
    return any(m["role"] == "user" and m["content"].startswith("The evidence checks failed")
               for m in messages)


def test_backend_error_is_recorded_not_fatal(env):
    """A request the server rejects (context overflow) ends that system's run with no
    answer and no re-ask; it does not crash the run."""
    b = MockBackend(_RejectWhen(lambda m: len(m) > 3))
    a1, a2 = systems.run_agent(env, b, ST)
    assert not a1["schema_valid"] and a1["error"].startswith("backend: HTTP 400")
    assert not [t for t in a1["trace"] if t["type"] == "user"]  # no re-ask
    assert [t["type"] for t in a1["trace"]][-1] == "error"
    assert not a2["schema_valid"] and not a2["forced_fallback"]
    l1 = systems.run_l1(env, MockBackend(_RejectWhen(lambda m: True)), ST)
    assert not l1["schema_valid"] and l1["error"].startswith("backend")


def test_backend_error_in_repair_forces_fallback(env):
    b = MockBackend(_RejectWhen(_in_repair, mutate=_fake_row, repair=lambda a: a))
    a1, a2 = systems.run_agent(env, b, ST)
    assert a1["schema_valid"] and a2["forced_fallback"]
    assert a2["output"]["status"] == oracle.CANNOT_DETERMINE
    assert a2["error"].startswith("backend") and len(a2["verifier"]) == 1


def test_l1_estimate_counts_tools_and_takes_chars_per_token(env):
    """Real tokenizers give about 1.75x chars/4 on these prompts; the build-stop estimate
    must be calibratable, and it includes the prevent_risk definition."""
    ser4, p4 = systems.l1_prompt_tokens(env)
    msgs = systems.l1_messages(env)
    assert p4 > serialize.est_tokens("".join(m["content"] for m in msgs))
    ser2, p2 = systems.l1_prompt_tokens(env, 2.2)
    assert p2 > 1.8 * p4 > p4 and ser2 > ser4
    assert systems.Settings.from_dict({"chars_per_token": 2.2}).chars_per_token == 2.2


def test_score_uses_the_run_case_list(case_set, tmp_path):
    """A run on part of a case set is scored on its own cases (manifest case_ids), not
    with every other case counted wrong; a listed case with no record is wrong."""
    cl = cases.read_cases(case_set)
    picked = [c for c in cl if c["condition"] in ("C0", "C1")][:3]
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"case_ids": [c["case_id"] for c in picked]}))
    lines = []
    for c in picked[:2]:
        env = CaseEnv.from_case(case_set, c["case_id"])
        lines.append(json.dumps({"case_id": c["case_id"], "system": "R1",
                                 **systems.run_rule("R1", env)}, default=str))
        env.close()
    (run / "R1.jsonl").write_text("\n".join(lines) + "\n")
    s = score.score(case_set, run, n_boot=50)
    assert s["n_cases"] == 3 and s["n_cases_in_set"] == len(cl)
    acc = s["accuracy_main"]["systems"]["R1"]["by_condition"]
    n_ok = sum(acc[k]["est"] * s["accuracy_main"]["n_cases"][k] for k in ("C0", "C1")
               if acc[k]["est"] is not None)
    assert round(n_ok) == 2  # the third listed case has no record: wrong
    (run / "manifest.json").write_text("{}")
    assert score.score(case_set, run, n_boot=50)["n_cases"] == len(cl)
