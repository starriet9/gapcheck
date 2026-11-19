"""Render the oracle's decision table, reasons and conventions as a document.

Everything comes from the code's own tables (`oracle.ROWS`, `oracle.REASONS`,
`oracle.CONTEXT_SOURCES`, `oracle.CONVENTIONS`, `oracle.NOT_ENCODED`), so the
document cannot drift from the oracle. `tests/test_oracle_sheet.py` fails if
`reports/oracle_rules.md` is stale.

Usage:
  python scripts/oracle_sheet.py                       # writes reports/oracle_rules.md
  python scripts/oracle_sheet.py --out FILE.md         # English document elsewhere
  python scripts/oracle_sheet.py --hashes              # print the hash of every item
  python scripts/oracle_sheet.py --overlay EXPL.json --out SHEET.md

With --overlay, the output is a review sheet: every item shows a reviewer's
explanation (any language) from the overlay, the English item as rendered from
the code, and a checkbox line. The overlay is a JSON object keyed by item id
(row id such as "5" or "11a", reason category, convention id, "context",
"not_encoded"):

  {"_meta": {"title": str, "intro": [str], "changes": [str], "legend": [str]},
   "<item id>": {"text": str, "judge": [str], "hash": str}}

`hash` is the hash (`--hashes`) of the English item the text was written for.
If the item has changed since, the sheet prints a warning next to it. Items
with [confirm] entries or a non-empty "judge" list are marked as needing a
human judgment; items with only [encoding] entries are marked as such.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from gapcheck import oracle

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "reports" / "oracle_rules.md"

ROLE_NOTE = {"basis": "", "context": " · context, not the basis of the label",
             "conflict": " · reads differently; see [encoding]"}
STALE = "⚠ 설명이 코드보다 오래됨"  # shown in review sheets when an overlay hash is stale
CHECKBOX = "검토: [ ] 맞음  [ ] 수정 필요 → 메모:"
JUDGE = "★ 사람 판단 필요"       # [confirm] entries or a "judge" list in the overlay
ENCODING = "◆ 인코딩 선택 있음"  # [encoding] entries only


# ---------------------------------------------------------------- English items
def _source(s: oracle.Source) -> list[str]:
    where = f"Sec. {s.section}" if s.section[0].isdigit() else s.section
    head = f"{where}, {'rec ' + s.rec if s.rec.isdigit() else s.rec}"
    if s.cor:
        head += f" · COR {s.cor}"
    if s.loe:
        head += f" · LOE {s.loe}"
    head += f" · journal p. {s.page} (PDF p. {s.pdf_page})"
    if s.kind == "figure":
        head += " · figure summary, not a verbatim quote"
    head += ROLE_NOTE[s.role]
    return [f"- {head}", f"  > {s.quote}"]


def _tagged(tag: str, items) -> list[str]:
    return [f"- [{tag}] {t}" for t in items]


def row_block(r: oracle.Row) -> str:
    L = [f"### Row {r.id} · {r.group} → **{r.label}**", "", f"- Rule: {r.rule}"]
    if r.scope:
        L.append(f"- Effective scope: {r.scope}")
    if r.label != oracle.EXCLUDED and r.id != "0":
        variants = {v: oracle.row_status(r.id, v) for v in ("S-A", "S-C")}
        changed = [f"{v}: {s}" for v, s in variants.items() if s != r.label]
        if changed:
            L.append(f"- Sensitivity analyses: {'; '.join(changed)}.")
    if r.sources:
        L.append("- Sources:")
        L += ["  " + x for s in r.sources for x in _source(s)]
    else:
        L.append("- Sources: none (not a guideline row).")
    L += _tagged("encoding", r.encoding) + _tagged("data", r.data_limits)
    L += _tagged("confirm", r.confirm)
    if r.not_sampled:
        L.append(f"- Sampling: {r.not_sampled}")
    return "\n".join(L)


def reason_block(c: oracle.ReasonCategory) -> str:
    L = [f"### Reason `{c.id}`: {c.name}", ""]
    L.append(f"- Structured entries: {c.structured or 'none (notes only).'}")
    if c.sources:
        L.append("- Sources:")
        L += ["  " + x for s in c.sources for x in _source(s)]
    else:
        L.append("- Sources: none in the guideline.")
    L += _tagged("encoding", c.encoding) + _tagged("data", c.data_limits)
    L += _tagged("confirm", c.confirm)
    return "\n".join(L)


def context_block() -> str:
    L = ["### Context: GAP is a prompt for review, not a finding of an error in care", ""]
    L += [x for s in oracle.CONTEXT_SOURCES for x in _source(s)]
    return "\n".join(L)


def convention_block(c: oracle.Convention) -> str:
    L = [f"### {c.title}", "", f"- [{c.tag}] {c.text}"]
    if c.sources:
        L.append("- Sources:")
        L += ["  " + x for s in c.sources for x in _source(s)]
    return "\n".join(L)


def not_encoded_block() -> str:
    L = ["### Not encoded", ""]
    L += [f"- {sec}: {what}" for sec, what in oracle.NOT_ENCODED]
    return "\n".join(L)


def _tag(x) -> str:
    return "confirm" if x.confirm else ("encoding" if x.encoding else "")


def items() -> list[tuple[str, str, str, str]]:
    """(id, section, English markdown, strongest tag: "confirm", "encoding" or "")."""
    out = []
    for r in oracle.ROWS:
        out.append((r.id, "rows", row_block(r), _tag(r)))
    for c in oracle.REASONS:
        out.append((c.id, "reasons", reason_block(c), _tag(c)))
    out.append(("context", "reasons", context_block(), ""))
    for c in oracle.CONVENTIONS:
        out.append((c.id, "conventions", convention_block(c),
                    "encoding" if c.tag == "encoding" else ""))
    out.append(("not_encoded", "conventions", not_encoded_block(), "encoding"))
    return out


def item_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


SECTION_TITLES = {
    "rows": "Decision table (first matching row wins; patients aged 30-79 at index)",
    "reasons": "Documented reasons (SPEC 3.3)",
    "conventions": "Conventions, encoding choices and scope",
}

INTRO = """\
Generated by `scripts/oracle_sheet.py` from `src/gapcheck/oracle.py`; do not edit by hand. \
Source: 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on the Management \
of Dyslipidemia, J Am Coll Cardiol 2026;87(19):2624-2757, doi:10.1016/j.jacc.2025.11.016. \
Journal page = PDF page + 2623.

Quotes are verbatim except for reference superscripts and footnote markers, which are \
left out; "≥" is printed as a symbol that some text extractors render as "$". Items marked \
"figure summary" describe the boxes and arrows of a figure and are not verbatim sentences. \
Tags: [encoding] = a choice made where the guideline leaves room; [data] = what the \
generated data can and cannot test; [confirm] = an open point for a reviewer.

Labels: a COR 1 recommendation to start a statin gives GAP, COR 2a or 2b only gives \
DISCUSS, none gives NOT_INDICATED. A documented reason changes GAP to DOCUMENTED_REASON \
and nothing else. Missing deciding inputs give CANNOT_DETERMINE (SPEC 3.4). Rows 8-11b are \
reached only at ages 30-75 with LDL-C 70-189, no ASCVD, no diabetes at 40-75 and no CKD \
stage 3-4 at 40-75; diabetes at 30-39 reaches only rows 2, 5, 7 and 11b."""


def render_en() -> str:
    L = ["# Oracle rules: decision table, reasons and conventions", "", INTRO]
    sec = None
    for _, s, text, _ in items():
        if s != sec:
            L += ["", f"## {SECTION_TITLES[s]}"]
            sec = s
        L += ["", text]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- review sheet
def render_overlay(overlay: dict) -> str:
    meta = overlay.get("_meta", {})
    L = [f"# {meta.get('title', 'Oracle review sheet')}", ""]
    L += [p for para in meta.get("intro", []) for p in (para, "")]
    if meta.get("changes"):
        L += ["## 이번에 바뀐 것", ""]
        L += [f"- {c}" for c in meta["changes"]] + [""]
    if meta.get("legend"):
        L += [f"- {x}" for x in meta["legend"]] + [""]
    sec = None
    stale = []
    for iid, s, text, tag in items():
        if s != sec:
            L += [f"## {SECTION_TITLES[s]}", ""]
            sec = s
        o = overlay.get(iid, {})
        head, _, body = text.partition("\n")
        judge = list(o.get("judge", []))
        mark = JUDGE if (tag == "confirm" or judge) else (ENCODING if tag else "")
        L.append(head + (f"  {mark}" if mark else ""))
        L.append("")
        if not o:
            L += [f"> {STALE} (설명 없음)", ""]
            stale.append(iid)
        else:
            if o.get("hash") != item_hash(text):
                L += [f"> {STALE}", ""]
                stale.append(iid)
            L += [o.get("text", "").strip(), ""]
            if judge:
                L += ["판단할 것:"] + [f"- {j}" for j in judge] + [""]
        L += ["<details><summary>English rule from the code</summary>", "", body.strip(), "",
              "</details>", "", f"- {CHECKBOX}", ""]
    unknown = sorted(k for k in overlay if not k.startswith("_")
                     and k not in {i for i, *_ in items()})
    if unknown:
        L += [f"> {STALE}: 코드에 없는 항목 {', '.join(unknown)}", ""]
    out = "\n".join(L).rstrip() + "\n"
    for iid in stale:
        print(f"stale: {iid}", file=sys.stderr)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--overlay", type=Path, default=None)
    ap.add_argument("--hashes", action="store_true")
    a = ap.parse_args(argv)
    if a.hashes:
        print(json.dumps({iid: item_hash(t) for iid, _, t, _ in items()}, indent=1))
        return
    if a.overlay is not None:
        if a.out is None:
            ap.error("--overlay needs --out")
        text = render_overlay(json.loads(a.overlay.read_text(encoding="utf-8")))
    else:
        text = render_en()
    out = a.out or DEFAULT_OUT
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out.name} ({len(text)} chars)")


if __name__ == "__main__":
    main()
