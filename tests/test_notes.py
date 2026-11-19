"""Note renderer: sections as of the encounter date, plants, purity."""
import pandas as pd

from gapcheck import notes

ENC = ["Id", "START", "STOP", "PATIENT", "ENCOUNTERCLASS", "CODE", "DESCRIPTION",
       "REASONDESCRIPTION"]


def _t(rows, cols):
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


def tables():
    p = _t([("p1", "1970-05-01", "Ann1", "Lee2", "F", "white", "nonhispanic", "M")],
           ["Id", "BIRTHDATE", "FIRST", "LAST", "GENDER", "RACE", "ETHNICITY", "MARITAL"])
    e = _t([("e1", "2024-01-10T09:00:00Z", None, "p1", "wellness", "1", "General examination of patient (procedure)", None),
            ("e2", "2025-03-01T09:00:00Z", None, "p1", "ambulatory", "2", "Encounter for problem (procedure)", "Acute bronchitis (disorder)")],
           ENC)
    c = _t([("c1", "2010-01-01T00:00:00Z", None, "e0", "Essential hypertension (disorder)"),
            ("c2", "2024-01-10T09:00:00Z", "2024-02-01T00:00:00Z", "e1", "Stress (finding)"),
            ("c3", "2025-03-01T09:00:00Z", None, "e2", "Acute bronchitis (disorder)")],
           ["row_id", "START", "STOP", "ENCOUNTER", "DESCRIPTION"])
    o = _t([("o1", "2024-01-10T09:00:00Z", "e1", "laboratory", "2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "201.3"),
            ("o2", "2024-01-10T09:00:00Z", "e1", "social-history", "72166-2", "Tobacco smoking status", "Ex-smoker (finding)"),
            ("o3", "2024-01-10T09:00:00Z", "e1", "vital-signs", "8480-6", "Systolic Blood Pressure", "131.0")],
           ["row_id", "DATE", "ENCOUNTER", "CATEGORY", "CODE", "DESCRIPTION", "VALUE"])
    m = _t([("m1", "2024-01-10T09:00:00Z", None, "e1", "lisinopril 10 MG Oral Tablet")],
           ["row_id", "START", "STOP", "ENCOUNTER", "DESCRIPTION"])
    pr = _t([], ["row_id", "START", "ENCOUNTER", "DESCRIPTION"])
    a = _t([], ["row_id", "START", "STOP", "DESCRIPTION", "CATEGORY"])
    return {"patients": p, "encounters": e, "conditions": c, "observations": o,
            "medications": m, "procedures": pr, "allergies": a}


def test_sections_as_of_encounter_date():
    n1, n2 = notes.render_notes(tables())
    assert n1.note_id == notes.note_id("e1") and n1.text.startswith("Date: 2024-01-10\n")
    assert "Ann Lee is a 53-year-old white nonhispanic woman." in n1.text
    assert "Diagnosed at this visit: Stress." in n1.text
    assert "Laboratory tests done: lipid panel." in n1.text
    assert "201.3" not in n1.text and "131" not in n1.text  # no numeric results
    assert "Tobacco smoking status: Ex-smoker." in n1.text
    # Stress stopped before e2; bronchitis is new at e2; medication still active
    assert "Stress" not in n2.text and "history of: Essential hypertension." in n2.text
    assert "Diagnosed at this visit: Acute bronchitis." in n2.text
    assert "- lisinopril 10 MG Oral Tablet" in n2.text
    assert "# Chief Complaint\nAcute bronchitis\n" in n2.text


def test_smoking_line_omitted_without_rows():
    t = tables()
    t["observations"] = t["observations"][t["observations"].CODE != "72166-2"]
    assert all("smok" not in n.text.lower() for n in notes.render_notes(t))


def test_plants_and_purity():
    t = tables()
    nid = notes.note_id("e2")
    base = notes.render_notes(t)
    for sec in ("hpi", "plan"):
        for slot in range(4):
            pl = notes.Plant(nid, sec, slot, "Patient declines statin therapy.", "x-01")
            a = notes.render_notes(t, [pl])
            assert a == notes.render_notes(t, [pl])
            assert a[0] == base[0]
            assert a[1].text.count("Patient declines statin therapy.") == 1
    pos = {notes.render_notes(t, [notes.Plant(nid, "plan", s, "XX.", "x")])[1].text.index("XX.")
           for s in range(3)}
    assert len(pos) > 1  # not a fixed position


def test_unknown_note_raises():
    import pytest
    with pytest.raises(KeyError):
        notes.render_notes(tables(), [notes.Plant("note_x", "hpi", 0, "t", "x")])
