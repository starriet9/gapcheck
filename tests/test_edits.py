"""C5 LDL-C threshold twins (SPEC 5.2): upward only, to 195, no LDL-C >= 190 ever."""
from datetime import date

import casedb
from gapcheck import edits as E
from gapcheck import oracle


def _base(ldl=170.0, tc=220.0, sbp=115, bp=False, extra_obs=(), stratum="S5"):
    rows = casedb.default_rows()
    o = [r for r in rows["observations"] if not r["row_id"].startswith(("obs_b", "obs_sbplast",
                                                                       "obs_late"))]
    o += casedb.lipid("b", "2026-05-01", tc, 45, ldl)
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


def test_ldl_twin_moves_up_to_195():
    b = _base()
    assert b.result.status != oracle.GAP and not b.facts.ldl_ever_ge190
    tw = E.c5_ldl(b, 1)
    assert len(tw) == 1
    t = tw[0]
    assert t.variant == "ldl195" and t.partner == "C0/clean" and t.pair
    assert t.facts.ldl == 195.0
    assert float(t.facts.lipid_panel.tc.value) == 220.0 + 25.0
    assert t.result.status == oracle.GAP and t.result.row == "2"
    assert t.check and t.result.status != b.result.status


def test_no_ldl_twin_if_ldl_ever_ge190():
    old = casedb.lipid("z", "2019-05-01", 280, 45, 200)
    b = _base(extra_obs=old)
    assert b.facts.ldl_ever_ge190
    assert E.c5_ldl(b, 1) == []


def test_no_ldl_twin_outside_window():
    assert E.c5_ldl(_base(ldl=150.0), 1) == []


def test_no_ldl_twin_if_labels_equal():
    b = _base(sbp=150, bp=True, stratum="S4")
    assert b.result.status == oracle.GAP
    assert E.c5_ldl(b, 1) == []
