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
            assert set(p) == {"id", "text", "category", "sex"}
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


# ---------------------------------------------------------------- sex of a sentence
@pytest.mark.parametrize("text, want", [
    ("She developed proximal muscle weakness while on pravastatin.", {"F"}),
    ("Pt states every statin he has tried made his muscles ache.", {"M"}),
    ("stopped it herself, does not want to retry", {"F"}),
    ("Her sister refused statins and later had an MI.", {"F"}),     # her = the patient
    ("Per pt, his dad could not tolerate any statin.", {"M"}),
    ("Father stopped his statin because of muscle cramps.", set()),  # his = the father
    ("Mom developed liver failure in her sixties.", set()),
    ("Pt's mother refused cholesterol medicines her whole life.", set()),
    ("Pt's husband recently enrolled in hospice.", {"F"}),
    ("Wife is in hospice; pt is her caregiver.", {"M"}),
    ("Her sister's husband declined a statin.", {"F"}),              # spouse of a relative
    ("Daughter is pregnant with pt's first grandchild.", set()),
    ("pt and partner are trying for a baby", set()),
    ("Not pregnant and not planning pregnancy.", {"F"}),
    ("currently pregant, prenatal vitamins only", {"F"}),            # typo still counts
    ("G2P1, follows with OB.", {"F"}),
    ("Postmenopausal, on no hormone therapy.", {"F"}),
    ("BPH on tamsulosin; prostate exam normal.", {"M"}),
    ("Mr. Smith declined a statin.", {"M"}),
    ("On pregabalin for neuropathy.", set()),
    ("Hx of MS, stable.", set()),                                    # not the title Ms
    ("Hepatology follows; the patient is here today.", set()),       # no whole-word match
    ("She says he refused it.", {"F", "M"}),
])
def test_sex_stated(text, want):
    assert P.sex_stated(text) == want


def test_dev_sex_field():
    """Every dev phrase's sex is what its words state; pregnancy reasons are women's."""
    for t, p in ALL:
        stated = P.sex_stated(p["text"])
        assert len(stated) <= 1, p["id"]
        assert p["sex"] == ("F" if t == "reason_pregnancy" else next(iter(stated), None)), p["id"]
    n = {(t, p["sex"]) for t, p in ALL}
    assert ("reason_intolerance", "F") in n and ("reason_intolerance", "M") in n
    assert {p["id"] for t, p in ALL if p["sex"] == "F" and t != "reason_pregnancy"} >= {
        "reason_intolerance-dev-04", "distractor_relative-dev-12"}


@pytest.mark.parametrize("phrase, sex, ok", [
    ({"sex": None, "category": "statin_intolerance"}, "M", True),
    ({"sex": None, "category": "statin_intolerance"}, "F", True),
    ({"sex": "F", "category": "statin_intolerance"}, "M", False),
    ({"sex": "F", "category": "statin_intolerance"}, "F", True),
    ({"sex": "M", "category": "smoking"}, "F", False),
    ({"sex": None, "category": "pregnancy"}, "M", False),  # "Daughter is pregnant": women only
    ({"sex": None, "category": "pregnancy"}, "F", True),
    ({"sex": "F", "category": "pregnancy"}, "F", True),
])
def test_may_plant(phrase, sex, ok):
    assert P.may_plant(phrase, sex) is ok


def _edited(type_, i, **change):
    bad = json.loads(json.dumps(DEV))
    bad["types"][type_][i - 1].update(change)
    return bad


@pytest.mark.parametrize("type_, i, change", [
    ("reason_intolerance", 4, {"sex": None}),     # "She developed ..." without a sex
    ("reason_intolerance", 4, {"sex": "M"}),      # disagrees with "She"
    ("reason_intolerance", 1, {"sex": "F"}),      # no word states a sex
    ("reason_pregnancy", 11, {"sex": None}),      # pregnancy reasons are women's
    ("reason_liver", 1, {"sex": "X"}),
    ("smoking_current", 1, {"text": "He and she smoke daily."}),
    ("distractor_relative", 2, {"text": "Father stopped her statin."}),  # her = the patient
])
def test_validate_rejects_sex_mismatch(type_, i, change):
    with pytest.raises(ValueError):
        P.validate(_edited(type_, i, **change), "dev")


def test_validate_requires_sex_field():
    bad = json.loads(json.dumps(DEV))
    del bad["types"]["reason_liver"][0]["sex"]
    with pytest.raises(ValueError):
        P.validate(bad, "dev")



def test_pool_draws_only_sentences_the_patient_may_receive():
    from gapcheck import edits as E
    pool = E.PhrasePool(DEV, seed=7)
    for t in E.PHRASE_TYPES:
        cap = {s: sum(P.may_plant(p, s) for p in DEV["types"][t]) for s in P.SEXES}
        sexes = ["M"] * cap["M"] if t != "reason_pregnancy" else ["F"] * cap["F"]
        got = [pool.draw(t, s) for s in sexes]
        assert all(P.may_plant(p, s) for p, s in zip(got, sexes)), t
        assert len({p["id"] for p in got}) == len(got)
        with pytest.raises(E.PhraseShortage):  # every sentence a man may receive is used
            pool.draw(t, sexes[0])
    women_only = {p["id"] for t in E.PHRASE_TYPES if t != "reason_pregnancy"
                  for p in DEV["types"][t] if not P.may_plant(p, "M")}
    assert women_only and women_only.isdisjoint(pool.used)


def test_pool_order_is_seeded():
    from gapcheck import edits as E
    a, b, c = E.PhrasePool(DEV, 1), E.PhrasePool(DEV, 1), E.PhrasePool(DEV, 2)
    first = lambda pool: [pool.draw("reason_declined", "F")["id"] for _ in range(5)]
    assert first(a) == first(b) != first(c)


# ---------------------------------------------------------------- test split
TEST = P.load("test")
ALL_TEST = [(t, p) for t, ps in TEST["types"].items() for p in ps]


def _norm(text):
    return " ".join(text.lower().split())


def test_test_format_and_counts():
    raw = json.loads((P.HERE / "test.json").read_text(encoding="utf-8"))
    assert raw == TEST
    assert TEST["version"] == "test-1" and TEST["split"] == "test"
    assert tuple(TEST["types"]) == P.FACT_TYPES
    for t, ps in TEST["types"].items():
        assert len(ps) == 30, t  # SPEC 5.2: 30 test phrasings per type
        for i, p in enumerate(ps, 1):
            assert p["id"] == f"{t}-test-{i:02d}"
            if P.is_reason(t):
                assert p["category"] == P.REASON_TYPES[t]
            elif t in P.DISTRACTOR_TYPES:
                assert p["category"] in oracle.REASON_CATEGORIES
            else:
                assert p["category"] == "smoking"


def test_test_split_loads_for_case_builds():
    from gapcheck import edits as E
    assert E.load_phrases("test") is TEST
    assert set(P.by_id("test")).isdisjoint(P.by_id("dev"))


def test_test_disjoint_from_dev():
    dev = {_norm(p["text"]) for _, p in ALL}
    assert not [p["id"] for _, p in ALL_TEST if _norm(p["text"]) in dev]
    worst = max((difflib.SequenceMatcher(None, a["text"].lower(), b["text"].lower()).ratio(),
                 a["id"], b["id"]) for _, a in ALL_TEST for _, b in ALL)
    assert worst[0] < 0.8, worst


def test_test_no_near_duplicates_and_content_rules():
    texts = [_norm(p["text"]) for _, p in ALL_TEST]
    assert len(set(texts)) == len(texts)
    worst = max(((difflib.SequenceMatcher(None, a["text"].lower(), b["text"].lower()).ratio(),
                  a["id"], b["id"]) for (_, a), (_, b) in itertools.combinations(ALL_TEST, 2)))
    assert worst[0] < 0.8, worst
    lab_value = re.compile(r"\d\s*(?:mg/dl|mmol|%|u/l|iu/l)|\b(?:ldl|hdl|alt|ast|ck|a1c)\s*[:=]?\s*\d",
                           re.I)
    forbidden = re.compile(r"/home/|https?:|@")
    for t, p in ALL_TEST:
        assert not lab_value.search(p["text"]), p["id"]
        assert not forbidden.search(p["text"]), p["id"]
        assert p["text"].isascii(), p["id"]


def test_test_sex_rules_cover_the_test_scale():
    """SPEC 5.2: every distractor (and every smoking phrasing) may go to any patient; at
    most 5 sex-tied phrasings per sex in each reason type other than pregnancy; every
    pregnancy reason is a woman's."""
    for t, ps in TEST["types"].items():
        for p in ps:
            assert p["sex"] == ("F" if t == "reason_pregnancy" else next(iter(
                P.sex_stated(p["text"])), None)), p["id"]
        if t in P.DISTRACTOR_TYPES or t in P.SMOKING_TYPES:
            assert all(P.may_plant(p, s) for p in ps for s in P.SEXES), t
        elif t == "reason_pregnancy":
            assert all(p["sex"] == "F" for p in ps)
        else:
            for s in P.SEXES:
                assert sum(p["sex"] == s for p in ps) <= 5, (t, s)
