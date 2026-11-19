"""Phrase library: format, uniqueness, content rules (SPEC 5.2)."""
import difflib
import itertools
import json
import re
from datetime import date

import pytest

from gapcheck import oracle
from gapcheck import phrases as P

DEV = P.load("dev")
ALL = [(t, p) for t, ps in DEV["types"].items() for p in ps]


def test_format_and_counts():
    raw = json.loads((P.HERE / "dev.json").read_text(encoding="utf-8"))
    assert raw == DEV
    assert DEV["version"] == "dev-1" and DEV["split"] == "dev"
    assert tuple(DEV["types"]) == P.FACT_TYPES
    for t, ps in DEV["types"].items():
        assert len(ps) == 20, t
        for i, p in enumerate(ps, 1):
            assert set(p) == {"id", "text", "category"}
            assert p["id"] == f"{t}-dev-{i:02d}"


def test_categories():
    assert set(P.REASON_TYPES.values()) == set(oracle.REASON_CATEGORIES)
    for t, p in ALL:
        if P.is_reason(t):
            assert p["category"] == P.REASON_TYPES[t]
            oracle.Reason(p["category"], date(2026, 1, 1), "note_x")  # valid category
        elif t in P.DISTRACTOR_TYPES:
            assert p["category"] in oracle.REASON_CATEGORIES
        else:
            assert p["category"] == "smoking"


def test_unique_ids_and_texts():
    ids = [p["id"] for _, p in ALL]
    texts = [" ".join(p["text"].lower().split()) for _, p in ALL]
    assert len(set(ids)) == len(ids)
    assert len(set(texts)) == len(texts)


def test_no_near_duplicates():
    """Pairwise character similarity below 0.8 within each fact type and overall."""
    worst = max(((difflib.SequenceMatcher(None, a["text"].lower(), b["text"].lower()).ratio(),
                  a["id"], b["id"]) for (_, a), (_, b) in itertools.combinations(ALL, 2)))
    assert worst[0] < 0.8, worst


def test_content_rules():
    lab_value = re.compile(r"\d\s*(?:mg/dl|mmol|%|u/l|iu/l)|\b(?:ldl|hdl|alt|ast|ck|a1c)\s*[:=]?\s*\d",
                           re.I)
    forbidden = re.compile(r"/home/|https?:|@")
    for t, p in ALL:
        assert not lab_value.search(p["text"]), p["id"]
        assert not forbidden.search(p["text"]), p["id"]
        assert p["text"].isascii(), p["id"]


def test_validate_rejects_bad_documents():
    bad = json.loads(json.dumps(DEV))
    bad["types"]["reason_liver"][0]["category"] = "palliative"
    with pytest.raises(ValueError):
        P.validate(bad, "dev")
    bad = json.loads(json.dumps(DEV))
    bad["types"]["reason_liver"].append(dict(bad["types"]["reason_liver"][0]))
    with pytest.raises(ValueError):
        P.validate(bad, "dev")
    with pytest.raises(ValueError):
        P.load("train")


def test_by_id():
    d = P.by_id("dev")
    assert len(d) == 200
    assert d["reason_declined-dev-01"]["type"] == "reason_declined"
