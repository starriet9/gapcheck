"""Conventions page: deterministic, complete."""
from gapcheck import codes as C
from gapcheck import conventions, oracle
from gapcheck import schema as S
from gapcheck.verifier import REQUIRED_INPUTS


def test_deterministic_and_hashed():
    a = conventions.render()
    conventions.render.cache_clear()
    assert conventions.render() == a
    assert len(conventions.sha256()) == 64


def test_contents():
    t = conventions.render()
    for r in oracle.ROWS:
        if r.id == "X":
            continue
        assert f"| {r.id} | {r.group} | {r.rule} | {r.label} |" in t
    for s in oracle.STATUSES:
        assert f"- {s}: " in t
    for name in S.ABSENT_LISTS:
        assert f"  - {name}: " in t
    for row in REQUIRED_INPUTS:
        assert f"- Row {row}: " in t
    for code in list(C.STATINS) + list(C.ASCVD_CONDITIONS) + list(C.BP_DRUGS) + [C.LDL.code]:
        assert code in t
    assert "Deferral" in t and "365 days" in t and "Closed-world" in t
    assert "1 (counseling, no statin)" in t  # row 11b's COR 1 is not a statin recommendation
    assert "/home/" not in t
