"""Every verbatim quote in the oracle's source tables appears in the guideline text.

The guideline PDF and its text extraction are not in the repository. Set
GAPCHECK_GUIDELINE_TEXT to one or more text extractions of the article (paths
separated by os.pathsep, for example a plain and a layout-preserving
`pdftotext` output, pages separated by form feeds) to run this check; it is
skipped otherwise.

Matching is tolerant of the extraction, not of the wording: both sides are
reduced to lowercase word tokens (hyphens removed, '$' read as '≥'), and the
quote's tokens must appear in order on the cited page or its neighbours, with
at most MAX_GAP foreign tokens between two quote tokens (two-column layout,
reference superscripts). A word split across a line break may match as two
tokens, and a word may carry a trailing reference number. Figure summaries
(kind "figure") are not verbatim and are skipped. Negative controls check that
a changed word or number is caught.
"""
import os
import re
import unicodedata
from pathlib import Path

import pytest

from gapcheck import oracle

MAX_GAP = 40
ENV = "GAPCHECK_GUIDELINE_TEXT"


def _paths():
    raw = os.environ.get(ENV, "")
    return [Path(p) for p in raw.split(os.pathsep) if p and Path(p).is_file()]


def tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text).replace("$", "≥").replace("#", "≤")
    t = t.replace("-", "").replace("‐", "")  # "LDL-C" -> "ldlc"; "dia-" + "betes" -> 2 tokens
    return re.findall(r"[a-z0-9]+|[≥≤<>%]", t.lower())


def found(quote: str, page_tokens: list[str], max_gap: int = MAX_GAP) -> bool:
    q = tokens(quote)
    pt = page_tokens
    for start in range(len(pt)):
        if pt[start] != q[0] and not (len(pt[start]) >= 2 and q[0].startswith(pt[start])):
            continue
        i, j, gap, rest = 0, start, 0, ""
        while i < len(q) and j < len(pt):
            want = rest or q[i]
            tok = pt[j]
            if tok == want or (tok.startswith(want) and tok[len(want):].isdigit()):
                i, rest, gap = i + 1, "", 0          # (trailing reference number)
            elif want.startswith(tok) and want != tok and (
                    len(tok) >= 2 or want[len(tok):].isdigit()):  # "m" + "2" for "m2"
                rest, gap = want[len(tok):], 0       # word split across a line
            else:
                gap += 1
                if gap > max_gap:
                    break
            j += 1
        if i == len(q):
            return True
    return False


def sources():
    out = []
    for r in oracle.ROWS:
        out += [(f"row {r.id}", s) for s in r.sources]
    for c in oracle.REASONS:
        out += [(f"reason {c.id}", s) for s in c.sources]
    out += [("context", s) for s in oracle.CONTEXT_SOURCES]
    for c in oracle.CONVENTIONS:
        out += [(f"convention {c.id}", s) for s in c.sources]
    return [(tag, s) for tag, s in out if s.kind == "quote"]


@pytest.fixture(scope="module")
def pages():
    paths = _paths()
    if not paths:
        pytest.skip(f"{ENV} not set (guideline text extraction is not in the repository)")
    return [[tokens(p) for p in path.read_text(encoding="utf-8").split("\f")]
            for path in paths]


def _on_page(quote, s, pages):
    for doc in pages:
        lo, hi = max(0, s.pdf_page - 2), min(len(doc), s.pdf_page + 1)
        for k in range(lo, hi):
            if found(quote, doc[k] + (doc[k + 1] if k + 1 < len(doc) else [])):
                return True
    return False


@pytest.mark.parametrize("tag,s", sources(), ids=lambda v: v if isinstance(v, str) else "")
def test_quote_in_guideline_text(tag, s, pages):
    assert _on_page(s.quote, s, pages), f"{tag}: {s.section} {s.rec} p. {s.page}"


def test_altered_quotes_not_found(pages):
    """Negative controls: one changed word or number must make the check fail."""
    rec5 = oracle.ROW_BY_ID["9"].sources[0]
    rec3 = oracle.ROW_BY_ID["11a"].sources[0]
    take8 = oracle.ROW_BY_ID["3"].sources[1]
    for s, old, new in ((rec5, "at least a moderate-intensity", "at least a high-intensity"),
                        (rec3, "160 to 189", "150 to 189"),
                        (take8, "40 to 75", "40 to 79"),
                        (take8, "regardless of LDL-C level", "irrespective of LDL-C level")):
        assert old in s.quote
        assert not _on_page(s.quote.replace(old, new), s, pages), (s.rec, new)


def test_matcher_on_synthetic_text():
    page = tokens("In adults with dia-\nbetes 12,13 and an LDL-C of $190\nmg/dL, therapy")
    assert found("In adults with diabetes and an LDL-C of ≥190 mg/dL, therapy", page)
    assert not found("In adults with diabetes and an LDL-C of ≥160 mg/dL, therapy", page)
