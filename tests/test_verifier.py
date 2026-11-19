"""Verifier checks 1-4 (SPEC 6.2) on a hand-made case."""
import ast
import copy
from pathlib import Path

import pytest

import casedb
from gapcheck import baselines, oracle, verifier
from gapcheck import schema as S
from gapcheck.systems import notes_of
from gapcheck.tools import CaseEnv


@pytest.fixture
def env():
    e = CaseEnv(casedb.connect())
    yield e
    e.close()


@pytest.fixture
def good(env):
    return baselines.r1(env.facts(), notes_of(env))


def _checks(rep):
    return {f.check for f in rep.failures}


def _idx(out, claim, support=None):
    return next(i for i, it in enumerate(out["evidence"])
                if it["claim"] == claim and (support is None or it["support"] == support))


def test_reference_answer_passes(env, good):
    assert good["status"] == oracle.DOCUMENTED_REASON and good["benefit_group"] == "9"
    rep = verifier.verify(good, env)
    assert rep.ok, rep.failures
    assert verifier.unsupported(rep)[0] == 0


def test_check1_missing_row(env, good):
    out = copy.deepcopy(good)
    i = _idx(out, "sbp")
    out["evidence"][i]["source"] = ["obs_doesnotexist"]
    rep = verifier.verify(out, env)
    assert [(f.check, f.ref) for f in rep.failures] == [(1, f"evidence[{i}]")]
    assert verifier.unsupported(rep)[0] == 1


def test_check1_value_and_kind(env, good):
    out = copy.deepcopy(good)
    i = _idx(out, "lipid_panel")
    out["evidence"][i]["value"] = {"tc": 220, "hdl": 45, "ldl": 165}
    assert _checks(verifier.verify(out, env)) == {1}
    out = copy.deepcopy(good)
    out["evidence"].append(S.item("statins", S.DOCUMENTED, ["med_lis"], None))  # not a statin
    assert 1 in _checks(verifier.verify(out, env))
    out = copy.deepcopy(good)
    out["evidence"].append(S.item("reason", S.DOCUMENTED, "note_b", "patient_declined",
                                  quote="patient declined statins"))  # not an exact substring
    assert _checks(verifier.verify(out, env)) == {1}
    out = copy.deepcopy(good)
    out["evidence"][_idx(out, "age")]["value"] = 56
    assert _checks(verifier.verify(out, env)) == {1, 3}  # PREVENT age no longer backed


def test_check2_inferred_deciding_input(env, good):
    out = copy.deepcopy(good)
    i = _idx(out, "lipid_panel")
    out["evidence"][i] = S.item("lipid_panel", S.INFERRED, None, {"tc": 220, "hdl": 45,
                                                                   "ldl": 150})
    rep = verifier.verify(out, env)
    assert 2 in _checks(rep)
    assert any(f.check == 2 and f.ref is None for f in rep.failures)
    assert "check 2" in verifier.feedback(rep)


def test_check2_requires_risk(env, good):
    out = copy.deepcopy(good)
    out["evidence"] = [it for it in out["evidence"] if it["support"] != S.COMPUTED]
    assert _checks(verifier.verify(out, env)) == {2}


def test_check3_recomputation(env, good):
    out = copy.deepcopy(good)
    i = _idx(out, "risk_10y", S.COMPUTED)
    out["evidence"][i]["value"] += 0.002
    rep = verifier.verify(out, env)
    assert [(f.check, f.ref) for f in rep.failures] == [(3, f"evidence[{i}]")]
    out = copy.deepcopy(good)
    out["evidence"][i]["value"] += 0.0005  # within 0.1 point
    assert verifier.verify(out, env).ok


def test_check3_inputs_backed(env, good):
    out = copy.deepcopy(good)
    i = _idx(out, "risk_10y", S.COMPUTED)
    from gapcheck.tools import compute_prevent
    out["evidence"][i]["inputs"]["sbp"] = 130.0
    out["evidence"][i]["value"] = compute_prevent(out["evidence"][i]["inputs"])["risk_10y"]
    assert _checks(verifier.verify(out, env)) == {3}  # SBP 130 is not the cited SBP


def test_check4_absent_and_missing(env, good):
    out = copy.deepcopy(good)
    out["evidence"][_idx(out, "bp_drugs")] = S.item("bp_drugs", S.ABSENT, "bp_drugs", None)
    rep = verifier.verify(out, env)
    assert 4 in _checks(rep)
    out = copy.deepcopy(good)
    out["missing"] = ["sbp"]
    rep = verifier.verify(out, env)
    assert [(f.check, f.ref) for f in rep.failures] == [(4, "missing[0]")]


def _no_smoking_env():
    rows = casedb.default_rows()
    rows["observations"] = [o for o in rows["observations"] if o["row_id"] != "obs_smk"]
    return CaseEnv(casedb.connect(rows))


def test_two_call_rule_for_missing_smoking():
    env = _no_smoking_env()
    f = env.facts()
    assert f.smoking is None
    base = baselines.r1(f, notes_of(env))
    from gapcheck.tools import compute_prevent
    ev = [it for it in base["evidence"] if it["claim"] not in ("smoking", "risk_10y", "risk_30y")]
    ev.append(S.item("ckd", S.ABSENT, "ckd_codes", None))
    args = {"sex": "male", "age": 55, "tc": 220.0, "hdl": 45.0, "sbp": 142.0, "bp_treated": 1,
            "statin": 0, "diabetes": 0, "smoker": 0, "egfr": 88.0}
    r0 = compute_prevent(args)["risk_10y"]
    r1 = compute_prevent({**args, "smoker": 1})["risk_10y"]
    assert verifier._band(r0) == verifier._band(r1)  # both 5 to < 10%
    one = S.item("risk_10y", S.COMPUTED, S.PREVENT_TOOL, r0, inputs=args)
    two = S.item("risk_10y", S.COMPUTED, S.PREVENT_TOOL, r1, inputs={**args, "smoker": 1})
    out = {**base, "status": oracle.DOCUMENTED_REASON, "benefit_group": "9", "risk_10y": r0,
           "missing": ["smoking"]}
    # one call with an assumed smoker = 0: input not backed, smoking not covered
    rep = verifier.verify({**out, "evidence": ev + [one]}, env)
    assert _checks(rep) == {2, 3}
    # two calls in the same band: covered
    assert verifier.verify({**out, "evidence": ev + [one, two]}, env).ok
    # an ABSENT smoking claim covers check 2, but a single assumed value still fails check 3
    absent = S.item("smoking", S.ABSENT, "smoking", None)
    assert _checks(verifier.verify({**out, "evidence": ev + [absent, one]}, env)) == {3}
    env.close()


def test_feedback_shows_only_numbers(env, good):
    out = copy.deepcopy(good)
    out["evidence"][_idx(out, "sbp")]["source"] = ["obs_doesnotexist"]
    out["missing"] = ["sbp"]
    fb = verifier.feedback(verifier.verify(out, env))
    assert fb == f"check 1 (evidence[{_idx(out, 'sbp')}]); check 4 (missing[0])"
    assert "obs_" not in fb and "sbp" not in fb


# ---------------------------------------------------------------- static table
def _expand(names):
    out = set()
    for n in names:
        out |= set(oracle.PREVENT_INPUTS) | {"risk_10y"} if n == "prevent" else {n}
    return out


def test_required_inputs_consistent_with_oracle_rows():
    rows = [r.id for r in oracle.ROWS if r.id != "X"]
    assert list(verifier.REQUIRED_INPUTS) == rows
    seen: set[str] = set()
    for r in oracle.ROWS:
        own = _expand(oracle.ROW_INPUTS[r.id])
        seen |= own
        if r.id == "X":
            continue
        req = set(verifier.REQUIRED_INPUTS[r.id])
        assert own <= req | {"hiv", "esrd"}, r.id          # the row's own inputs
        assert req - {"reason"} <= seen, r.id              # nothing from later rows
        assert ("reason" in req) == (r.label == oracle.GAP), r.id
        assert set(req) - {"reason", "risk_10y"} <= set(S.INPUT_CLAIMS)
        if r.id != "0":
            assert "statins" in req                        # row 0 comes first
    # the PREVENT rows need every PREVENT input
    for r in ("5", "8", "9", "10", "11a", "11b"):
        assert set(oracle.PREVENT_INPUTS) | {"risk_10y"} <= set(verifier.REQUIRED_INPUTS[r])


def test_required_inputs_cover_oracle_paths():
    """On hand-built patients, every row-specific input the oracle reads for its own row
    and the rows that could have matched before it is in the static list."""
    import vignette as V
    pats = [V.make(), V.make(age=45, diabetes=True), V.make(ldl=200.0, tc=280.0),
            V.make(ascvd=("MI",)), V.make(statin=True), V.make(ldl=60.0, tc=140.0),
            V.make(age=35, diabetes=True), V.make(age=78), V.make(age=50, ckd=3),
            V.make(age=40, sbp=170.0, smoker=True, bp=True)]
    for f in pats:
        r = oracle.label(f, reasons=())
        assert r.row in verifier.REQUIRED_INPUTS, r
        used = set(r.inputs_used) - {"hiv", "esrd"}
        # row 5 is evaluated (and listed) on every primary-prevention path; its features
        # only matter for diabetes aged 30-39
        if r.row != "5":
            used -= {"row5_features"}
        if r.row in ("0", "1", "2", "3", "3b", "4", "6", "7"):
            used -= set(oracle.PREVENT_INPUTS) - {"age", "lipid_panel", "statins", "diabetes"}
        if r.row in ("4", "6", "7"):
            used -= {"ckd"}
        assert used <= set(verifier.REQUIRED_INPUTS[r.row]), (r.row, used)


def test_verifier_does_not_use_oracle_or_ledger():
    src = Path(verifier.__file__).read_text()
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    for bad in ("oracle", "baselines", "cases", "edits", "strata", "label", "evaluate"):
        assert not any(m == bad or m.endswith("." + bad) for m in mods), bad
    for word in ("deltas", "ledger.csv", "cases.jsonl", "expected_status"):
        assert word not in src
