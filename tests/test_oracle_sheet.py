"""reports/oracle_rules.md is generated from the oracle's tables and must be current."""
import importlib.util
import json
from pathlib import Path

from gapcheck import oracle

ROOT = Path(__file__).resolve().parents[1]


def _sheet():
    spec = importlib.util.spec_from_file_location("oracle_sheet", ROOT / "scripts" / "oracle_sheet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_is_current():
    m = _sheet()
    path = ROOT / "reports" / "oracle_rules.md"
    assert path.read_text(encoding="utf-8") == m.render_en(), \
        "reports/oracle_rules.md is stale: run `python scripts/oracle_sheet.py`"


def test_report_covers_every_item():
    m = _sheet()
    text = m.render_en()
    for r in oracle.ROWS:
        assert f"### Row {r.id} · {r.group} → **{r.label}**" in text
        for s in r.sources:
            assert s.quote in text
    for c in oracle.REASONS:
        assert f"### Reason `{c.id}`" in text
    ids = [i for i, *_ in m.items()]
    assert len(ids) == len(set(ids))
    assert "figure summary, not a verbatim quote" in text
    assert "/home/" not in text


def _render(m, tmp_path, overlay) -> str:
    p = tmp_path / "o.json"
    p.write_text(json.dumps(overlay), encoding="utf-8")
    out = tmp_path / "sheet.md"
    m.main(["--overlay", str(p), "--out", str(out)])
    return out.read_text(encoding="utf-8")


def test_overlay_sheet_marks_stale_items(tmp_path):
    m = _sheet()
    its = m.items()
    first, second = its[0], its[1]
    overlay = {"_meta": {"title": "T", "changes": ["c1"]},
               first[0]: {"text": "explanation one", "hash": m.item_hash(first[2])},
               second[0]: {"text": "explanation two", "hash": "0" * 16, "judge": ["what"]}}
    text = _render(m, tmp_path, overlay)
    lab = m.LABELS
    assert f"## {lab['changes']}" in text and "- c1" in text
    assert text.count(lab["checkbox"]) == len(its)
    blocks = text.split("### ")
    b1 = next(b for b in blocks if "explanation one" in b)
    b2 = next(b for b in blocks if "explanation two" in b)
    assert lab["stale"] not in b1 and lab["stale"] in b2
    assert f"{lab['judge_list']}\n- what" in b2
    # items without an explanation are flagged too
    assert text.count(lab["stale"]) == len(its) - 1
    assert text.count(lab["no_text"]) == len(its) - 2


def test_overlay_labels_replace_the_defaults(tmp_path):
    m = _sheet()
    its = m.items()
    labels = {"stale": "STALE-X", "checkbox": "BOX-X", "changes": "CHANGED-X",
              "judge_list": "JUDGE-LIST-X", "unknown": "UNKNOWN-X"}
    overlay = {"_meta": {"changes": ["c1"], "labels": labels},
               its[0][0]: {"text": "t", "hash": "0" * 16, "judge": ["j"]},
               "no_such_item": {"text": "t"}}
    text = _render(m, tmp_path, overlay)
    assert "## CHANGED-X" in text and "JUDGE-LIST-X\n- j" in text
    assert text.count("BOX-X") == len(its)
    assert "> STALE-X: UNKNOWN-X no_such_item" in text
    for k, default in m.LABELS.items():
        assert (default in text) == (k not in labels), k
