"""Phrase library for sentences planted in notes (SPEC 5.2).

Files: `<split>.json` in this folder, one per split: dev.json ("dev-1") and
test.json ("test-1").
Format:

    {"version": "dev-1", "split": "dev",
     "types": {<fact_type>: [{"id": "<type>-<split>-NN", "text": "...",
                              "category": "...", "sex": "F" | "M" | null}]}}

`category`:
- reason types: the oracle reason category the sentence states
  (`oracle.REASON_CATEGORIES`), used to build the `oracle.Reason`;
- distractor types: the reason category the sentence imitates (deferral
  imitates "patient_declined"). A distractor is never a reason;
- smoking_current: "smoking".

`sex`: the patient sex the sentence states, or null if it states none. A
sentence states a sex when a word in it refers to the patient and belongs to
one sex: a pronoun ("She developed ...", "his dad"), a spouse ("Pt's
husband"), or pregnancy and other female or male anatomy ("Not pregnant").
A pronoun or pregnancy word that can refer to a relative named before it
("Father stopped his statin", "Daughter is pregnant") states nothing about
the patient. Every reason_pregnancy sentence has sex "F". `validate` checks
the field against these words (SEX_WORDS, below).

Who may receive a sentence (`may_plant`): a patient of its `sex` (any patient
if null), and only a woman if its category is "pregnancy" (reasons and
distractors). The pregnancy reason counts only at ages 30-49
(`oracle.applicable_reasons`).

Rules (SPEC 5.2, 8): 20 dev and 30 test phrasings per type, dev and test
disjoint, each test phrasing used at most once, reviewed by a clinician on a
sample. The dev split is frozen before the test phrasings are written, and
R1 (`gapcheck.baselines`) is written from the dev split only.
"""
from __future__ import annotations

import json
import re
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
SEXES = ("F", "M")

# Words that tie a sentence to the patient's sex (`sex_stated`). Matched as
# whole words, lower case; entries ending in "*" are prefixes (catches typos
# such as "pregant"), except the words in NOT_SEX_WORDS.
SEX_WORDS = {
    "F": ("she", "her", "hers", "herself", "husband", "widow", "woman", "female", "lady",
          "preg*", "conceiv*", "conception", "gestation*", "trimester", "iup", "prenatal",
          "postpartum", "breastfeed*", "lactating", "lactation", "menopaus*",
          "postmenopaus*", "perimenopaus*", "menstrua*", "menses", "hysterectomy", "ovar*",
          "uterus", "uterine", "endometri*", "ob", "obgyn", "gyn", "mammogra*"),
    "M": ("he", "him", "his", "himself", "wife", "widower", "man", "male", "gentleman",
          "prostat*", "bph", "testic*", "scrot*", "erectile", "vasectomy"),
}
NOT_SEX_WORDS = {"pregabalin", "pregnenolone"}
# Words that can belong to a relative named earlier in the sentence: pronouns
# and pregnancy words refer back to a relative of the same sex or of unstated
# sex ("Father stopped his statin", "Daughter is pregnant"); a spouse word to
# any relative ("sister's husband").
REFERS_BACK = {"she", "her", "hers", "herself", "he", "him", "his", "himself", "preg*",
               "conceiv*", "conception", "gestation*", "trimester", "prenatal", "postpartum",
               "breastfeed*", "lactating", "lactation"}
SPOUSES = {"husband", "wife"}
RELATIVES = {
    "F": {"mother", "mom", "mum", "sister", "daughter", "aunt", "grandmother", "niece",
          "granddaughter", "stepmother", "wife", "girlfriend"},
    "M": {"father", "dad", "brother", "son", "uncle", "grandfather", "nephew", "grandson",
          "stepfather", "husband", "boyfriend"},
    None: {"partner", "spouse", "child", "children", "parent", "parents", "sibling",
           "siblings", "cousin", "relative", "relatives", "family", "friend", "grandchild"},
}
_OBSTETRIC = re.compile(r"\bG\d+\s*P\d+", re.I)   # gravida/para, e.g. "G2P1"
_TITLE = {"F": re.compile(r"\b(?:Mrs|Ms|Miss)\b"), "M": re.compile(r"\bMr\b")}


def _word_sex(w: str) -> tuple[str | None, str | None]:
    """(sex, SEX_WORDS entry) of a lower-case word, or (None, None)."""
    if w in NOT_SEX_WORDS:
        return None, None
    for sex, words in SEX_WORDS.items():
        for x in words:
            if w == x or (x.endswith("*") and w.startswith(x[:-1])):
                return sex, x
    return None, None


def sex_stated(text: str) -> set[str]:
    """Sexes the sentence states about the patient (empty: none; two: contradictory).

    Every word of SEX_WORDS counts, unless a relative it can belong to
    (REFERS_BACK, SPOUSES) is named before it. Titles (Mr, Mrs, Ms, Miss) and
    gravida/para notation count too.
    """
    out = {s for s, rx in _TITLE.items() if rx.search(text)}
    if _OBSTETRIC.search(text):
        out.add("F")
    named: set[str | None] = set()
    for w in re.findall(r"[a-z]+", text.lower().replace("'s", "")):
        sex, entry = _word_sex(w)
        if entry in SPOUSES:
            relative = bool(named)
        else:
            relative = entry in REFERS_BACK and (sex in named or None in named)
        if sex and not relative:
            out.add(sex)
        named |= {rs for rs, words in RELATIVES.items() if w in words}
    return out


def may_plant(phrase: dict, sex: str) -> bool:
    """Whether a patient of `sex` may receive the sentence (module docstring)."""
    return phrase["sex"] in (None, sex) and (phrase["category"] != "pregnancy" or sex == "F")


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
            if set(p) != {"id", "text", "category", "sex"}:
                raise ValueError(f"phrase keys must be id, text, category, sex: {p}")
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
            _check_sex(t, p)
            key = " ".join(text.lower().split())
            if p["id"] in seen_ids or key in seen_text:
                raise ValueError(f"duplicate phrase {p['id']}: {text!r}")
            seen_ids.add(p["id"])
            seen_text.add(key)


def _check_sex(fact_type: str, p: dict) -> None:
    """The `sex` field agrees with the words of the sentence (module docstring)."""
    sex, stated = p["sex"], sex_stated(p["text"])
    if sex is not None and sex not in SEXES:
        raise ValueError(f"{p['id']}: sex must be 'F', 'M' or null, got {sex!r}")
    if len(stated) > 1:
        raise ValueError(f"{p['id']}: the sentence states both sexes: {p['text']!r}")
    if fact_type == "reason_pregnancy":
        ok, want = sex == "F" and stated <= {"F"}, "'F' (pregnancy reason)"
    else:
        ok, want = stated == ({sex} if sex else set()), repr(next(iter(stated), None))
    if not ok:
        raise ValueError(f"{p['id']}: sex is {sex!r}, the sentence states {want}: "
                         f"{p['text']!r}")
