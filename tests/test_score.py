"""Scoring: correctness rule, macro-average, paired cluster bootstrap, secondary outcomes."""
import numpy as np

from gapcheck import score

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
    assert out["c3b_answer_change"]["k"] == 1
    assert out["c3a_miss"]["k"] == 0
    assert out["gap_sensitivity"]["n"] == sum(c["expected_status"] == "GAP" for c in cl)
    runs = [recs, {**recs, "2-C0": _rec("DISCUSS")}]
    assert score.stability(runs, [c["case_id"] for c in cl])["k"] == len(cl) - 1
