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


def test_overlay_sheet_marks_stale_items(tmp_path):
    m = _sheet()
    its = m.items()
    first, second = its[0], its[1]
    overlay = {"_meta": {"title": "T", "changes": ["c1"]},
               first[0]: {"text": "설명 하나", "hash": m.item_hash(first[2])},
               second[0]: {"text": "설명 둘", "hash": "0" * 16, "judge": ["무엇을"]}}
    p = tmp_path / "o.json"
    p.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "sheet.md"
    m.main(["--overlay", str(p), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "## 이번에 바뀐 것" in text and "- c1" in text
    assert text.count(m.CHECKBOX) == len(its)
    blocks = text.split("### ")
    b1 = next(b for b in blocks if "설명 하나" in b)
    b2 = next(b for b in blocks if "설명 둘" in b)
    assert m.STALE not in b1 and m.STALE in b2 and "무엇을" in b2
    # items without an explanation are flagged too
    assert text.count(m.STALE) == len(its) - 1
