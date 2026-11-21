"""Scoring: correctness rule, macro-average, paired cluster bootstrap, secondary outcomes,
hypotheses, the page's numbers, verifier first-round rejections and cost per case."""
from pathlib import Path

import numpy as np
import pytest

from gapcheck import score

ROOT = Path(__file__).resolve().parents[1]

COND = ["C0", "C1", "C2a", "C2b", "C3a", "C3b", "C4", "C5"]


def _cases():
    out = []
    for p in range(8):
        st = "S4" if p < 4 else "S5"
        for k in COND:
            exp = {"C1": "CANNOT_DETERMINE", "C3a": "DOCUMENTED_REASON"}.get(k, "GAP")
            out.append({"case_id": f"{p}-{k}", "base_patient": f"p{p}", "stratum": st,
                        "base_statin": False, "condition": k,
                        "variant": "lipid_panel" if k == "C1" else "x",
                        "expected_status": exp, "expected_status_S-A": exp,
                        "expected_status_S-C": exp, "partner_case_id": None})
    return out


def _rec(status, missing=(), bg="9"):
    return {"schema_valid": True, "output": {"status": status, "benefit_group": bg,
                                             "missing": list(missing), "evidence": []}}


def test_correct_rule():
    c = {"condition": "C1", "variant": "sbp", "expected_status": "CANNOT_DETERMINE"}
    assert not score.correct(c, _rec("CANNOT_DETERMINE", ["lipid_panel"]))
    assert score.correct(c, _rec("CANNOT_DETERMINE", ["sbp"]))
    assert not score.correct(c, None)
    assert not score.correct(c, {"schema_valid": False, "output": None})
    g = {"condition": "C0", "variant": "clean", "expected_status": "GAP",
         "expected_status_S-A": "DISCUSS"}
    assert score.correct(g, _rec("GAP", bg="9"))
    assert score.correct(g, _rec("GAP", bg="8"), "S-A")      # relabelled by its own row
    assert not score.correct(g, _rec("GAP", bg="9"), "S-A")


def test_macro_and_bootstrap():
    cl = _cases()
    d = score.Design(cl)
    perfect = np.ones(len(cl), dtype=bool)
    half = np.array([c["base_patient"] in ("p0", "p1", "p4", "p5") for c in cl])
    t = score.accuracy_table(d, {"A2": perfect, "L1": half, "A1": perfect}, n_boot=500, seed=1)
    assert t["systems"]["A2"]["macro"]["est"] == 1.0
    assert t["systems"]["L1"]["macro"]["est"] == 0.5
    lo, hi = t["systems"]["L1"]["macro"]["ci"]
    assert lo < 0.5 < hi
    assert t["differences"]["A2-L1"]["est"] == 0.5
    assert t["differences"]["A2-A1"] == {"est": 0.0, "ci": [0.0, 0.0]}
    t2 = score.accuracy_table(d, {"A2": perfect, "L1": half}, n_boot=500, seed=1)
    assert t2["systems"]["L1"]["macro"]["ci"] == t["systems"]["L1"]["macro"]["ci"]  # seeded


def test_weights_stay_within_strata():
    cl = _cases()
    d = score.Design(cl)
    W = d.weights(200, 3)
    s4 = [d.cl_index[f"p{i}"] for i in range(4)]
    assert np.all(W[:, s4].sum(axis=1) == 4) and np.all(W.sum(axis=1) == 8)


def test_secondary():
    cl = _cases()
    d = score.Design(cl)
    recs = {c["case_id"]: _rec(c["expected_status"], ["lipid_panel"] if c["condition"] == "C1"
                               else []) for c in cl}
    recs["0-C1"] = _rec("GAP")
    recs["0-C2a"] = _rec("CANNOT_DETERMINE")
    recs["1-C3b"] = _rec("NOT_INDICATED")
    out = score.secondary(d, recs, None, d.weights(100, 2))
    assert out["c1_fabricated_certainty"]["k"] == 1 and out["c1_fabricated_certainty"]["n"] == 8
    assert out["c2a_over_abstention"]["k"] == 1
    assert out["answer_change"]["C3b"]["k"] == 1
    assert out["answer_change"]["C2a"]["k"] == 1      # CANNOT_DETERMINE against its own GAP
    assert out["answer_change"]["pooled"] == {**out["answer_change"]["pooled"], "k": 2, "n": 32}
    assert out["c3a_miss"]["k"] == 0
    assert out["gap_sensitivity"]["n"] == sum(c["expected_status"] == "GAP" for c in cl)
    runs = [recs, {**recs, "2-C0": _rec("DISCUSS")}]
    assert score.stability(runs, [c["case_id"] for c in cl])["k"] == len(cl) - 1
    st = score.stability(runs, [c["case_id"] for c in cl], d, d.weights(100, 2))
    assert st["k"] == len(cl) - 1 and st["n"] == len(cl) and st["ci"][1] == 1.0


def test_unsupported_claims_interval():
    cl = _cases()
    d = score.Design(cl)
    recs = {c["case_id"]: {**_rec(c["expected_status"]), "audit": {"unsupported": [0, 4]}}
            for c in cl}
    recs["0-C0"]["audit"] = {"unsupported": [2, 4]}
    u = score.secondary(d, recs, None, d.weights(200, 2))["unsupported_claims"]
    assert u["k"] == 2 and u["n"] == 4 * len(cl)
    assert u["ci"][0] == 0.0 and 0 < u["ci"][1] < 0.05


def test_passes_all_checks_share():
    """Beside outcome 6: final answers whose audit passes all four checks; a case without
    an answer (no audit) is not in the denominator."""
    cl = _cases()
    d = score.Design(cl)
    recs = {c["case_id"]: {**_rec(c["expected_status"]), "audit": {"ok": True, "unsupported": [0, 4]}}
            for c in cl}
    recs["0-C0"]["audit"] = {"ok": False, "unsupported": [0, 4]}
    recs["1-C0"] = {"schema_valid": False, "output": None, "audit": None}
    a = score.secondary(d, recs, None, d.weights(200, 2))["passes_all_checks"]
    assert a["n"] == len(cl) - 1 and a["k"] == len(cl) - 2
    assert a["ci"][0] < a["rate"] <= a["ci"][1] <= 1.0


# ---------------------------------------------------------------- answer change
def test_answer_change_counts_invalid_or_missing_as_changed():
    ok, bad = _rec("GAP"), {"schema_valid": False, "output": None}
    assert not score.answer_changed(ok, _rec("GAP"))
    assert score.answer_changed(_rec("DISCUSS"), ok)
    assert score.answer_changed(bad, bad)            # two invalid outputs: changed
    assert score.answer_changed(bad, ok) and score.answer_changed(ok, bad)
    assert score.answer_changed(None, ok) and score.answer_changed(ok, None)
    assert score.answer_changed(None, None)


def test_pooled_answer_change_in_secondary():
    cl = _cases()
    d = score.Design(cl)
    recs = {c["case_id"]: _rec(c["expected_status"], ["lipid_panel"] if c["condition"] == "C1"
                               else []) for c in cl}
    bad = {"schema_valid": False, "output": None}
    recs["2-C0"] = bad          # patient 2: every must-not-change case now counts as changed
    recs["2-C2b"] = bad         # ... including the one that is invalid too
    del recs["3-C4"]            # a missing output is a change
    recs["5-C3a"] = bad         # C3a is not a must-not-change condition
    ac = score.secondary(d, recs, None, d.weights(200, 4))["answer_change"]
    assert {k: ac[k]["k"] for k in score.MUST_NOT_CHANGE} == {"C2a": 1, "C2b": 1, "C3b": 1,
                                                               "C4": 2}
    assert ac["pooled"]["k"] == 5 and ac["pooled"]["n"] == 32
    assert ac["pooled"]["rate"] == 5 / 32
    lo, hi = ac["pooled"]["ci"]
    assert 0 <= lo <= 5 / 32 <= hi <= 1


# ---------------------------------------------------------------- bootstrap
def _design_random(n_per=(3, 4, 2), seed=0):
    """Clusters in three strata, each with a random subset of conditions."""
    rng = np.random.default_rng(seed)
    out = []
    for si, n in enumerate(n_per):
        for p in range(n):
            for k in COND:
                if k != "C0" and rng.random() < 0.4:
                    continue
                out.append({"case_id": f"s{si}p{p}-{k}", "base_patient": f"s{si}p{p}",
                            "stratum": f"S{si + 1}", "base_statin": False, "condition": k,
                            "variant": "x", "expected_status": "GAP"})
    return out


def _explicit_macro(cases, ok, mult):
    """Macro accuracy of a resample given cluster multiplicities, in plain Python."""
    num, den = {}, {}
    for c, o in zip(cases, ok):
        m = mult.get(c["base_patient"], 0)
        num[c["condition"]] = num.get(c["condition"], 0) + m * o
        den[c["condition"]] = den.get(c["condition"], 0) + m
    accs = [num[k] / den[k] for k in COND if den.get(k)]
    return sum(accs) / len(accs)


def test_bootstrap_matches_explicit_resampling():
    """The vectorized paired bootstrap equals resampling the case list cluster by
    cluster and recomputing the macro-average from scratch."""
    cl = _design_random()
    d = score.Design(cl)
    rng = np.random.default_rng(7)
    ok_a = rng.random(len(cl)) < 0.8
    ok_b = rng.random(len(cl)) < 0.5
    W = d.weights(300, 11)
    inv = {i: p for p, i in d.cl_index.items()}
    diffs = []
    for r in range(W.shape[0]):
        mult = {inv[i]: W[r, i] for i in range(W.shape[1])}
        # within stratum, each cell keeps its own size
        for key, idx in d.strata.items():
            assert sum(mult[inv[i]] for i in idx) == len(idx)
        diffs.append(_explicit_macro(cl, ok_a, mult) - _explicit_macro(cl, ok_b, mult))
    t = score.accuracy_table(d, {"A2": ok_a, "L1": ok_b}, n_boot=300, seed=11)
    lo, hi = t["differences"]["A2-L1"]["ci"]
    assert lo == pytest.approx(np.percentile(diffs, 2.5))
    assert hi == pytest.approx(np.percentile(diffs, 97.5))
    full = {p: 1 for p in d.cl_index}
    assert t["differences"]["A2-L1"]["est"] == pytest.approx(
        _explicit_macro(cl, ok_a, full) - _explicit_macro(cl, ok_b, full))


def test_bootstrap_width_matches_binomial_scale():
    """One stratum, 400 one-case clusters, half correct: the 95% interval is about
    +-1.96 * sqrt(.25 / 400) = +-4.9 points."""
    cl = [{"case_id": f"c{i}", "base_patient": f"p{i}", "stratum": "S5", "base_statin": False,
           "condition": "C0", "variant": "clean", "expected_status": "GAP"} for i in range(400)]
    d = score.Design(cl)
    ok = np.arange(400) % 2 == 0
    t = score.accuracy_table(d, {"L1": ok}, n_boot=4000, seed=5)
    lo, hi = t["systems"]["L1"]["macro"]["ci"]
    assert 0.04 < (hi - lo) / 2 < 0.058
    assert t["systems"]["L1"]["by_condition"]["C0"]["k"] == 200


def test_paired_rate_difference():
    cl = _cases()
    d = score.Design(cl)
    W = d.weights(300, 9)
    mask = np.array([c["condition"] == "C1" for c in cl])
    a = np.zeros(len(cl), dtype=bool)
    b = mask.copy()                     # b: definite on every C1 case
    r = score._boot_diff_prop(d, mask, a, b, W)
    assert r["n"] == 8 and r["est"] == -1.0 and r["ci"] == [-1.0, -1.0]
    same = score._boot_diff_prop(d, mask, b, b, W)
    assert same["est"] == 0.0 and same["ci"] == [0.0, 0.0]
    assert score._boot_diff_prop(d, np.zeros(len(cl), bool), a, b, W)["est"] is None


# ---------------------------------------------------------------- hypotheses
def test_verdicts():
    assert score.verdict([0.01, 0.2]) == "supported"
    assert score.verdict([-0.2, -0.01]) == "contradicted"
    assert score.verdict([0.0, 0.2]) == "unresolved"            # touches 0: includes it
    assert score.verdict([-0.2, -0.01], direction=-1) == "supported"
    assert score.verdict([0.01, 0.2], direction=-1) == "contradicted"
    assert score.verdict([None, None]) == "unresolved"
    assert score.margin_verdict([[-0.1, 0.05], [0.0, 0.03]]) == "met"   # at most 5 points
    assert score.margin_verdict([[-0.1, 0.05], [0.051, 0.2]]) == "not met"
    assert score.margin_verdict([[-0.1, 0.06], [0.0, 0.03]]) == "unresolved"
    assert score.h3_verdict("supported", "met") == "supported"
    assert score.h3_verdict("supported", "unresolved") == "unresolved"
    assert score.h3_verdict("unresolved", "met") == "unresolved"
    assert score.h3_verdict("contradicted", "met") == "contradicted"
    assert score.h3_verdict("supported", "not met") == "contradicted"


def _run(cl, wrong=None, abstain_c2=None, definite_c1=None):
    """Records that are right except: `wrong` {system: set of conditions answered GAP-wrong},
    `abstain_c2` {system: patients abstaining on C2a}, `definite_c1` {system: patients
    answering GAP on C1}."""
    run = {}
    for s in ("R1", "L1", "A1", "A2"):
        recs = {}
        for c in cl:
            p = int(c["base_patient"][1:])
            st = c["expected_status"]
            miss = ["lipid_panel"] if c["condition"] == "C1" else []
            if c["condition"] in (wrong or {}).get(s, ()):
                st = "DISCUSS"
            if c["condition"] == "C2a" and p in (abstain_c2 or {}).get(s, ()):
                st = "CANNOT_DETERMINE"
            if c["condition"] == "C1" and p in (definite_c1 or {}).get(s, ()):
                st = "GAP"
            recs[c["case_id"]] = _rec(st, miss)
        run[s] = recs
    return run


def test_hypotheses_and_headline():
    cl = _cases()
    d = score.Design(cl)
    run = _run(cl, wrong={"L1": {"C3a", "C4", "C5"}, "R1": {"C3a"}},
               abstain_c2={"A2": {0}}, definite_c1={"A1": set(range(8)), "L1": {1, 2}})
    s = score.score_design(d, run, n_boot=400, seed=3)
    h = s["hypotheses"]
    # A2: 63/64 (one C2a abstention); L1: 0 on C3a, C4, C5 and 6/8 on C1, so 38/64
    assert h["H1"]["est"] == pytest.approx(25 / 64) and h["H1"]["verdict"] == "supported"
    assert h["H2"]["est"] == pytest.approx(1 / 3) and h["H2"]["conditions"] == ["C3a", "C3b", "C4"]
    assert h["H2"]["A2"]["est"] == 1.0 and h["H2"]["R1"]["est"] == pytest.approx(2 / 3)
    assert h["H3"]["part1"]["est"] == -1.0 and h["H3"]["part1"]["verdict"] == "supported"
    assert h["H3"]["part2"]["C2a"]["est"] == pytest.approx(1 / 8)
    assert h["H3"]["part2"]["verdict"] in ("unresolved", "not met")
    hd = s["headline"]
    assert hd["primary"]["verdict"] == "supported"
    assert hd["primary"]["difference"]["est"] == pytest.approx(25 / 64)
    assert hd["answered_anyway"]["A1"]["k"] == 8 and hd["answered_anyway"]["L1"]["k"] == 2
    assert hd["answered_anyway"]["A2"] == {"k": 0, "n": 8, "rate": 0.0, "ci": [0.0, 0.0]}
    # C1 correct: CANNOT_DETERMINE with the deleted input named (the complement here)
    assert hd["c1_correct"]["A2"]["k"] == 8 and hd["c1_correct"]["A2"]["n"] == 8
    assert hd["c1_correct"]["L1"]["k"] == 6 and hd["c1_correct"]["A1"]["k"] == 0
    assert hd["c1_correct"]["L1"]["rate"] == pytest.approx(6 / 8)
    assert set(hd["c1_correct"]) == {"L1", "A1", "A2"}       # R1 is not a page system
    # L1 got C4 wrong against a right C0: every C4 case changed; A2 abstained on one C2a
    assert hd["changed_when_must_not"]["L1"]["k"] == 8
    assert hd["changed_when_must_not"]["A2"]["k"] == 1
    assert hd["changed_when_must_not"]["A2"]["n"] == 32
    assert s["weakest_A2"]["condition"] == "C2a" and s["weakest_A2"]["k"] == 7
    assert s["n_patients"] == 8
    assert s["verifier_A2"] is None                 # these records carry no verifier report
    r = score.results({**s, "schema": score.RESULTS_SCHEMA, "case_set": "x", "run": "y",
                       "n_cases": len(cl)})
    assert list(r) == list(score.RESULT_KEYS)
    assert r["cost"] is None and r["n_patients"] == 8
    assert "sentences" not in r


def test_ceiling_and_invalid_flags():
    cl = _cases()
    d = score.Design(cl)
    run = _run(cl)
    bad = {"schema_valid": False, "output": None}
    for c in cl[:3]:                    # 3 of 64 = 4.7%: under the 5% flag
        run["L1"][c["case_id"]] = bad
    s = score.score_design(d, run, n_boot=200, seed=1)
    assert s["flags"]["ceiling"]
    assert not s["flags"]["no_valid_output"]["L1"]["over_threshold"]
    run["L1"][cl[3]["case_id"]] = bad   # 4 of 64 = 6.25%
    del run["L1"][cl[4]["case_id"]]     # a missing output is no valid answer either
    s = score.score_design(d, run, n_boot=200, seed=1)
    assert s["flags"]["no_valid_output"]["L1"]["k"] == 5
    assert s["flags"]["no_valid_output"]["L1"]["over_threshold"]
    assert s["headline"]["primary"]["verdict"] in ("unresolved", "supported")
    run2 = _run(cl)
    s2 = score.score_design(d, run2, n_boot=200, seed=1)
    assert s2["headline"]["primary"]["verdict"] == "unresolved"


def test_partial_run_has_no_headline_values():
    cl = _cases()
    d = score.Design(cl)
    run = {k: v for k, v in _run(cl).items() if k in ("A1", "A2")}
    s = score.score_design(d, run, n_boot=50, seed=1)
    assert s["headline"]["primary"] is None and s["hypotheses"]["H1"] is None
    assert s["hypotheses"]["H2"] is None and s["hypotheses"]["H3"] is not None
    assert set(s["headline"]["answered_anyway"]) == {"A1", "A2"}
    assert set(s["headline"]["c1_correct"]) == {"A1", "A2"}


def test_protocol_has_no_fixed_page_rules():
    """PROTOCOL sec. 10 keeps the primary-outcome and labelling rules; the conditions the
    scoring code relies on are named in the protocol."""
    text = (ROOT / "PROTOCOL.md").read_text()
    assert "is reported wherever results are reported, whatever its value" in text
    assert "chosen by the author and labelled as such" in text
    for gone in ("Fixed sentences", "Headline numbers, chosen now", "Example record on the page"):
        assert gone not in text
    for k in ("MUST_NOT_CHANGE", "NOTE_CONDITIONS"):
        assert all(c in text for c in getattr(score, k))


# ---------------------------------------------------------------- verifier and cost
def _vrec(status, rounds, forced=False):
    """An A2 record whose verifier reports fail the listed checks round by round."""
    v = [{"ok": not fails, "failures": [{"check": c, "ref": f"evidence[{i}]", "detail": "x"}
                                        for i, c in enumerate(fails)], "claims": []}
         for fails in rounds]
    return {**_rec(status), "verifier": v, "forced_fallback": forced}


def test_verifier_first_round_rejections_by_check():
    cl = _cases()
    d = score.Design(cl)
    W = d.weights(300, 5)
    recs = {c["case_id"]: _vrec(c["expected_status"], [[]]) for c in cl}   # all pass
    recs["0-C0"] = _vrec("GAP", [[4, 4, 4], []])            # rejected, repaired
    recs["1-C0"] = _vrec("GAP", [[1, 3], []])               # two checks in one draft
    recs["2-C0"] = _vrec("CANNOT_DETERMINE", [[3], [3]], forced=True)
    recs["3-C0"] = _vrec("CANNOT_DETERMINE", [[2]], forced=True)  # repair gave no valid answer
    recs["4-C0"] = {**_rec("GAP"), "verifier": [], "forced_fallback": False}  # no first answer
    del recs["5-C0"]                                        # no record
    v = score.secondary(d, recs, None, W)["verifier_first_round"]
    assert v["checked"] == len(cl) - 2 and v["not_checked"] == 2
    assert v["rejected"]["k"] == 4 and v["rejected"]["n"] == len(cl) - 2
    assert v["rejected"]["rate"] == pytest.approx(4 / (len(cl) - 2))
    lo, hi = v["rejected"]["ci"]
    assert 0 <= lo <= v["rejected"]["rate"] <= hi <= 1
    assert v["by_check"] == {"1": 1, "2": 1, "3": 2, "4": 1}
    assert v["failures_by_check"] == {"1": 1, "2": 1, "3": 2, "4": 3}
    assert v["repaired"] == 2 and v["forced"] == 2
    # systems without a verifier report get no entry; the score copies A2's to the top level
    plain = {c["case_id"]: _rec(c["expected_status"]) for c in cl}
    assert "verifier_first_round" not in score.secondary(d, plain, None, W)
    s = score.score_design(d, {"A2": recs, "A1": plain}, n_boot=100, seed=2)
    assert s["verifier_A2"]["rejected"]["k"] == 4
    assert score.results(s)["verifier_A2"]["by_check"]["3"] == 2


def test_cost_per_case():
    c = score.cost_per_case(12.5, 1.89, 805)
    assert c["usd_per_case"] == pytest.approx(12.5 * 1.89 / 805)
    assert c["gpu_seconds_per_case"] == pytest.approx(12.5 * 3600 / 805)
    assert c["n_cases"] == 805 and c["gpu_hours"] == 12.5
    with pytest.raises(ValueError):
        score.cost_per_case(1.0, 2.0, 0)
    with pytest.raises(ValueError):
        score.cost_per_case(-1.0, 2.0, 10)


def test_rank_conditions_ties_in_protocol_order():
    bc = {k: {"est": 1.0} for k in COND}
    bc.update({"C4": {"est": 0.5}, "C1": {"est": 0.5}, "C2b": {"est": None}, "C0": {"est": 0.9}})
    assert score.rank_conditions(bc)[:3] == ["C1", "C4", "C0"]
    assert "C2b" not in score.rank_conditions(bc)


# ---------------------------------------------------------------- parser failures
def test_parser_failures_from_trace():
    rec = {"trace": [
        {"type": "model", "text": "", "tool_calls": [], "stop_reason": "tool_calls"},
        {"type": "model", "text": "<tool_call>{bad", "tool_calls": [], "stop_reason": "stop"},
        {"type": "model", "text": "", "stop_reason": "tool_calls", "tool_calls": [
            {"name": "read_note", "arguments": {}},
            {"name": "get_demographics", "arguments": {}},     # no required parameters
            {"name": "get_observations", "arguments": {"_unparsed": "{code:"}},
            {"name": "get_observations", "arguments": {"code": "2093-3"}}]},
        {"type": "tool", "name": "read_note", "args": {}, "result": {}},
        {"type": "model", "text": "{}", "tool_calls": [], "stop_reason": "stop"}]}
    assert score.parser_failures(rec) == {"signal_without_call": 2,
                                          "empty_required_arguments": 1,
                                          "unparsed_arguments": 1}
    assert score.parser_failures(None) == {"signal_without_call": 0,
                                           "empty_required_arguments": 0,
                                           "unparsed_arguments": 0}
