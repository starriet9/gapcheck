"""Systems L1, A1, A2 with scripted mock models; serialization; R0/R1 on a case set."""
import copy
import hashlib
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
    assert user.startswith(systems.task_l1(2)) and "# Patient record" in user
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


def _well_formed(messages):
    """Every assistant tool call is answered by a tool message before the next turn."""
    for k, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {c["id"] for c in m["tool_calls"]}
            got = set()
            for n in messages[k + 1:]:
                if n["role"] != "tool":
                    break
                got.add(n["tool_call_id"])
            if ids - got:
                return False
    return True


def test_tool_call_after_limit_is_answered_and_reasked_truthfully(env):
    """A model that keeps calling tools after the limit prompt (seen with a real model in
    L1) must not leave a tool call unanswered in the conversation (the re-ask request
    would be rejected by the Messages API), and the re-ask names the real problem."""
    n = {"i": 0}

    def spin(messages, tools):
        n["i"] += 1
        return Response("", [ToolCall(f"x{n['i']}", "get_allergies", {})])

    b = MockBackend(spin)
    a1, _ = systems.run_agent(env, b, systems.Settings(max_steps=1), verify=False)
    assert not a1["schema_valid"]
    assert all(_well_formed(c["messages"]) for c in b.calls)
    reask = [t for t in a1["trace"] if t["type"] == "user" and t["kind"] == "reask"]
    assert reask and "a tool call after the tool limit" in reask[0]["content"]
    assert a1["tool_calls"] == n["i"]  # every call is logged, refused ones included
    assert a1["error"] == "a tool call after the tool limit"


def test_task_prompts_state_the_harness_limits(env):
    """The round budget a model reads is the limit the harness enforces (A1: max_steps, L1:
    l1_tool_rounds), and the prompts carry the stop rule (with parallel calls and a stop
    condition for the agent), the COMPUTED rule and the over-abstention rule (local-model
    smoke runs)."""
    for ps in systems.PROMPT_SETS:
        for n in (3, 12, 20):
            b = MockBackend(lambda m, t: Response(json.dumps(ABSTAIN)))
            systems.run_agent(env, b, systems.Settings(max_steps=n, prompt_set=ps), verify=False)
            task = b.calls[0]["messages"][1]["content"]
            assert task == systems.task_agent(n, ps)
            assert f"at most {n} rounds of tool calls" in task and "stop searching" in task.lower()
            assert "several tool calls" in task and "confirmed absent" in task
            b = MockBackend(lambda m, t: Response(json.dumps(ABSTAIN)))
            systems.run_l1(env, b, systems.Settings(l1_tool_rounds=n, prompt_set=ps))
            assert f"at most {n} rounds of calls" in b.calls[0]["messages"][1]["content"]
        for name in ("task_l1", "task_agent"):
            assert "never makes the answer CANNOT_DETERMINE" in systems.prompt(name, ps)
        assert "{" not in systems.task_l1(2, ps) + systems.task_agent(20, ps) \
            + systems.rounds_left(2, ps)
    assert "A COMPUTED claim must copy a prevent_risk result" in systems.prompt("system")
    assert "do not call it again with the same inputs" in systems.prompt("task_l1")


# ---------------------------------------------------------------- run options
NOREPEAT = "Do not repeat a search whose rows you already have."


def _spinner():
    """A model that calls one tool per turn for ever (until refused twice)."""
    n = {"i": 0}

    def spin(messages, tools):
        n["i"] += 1
        return Response("", [ToolCall(f"s{n['i']}", "get_allergies", {})])
    return spin


def _notices(trace):
    return [t for t in trace if t["type"] == "user" and t["kind"] == "rounds_left"]


def test_run_options_default_off():
    st = systems.Settings()
    assert st.prompt_set == "default" and st.rounds_left_notice == 0
    assert systems.Settings.from_dict({}) == st
    rec = systems.prompt_record()
    assert rec["prompt_set"] == "default"
    assert rec["prompt_files"] == {n: f"{n}.txt" for n in systems.PROMPTS}
    assert rec["prompts_sha256"] == systems.prompt_hashes()
    with pytest.raises(ValueError, match="prompt_set"):
        systems.Settings.from_dict({"prompt_set": "nosuchset"})
    with pytest.raises(ValueError, match="rounds_left_notice"):
        systems.Settings(rounds_left_notice=-1)


def test_no_notice_by_default(env):
    b = MockBackend(_spinner())
    a1, _ = systems.run_agent(env, b, systems.Settings(max_steps=4), verify=False)
    assert not _notices(a1["trace"])
    assert not any(m["role"] == "user" and m["content"].startswith("Rounds of tool calls left")
                   for c in b.calls for m in c["messages"])


def test_rounds_left_notice_after_the_round_that_leaves_n(env):
    b = MockBackend(_spinner())
    st = systems.Settings(max_steps=5, rounds_left_notice=2)
    a1, _ = systems.run_agent(env, b, st, verify=False)
    notes = _notices(a1["trace"])
    assert [t["content"] for t in notes] == ["Rounds of tool calls left: 2."]
    assert systems.rounds_left(2) == "Rounds of tool calls left: 2."
    # it follows the tool results of round 3 (5 - 3 = 2 left) and precedes the 4th call
    msgs = b.calls[3]["messages"]
    assert msgs[-1] == {"role": "user", "content": "Rounds of tool calls left: 2."}
    assert msgs[-2]["role"] == "tool" and msgs[-3]["role"] == "assistant"
    assert sum(m["role"] == "assistant" for m in msgs) == 3
    assert all(_well_formed(c["messages"]) for c in b.calls)
    # the limit still applies at 5 rounds: 5 real calls, then refusals
    real = [t for t in a1["trace"] if t["type"] == "tool" and t["result"] != {"error": "tool limit reached"}]
    assert len(real) == 5
    # a notice at or above the limit never fires; L1 never gets one
    a1, _ = systems.run_agent(env, MockBackend(_spinner()),
                              systems.Settings(max_steps=2, rounds_left_notice=2), verify=False)
    assert not _notices(a1["trace"])
    r = systems.run_l1(env, MockBackend(lambda m, t: Response("", [ToolCall(
        "p", "prevent_risk", {"sex": "male", "age": 55, "tc": 220, "hdl": 45, "sbp": 142,
                              "bp_treated": 1, "statin": 0, "diabetes": 0, "smoker": 0,
                              "egfr": 88})])), systems.Settings(l1_tool_rounds=3, rounds_left_notice=1))
    assert not _notices(r["trace"])


def test_rounds_left_notice_in_the_repair_loop(env):
    """A2's repair loop counts its own rounds (repair_steps) and gets the notice too."""
    state = {"n": 0}
    first = ReferenceAgent(mutate=_fake_row)  # first draft cites a row that does not exist
    first.bind(env)
    good = systems.run_rule("R1", env)["output"]

    def policy(messages, tools):
        said = next(m for m in reversed(messages) if m["role"] == "user")["content"]
        if "evidence checks failed" in said or state["n"]:
            state["n"] += 1  # repair: three rounds of calls, then the corrected answer
            if state["n"] <= 3:
                return Response("", [ToolCall(f"r{state['n']}", "get_allergies", {})])
            return Response(json.dumps(good))
        return first(messages, tools)
    b = MockBackend(policy)
    _, a2 = systems.run_agent(env, b, systems.Settings(max_steps=6, repair_steps=4,
                                                       rounds_left_notice=1))
    kinds = [t["kind"] for t in a2["trace"] if t["type"] == "user"]
    assert kinds == ["repair", "rounds_left"]  # main loop: 4 rounds of 6, never 1 left
    assert a2["verifier"][-1]["ok"] and not a2["forced_fallback"]


def test_prompt_set_norepeat_adds_one_sentence(env):
    base, var = systems.prompt("task_agent"), systems.prompt("task_agent", "norepeat")
    assert NOREPEAT not in base and var.count(NOREPEAT) == 1
    assert var.replace(NOREPEAT + " ", "") == base  # nothing else differs
    assert systems.prompt_file("task_agent", "norepeat").name == "task_agent_norepeat.txt"
    for role in systems.PROMPTS:
        if role != "task_agent":
            assert systems.prompt(role, "norepeat") == systems.prompt(role)
    d, n = systems.prompt_hashes(), systems.prompt_hashes("norepeat")
    assert {k for k in d if d[k] != n[k]} == {"task_agent"}
    rec = systems.prompt_record("norepeat")
    assert rec["prompt_files"]["task_agent"] == "task_agent_norepeat.txt"
    assert rec["prompts_sha256"]["task_agent"] == hashlib.sha256(
        (systems.PROMPT_DIR / "task_agent_norepeat.txt").read_bytes()).hexdigest()
    b = MockBackend(lambda m, t: Response(json.dumps(ABSTAIN)))
    systems.run_agent(env, b, systems.Settings(max_steps=12, prompt_set="norepeat"), verify=False)
    task = b.calls[0]["messages"][1]["content"]
    assert NOREPEAT in task and "at most 12 rounds of tool calls" in task
    b = MockBackend(lambda m, t: Response(json.dumps(ABSTAIN)))
    systems.run_agent(env, b, systems.Settings(max_steps=12), verify=False)
    assert NOREPEAT not in b.calls[0]["messages"][1]["content"]
    # L1 is the same in both sets
    assert systems.l1_messages(env, 2, "norepeat") == systems.l1_messages(env, 2)


def test_every_prompt_file_is_a_role_or_in_a_set():
    """A prompt file nobody reads would be frozen without effect, and a set that names a
    missing file would fail mid-run."""
    used = set(systems.PROMPTS) | {f for m in systems.PROMPT_SETS.values() for f in m.values()}
    assert {p.stem for p in systems.PROMPT_DIR.glob("*.txt")} == used
    for ps in systems.PROMPT_SETS:
        assert all(systems.prompt_file(r, ps).exists() for r in systems.PROMPTS)


def test_round_counter_after_every_round(env):
    b = MockBackend(_spinner())
    a1, _ = systems.run_agent(env, b, systems.Settings(max_steps=3, round_counter=True), verify=False)
    counts = [t["content"] for t in a1["trace"] if t["type"] == "user" and t["kind"] == "round_counter"]
    assert counts == ["Round 1 of 3 used.", "Round 2 of 3 used.", "Round 3 of 3 used."]
    assert systems.round_counter(2, 12) == "Round 2 of 12 used."
    for k in (1, 2, 3):  # each follows that round's tool results, before the next model turn
        msgs = b.calls[k]["messages"]
        assert msgs[-1] == {"role": "user", "content": f"Round {k} of 3 used."}
        assert msgs[-2]["role"] == "tool"
    assert all(_well_formed(c["messages"]) for c in b.calls)
    kinds = [t["kind"] for t in a1["trace"] if t["type"] == "user"]
    assert kinds[:4] == ["round_counter"] * 3 + ["limit"]  # the limit is unchanged
    assert not _notices(a1["trace"])
    # off by default; L1 never gets it
    a1, _ = systems.run_agent(env, MockBackend(_spinner()), systems.Settings(max_steps=3), verify=False)
    assert not [t for t in a1["trace"] if t["type"] == "user" and t["kind"] == "round_counter"]
    r = systems.run_l1(env, MockBackend(lambda m, t: Response(json.dumps(ABSTAIN))),
                       systems.Settings(round_counter=True))
    assert not [t for t in r["trace"] if t["type"] == "user"]


def test_round_counter_settings():
    with pytest.raises(ValueError, match="not both"):
        systems.Settings(round_counter=True, rounds_left_notice=2)
    with pytest.raises(ValueError, match="round_counter"):
        systems.Settings.from_dict({"round_counter": "yes"})
    assert systems.Settings.from_dict({"round_counter": True, "prompt_set": "plan",
                                       "max_steps": 12}).round_counter


def test_prompt_set_plan(env):
    base, plan = systems.prompt("task_agent"), systems.prompt("task_agent", "plan")
    added = ("An efficient plan: in the first round, call get_demographics, get_medications, "
             "search_conditions with the codes from the code lists, get_observations for each "
             "open-world input (one call per code), and search_notes for statin use or reasons not "
             "to prescribe; in the next round, call prevent_risk if your row needs a risk; then "
             "answer. A structured lookup that returns total 0 is enough to claim that input "
             "ABSENT; do not search for it again. " + NOREPEAT)
    assert plan.replace(added + " ", "") == base  # the base prompt, plus these sentences only
    # tool names it mentions exist; it says nothing about the test design
    from gapcheck.tools import TOOL_SPECS
    named = {w.strip(",;.()") for w in added.split() if "_" in w and w.islower()}
    assert named - {"open-world"} <= set(TOOL_SPECS)
    prose = " ".join(w for w in added.split() if "_" not in w)  # without the tool names
    for word in ("C0", "C1", "C2", "C3", "C4", "C5", "edit", "answer key", "ledger", "plant",
                 "oracle", "condition", "test"):
        assert word not in prose
    conv = systems.system_message()["content"]
    for term in ("Code lists", "Open-world inputs", "Required claims per benefit_group"):
        assert term.lower() in conv.lower()
    b = MockBackend(lambda m, t: Response(json.dumps(ABSTAIN)))
    systems.run_agent(env, b, systems.Settings(max_steps=12, prompt_set="plan", round_counter=True),
                      verify=False)
    assert b.calls[0]["messages"][1]["content"] == systems.task_agent(12, "plan")
