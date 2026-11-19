"""Phrase library for sentences planted in notes (SPEC 5.2).

Files: `<split>.json` in this folder, one per split ("dev", later "test").
Format:

    {"version": "dev-1", "split": "dev",
     "types": {<fact_type>: [{"id": "<type>-<split>-NN", "text": "...",
                              "category": "..."}]}}

`category`:
- reason types: the oracle reason category the sentence states
  (`oracle.REASON_CATEGORIES`), used to build the `oracle.Reason`;
- distractor types: the reason category the sentence imitates (deferral
  imitates "patient_declined"). A distractor is never a reason;
- smoking_current: "smoking".
Sentences with category "pregnancy" (reasons and distractors) should be
planted only in women; the pregnancy reason counts only at ages 30-49
(`oracle.applicable_reasons`).

Rules (SPEC 5.2, 8): about 40 phrasings per type in total, dev and test
disjoint, each test phrasing used at most once, reviewed by a clinician on a
sample. The dev split is frozen before the test phrasings are written, and
R1 (`gapcheck.baselines`) is written from the dev split only.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from ..oracle import REASON_CATEGORIES

HERE = Path(__file__).parent
SPLITS = ("dev", "test")

REASON_TYPES = {
    "reason_intolerance": "statin_intolerance",
    "reason_declined": "patient_declined",
    "reason_pregnancy": "pregnancy",
    "reason_liver": "liver_failure",
    "reason_palliative": "palliative",
}
DISTRACTOR_TYPES = ("distractor_negation", "distractor_relative", "distractor_deferral",
                    "distractor_stable_liver")
SMOKING_TYPES = ("smoking_current",)
FACT_TYPES = tuple(REASON_TYPES) + DISTRACTOR_TYPES + SMOKING_TYPES
CATEGORIES = REASON_CATEGORIES + ("smoking",)


def is_reason(fact_type: str) -> bool:
    return fact_type in REASON_TYPES


@cache
def load(split: str = "dev") -> dict:
    """The phrase file of one split, validated. Returns the parsed document."""
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    path = HERE / f"{split}.json"
    if not path.exists():
        raise FileNotFoundError(f"no phrase file for split {split!r} (written after R1 is frozen)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate(doc, split)
    return doc


def phrases(split: str, fact_type: str) -> list[dict]:
    """The phrase list of one fact type."""
    return load(split)["types"][fact_type]


def by_id(split: str = "dev") -> dict[str, dict]:
    """Every phrase of a split keyed by id, with its fact type added as "type"."""
    return {p["id"]: {**p, "type": t} for t, ps in load(split)["types"].items() for p in ps}


def validate(doc: dict, split: str) -> None:
    """Raise ValueError if the document breaks the format."""
    if set(doc) != {"version", "split", "types"}:
        raise ValueError(f"top-level keys must be version, split, types: {sorted(doc)}")
    if doc["split"] != split or not str(doc["version"]).startswith(split + "-"):
        raise ValueError(f"split/version mismatch: {doc['split']!r}, {doc['version']!r}")
    if set(doc["types"]) != set(FACT_TYPES):
        raise ValueError(f"fact types differ: {sorted(set(doc['types']) ^ set(FACT_TYPES))}")
    seen_ids, seen_text = set(), set()
    for t, ps in doc["types"].items():
        if not ps:
            raise ValueError(f"no phrases for {t}")
        for i, p in enumerate(ps, 1):
            if set(p) != {"id", "text", "category"}:
                raise ValueError(f"phrase keys must be id, text, category: {p}")
            if p["id"] != f"{t}-{split}-{i:02d}":
                raise ValueError(f"bad id {p['id']!r} (expected {t}-{split}-{i:02d})")
            if p["category"] not in CATEGORIES:
                raise ValueError(f"unknown category {p['category']!r} in {p['id']}")
            if t in REASON_TYPES and p["category"] != REASON_TYPES[t]:
                raise ValueError(f"{p['id']}: category must be {REASON_TYPES[t]!r}")
            if t in SMOKING_TYPES and p["category"] != "smoking":
                raise ValueError(f"{p['id']}: category must be 'smoking'")
            if t in DISTRACTOR_TYPES and p["category"] not in REASON_CATEGORIES:
                raise ValueError(f"{p['id']}: distractor must name the reason it imitates")
            text = p["text"]
            if not text or text != text.strip() or "\n" in text:
                raise ValueError(f"{p['id']}: text must be one non-empty line")
            key = " ".join(text.lower().split())
            if p["id"] in seen_ids or key in seen_text:
                raise ValueError(f"duplicate phrase {p['id']}: {text!r}")
            seen_ids.add(p["id"])
            seen_text.add(key)
