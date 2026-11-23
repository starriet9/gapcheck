"""C5 LDL-C threshold twins (SPEC 5.2): a source gives two twins, LDL-C moved up to 185 and
to 195 (TC by the same amount); no LDL-C >= 190 ever; the pair's labels differ."""
from datetime import date

import casedb
from gapcheck import edits as E
from gapcheck import oracle


def _base(ldl=170.0, tc=220.0, sbp=115, bp=False, extra_obs=(), stratum=E.LDL_STRATUM, hdl=65):
    rows = casedb.default_rows()
    o = [r for r in rows["observations"] if not r["row_id"].startswith(("obs_b", "obs_sbplast",
                                                                       "obs_late"))]
    o += casedb.lipid("b", "2026-05-01", tc, hdl, ldl)
    o.append(casedb.obs("obs_sbplast", "2026-05-01", "8480-6", "Systolic Blood Pressure", sbp,
                        "mm[Hg]", "vital-signs"))
    o += list(extra_obs)
    rows["observations"] = o
    if not bp:
        rows["medications"] = [m for m in rows["medications"] if m["row_id"] != "med_lis"]
    t = casedb.tables(rows)
    t.pop("notes")
    b = E.Base(casedb.PATIENT, date.fromisoformat(casedb.INDEX), date.fromisoformat(casedb.BIRTH),
               "M", t, stratum, False)
    return E.finish_base(b)


def test_ldl_twins_185_and_195():
    b = _base()
    assert b.result.status == oracle.DISCUSS and b.result.row == "11a"
    assert not b.facts.ldl_ever_ge190
    tw = E.c5_ldl(b, 1)
    assert [t.variant for t in tw] == ["ldl185", "ldl195"]
    lo, hi = tw
    assert lo.pair == hi.pair and lo.partner == hi.key and hi.partner == lo.key
    assert lo.facts.ldl == 185.0 and hi.facts.ldl == 195.0
    assert float(lo.facts.lipid_panel.tc.value) == 220.0 + 15.0
    assert float(hi.facts.lipid_panel.tc.value) == 220.0 + 25.0
    assert hi.result.status == oracle.GAP and hi.result.row == "2"
    assert lo.result.status == oracle.DISCUSS and lo.result.row == "11a"
    assert lo.check and hi.check and lo.p1_stable and hi.p1_stable
    # both twins at least 5 mg/dL from the 190 cut (and from 160)
    assert all(E.margins_ok(t.facts, t.result) for t in tw)


def test_ldl_twins_move_at_most_35():
    tw = E.c5_ldl(_base(ldl=160.0), 1)
    assert [t.detail["ldl_to"] - t.detail["ldl_from"] for t in tw] == [25.0, 35.0]


def test_no_ldl_twin_if_ldl_ever_ge190():
    old = casedb.lipid("z", "2019-05-01", 280, 45, 200)
    b = _base(extra_obs=old)
    assert b.facts.ldl_ever_ge190
    assert E.c5_ldl(b, 1) == []


def test_no_ldl_twin_outside_source_range():
    assert E.c5_ldl(_base(ldl=150.0), 1) == []
    assert E.c5_ldl(_base(ldl=186.0), 1) == []   # the 185 twin would move down


def test_no_ldl_twin_if_tc_out_of_range():
    assert E.c5_ldl(_base(ldl=170.0, tc=300.0), 1) == []  # TC 325 for the 195 twin


def test_no_ldl_twin_if_source_is_gap():
    b = _base(sbp=170, bp=True, hdl=40)
    assert b.result.status == oracle.GAP
    assert E.c5_ldl(b, 1) == []


def test_no_ldl_twin_in_the_nonhdl_zone():
    """Footnote * of the sec. 4.2.4.3 recommendations reads severe hypercholesterolemia as
    'LDL-C >190 mg/dL, non-HDL-C >220 mg/dL, and/or apoB >140 mg/dL'. The 185 twin of this
    source has TC 305 and HDL-C 85 (non-HDL-C 220) and a DISCUSS label, so the pair is not
    used; with TC 0.1 lower it is."""
    b = _base(ldl=175.0, tc=295.0, hdl=85, sbp=115)
    assert b.result.status == oracle.DISCUSS
    assert E.c5_ldl(b, 1) == []
    lo, hi = E.c5_ldl(_base(ldl=175.0, tc=294.9, hdl=85, sbp=115), 1)
    assert lo.result.status == oracle.DISCUSS and not E.nonhdl_zone(lo.facts, lo.result)


def test_nonhdl_zone_only_where_row_2_would_change_the_label():
    from vignette import make
    zone = dict(tc=290.0, hdl=70.0, ldl=100.0)
    for kw, status, used in ((dict(age=62, sex="F", sbp=105.0), oracle.NOT_INDICATED, False),
                             (dict(age=50, diabetes=True), oracle.GAP, True),
                             (dict(age=50, ascvd=("MI",)), oracle.GAP, True),
                             (dict(age=62, sex="F", sbp=105.0, statin=True), oracle.ON_THERAPY,
                              True)):
        f = make(**zone, **kw)
        r = oracle.label(f)
        assert r.status == status and E.nonhdl_zone(f, r) == (not used), kw
    f = make(age=62, sex="F", sbp=105.0, tc=289.9, hdl=70.0, ldl=100.0)
    assert not E.nonhdl_zone(f, oracle.label(f))
