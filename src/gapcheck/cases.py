"""Base-patient sampling (SPEC 5.1) and case sets (SPEC 5.2, 8).

A case set directory holds:
- cases.jsonl       one line per case: id, base patient, stratum, condition,
                    expected status and missing (main, S-A, S-C), pair
- edit_ledger.csv   one row per case: rows deleted/added, planted phrase and
                    note, expected label, direction check
- base/<table>.csv  every row of the base patients (7 Synthea tables, with
                    row_id and the internal patch column) and base/cohort.csv
- base/notes.jsonl  rendered notes of the base records
- deltas.jsonl      per case: row ids deleted, rows added, sentences planted,
                    and every note whose text differs from the base note
- manifest.json     seeds, sub-seeds, sources, counts, sha256 of every file

`materialize(case_dir, case_id)` gives one case as an in-memory DuckDB
(tables of one patient, `cohort` and `notes`).

Seeds: sub-seed = patches.sub_seed(master_seed, label) for the labels
"sample", "edits" and "phrases" (SPEC 8).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

from . import edits as E
from . import facts, oracle, patches, strata
from .notes import Plant, render_notes

ROOT = Path(__file__).resolve().parents[2]
SUB_SEED_LABELS = ("sample", "edits", "phrases")
CONDITION_ORDER = {c: i for i, c in enumerate(E.CONDITIONS)}
S4_BINS = (">=10", "5-<10")


# ---------------------------------------------------------------- strata
def pre_p2(rec: facts.PatientRecord) -> facts.PatientRecord:
    m = rec.medications
    if m.empty or "patch" not in m or not (m.patch == "P2").any():
        return rec
    return rec.replace(medications=m[m.patch != "P2"])


def ensure_p2(con: duckdb.DuckDBPyConnection, seed: int) -> None:
    """Apply P2 (as scripts/gate.py does) if it is not applied yet."""
    if patches.p2_applied(con):
        return
    S = {r.patient: strata.assign(facts.facts_at_index(pre_p2(r)))
         for r in facts.iter_records(con)}
    groups = pd.DataFrame([{"PATIENT": p, "group": s.indication_row} for p, s in S.items()
                           if s.indication_row in patches.P2_GROUPS], columns=["PATIENT", "group"])
    patches.apply_p2(con, seed, groups)


def strata_and_facts(con) -> tuple[dict[str, strata.StrataRow], dict[str, facts.PatientFacts]]:
    """Strata on pre-P2 facts and facts after P2, for every cohort patient."""
    S, F = {}, {}
    for r in facts.iter_records(con):
        S[r.patient] = strata.assign(facts.facts_at_index(pre_p2(r)))
        F[r.patient] = facts.facts_at_index(r)
    return S, F


# ---------------------------------------------------------------- sampling
def _rank(seed: int, stratum: str, patient: str) -> str:
    return hashlib.sha256(f"{seed}:{stratum}:{patient}".encode()).hexdigest()


def cell_targets(stratum: str, n: int) -> dict[tuple, int]:
    """Base patients per cell: (statin,) or (statin, S4 bin); S5 off statin only."""
    if stratum == "S5":
        return {(False,): n}
    on, off = n // 2, n - n // 2
    if stratum != "S4":
        return {(True,): on, (False,): off}
    # S4: even split of each statin half between the bins; >=10 gets the
    # smaller share among treated and the larger among untreated.
    return {(True, ">=10"): on // 2, (True, "5-<10"): on - on // 2,
            (False, ">=10"): off - off // 2, (False, "5-<10"): off // 2}


def sample(S: dict, F: dict, n: int, seed: int) -> tuple[list[dict], dict]:
    """Base patients per SPEC 5.1. Returns (picks in order, shortfalls by cell)."""
    s = patches.sub_seed(seed, "sample")
    picks, short = [], {}
    for st in strata.STRATA:
        elig = sorted((p for p, r in S.items() if r.stratum == st), key=lambda p: _rank(s, st, p))
        for cell, k in cell_targets(st, n).items():
            on = cell[0]
            pool = [p for p in elig if F[p].on_statin == on
                    and (len(cell) == 1 or S[p].s4_bin == cell[1])]
            chosen = []
            if st == "S1" and not on:
                cap, n_stroke = k // 2, 0  # at most half stroke-only (SPEC 4.1)
                for p in pool:
                    if len(chosen) == k:
                        break
                    if S[p].ascvd_type == "stroke_only":
                        if n_stroke >= cap:
                            continue
                        n_stroke += 1
                    chosen.append(p)
            else:
                chosen = pool[:k]
            if len(chosen) < k:
                short["/".join([st, "on" if on else "off", *cell[1:]])] = k - len(chosen)
            for p in chosen:
                picks.append({"patient": p, "stratum": st, "statin": on,
                              "s4_bin": S[p].s4_bin, "ascvd_type": S[p].ascvd_type,
                              "rank": _rank(s, st, p)})
    return picks, short


def load_bases(con, picks: list[dict], F: dict) -> list[E.Base]:
    """Every row of the picked patients, as `edits.Base` (facts and label computed)."""
    ids = [p["patient"] for p in picks]
    con.register("_want", pd.DataFrame({"PATIENT": ids}))
    t = {}
    for name in E.ALL_TABLES:
        col = "Id" if name == "patients" else "PATIENT"
        order = "" if name == "patients" else ", row_id"
        dcol = facts_dcol(name)
        df = con.execute(f'select * from {name} where "{col}" in (select PATIENT from _want) '
                         f'order by "{col}"{dcol}{order}').df()
        t[name] = dict(tuple(df.groupby(col, sort=False))), df.iloc[0:0]
    coh = con.execute("select PATIENT, index_date, birthdate, sex from cohort "
                      "where PATIENT in (select PATIENT from _want)").fetchall()
    con.unregister("_want")
    coh = {r[0]: r for r in coh}
    out = []
    for p in picks:
        pid = p["patient"]
        tabs = {n: t[n][0].get(pid, t[n][1]).reset_index(drop=True) for n in E.ALL_TABLES}
        _, ix, bd, sex = coh[pid]
        b = E.finish_base(E.Base(pid, facts._to_date(ix), facts._to_date(bd), sex, tabs,
                                 p["stratum"], p["statin"]))
        ref = oracle.label(F[pid])
        if (b.result.status, b.result.missing) != (ref.status, ref.missing):
            raise RuntimeError(f"full-record label differs from the cohort label for {pid}")
        if b.facts.on_statin != p["statin"]:
            raise RuntimeError(f"statin status mismatch for {pid}")
        out.append(b)
    return out


def facts_dcol(table: str) -> str:
    from .load import TABLES
    d = TABLES[table][2]
    return f', "{d}"' if d else ""


# ---------------------------------------------------------------- build
def case_id(split: str, seed: int, patient: str, key: str) -> str:
    h = hashlib.sha256(f"{split}:{seed}:{patient}:{key}".encode()).hexdigest()
    return f"case_{h[:12]}"


def near_targets(n: int) -> dict[float, int]:
    """Near-threshold C5 sources per cut: n // 2 at 5%, the rest at 3%."""
    return {E.NEAR_CUTS[0]: n // 2, E.NEAR_CUTS[1]: n - n // 2}


def near_candidates(S: dict, F: dict, taken: set, seed: int) -> dict[float, list[str]]:
    """Near-threshold C5 sources, ranked: off statin, oracle row 9, 10 or 11b, 10-year
    risk within NEAR_SOURCE_BAND of a cut (the nearer cut), not a base patient, and no
    sampling exclusion (the P1 rule is checked on the twins instead)."""
    s = patches.sub_seed(seed, "sample")
    out = {c: [] for c in E.NEAR_CUTS}
    for p, f in F.items():
        if p in taken or f.on_statin:
            continue
        if [e for e in S[p].exclusions if e != "p1_egfr_dependent"]:
            continue
        r = oracle.label(f)
        if r.row not in E.NEAR_ROWS or r.risk10 is None:
            continue
        cut = min(E.NEAR_CUTS, key=lambda c: abs(r.risk10 - c))
        if abs(r.risk10 - cut) <= E.NEAR_SOURCE_BAND:
            out[cut].append(p)
    return {c: sorted(v, key=lambda p: _rank(s, f"{E.NEAR_STRATUM}:{c}", p))
            for c, v in out.items()}


def sample_near(con, S, F, taken: set, n: int, seed: int) -> tuple[list, list, dict, dict]:
    """Near-threshold sources that yield a valid C5 pair, in rank order, until each cut
    has its target. Returns (picks, bases, twins by patient, shortfalls)."""
    edit_seed = patches.sub_seed(seed, "edits")
    cand = near_candidates(S, F, taken, seed)
    picks, bases, twins, short = [], [], {}, {}
    for cut, k in near_targets(n).items():
        got, pos, batch = 0, 0, max(3 * k, 10)
        while got < k and pos < len(cand[cut]):
            chunk = [{"patient": p, "stratum": E.NEAR_STRATUM, "statin": False, "s4_bin": None,
                      "ascvd_type": None, "rank": None} for p in cand[cut][pos:pos + batch]]
            pos += batch
            for pk, b in zip(chunk, load_bases(con, chunk, F)):
                if got == k:
                    break
                b.near_cut = cut
                tw = E.c5_near(b, edit_seed, cut)
                if tw and all(t.check and t.p1_stable for t in tw):
                    pk["near_cut"] = cut
                    picks.append(pk)
                    bases.append(b)
                    twins[b.patient] = tw
                    got += 1
        if got < k:
            short[f"{E.NEAR_STRATUM}/{round(cut * 100)}%"] = k - got
    return picks, bases, twins, short


def ldl_target(n: int) -> int:
    """LDL-C threshold C5 sources (two twins each): n // 2."""
    return n // 2


def ldl_candidates(S: dict, F: dict, taken: set, seed: int) -> list[str]:
    """LDL-C threshold C5 sources, ranked: `edits.ldl_source_ok` (off statin, no ASCVD,
    latest LDL-C 160 to < 185, no LDL-C >= 190 ever, label not GAP), not a base patient
    or near-threshold source, and no sampling exclusion (the P1 rule is checked on the
    twins instead)."""
    s = patches.sub_seed(seed, "sample")
    out = []
    for p, f in F.items():
        if p in taken or [e for e in S[p].exclusions if e != "p1_egfr_dependent"]:
            continue
        if E.ldl_source_ok(f, oracle.label(f)):
            out.append(p)
    return sorted(out, key=lambda p: _rank(s, E.LDL_STRATUM, p))


def sample_ldl(con, S, F, taken: set, n: int, seed: int) -> tuple[list, list, dict, dict]:
    """LDL-C threshold sources that yield a valid C5 pair, in rank order, up to
    `ldl_target(n)`. Returns (picks, bases, twins by patient, shortfalls)."""
    edit_seed = patches.sub_seed(seed, "edits")
    cand = ldl_candidates(S, F, taken, seed)
    k = ldl_target(n)
    picks, bases, twins, short = [], [], {}, {}
    got, pos, batch = 0, 0, max(3 * k, 10)
    while got < k and pos < len(cand):
        chunk = [{"patient": p, "stratum": E.LDL_STRATUM, "statin": False, "s4_bin": None,
                  "ascvd_type": None, "rank": None} for p in cand[pos:pos + batch]]
        pos += batch
        for pk, b in zip(chunk, load_bases(con, chunk, F)):
            if got == k:
                break
            tw = E.c5_ldl(b, edit_seed)
            if tw and all(t.check and t.p1_stable for t in tw):
                picks.append(pk)
                bases.append(b)
                twins[b.patient] = tw
                got += 1
    if got < k:
        short[E.LDL_STRATUM] = k - got
    return picks, bases, twins, short


def plan(con, n: int, seed: int) -> dict:
    """Sample base patients and build every twin (no phrases). For counting and building."""
    S, F = strata_and_facts(con)
    picks, short = sample(S, F, n, seed)
    bases = load_bases(con, picks, F)
    twins, rejected = E.build_twins(bases, patches.sub_seed(seed, "edits"))
    npicks, nbases, ntwins, nshort = sample_near(con, S, F, {p["patient"] for p in picks},
                                                 n, seed)
    short.update(nshort)
    twins += [t for b in nbases for t in ntwins[b.patient]]
    taken = {p["patient"] for p in picks + npicks}
    lpicks, lbases, ltwins, lshort = sample_ldl(con, S, F, taken, n, seed)
    short.update(lshort)
    twins += [t for b in lbases for t in ltwins[b.patient]]
    return {"picks": picks + npicks + lpicks, "shortfalls": short,
            "bases": bases + nbases + lbases, "twins": twins, "rejected": rejected}


def counts(p: dict) -> dict:
    tw = p["twins"]
    by_cond = Counter(t.condition for t in tw)
    by_var = Counter(f"{t.condition}/{t.variant}" for t in tw)
    by_stratum = Counter(f"{b.stratum}/{'on' if b.statin else 'off'}" for b in p["bases"])
    st_of = {b.patient: b.stratum for b in p["bases"]}
    cond_stratum = Counter(f"{t.condition}/{st_of[t.patient]}" for t in tw)
    status = Counter(f"{t.condition}/{t.result.status}" for t in tw)
    pairs = len({t.pair for t in tw if t.pair})
    return {"base_patients": len(p["bases"]), "cases": len(tw),
            "bases_by_stratum": dict(sorted(by_stratum.items())),
            "cases_by_condition": {c: by_cond.get(c, 0) for c in E.CONDITIONS},
            "cases_by_condition_variant": dict(sorted(by_var.items())),
            "cases_by_condition_stratum": dict(sorted(cond_stratum.items())),
            "expected_status_by_condition": dict(sorted(status.items())),
            "c5_pairs": pairs, "shortfalls": p["shortfalls"], "rejected": p["rejected"]}


def _json(o) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _csv(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    return buf.getvalue()


def _rows_json(rows: list[dict]) -> list[dict]:
    return [{k: E._val(v) for k, v in r.items()} for r in rows]


def build_case_set(con, out_dir: str | Path, split: str, n: int, seed: int,
                   phrase_lib: dict, phrase_file: str | Path | None = None,
                   source: str | None = None) -> dict:
    """Build and write one case set. Returns the manifest."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "base").mkdir(exist_ok=True)
    p = plan(con, n, seed)
    bases = {b.patient: b for b in p["bases"]}
    twins = p["twins"]
    E.assign_phrases(twins, E.PhrasePool(phrase_lib, patches.sub_seed(seed, "phrases")),
                     {b.patient: b.sex for b in p["bases"]})

    order = {b.patient: i for i, b in enumerate(p["bases"])}
    twins.sort(key=lambda t: (order[t.patient], CONDITION_ORDER[t.condition], t.variant))
    ids = {(t.patient, t.key): case_id(split, seed, t.patient, t.key) for t in twins}

    files: dict[str, str] = {}

    # base tables and notes
    for name in E.ALL_TABLES:
        df = pd.concat([bases[pid].tables[name] for pid in order], ignore_index=True)
        files[f"base/{name}.csv"] = _csv(df)
    coh = pd.DataFrame([{"PATIENT": b.patient, "index_date": b.index_date.isoformat(),
                         "birthdate": b.birthdate.isoformat(), "sex": b.sex, "age": b.facts.age}
                        for b in p["bases"]])
    files["base/cohort.csv"] = _csv(coh)
    base_notes = {}
    lines = []
    for b in p["bases"]:
        ns = render_notes(b.tables)
        base_notes[b.patient] = {x.note_id: x.text for x in ns}
        lines += [_json({"note_id": x.note_id, "PATIENT": x.patient, "ENCOUNTER": x.encounter,
                         "DATE": x.date.isoformat(), "TEXT": x.text}) for x in ns]
    files["base/notes.jsonl"] = "\n".join(lines) + "\n"

    s4_bin = {x["patient"]: x["s4_bin"] for x in p["picks"]}
    cases, deltas, ledger = [], [], []
    for t in twins:
        b = bases[t.patient]
        cid = ids[(t.patient, t.key)]
        changed = []
        if t.condition != "C0":
            ns = render_notes(E.apply(b, t), [pl.plant() for pl in t.plants])
            changed = [{"note_id": x.note_id, "TEXT": x.text} for x in ns
                       if base_notes[t.patient].get(x.note_id) != x.text]
        partner = None
        if t.partner:
            partner = case_id(split, seed, t.patient, t.partner)
        res = t.result
        var = {v: oracle.label(t.facts, reasons=t.reasons() + oracle.reasons_from_facts(t.facts),
                               variant=v) for v in ("S-A", "S-C")}
        cases.append({
            "case_id": cid, "split": split, "base_patient": t.patient, "stratum": b.stratum,
            "base_statin": b.statin, "s4_bin": s4_bin[t.patient],
            "condition": t.condition, "variant": t.variant, "direction": t.direction,
            "base_status": t.base_status, "expected_status": res.status,
            "expected_missing": list(res.missing), "expected_row": res.row,
            "expected_status_S-A": var["S-A"].status, "expected_status_S-C": var["S-C"].status,
            "risk10": res.risk10, "pair_id": (hashlib.sha256(t.pair.encode()).hexdigest()[:12]
                                              if t.pair else None),
            "partner_case_id": partner})
        plants = [{"note_id": pl.note_id, "note_date": pl.note_date.isoformat(),
                   "section": pl.section, "slot": pl.slot, "phrase_type": pl.phrase_type,
                   "phrase_id": pl.phrase_id, "text": pl.text,
                   "reason_category": pl.reason_category} for pl in t.plants]
        deltas.append({"case_id": cid, "patient": t.patient,
                       "delete": {k: list(v) for k, v in sorted(t.deletes.items())},
                       "add": {k: _rows_json(v) for k, v in sorted(t.adds.items())},
                       "plants": plants, "notes": changed})
        pl = t.plants[0] if t.plants else None
        ledger.append({
            "case_id": cid, "base_patient": t.patient, "condition": t.condition,
            "variant": t.variant,
            "deleted_row_ids": ";".join(i for k in sorted(t.deletes) for i in t.deletes[k]),
            "added_row_ids": ";".join(r["row_id"] for k in sorted(t.adds) for r in t.adds[k]),
            "replaced": ";".join(f"{new}>{old}" for new, old in sorted(t.replaces.items())),
            "phrase_type": pl.phrase_type if pl else None,
            "phrase_id": pl.phrase_id if pl else None,
            "note_id": pl.note_id if pl else None,
            "note_date": pl.note_date.isoformat() if pl else None,
            "section": pl.section if pl else None, "slot": pl.slot if pl else None,
            "reason_category": pl.reason_category if pl else None,
            "notes_changed": len(changed),
            "base_status": t.base_status, "expected_status": res.status,
            "expected_missing": ";".join(res.missing), "direction": t.direction,
            "direction_check": "pass" if t.check else "fail",
            "p1_egfr_stable": t.p1_stable,
            "detail": _json(t.detail) if t.detail else None})
    files["cases.jsonl"] = "\n".join(_json(c) for c in cases) + "\n"
    files["deltas.jsonl"] = "\n".join(_json(d) for d in deltas) + "\n"
    files["edit_ledger.csv"] = _csv(pd.DataFrame(ledger))

    for name, text in files.items():
        (out / name).write_text(text)
    meta = dict(con.execute("select key, value from meta").fetchall())
    pf = Path(phrase_file) if phrase_file else None
    manifest = {
        "split": split, "name": out.name, "master_seed": seed, "n_per_stratum": n,
        "sub_seeds": {k: str(patches.sub_seed(seed, k)) for k in SUB_SEED_LABELS},
        "source_db": {"path": source, "seed": meta.get("seed"), "patches": meta.get("patches"),
                      "p1_sub_seed": meta.get("p1_sub_seed"),
                      "p2_sub_seed": meta.get("p2_sub_seed"),
                      "row_id_key": meta.get("row_id_key")},
        "phrases": {"version": phrase_lib.get("version"),
                    "file": pf.name if pf else None,
                    "sha256": hashlib.sha256(pf.read_bytes()).hexdigest() if pf else None},
        "counts": counts(p),
        "files": {name: hashlib.sha256(text.encode()).hexdigest()
                  for name, text in sorted(files.items())},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


# ---------------------------------------------------------------- loader
def _read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.astype(object).where(df != "", None)


@lru_cache(maxsize=4)
def _case_set(case_dir: str) -> dict:
    d = Path(case_dir)
    base = {n: _read_csv(d / "base" / f"{n}.csv") for n in E.ALL_TABLES}
    notes = pd.read_json(d / "base" / "notes.jsonl", lines=True, dtype=False)
    cases = {c["case_id"]: c for c in map(json.loads, (d / "cases.jsonl").read_text().splitlines())}
    deltas = {x["case_id"]: x for x in map(json.loads, (d / "deltas.jsonl").read_text().splitlines())}
    coh = pd.read_csv(d / "base" / "cohort.csv", dtype=str)
    return {"base": base, "notes": notes, "cases": cases, "deltas": deltas, "cohort": coh}


def read_cases(case_dir: str | Path) -> list[dict]:
    return list(_case_set(str(Path(case_dir)))["cases"].values())


def case_tables(case_dir: str | Path, cid: str) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict]:
    """Edited tables, notes and delta of one case (pandas)."""
    cs = _case_set(str(Path(case_dir)))
    dl = cs["deltas"][cid]
    pid = dl["patient"]
    out = {}
    for name, df in cs["base"].items():
        col = "Id" if name == "patients" else "PATIENT"
        t = df[df[col] == pid]
        dele = set(dl["delete"].get(name, ()))
        if dele:
            t = t[~t.row_id.isin(dele)]
        adds = dl["add"].get(name, [])
        if adds:
            t = pd.concat([t, pd.DataFrame(adds, columns=df.columns, dtype=object)],
                          ignore_index=True)
        if dele or adds:
            from .load import TABLES
            dcol = TABLES[name][2]
            t = t.sort_values(([dcol] if dcol else []) + ["row_id"], kind="stable")
        out[name] = t.reset_index(drop=True)
    nt = cs["notes"][cs["notes"].PATIENT == pid].copy()
    for ch in dl["notes"]:
        nt.loc[nt.note_id == ch["note_id"], "TEXT"] = ch["TEXT"]
    return out, nt.reset_index(drop=True), dl


def materialize(case_dir: str | Path, cid: str) -> duckdb.DuckDBPyConnection:
    """One case as an in-memory DuckDB: the 7 tables, `cohort` and `notes` (VARCHAR columns)."""
    tables, nt, dl = case_tables(case_dir, cid)
    cs = _case_set(str(Path(case_dir)))
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    for name, df in list(tables.items()) + [("notes", nt.astype(str))]:
        cols = ", ".join(f'"{c}" varchar' for c in df.columns)
        con.execute(f"create table {name} ({cols})")
        if len(df):
            con.register("_df", df)
            con.execute(f"insert into {name} select * from _df")
            con.unregister("_df")
    coh = cs["cohort"][cs["cohort"].PATIENT == dl["patient"]]
    con.register("_coh", coh)
    con.execute("create table cohort as select PATIENT, cast(index_date as date) as index_date, "
                "cast(birthdate as date) as birthdate, sex, cast(age as integer) as age from _coh")
    con.unregister("_coh")
    return con


def case_reasons(case_dir: str | Path, cid: str) -> tuple[oracle.Reason, ...]:
    """Reasons planted by the case (from the ledger), for the oracle."""
    from datetime import date
    dl = _case_set(str(Path(case_dir)))["deltas"][cid]
    return tuple(oracle.Reason(p["reason_category"], date.fromisoformat(p["note_date"]),
                               p["note_id"]) for p in dl["plants"] if p["reason_category"])


def case_plants(case_dir: str | Path, cid: str) -> list[Plant]:
    dl = _case_set(str(Path(case_dir)))["deltas"][cid]
    return [Plant(p["note_id"], p["section"], p["slot"], p["text"], p["phrase_id"])
            for p in dl["plants"]]


def oracle_on_case(case_dir: str | Path, cid: str) -> oracle.OracleResult:
    con = materialize(case_dir, cid)
    pid = _case_set(str(Path(case_dir)))["deltas"][cid]["patient"]
    f = facts.patient_facts(con, pid)
    con.close()
    return oracle.label(f, reasons=case_reasons(case_dir, cid) + oracle.reasons_from_facts(f))
