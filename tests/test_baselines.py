"""R0 and R1 baselines (SPEC 6) and the shared output schema (SPEC 6.1)."""
from datetime import timedelta

import pytest

from gapcheck import baselines as B
from gapcheck import oracle
from gapcheck import phrases as P
from gapcheck import schema as S
from gapcheck.oracle import (CANNOT_DETERMINE, DOCUMENTED_REASON, GAP, NOT_INDICATED,
                             ON_THERAPY)
from test_oracle import LOW, P3, P5, P10, sbp_at
from vignette import INDEX, make

DEV = P.load("dev")
REASONS = [(t, p) for t in P.REASON_TYPES for p in DEV["types"][t]]
NON_REASONS = [(t, p) for t in P.DISTRACTOR_TYPES + P.SMOKING_TYPES for p in DEV["types"][t]]


def note_text(sentence: str) -> str:
    """A note in the harness layout with one planted sentence."""
    return ("2026-06-01\n\n# Chief Complaint\nNo complaints.\n\n"
            "# History of Present Illness\nPatient is a 55 year-old seen for a wellness visit. "
            "Patient has a history of essential hypertension.\n\n"
            "# Social History\nPatient has never smoked.\n\n"
            "# Allergies\nNo Known Allergies.\n\n"
            "# Medications\nlisinopril 10 MG Oral Tablet\n\n"
            f"# Assessment and Plan\n{sentence}\nFollow up in one year.\n")


# ---------------------------------------------------------------- R1 rules on dev phrases
@pytest.mark.parametrize("t,p", REASONS, ids=[p["id"] for _, p in REASONS])
def test_every_dev_reason_triggers_its_category(t, p):
    hits = B.find_reasons(p["text"])
    assert P.REASON_TYPES[t] in {h.category for h in hits}, p["text"]
    for h in hits:
        assert p["text"][h.start:h.end] == h.quote
    assert P.REASON_TYPES[t] in B.categories(note_text(p["text"]))


@pytest.mark.parametrize("t,p", NON_REASONS, ids=[p["id"] for _, p in NON_REASONS])
def test_no_dev_distractor_or_smoking_phrase_triggers(t, p):
    assert B.categories(p["text"]) == set(), p["text"]
    assert B.categories(note_text(p["text"])) == set()


def test_dev_report_is_perfect():
    rep = B.dev_report("dev")
    assert all(v["correct"] == v["n"] == 20 and not v["errors"] for v in rep.values())


def test_negation_and_family_rules_are_load_bearing(monkeypatch):
    """Without negation or family handling some dev distractors would trigger."""
    texts = [p["text"] for t in P.DISTRACTOR_TYPES for p in DEV["types"][t]]
    monkeypatch.setattr(B, "_negated", lambda before: False)
    assert sum(bool(B.categories(x)) for x in texts) >= 10
    monkeypatch.undo()
    monkeypatch.setattr(B, "FAMILY_CUE", B.re.compile(r"(?!x)x"))
    assert sum(bool(B.categories(x)) for x in texts) >= 15


def test_rule_units():
    assert B.categories("Denies myalgias on statins.") == set()
    assert B.categories("Myalgias on statins, denies weakness.") == {"statin_intolerance"}
    assert B.categories("No ascites, but hepatorenal syndrome this year.") == {"liver_failure"}
    assert B.categories("Pt declined the flu shot.") == set()  # no statin in the sentence
    assert B.categories("Mother had myalgias on simvastatin; pt had myalgias on atorvastatin.") \
        == {"statin_intolerance"}
    assert B.categories("compensated cirrhosis") == set()


# ---------------------------------------------------------------- R1 on patients
def _note(text, days, nid="note_a"):
    return {"note_id": nid, "date": (INDEX - timedelta(days=days)).isoformat(), "text": text}


def test_r1_reason_in_note_changes_gap_only():
    gap = make(ascvd=("MI",))
    s = "Offered atorvastatin, patient refused."
    out = B.r1(gap, [_note(note_text(s), 100)])
    assert out["status"] == DOCUMENTED_REASON and out["benefit_group"] == "1"
    ev = [e for e in out["evidence"] if e["claim"] == "reason"]
    assert len(ev) == 1 and ev[0]["support"] == S.DOCUMENTED
    assert ev[0]["source"] == "note_a" and ev[0]["quote"] in note_text(s)
    assert ev[0]["value"] == "patient_declined"
    assert B.r0(gap)["status"] == GAP  # R0 does not read notes
    low = make(**LOW | {"ldl": 60.0, "tc": 140.0})
    assert B.r1(low, [_note(s, 100)])["status"] == NOT_INDICATED
    assert B.r1(make(statin=True), [_note(s, 100)])["status"] == ON_THERAPY


@pytest.mark.parametrize("days,status", [(0, DOCUMENTED_REASON), (365, DOCUMENTED_REASON),
                                         (366, GAP), (-1, GAP)])
def test_r1_note_window(days, status):
    out = B.r1(make(ascvd=("MI",)), [_note("Enrolled in hospice.", days)])
    assert out["status"] == status


def test_r1_distractor_and_pregnancy_sex():
    gap = make(ascvd=("MI",))
    assert B.r1(gap, [_note("Mother had myalgias on simvastatin.", 30)])["status"] == GAP
    assert B.r1(make(ascvd=("MI",), sex="M", age=45),
                [_note("Currently pregnant, statin not started.", 30)])["status"] == GAP
    assert B.r1(make(ascvd=("MI",), sex="F", age=45),
                [_note("Currently pregnant, statin not started.", 30)])["status"] == \
        DOCUMENTED_REASON


def test_r1_no_notes_equals_r0():
    for f in vignettes():
        assert B.r1(f, []) == B.r0(f)


# ---------------------------------------------------------------- R0 equals the oracle
def vignettes():
    s5, s10, s3 = sbp_at(0.05, **P5), sbp_at(0.10, **P10), sbp_at(0.03, **P3)
    alg = [("Statin intolerance", INDEX - timedelta(days=100))]
    return [
        make(statin=True), make(statin=True, lipid=False, ascvd=("MI",)),
        make(ascvd=("MI",)), make(ascvd=("STROKE",), lipid=False, sbp=None, smoker=None),
        make(age=79, ascvd=("IHD",), ldl=50.0, tc=140.0), make(ldl=200.0, tc=290.0),
        make(age=78, ldl=190.0, tc=280.0), make(age=50, diabetes=True),
        make(age=50, diabetes=True, lipid=False), make(age=50, ckd=3), make(age=50, ckd=4),
        make(age=77, diabetes=True), make(age=77),
        make(**LOW | {"age": 35}, diabetes=True, neuropathy=True),
        make(**LOW | {"age": 35, "ldl": 100.0}, diabetes=True, dm_years=11),
        make(ldl=60.0, tc=150.0), make(**P10, sbp=s10 + 1), make(**P10, sbp=s10 - 1),
        make(**P5, sbp=s5 + 1), make(**P5, sbp=s5 - 1), make(**P3, sbp=s3 + 1),
        make(**P3, sbp=s3 - 1), make(**LOW | {"ldl": 170.0, "tc": 260.0}), make(**LOW),
        make(**P5, sbp=150.0, lipid=False), make(**P5, sbp=150.0, smoker=None),
        make(**P5, sbp=158.0, smoker=None, egfr=None), make(**P5, sbp=None),
        make(**P5, sbp=85.0), make(**P5 | {"tc": 125.0, "hdl": 40.0, "ldl": 75.0}, sbp=150.0),
        make(**P5, sbp=150.0, smoking_value="Unknown if ever smoked"),
        make(**P5, sbp=150.0, smoker=True, bp=True),
        make(ascvd=("MI",), allergies=alg), make(**P5, sbp=s5 + 1, allergies=alg),
        make(**LOW | {"ldl": 60.0, "tc": 140.0}, allergies=alg),
        make(ascvd=("MI",), allergies=[("Aspirin", INDEX - timedelta(days=100))]),
    ]


@pytest.mark.parametrize("variant", oracle.VARIANTS)
def test_r0_equals_oracle_on_vignettes(variant):
    statuses = set()
    for f in vignettes():
        r = oracle.label(f, variant=variant)
        out = B.r0(f, variant)
        S.validate(out)
        statuses.add(out["status"])
        assert out["status"] == r.status
        assert out["benefit_group"] == (None if r.status == CANNOT_DETERMINE else r.row)
        assert out["risk_10y"] == r.risk10
        assert out["missing"] == list(r.absent)
        if r.status == CANNOT_DETERMINE:
            assert set(r.missing) <= set(out["missing"])
        if r.status == DOCUMENTED_REASON:
            ev = [e for e in out["evidence"] if e["claim"] == "reason"]
            assert ev and ev[0]["source"] == [r.reason.source]
    if variant == "main":
        assert statuses == set(oracle.STATUSES)


def test_r0_evidence_cites_record_rows():
    s5 = sbp_at(0.05, **P5)
    f = make(**P5, sbp=s5 + 1)
    out = B.r0(f)
    by = {e["claim"]: e for e in out["evidence"]}
    assert by["lipid_panel"]["support"] == S.DOCUMENTED
    assert by["lipid_panel"]["source"] == list(f.lipid_panel.row_ids)
    assert by["sbp"]["source"] == [f.sbp.row_id] and by["sbp"]["value"] == f.sbp.value
    assert by["statins"] == S.item("statins", S.ABSENT, "statins", None)
    assert by["ascvd"]["source"] == "ascvd_codes"
    assert by["reason"]["support"] == S.ABSENT
    risk = by["risk_10y"]
    assert risk["support"] == S.COMPUTED and risk["value"] == out["risk_10y"]
    assert oracle.prevent.ascvd_risk(**risk["inputs"], horizon=10) == pytest.approx(risk["value"])


def test_r0_cannot_determine_names_missing_input():
    out = B.r0(make(**P5, sbp=150.0, lipid=False))
    assert out["status"] == CANNOT_DETERMINE and out["benefit_group"] is None
    assert "lipid_panel" in out["missing"]
    assert any(e["claim"] == "lipid_panel" and e["support"] == S.ABSENT for e in out["evidence"])


# ---------------------------------------------------------------- schema
def test_schema_rejects_bad_outputs():
    good = B.r0(make(ascvd=("MI",)))
    for k, v in (("status", "MAYBE"), ("benefit_group", "12"), ("risk_10y", 5.0),
                 ("missing", ["bmi"]), ("evidence", {})):
        with pytest.raises(ValueError):
            S.validate(good | {k: v})
    with pytest.raises(ValueError):
        S.validate({k: v for k, v in good.items() if k != "missing"})
    with pytest.raises(ValueError):
        S.item("reason", S.DOCUMENTED, "note_1", "patient_declined")  # note source needs a quote
    with pytest.raises(ValueError):
        S.item("ascvd", S.ABSENT, "everything", None)
    with pytest.raises(ValueError):
        S.item("risk_10y", S.COMPUTED, "prevent_risk", 0.1)  # no inputs
    with pytest.raises(ValueError):
        S.item("bmi", S.INFERRED, None, 30)
