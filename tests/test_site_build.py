"""Text helpers of the page generator (scripts/site_build.py): the verifier's rejection
sentences, the counts shown with the results, and the alert summary line."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "scripts" / "site_build.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _f(check, kind, name, label):
    return {"check": check, "kind": kind, "name": name, "label": label}


def test_rejection_sentences():
    m = _mod()
    three = [_f(4, "missing", "lipid_panel", "Lipid panel"), _f(4, "missing", "sbp", "Systolic BP"),
             _f(4, "missing", "egfr", "eGFR")]
    assert m.failures_text(three) == ("It listed lipid panel, systolic BP and eGFR as missing. "
                                      "All three are on record.")
    assert m.failures_text(three[:1]) == "It listed lipid panel as missing. It is on record."
    assert m.failures_text([_f(4, "absent", "statins", "Statin")]) == (
        "It said statin is not on record. It is.")
    assert m.failures_text([_f(3, "claim", "risk_10y", "10-year ASCVD risk")]).startswith(
        "A risk it reported could not be reproduced")


def test_count_tile_complement_and_rate():
    m = _mod()
    changed = {"k": 16, "n": 80, "rate": 0.2, "ci": [0.15, 0.25]}
    html = m.count_tile(changed, "Kept its answer.",
                        [("Agent before the verifier", {"k": 8, "n": 80}), ("Single model call", {"k": 0, "n": 80})],
                        complement=True, extra="Forced fallbacks on these edits: 4")
    assert "64 <span class=\"of\">of</span> 80" in html
    assert "(80.0%, 95% CI 75.0 to 85.0)" in html            # k of n first, then the rate
    assert "Agent before the verifier: 72 of 80" in html and "Single model call: 80 of 80" in html
    assert html.index("Agent before") < html.index("Single model") < html.index("Forced fallbacks on these edits: 4")
    plain = m.count_tile({"k": 3, "n": 10, "rate": 0.3, "ci": [None, None]}, "x")
    assert "(30.0%)" in plain and "vs" not in plain


def test_summary_lines():
    m = _mod()
    gap1 = {"status": "GAP", "row": "1", "row_info": {"row": "1", "group": "Clinical ASCVD"},
            "risk_10y": None, "missing": []}
    assert m.summary_line(gap1) == "Clinical ASCVD, no statin on record."
    gap8 = {**gap1, "row": "8", "row_info": {"row": "8", "group": "PREVENT ≥ 10%"}, "risk_10y": 0.10295}
    assert m.summary_line(gap8) == "10-year risk 10.3%, no statin on record."
    cd = {"status": "CANNOT_DETERMINE", "row": None, "row_info": None, "risk_10y": None,
          "missing": [{"name": "lipid_panel", "lower": "lipid panel"}]}
    assert m.summary_line(cd) == "Cannot determine: lipid panel not on record."
    assert m.risk_band(0.103) == m.risk_band(0.126) == "at or above 10%"


def _res():
    bc = lambda k, n: {"k": k, "n": n, "est": k / n, "ci": [0.5, 0.9]}
    acc = {s: {"by_condition": {c: bc(k, 10) for c, k in zip(("C0", "C1", "C2a", "C3a"), ks)}}
           for s, ks in (("R0", (10, 10, 10, 0)), ("R1", (10, 10, 10, 10)),
                         ("A1", (9, 8, 9, 7)), ("A2", (9, 9, 6, 7)))}
    sec = {s: {"passes_all_checks": {"k": k, "n": 40, "rate": k / 40, "ci": [0, 1]}}
           for s, k in (("L1", 30), ("A1", 28), ("A2", 40))}
    sec["A2"]["forced_fallbacks"] = {"C0": 0, "C1": 2, "C2a": 1, "C3a": 0}
    return {"accuracy_main": {"systems": acc}, "secondary": sec,
            "weakest_A2": {"condition": "C2a", **bc(6, 10)}}


def test_rows_weakest_and_checks():
    """PROTOCOL sec. 10: A1 beside A2, A2's forced fallbacks per condition, the rule column as the
    answer key on the structured-data rows, the weakest condition with A1, the all-checks share."""
    m = _mod()
    conds = {c: {"name": n, "correct": x} for c, n, x in (
        ("C0", "clean", "Oracle label"), ("C1", "deciding fact deleted", "CANNOT_DETERMINE"),
        ("C2a", "non-deciding fact deleted", "Unchanged"), ("C3a", "reason only in a note", "DOCUMENTED_REASON"))}
    res = _res()
    t = m.rows_table(res, conds)
    rows = t.split("<tr>")[2:]
    assert [r.split("<span")[0].split(">")[-1] for r in rows] == [
        "Record as is", "One sentence added to a note", "Lipid panel deleted (or another deciding input)"]
    assert m.BY_CONSTRUCTION in rows[0] and m.BY_CONSTRUCTION in rows[2] and m.BY_CONSTRUCTION not in rows[1]
    assert "Rule on structured data: 0 of 10" in rows[1] and "Keyword rule: 10 of 10" in rows[1]
    assert "correct: the answer-key label" in rows[0]
    assert ">8 of 10 (80.0%)</td>" in rows[2] and "9 of 10 (90.0%)</strong>" in rows[2]
    assert "2 forced fallbacks" in rows[2] and "0 forced fallbacks" in rows[0]
    w = m.weakest_text(res, conds)
    assert "C2a (non-deciding fact deleted), 6 of 10 (60.0%, 95% CI 50.0 to 90.0), 1 forced fallback;" in w
    assert "before the verifier, 9 of 10 (90.0%)." in w
    c = m.checks_text(res)
    assert ("single model call 30 of 40 (75.0%), agent before the verifier 28 of 40 (70.0%) and "
            "agent with a code verifier 40 of 40 (100.0%)") in c
    assert "not that the answer is correct" in c
    assert m.checks_text({"secondary": {}}) == ""


def test_heading_hyphens_do_not_break():
    m = _mod()
    assert m.no_break_hyphens("A care-gap agent. No alert until code re-checks it.") == (
        "A care\u2011gap agent. No alert until code re\u2011checks it.")
    assert m.no_break_hyphens("2026-11-22 and A2 - A1") == "2026-11-22 and A2 - A1"


def test_public_leaves_out_unwritten_author_text():
    m = _mod()
    secs = {"next": "<!-- USER: one sentence -->", "author": "Jane Doe, MD."}
    m.PUBLIC, m.HIDDEN[:] = False, []
    assert 'class="user"' in m.user_block(secs, "next")
    m.PUBLIC = True
    try:
        assert m.user_block(secs, "next") == "" and m.HIDDEN == ["next"]
        assert m.user_block(secs, "author") == "<p>Jane Doe, MD.</p>"
    finally:
        m.PUBLIC, m.HIDDEN[:] = False, []
