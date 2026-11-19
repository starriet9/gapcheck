"""Failure-case and example-record selection (PROTOCOL.md sec. 10) on mock outputs."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COND = ["C0", "C1", "C2a", "C2b", "C3a", "C3b", "C4", "C5"]


def _mod():
    spec = importlib.util.spec_from_file_location("select_failures",
                                                  ROOT / "scripts" / "select_failures.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SF = _mod()


def _cases(n_pat=6):
    out = []
    for p in range(n_pat):
        for k in COND:
            exp = {"C1": "CANNOT_DETERMINE", "C3a": "DOCUMENTED_REASON"}.get(k, "GAP")
            var = ("lipid_panel" if p % 2 == 0 else "sbp") if k == "C1" else "x"
            out.append({"case_id": f"case_{p:x}{COND.index(k):x}{'0' * 10}", "base_patient": f"p{p}",
                        "stratum": "S4", "base_statin": False, "condition": k, "variant": var,
                        "expected_status": exp, "expected_missing": []})
    return out


def _right(c):
    miss = [c["variant"]] if c["condition"] == "C1" else []
    return {"schema_valid": True, "output": {"status": c["expected_status"], "benefit_group": "9",
                                             "missing": miss, "evidence": []}}


def _wrong():
    return {"schema_valid": True, "output": {"status": "DISCUSS", "benefit_group": "6",
                                             "missing": [], "evidence": []}}


def _run(cl, wrong: dict[str, set]):
    return {s: {c["case_id"]: (_wrong() if c["case_id"] in wrong.get(s, set()) else _right(c))
                for c in cl} for s in ("R1", "L1", "A1", "A2")}


def _id(cl, p, k):
    return next(c["case_id"] for c in cl if c["base_patient"] == f"p{p}" and c["condition"] == k)


def test_three_lowest_a2_conditions_smallest_id():
    cl = _cases()
    # A2 accuracy: C4 2/6 wrong (lowest), C1 and C5 1/6 wrong (tie: C1 first), C0 1/6 too
    w = {_id(cl, 4, "C4"), _id(cl, 2, "C4"), _id(cl, 5, "C1"), _id(cl, 3, "C5"),
         _id(cl, 1, "C0")}
    out = SF.select(cl, _run(cl, {"A2": w}))
    f = out["failure_cases"]
    assert [x["condition"] for x in f] == ["C4", "C0", "C1"]   # ties in order C0, C1, ..., C5
    assert f[0]["case_id"] == _id(cl, 2, "C4")                  # smaller id of the two
    assert all(x["system"] == "A2" for x in f) and not out["fewer_than_three"]
    assert f[0]["condition_accuracy"] == {"k": 4, "n": 6, "est": 4 / 6}
    assert f[0]["answer"]["status"] == "DISCUSS"


def test_fill_from_a1_then_l1_without_repeating_a_case():
    cl = _cases()
    shared = _id(cl, 0, "C2b")
    run = _run(cl, {"A2": {shared}, "A1": {shared}, "L1": {_id(cl, 3, "C3a"), _id(cl, 1, "C3a")}})
    f = SF.select(cl, run)["failure_cases"]
    assert [(x["system"], x["condition"]) for x in f] == [("A2", "C2b"), ("L1", "C3a")]
    assert f[1]["case_id"] == _id(cl, 1, "C3a")
    out = SF.select(cl, run)
    assert out["fewer_than_three"] and out["n_failure_cases"] == 2
    run["A1"][_id(cl, 4, "C2b")] = _wrong()       # A1: next smallest in the same condition
    f = SF.select(cl, run)["failure_cases"]
    assert [(x["system"], x["condition"], x["case_id"]) for x in f] == [
        ("A2", "C2b", shared), ("A1", "C2b", _id(cl, 4, "C2b")), ("L1", "C3a", _id(cl, 1, "C3a"))]


def test_missing_or_invalid_output_is_a_failure():
    cl = _cases()
    run = _run(cl, {})
    del run["A2"][_id(cl, 2, "C5")]
    run["A2"][_id(cl, 1, "C3b")] = {"schema_valid": False, "output": None, "error": "parse"}
    f = SF.select(cl, run)["failure_cases"]
    assert [x["condition"] for x in f] == ["C3b", "C5"]
    assert f[0]["answer"] == {"record": True, "schema_valid": False, "status": None,
                              "benefit_group": None, "missing": None, "error": "parse",
                              "forced_fallback": False}
    assert f[1]["answer"]["record"] is False


def test_no_failures_says_so():
    cl = _cases()
    out = SF.select(cl, _run(cl, {}))
    assert out["failure_cases"] == [] and out["fewer_than_three"]


def test_example_record_is_smallest_lipid_c1_whatever_the_answer():
    cl = _cases()
    run = _run(cl, {"A2": {_id(cl, 0, "C1")}})
    ex = SF.select(cl, run)["example_record"]
    lipid = sorted(c["case_id"] for c in cl if c["condition"] == "C1"
                   and c["variant"] == "lipid_panel")
    assert ex["case_id"] == lipid[0] == _id(cl, 0, "C1")
    assert ex["base_case_id"] == _id(cl, 0, "C0") and ex["base_expected_status"] == "GAP"
    assert ex["answers"]["A2"]["status"] == "DISCUSS" and ex["answers"]["L1"]["status"] == \
        "CANNOT_DETERMINE"
    assert SF.example_case([c for c in cl if c["variant"] != "lipid_panel"]) is None


def test_deterministic_and_order_free():
    cl = _cases()
    run = _run(cl, {"A2": {_id(cl, 4, "C4"), _id(cl, 2, "C4"), _id(cl, 5, "C1")}})
    a = json.dumps(SF.select(cl, run), sort_keys=True)
    b = json.dumps(SF.select(list(reversed(cl)), run), sort_keys=True)
    assert a == b


def test_cli_on_a_run_directory(tmp_path, monkeypatch):
    cl = _cases()
    run = _run(cl, {"A2": {_id(cl, 3, "C4")}})
    rd = tmp_path / "run"
    rd.mkdir()
    for s, recs in run.items():
        (rd / f"{s}.jsonl").write_text("".join(json.dumps({"case_id": k, **v}) + "\n"
                                               for k, v in recs.items()))
    monkeypatch.setattr(SF.score, "scored_cases", lambda c, r: cl)
    monkeypatch.setattr("sys.argv", ["select_failures.py", "--cases", "x", "--run", str(rd)])
    SF.main()
    out = json.loads((rd / "failures.json").read_text())
    assert out["failure_cases"][0]["case_id"] == _id(cl, 3, "C4")
    assert out["example_record"]["case_id"] == _id(cl, 0, "C1")
