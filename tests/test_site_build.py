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
    html = m.count_tile(changed, "Kept its answer.", {"k": 0, "n": 80}, "Single model call",
                        complement=True)
    assert "64 <span class=\"of\">of</span> 80" in html
    assert "(80.0%, 95% CI 75.0 to 85.0)" in html            # k of n first, then the rate
    assert "Single model call: 80 of 80" in html
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
