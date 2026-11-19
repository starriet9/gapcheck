"""Scoring (SPEC 7).

Ground truth: `expected_status` of each case (oracle label for C0, ledger
label for C1-C5). A case is correct if the output is schema-valid and its
status is the expected status; C1 also needs the deleted input (the case
variant) in `missing`.

Primary outcome: exact-status accuracy macro-averaged over the eight
conditions. Intervals: paired cluster bootstrap. Base patients (and
near-threshold C5 sources) are resampled with replacement within stratum x
base statin status, carrying all their cases; every system is scored on the
same resamples. A condition with no case in a resample is left out of that
resample's macro-average.

Secondary outcomes 2-8 are computed where the run logs allow (`secondary`).
Sensitivity analyses S-A and S-C relabel both the truth (the case file's
`expected_status_S-A`/`_S-C`) and the prediction (GAP or DOCUMENTED_REASON
with benefit_group 8, or 8 and 9, becomes DISCUSS).
"""
from __future__ import annotations

import json
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from . import cases as CS
from .edits import CONDITIONS
from .oracle import CANNOT_DETERMINE, DISCUSS, DOCUMENTED_REASON, GAP

N_BOOT = 10_000
BOOT_SEED = 20261120
RELABEL = {"S-A": ("8",), "S-C": ("8", "9")}
MIN_OVERLAP = 15  # characters in common for C3a citation overlap


# ---------------------------------------------------------------- io
def read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def load_run(run_dir: str | Path) -> dict[str, dict[str, dict]]:
    """{system: {case_id: record}} from <run_dir>/<system>.jsonl."""
    out = {}
    for p in sorted(Path(run_dir).glob("*.jsonl")):
        out[p.stem] = {r["case_id"]: r for r in read_jsonl(p)}
    return out


# ---------------------------------------------------------------- correctness
def status_of(rec: dict | None, variant: str = "main") -> str | None:
    if rec is None or not rec.get("schema_valid") or rec.get("output") is None:
        return None
    o = rec["output"]
    s = o["status"]
    if variant in RELABEL and s in (GAP, DOCUMENTED_REASON) and o["benefit_group"] in RELABEL[variant]:
        return DISCUSS
    return s


def correct(case: dict, rec: dict | None, variant: str = "main") -> bool:
    s = status_of(rec, variant)
    want = case["expected_status"] if variant == "main" else case[f"expected_status_{variant}"]
    if s != want:
        return False
    if case["condition"] == "C1":
        return case["variant"] in rec["output"]["missing"]
    return True


# ---------------------------------------------------------------- bootstrap
class Design:
    """Cases grouped by cluster (base patient) and stratum (stratum x statin)."""

    def __init__(self, case_list: list[dict]):
        self.cases = case_list
        self.ids = [c["case_id"] for c in case_list]
        clusters = sorted({c["base_patient"] for c in case_list})
        self.cl_index = {p: i for i, p in enumerate(clusters)}
        stratum_of = {}
        for c in case_list:
            stratum_of[c["base_patient"]] = f"{c['stratum']}/{bool(c['base_statin'])}"
        self.strata = defaultdict(list)
        for p, i in self.cl_index.items():
            self.strata[stratum_of[p]].append(i)
        self.conds = list(CONDITIONS)
        nc, nk = len(clusters), len(self.conds)
        self.n = np.zeros((nc, nk))
        ci = {k: j for j, k in enumerate(self.conds)}
        self.case_cl = np.array([self.cl_index[c["base_patient"]] for c in case_list])
        self.case_k = np.array([ci[c["condition"]] for c in case_list])
        np.add.at(self.n, (self.case_cl, self.case_k), 1)

    def sums(self, ok: np.ndarray) -> np.ndarray:
        s = np.zeros_like(self.n)
        np.add.at(s, (self.case_cl, self.case_k), ok.astype(float))
        return s

    def weights(self, n_boot: int, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        W = np.zeros((n_boot, len(self.cl_index)))
        for key in sorted(self.strata):
            idx = np.array(self.strata[key])
            draw = rng.integers(0, len(idx), size=(n_boot, len(idx)))
            for j in range(len(idx)):
                np.add.at(W, (np.arange(n_boot), idx[draw[:, j]]), 1)
        return W


def _acc(W: np.ndarray, s: np.ndarray, n: np.ndarray) -> np.ndarray:
    num, den = W @ s, W @ n
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def _ci(x: np.ndarray) -> list[float]:
    x = x[~np.isnan(x)]
    return [float(np.percentile(x, 2.5)), float(np.percentile(x, 97.5))] if len(x) else [None, None]


def accuracy_table(design: Design, ok_by_system: dict[str, np.ndarray],
                   pairs=(("A2", "L1"), ("A1", "L1"), ("A2", "A1")),
                   n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """Per-condition accuracy, macro-average and paired differences, with 95% intervals."""
    W = design.weights(n_boot, seed)
    one = np.ones((1, design.n.shape[0]))
    out = {"n_boot": n_boot, "seed": seed, "conditions": design.conds,
           "n_cases": {k: int(design.n[:, j].sum()) for j, k in enumerate(design.conds)},
           "systems": {}, "differences": {}}
    macro_boot = {}
    for name, ok in ok_by_system.items():
        s = design.sums(ok)
        point = _acc(one, s, design.n)[0]
        boot = _acc(W, s, design.n)
        with np.errstate(all="ignore"):
            macro = np.nanmean(boot, axis=1)
        macro_boot[name] = (np.nanmean(point), macro, point, boot)
        out["systems"][name] = {
            "macro": {"est": float(np.nanmean(point)), "ci": _ci(macro)},
            "by_condition": {k: {"est": None if np.isnan(point[j]) else float(point[j]),
                                 "ci": _ci(boot[:, j])} for j, k in enumerate(design.conds)},
        }
    for a, b in pairs:
        if a in macro_boot and b in macro_boot:
            d = macro_boot[a][1] - macro_boot[b][1]
            out["differences"][f"{a}-{b}"] = {
                "est": float(macro_boot[a][0] - macro_boot[b][0]), "ci": _ci(d)}
    return out


def _prop(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": (k / n) if n else None}


def _boot_prop(design: Design, mask: np.ndarray, hit: np.ndarray, W: np.ndarray) -> dict:
    """A rate over cases `mask` with its cluster-bootstrap interval."""
    n_cl = np.zeros(design.n.shape[0])
    k_cl = np.zeros(design.n.shape[0])
    np.add.at(n_cl, design.case_cl[mask], 1)
    np.add.at(k_cl, design.case_cl[mask], hit[mask].astype(float))
    n, k = int(mask.sum()), int(hit[mask].sum())
    with np.errstate(invalid="ignore", divide="ignore"):
        b = (W @ k_cl) / (W @ n_cl)
    return {**_prop(k, n), "ci": _ci(b) if n else [None, None]}


def _overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size >= MIN_OVERLAP


def secondary(design: Design, recs: dict[str, dict], case_dir: str | Path | None,
              W: np.ndarray) -> dict:
    """Secondary outcomes 2-6 and 8 for one system (7 needs several runs: `stability`)."""
    cs = design.cases
    st = [status_of(recs.get(c["case_id"])) for c in cs]
    exp = [c["expected_status"] for c in cs]
    cond = np.array([c["condition"] for c in cs])
    ok = np.array([correct(c, recs.get(c["case_id"])) for c in cs])
    st_a, exp_a = np.array(st, dtype=object), np.array(exp, dtype=object)
    out = {}
    pos = exp_a == GAP
    out["gap_sensitivity"] = _boot_prop(design, pos, st_a == GAP, W)
    out["gap_specificity"] = _boot_prop(design, ~pos, st_a != GAP, W)
    definite = np.array([s is not None and s != CANNOT_DETERMINE for s in st])
    abst = np.array([s == CANNOT_DETERMINE for s in st])
    out["c1_fabricated_certainty"] = _boot_prop(design, cond == "C1", definite, W)
    out["c2a_over_abstention"] = _boot_prop(design, cond == "C2a", abst, W)
    out["c2b_over_abstention"] = _boot_prop(design, cond == "C2b", abst, W)
    # C5 pair consistency
    by_id = {c["case_id"]: (c, k) for k, c in enumerate(cs)}
    pairs = set()
    for c in cs:
        if c["condition"] == "C5" and c.get("partner_case_id") in by_id:
            pairs.add(tuple(sorted((c["case_id"], c["partner_case_id"]))))
    pairs = sorted(pairs)
    pair_ok = np.array([ok[by_id[a][1]] and ok[by_id[b][1]] for a, b in pairs], dtype=bool)
    pair_cl = np.array([design.cl_index[by_id[a][0]["base_patient"]] for a, _ in pairs], dtype=int)
    if len(pairs):
        n_cl, k_cl = np.zeros(design.n.shape[0]), np.zeros(design.n.shape[0])
        np.add.at(n_cl, pair_cl, 1)
        np.add.at(k_cl, pair_cl, pair_ok.astype(float))
        with np.errstate(invalid="ignore", divide="ignore"):
            b = (W @ k_cl) / (W @ n_cl)
        out["c5_pair_consistency"] = {**_prop(int(pair_ok.sum()), len(pairs)), "ci": _ci(b)}
    else:
        out["c5_pair_consistency"] = {**_prop(0, 0), "ci": [None, None]}
    # C3a miss; C3b and C4 answer change against the system's own C0 answer
    out["c3a_miss"] = _boot_prop(design, cond == "C3a",
                                 np.array([s != DOCUMENTED_REASON for s in st]), W)
    c0 = {c["base_patient"]: st[k] for k, c in enumerate(cs) if c["condition"] == "C0"}
    changed = np.array([c["base_patient"] in c0 and st[k] != c0[c["base_patient"]]
                        for k, c in enumerate(cs)])
    for k in ("C3b", "C4"):
        out[f"{k.lower()}_answer_change"] = _boot_prop(design, cond == k, changed, W)
    if case_dir is not None:
        hit = np.zeros(len(cs), dtype=bool)
        for k, c in enumerate(cs):
            if c["condition"] != "C3a":
                continue
            rec = recs.get(c["case_id"])
            if status_of(rec) is None:
                continue
            plants = CS.case_plants(case_dir, c["case_id"])
            for it in rec["output"]["evidence"]:
                if it["claim"] == "reason" and it["support"] == "DOCUMENTED" \
                        and isinstance(it["source"], str):
                    if any(it["source"] == p.note_id and _overlap(it.get("quote", ""), p.text)
                           for p in plants):
                        hit[k] = True
        out["c3a_citation_overlap"] = _boot_prop(design, cond == "C3a", hit, W)
    # unsupported claims (checks 1 or 4 on the final answer, from the run's audit)
    bad = tot = 0
    for c in cs:
        a = (recs.get(c["case_id"]) or {}).get("audit")
        if a:
            bad += a["unsupported"][0]
            tot += a["unsupported"][1]
    out["unsupported_claims"] = _prop(bad, tot)
    # cost and fallbacks
    rs = [recs[c["case_id"]] for c in cs if c["case_id"] in recs]
    for key in ("tool_calls", "model_calls", "seconds"):
        v = np.array([r.get(key, 0) or 0 for r in rs], dtype=float)
        out[f"{key}_per_case"] = {"mean": float(v.mean()) if len(v) else None,
                                  "median": float(np.median(v)) if len(v) else None}
    for key in ("input_tokens", "output_tokens"):
        v = np.array([(r.get("tokens") or {}).get(key, 0) for r in rs], dtype=float)
        out[f"{key}_per_case"] = {"mean": float(v.mean()) if len(v) else None,
                                  "median": float(np.median(v)) if len(v) else None}
    out["schema_invalid"] = sum(1 for r in rs if not r.get("schema_valid"))
    ff = defaultdict(int)
    for c in cs:
        if (recs.get(c["case_id"]) or {}).get("forced_fallback"):
            ff[c["condition"]] += 1
    out["forced_fallbacks"] = {k: ff.get(k, 0) for k in CONDITIONS}
    return out


def stability(runs: list[dict[str, dict]], case_ids: list[str]) -> dict:
    """Share of cases with the same status in every run (SPEC 7, outcome 7)."""
    same = sum(1 for cid in case_ids
               if len({status_of(r.get(cid)) for r in runs}) == 1)
    return _prop(same, len(case_ids))


def score(case_dir: str | Path, run_dir: str | Path, extra_runs=(), n_boot: int = N_BOOT,
          seed: int = BOOT_SEED) -> dict:
    case_list = CS.read_cases(case_dir)
    design = Design(case_list)
    run = load_run(run_dir)
    out = {"case_set": Path(case_dir).name, "run": Path(run_dir).name,
           "n_cases": len(case_list)}
    for variant in ("main", "S-A", "S-C"):
        ok = {s: np.array([correct(c, recs.get(c["case_id"]), variant) for c in case_list])
              for s, recs in run.items()}
        out[f"accuracy_{variant}"] = accuracy_table(design, ok, n_boot=n_boot, seed=seed)
    W = design.weights(n_boot, seed)
    out["secondary"] = {s: secondary(design, recs, case_dir, W) for s, recs in run.items()}
    if extra_runs:
        others = [load_run(r) for r in extra_runs]
        ids = [c["case_id"] for c in case_list]
        out["stability"] = {s: stability([run[s]] + [o.get(s, {}) for o in others], ids)
                            for s in run if s in ("L1", "A2", "A1")}
    return out
