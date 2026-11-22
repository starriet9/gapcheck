"""Selection rule of the hand-labeling check (scripts/hand_label_cards.py, PROTOCOL sec. 3)."""

import importlib.util
from pathlib import Path

import pytest

from gapcheck import oracle

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hand_label_cards.py"
spec = importlib.util.spec_from_file_location("hand_label_cards", SCRIPT)
hl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hl)


def fake_cases(n_per_status=12, patients_per_status=8):
    out = []
    for s in oracle.STATUSES:
        for i in range(n_per_status):
            out.append({"case_id": f"case_{s}_{i}", "expected_status": s,
                        "base_patient": f"p_{s}_{i % patients_per_status}"})
    return out


def test_five_per_status_one_per_patient():
    picked = hl.select(fake_cases())
    assert len(picked) == 5 * len(oracle.STATUSES)
    assert len({c["base_patient"] for c in picked}) == len(picked)
    for s in oracle.STATUSES:
        assert sum(c["expected_status"] == s for c in picked) == 5


def test_rule_is_the_published_one():
    cases = fake_cases()
    taken, patients, want = {s: 0 for s in oracle.STATUSES}, set(), []
    for c in sorted(cases, key=lambda c: hl._h(hl.SELECT_SALT, c["case_id"])):
        if taken[c["expected_status"]] < 5 and c["base_patient"] not in patients:
            want.append(c["case_id"])
            patients.add(c["base_patient"])
            taken[c["expected_status"]] += 1
    got = hl.select(cases)
    assert sorted(c["case_id"] for c in got) == sorted(want)
    assert [c["case_id"] for c in got] == sorted(want, key=lambda i: hl._h(hl.ORDER_SALT, i))


def test_shared_patient_is_taken_once():
    cases = fake_cases()
    for c in cases:  # every status shares one patient
        if c["case_id"].endswith("_0"):
            c["base_patient"] = "shared"
    picked = hl.select(cases)
    assert sum(c["base_patient"] == "shared" for c in picked) <= 1


def test_stops_instead_of_relaxing():
    with pytest.raises(SystemExit):
        hl.select(fake_cases(n_per_status=12, patients_per_status=4))


def test_page_patients_are_excluded():
    cases = fake_cases()
    first = hl.select(cases)
    banned = {first[0]["base_patient"]}
    again = hl.select(cases, banned)
    assert banned.isdisjoint(c["base_patient"] for c in again)
    assert len(again) == 5 * len(oracle.STATUSES)


def test_page_patients_reads_case_ids(tmp_path):
    cases = fake_cases()
    pick = tmp_path / "card_pick.json"
    pick.write_text('{"tabs": [{"case_id": "case_0123456789ab"}]}')
    cases[0]["case_id"] = "case_0123456789ab"
    assert hl.page_patients(cases, pick) == {cases[0]["base_patient"]}
    assert hl.page_patients(cases, tmp_path / "missing.json") == set()
