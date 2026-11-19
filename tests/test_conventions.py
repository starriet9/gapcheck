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
    for row, names in REQUIRED_INPUTS.items():
        assert f"| {row} | {', '.join(names)} |" in t  # the verifier's table, verbatim
    # every claim name of the table is explained in plain words
    for name in {n for v in REQUIRED_INPUTS.values() for n in v}:
        assert name in t[t.index("Each name is covered like this"):t.index("| Row | Required")]
    for code in list(C.STATINS) + list(C.ASCVD_CONDITIONS) + list(C.BP_DRUGS) + [C.LDL.code]:
        assert code in t
    assert "Deferral" in t and "365 days" in t and "Closed-world" in t
    assert "1 (counseling, no statin)" in t  # row 11b's COR 1 is not a statin recommendation
    assert "/home/" not in t


def test_two_call_rule_stated():
    t = conventions.render()
    rule = t[t.index("Two-call rule"):]
    assert '"smoking" in `missing`' in rule
    assert "smoker = 0" in rule and "smoker = 1" in rule
    assert "two COMPUTED risk_10y items" in rule
    assert "below 3%, 3% to below 5%, or 5% and above" in rule
    # rows that need PREVENT, as in the verifier's table
    prevent_rows = [r for r, v in REQUIRED_INPUTS.items() if "risk_10y" in v]
    assert f"rows {', '.join(prevent_rows)}" in rule
    assert "{{" not in t and "{prevent_rows}" not in t
    assert "give either row 8 or row 9; do not use null" in " ".join(rule.split())
    flat = " ".join(t.split())
    assert "Use null only with CANNOT_DETERMINE" in flat
    assert "a definite status with benefit_group null fails this check" in flat


def test_closed_world_value_format_and_examples():
    """The closed-world value format the verifier checks strictly is stated, and the
    examples show a note source as a string, not a list."""
    flat = " ".join(conventions.render().split())
    assert ("(closed-world): the codes of the cited rows, as a list of strings; for ckd, the "
            "CKD stage number of the cited rows") in flat
    assert "one note id as a string (not a list)" in flat
    assert '"source": "<note id>", "quote":' in flat
    assert '"claim": "ckd", "support": "DOCUMENTED", "source": ["<row id>"], "value": 3' in flat
    for c in conventions.CLOSED_WORLD_CLAIMS:
        assert c in S.CLAIMS
