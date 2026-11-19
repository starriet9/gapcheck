"""Scoring (PROTOCOL.md secs. 7-10; SPEC 7).

Ground truth: `expected_status` of each case (oracle label for C0, ledger
label for C1-C5). A case is correct if the output is schema-valid and its
status is the expected status; C1 also needs the deleted input (the case
variant) in `missing`. A case with no record, or no schema-valid output, is
wrong.

Primary outcome: exact-status accuracy macro-averaged over the eight
conditions. Intervals: paired cluster bootstrap. Base patients (and
near-threshold C5 sources) are resampled with replacement within stratum x
base statin status, carrying all their cases; every system and every
comparison uses the same resamples (one weight matrix per score). A
condition with no case in a resample is left out of that resample's
macro-average. Percentile 95% intervals; point estimates from the full set.

Secondary outcomes 2-8 are computed where the run logs allow (`secondary`).
Answer change (outcome 5): a C2a, C2b, C3b or C4 case whose status differs
from the same system's status on the same patient's C0 case; an invalid or
missing output on either case counts as changed (so two invalid outputs are
a change, not agreement). Sensitivity analyses S-A and S-C relabel both the
truth (the case file's `expected_status_S-A`/`_S-C`) and the prediction (GAP
or DOCUMENTED_REASON with benefit_group 8, or 8 and 9, becomes DISCUSS).

Results JSON (`results(score_dict)`, written by scripts/score_runs.py as
results.json next to score.json; score.json holds the same keys plus the
full tables). Schema "gapcheck-results/1". Rates and differences are
proportions (0-1); `ci` is [lo, hi] (None where not estimable); counts are
{k, n, rate, ci}. Stable keys:

  schema, case_set, run, n_cases
  headline.primary            {A2: {est, ci}, L1: {est, ci},
                               difference: {est, ci}  (A2 - L1, macro),
                               A1-L1: {est, ci}, A2-A1: {est, ci},
                               verdict: supported | unresolved | contradicted}
  headline.answered_anyway    {L1|A1|A2: {k, n, rate, ci}}  C1 definite status
  headline.changed_when_must_not
                              {L1|A1|A2: {k, n, rate, ci}}  pooled answer
                              change in C2a, C2b, C3b, C4 vs own C0
  hypotheses.H1               {comparison: "A2-L1", est, ci, verdict}
  hypotheses.H2               {comparison: "A2-R1", conditions, est, ci,
                               verdict, A2: {est, ci}, R1: {est, ci}}
  hypotheses.H3               {part1: {comparison: "A2-A1",
                                       measure: "c1_fabricated_certainty",
                                       est, ci, verdict},
                               part2: {margin: 0.05, C2a: {est, ci},
                                       C2b: {est, ci},
                                       verdict: met | not met | unresolved},
                               verdict}
  weakest_A2                  {condition, k, n, est, ci}
  flags.ceiling               bool (L1 and A2 macro both >= 0.95)
  flags.no_valid_output       {L1|A1|A2: {k, n, rate, over_threshold}}
  sentences                   {primary, ceiling, schema_invalid: [..],
                               weakest}; the fixed sentences of PROTOCOL.md
                               sec. 10 with numbers filled in (percent and
                               points to one decimal, U+2212 for minus);
                               None (or []) when not applicable or when a
                               system it needs is not in the run.

Any value is None when a system it needs is not in the run. H3: "supported"
needs part 1 supported and part 2 met; "contradicted" if part 1 is
contradicted or part 2 is not met; otherwise "unresolved".
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from . import cases as CS
from .edits import CONDITIONS
from .oracle import CANNOT_DETERMINE, DISCUSS, DOCUMENTED_REASON, GAP
from .tools import TOOL_SPECS

N_BOOT = 10_000
BOOT_SEED = 20261120
RELABEL = {"S-A": ("8",), "S-C": ("8", "9")}
MIN_OVERLAP = 15  # characters in common for C3a citation overlap
MUST_NOT_CHANGE = ("C2a", "C2b", "C3b", "C4")   # PROTOCOL 8.5 and headline 3
NOTE_CONDITIONS = ("C3a", "C3b", "C4")          # H2
H3_MARGIN = 0.05                                # H3 part 2, proportion
CEILING = 0.95                                  # PROTOCOL 10, ceiling sentence
INVALID_SHARE = 0.05                            # PROTOCOL 10, format-failure sentence
LLM_SYSTEMS = ("L1", "A1", "A2")
RESULTS_SCHEMA = "gapcheck-results/1"
EPS = 1e-12  # tolerance for comparing interval limits with 0 and with the margin
# Tool-call parser failures (PROTOCOL 6): stop reasons that signal a tool call,
# and Qwen/Hermes-style call markup left in the text when no call was parsed.
TOOL_CALL_STOPS = ("tool_calls", "tool_use", "function_call")
TOOL_CALL_MARKUP = ("<tool_call>", "<function=")
REQUIRED_ARGS = {n: tuple(s["parameters"].get("required", ())) for n, s in TOOL_SPECS.items()}


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
                   n_boot: int = N_BOOT, seed: int = BOOT_SEED,
                   W: np.ndarray | None = None) -> dict:
    """Per-condition accuracy (k, n, estimate), macro-average and paired differences,
    with 95% intervals. `W` (resample weights) defaults to `design.weights(n_boot, seed)`."""
    if W is None:
        W = design.weights(n_boot, seed)
    one = np.ones((1, design.n.shape[0]))
    out = {"n_boot": int(W.shape[0]), "seed": seed, "conditions": design.conds,
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
            "by_condition": {k: {"k": int(s[:, j].sum()), "n": int(design.n[:, j].sum()),
                                 "est": None if np.isnan(point[j]) else float(point[j]),
                                 "ci": _ci(boot[:, j])} for j, k in enumerate(design.conds)},
        }
    for a, b in pairs:
        if a in macro_boot and b in macro_boot:
            d = macro_boot[a][1] - macro_boot[b][1]
            out["differences"][f"{a}-{b}"] = {
                "est": float(macro_boot[a][0] - macro_boot[b][0]), "ci": _ci(d)}
    return out


def macro_subset(design: Design, ok_a: np.ndarray, ok_b: np.ndarray | None,
                 conds, W: np.ndarray) -> dict:
    """Accuracy macro-averaged over `conds` for system a (and b), and the paired
    difference a - b on the same resamples (H2)."""
    idx = [design.conds.index(k) for k in conds]
    one = np.ones((1, design.n.shape[0]))

    def one_system(ok):
        s = design.sums(ok)
        point = _acc(one, s, design.n)[0][idx]
        with np.errstate(all="ignore"):
            boot = np.nanmean(_acc(W, s, design.n)[:, idx], axis=1)
        return (float(np.nanmean(point)) if not np.all(np.isnan(point)) else None), boot

    ea, ba = one_system(ok_a)
    out = {"conditions": list(conds), "a": {"est": ea, "ci": _ci(ba)}}
    if ok_b is not None:
        eb, bb = one_system(ok_b)
        out["b"] = {"est": eb, "ci": _ci(bb)}
        out["difference"] = {"est": None if ea is None or eb is None else ea - eb,
                             "ci": _ci(ba - bb)}
    return out


def _prop(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": (k / n) if n else None}


def _cluster_sums(design: Design, mask: np.ndarray, hit: np.ndarray) -> tuple:
    n_cl = np.zeros(design.n.shape[0])
    k_cl = np.zeros(design.n.shape[0])
    np.add.at(n_cl, design.case_cl[mask], 1)
    np.add.at(k_cl, design.case_cl[mask], hit[mask].astype(float))
    return n_cl, k_cl


def _boot_prop(design: Design, mask: np.ndarray, hit: np.ndarray, W: np.ndarray) -> dict:
    """A rate over cases `mask` with its cluster-bootstrap interval."""
    n_cl, k_cl = _cluster_sums(design, mask, hit)
    n, k = int(mask.sum()), int(hit[mask].sum())
    with np.errstate(invalid="ignore", divide="ignore"):
        b = (W @ k_cl) / (W @ n_cl)
    return {**_prop(k, n), "ci": _ci(b) if n else [None, None]}


def _boot_diff_prop(design: Design, mask: np.ndarray, hit_a: np.ndarray, hit_b: np.ndarray,
                    W: np.ndarray) -> dict:
    """Paired difference of two systems' rates over the same cases `mask` (a - b)."""
    n_cl, ka = _cluster_sums(design, mask, hit_a)
    _, kb = _cluster_sums(design, mask, hit_b)
    n = int(mask.sum())
    if not n:
        return {"n": 0, "est": None, "ci": [None, None]}
    with np.errstate(invalid="ignore", divide="ignore"):
        b = (W @ ka - W @ kb) / (W @ n_cl)
    return {"n": n, "est": float((hit_a[mask].sum() - hit_b[mask].sum()) / n), "ci": _ci(b)}


def _overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size >= MIN_OVERLAP


def answer_changed(rec: dict | None, c0_rec: dict | None) -> bool:
    """PROTOCOL 8.5: the status differs from the same system's status on the same
    patient's C0 case; an invalid or missing output on either case counts as changed."""
    a, b = status_of(rec), status_of(c0_rec)
    return a is None or b is None or a != b


def parser_failures(rec: dict | None) -> dict[str, int]:
    """Tool-call parser failures in one record's trace (PROTOCOL 6): a model reply that
    signals a tool call (stop reason or call markup in the text) and carries no call; a
    call with empty arguments to a tool that has required parameters; arguments that did
    not parse (the backend keeps them as {"_unparsed": ...})."""
    out = {"signal_without_call": 0, "empty_required_arguments": 0, "unparsed_arguments": 0}
    for t in (rec or {}).get("trace") or []:
        if t.get("type") != "model":
            continue
        calls = t.get("tool_calls") or []
        text = t.get("text") or ""
        if not calls and (t.get("stop_reason") in TOOL_CALL_STOPS
                          or any(m in text for m in TOOL_CALL_MARKUP)):
            out["signal_without_call"] += 1
        for c in calls:
            args = c.get("arguments")
            if isinstance(args, dict) and "_unparsed" in args:
                out["unparsed_arguments"] += 1
            elif not args and REQUIRED_ARGS.get(c.get("name")):
                out["empty_required_arguments"] += 1
    return out


def case_flags(design: Design, recs: dict[str, dict]) -> dict[str, np.ndarray]:
    """Per-case arrays for one system: ok, definite, abstained, changed, no_valid."""
    cs = design.cases
    st = [status_of(recs.get(c["case_id"])) for c in cs]
    c0_id = {c["base_patient"]: c["case_id"] for c in cs if c["condition"] == "C0"}
    return {
        "status": np.array(st, dtype=object),
        "ok": np.array([correct(c, recs.get(c["case_id"])) for c in cs], dtype=bool),
        "definite": np.array([s is not None and s != CANNOT_DETERMINE for s in st], dtype=bool),
        "abstained": np.array([s == CANNOT_DETERMINE for s in st], dtype=bool),
        "no_valid": np.array([s is None for s in st], dtype=bool),
        # a base patient without a C0 case in the scored list has no reference answer:
        # treated as a missing output, so counted as changed
        "changed": np.array([answer_changed(recs.get(c["case_id"]),
                                            recs.get(c0_id.get(c["base_patient"])))
                             for c in cs], dtype=bool),
    }


def secondary(design: Design, recs: dict[str, dict], case_dir: str | Path | None,
              W: np.ndarray) -> dict:
    """Secondary outcomes 2-6 and 8 for one system (7 needs several runs: `stability`)."""
    cs = design.cases
    f = case_flags(design, recs)
    st_a, ok = f["status"], f["ok"]
    exp_a = np.array([c["expected_status"] for c in cs], dtype=object)
    cond = np.array([c["condition"] for c in cs])
    out = {}
    pos = exp_a == GAP
    out["gap_sensitivity"] = _boot_prop(design, pos, st_a == GAP, W)
    out["gap_specificity"] = _boot_prop(design, ~pos, st_a != GAP, W)
    out["c1_fabricated_certainty"] = _boot_prop(design, cond == "C1", f["definite"], W)
    out["c2a_over_abstention"] = _boot_prop(design, cond == "C2a", f["abstained"], W)
    out["c2b_over_abstention"] = _boot_prop(design, cond == "C2b", f["abstained"], W)
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
    # C3a miss; answer change against the system's own C0 answer, each and pooled
    out["c3a_miss"] = _boot_prop(design, cond == "C3a",
                                 np.array([s != DOCUMENTED_REASON for s in st_a]), W)
    out["answer_change"] = {k: _boot_prop(design, cond == k, f["changed"], W)
                            for k in MUST_NOT_CHANGE}
    out["answer_change"]["pooled"] = _boot_prop(design, np.isin(cond, MUST_NOT_CHANGE),
                                                f["changed"], W)
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
    bad, tot = np.zeros(len(cs)), np.zeros(len(cs))
    for k, c in enumerate(cs):
        a = (recs.get(c["case_id"]) or {}).get("audit")
        if a:
            bad[k], tot[k] = a["unsupported"][0], a["unsupported"][1]
    b_cl, t_cl = np.zeros(design.n.shape[0]), np.zeros(design.n.shape[0])
    np.add.at(b_cl, design.case_cl, bad)
    np.add.at(t_cl, design.case_cl, tot)
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = (W @ b_cl) / (W @ t_cl)
    out["unsupported_claims"] = {**_prop(int(bad.sum()), int(tot.sum())),
                                 "ci": _ci(boot) if tot.sum() else [None, None]}
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
    out["no_valid_output"] = _prop(int(f["no_valid"].sum()), len(cs))
    ff = defaultdict(int)
    for c in cs:
        if (recs.get(c["case_id"]) or {}).get("forced_fallback"):
            ff[c["condition"]] += 1
    out["forced_fallbacks"] = {k: ff.get(k, 0) for k in CONDITIONS}
    pf = [parser_failures(recs.get(c["case_id"])) for c in cs]
    out["parser_failures"] = {
        "cases_with_any": _prop(sum(1 for x in pf if any(x.values())), len(cs)),
        "events": {k: sum(x[k] for x in pf) for k in
                   ("signal_without_call", "empty_required_arguments", "unparsed_arguments")}}
    return out


def stability(runs: list[dict[str, dict]], case_ids: list[str], design: Design | None = None,
              W: np.ndarray | None = None) -> dict:
    """Share of cases with the same status in every run (PROTOCOL 8, outcome 7); with a
    design and weights, also its cluster-bootstrap interval."""
    same = np.array([len({status_of(r.get(cid)) for r in runs}) == 1 for cid in case_ids],
                    dtype=bool)
    if design is None or W is None:
        return _prop(int(same.sum()), len(case_ids))
    pos = {c["case_id"]: k for k, c in enumerate(design.cases)}
    hit = np.zeros(len(design.cases), dtype=bool)
    mask = np.zeros(len(design.cases), dtype=bool)
    for cid, s in zip(case_ids, same):
        mask[pos[cid]], hit[pos[cid]] = True, s
    return _boot_prop(design, mask, hit, W)


# ---------------------------------------------------------------- hypotheses
def _ci_ok(ci) -> bool:
    return ci is not None and len(ci) == 2 and None not in ci


def verdict(ci, direction: int = 1) -> str:
    """PROTOCOL 1: 'supported' if the interval excludes 0 in the stated direction
    (+1: a - b > 0; -1: a - b < 0), 'contradicted' if it excludes 0 in the other,
    'unresolved' if it includes 0 (or cannot be estimated)."""
    if not _ci_ok(ci):
        return "unresolved"
    lo, hi = ci
    if lo > EPS:
        return "supported" if direction > 0 else "contradicted"
    if hi < -EPS:
        return "contradicted" if direction > 0 else "supported"
    return "unresolved"


def margin_verdict(cis: list, margin: float = H3_MARGIN) -> str:
    """H3 part 2: 'met' if every upper limit of (A2 - A1) is at most the margin, 'not met'
    if any lower limit is above it, otherwise 'unresolved'."""
    cis = [ci for ci in cis if _ci_ok(ci)]
    if not cis:
        return "unresolved"
    if all(hi <= margin + EPS for _, hi in cis):
        return "met"
    if any(lo > margin + EPS for lo, _ in cis):
        return "not met"
    return "unresolved"


def h3_verdict(part1: str, part2: str) -> str:
    if part1 == "supported" and part2 == "met":
        return "supported"
    if part1 == "contradicted" or part2 == "not met":
        return "contradicted"
    return "unresolved"


def hypotheses(design: Design, flags: dict[str, dict], acc: dict, W: np.ndarray) -> dict:
    """H1-H3 of PROTOCOL 1, on the same resamples as the accuracy table."""
    out = {"H1": None, "H2": None, "H3": None}
    d = acc["differences"].get("A2-L1")
    if d is not None:
        out["H1"] = {"comparison": "A2-L1", "measure": "macro accuracy, 8 conditions",
                     **d, "verdict": verdict(d["ci"])}
    if "A2" in flags and "R1" in flags:
        m = macro_subset(design, flags["A2"]["ok"], flags["R1"]["ok"], NOTE_CONDITIONS, W)
        out["H2"] = {"comparison": "A2-R1", "conditions": list(NOTE_CONDITIONS),
                     "measure": "macro accuracy over the note conditions",
                     **m["difference"], "verdict": verdict(m["difference"]["ci"]),
                     "A2": m["a"], "R1": m["b"]}
    if "A2" in flags and "A1" in flags:
        cond = np.array([c["condition"] for c in design.cases])
        a2, a1 = flags["A2"], flags["A1"]
        p1 = _boot_diff_prop(design, cond == "C1", a2["definite"], a1["definite"], W)
        p1 = {"comparison": "A2-A1", "measure": "c1_fabricated_certainty", **p1,
              "verdict": verdict(p1["ci"], direction=-1)}
        p2 = {"margin": H3_MARGIN, "measure": "over_abstention"}
        for k in ("C2a", "C2b"):
            if (cond == k).any():
                p2[k] = _boot_diff_prop(design, cond == k, a2["abstained"], a1["abstained"], W)
        p2["verdict"] = margin_verdict([p2[k]["ci"] for k in ("C2a", "C2b") if k in p2])
        out["H3"] = {"part1": p1, "part2": p2, "verdict": h3_verdict(p1["verdict"], p2["verdict"])}
    return out


# ---------------------------------------------------------------- headline and sentences
def rank_conditions(by_condition: dict) -> list[str]:
    """Conditions with cases, lowest accuracy first; ties in the order C0 ... C5."""
    have = [k for k in CONDITIONS if (by_condition.get(k) or {}).get("est") is not None]
    return sorted(have, key=lambda k: (by_condition[k]["est"], CONDITIONS.index(k)))


MINUS = "\u2212"
# PROTOCOL.md sec. 10, verbatim (tests check that each is a substring of PROTOCOL.md).
# Placeholders are filled in order of appearance.
SENTENCE_ABOVE = (
    "On the primary outcome, the agent with the verifier (A2) was more accurate than the "
    "single call (L1): {a2}% against {l1}%, a difference of {d} percentage points (95% CI "
    "{lo} to {hi}). H1 is supported. A1 \u2212 L1 was {d1} ({lo1} to {hi1}); A2 \u2212 A1 was "
    "{d2} ({lo2} to {hi2}).")
SENTENCE_FLAT = (
    "On the primary outcome, A2 and L1 were not separated: {a2}% against {l1}%, a difference "
    "of {d} percentage points (95% CI {lo} to {hi}). H1 is unresolved. With this model and "
    "these records, tools and a verifier did not measurably change accuracy over one call on "
    "the full record.")
SENTENCE_REVERSED = (
    "On the primary outcome, A2 was less accurate than L1: {a2}% against {l1}%, a difference "
    "of {d} percentage points (95% CI {lo} to {hi}). H1 is contradicted. A1 \u2212 L1 was {d1} "
    "({lo1} to {hi1}); A2 \u2212 A1 was {d2} ({lo2} to {hi2}).")
SENTENCE_CEILING = (
    "L1 and A2 were both at or above 95%, so this test set does not separate the systems "
    "with this model. What this study then contributes is the test harness and the "
    "per-condition design, not a ranking of systems; separating them needs harder edits or "
    "real notes.")
SENTENCE_INVALID = (
    "{k} of {n} {system} outputs were not valid answers and were counted as wrong, so part "
    "of the difference is output format, not judgment.")
SENTENCE_WEAKEST = (
    "A2's lowest condition was {condition}: {k} of {n} correct ({p}%, 95% CI {lo} to {hi}). "
    "On the note conditions (C3a, C3b, C4), A2 against the keyword baseline R1 differed by "
    "{d} points (95% CI {lo} to {hi}); H2 is {supported | unresolved | contradicted}. H3 is "
    "{supported | unresolved | contradicted}.")
SENTENCES = {"supported": SENTENCE_ABOVE, "unresolved": SENTENCE_FLAT,
             "contradicted": SENTENCE_REVERSED}


def fill(template: str, values: list) -> str:
    """Replace the {...} placeholders of a template in order of appearance."""
    slots = re.findall(r"\{[^{}]*\}", template)
    if len(slots) != len(values):
        raise ValueError(f"{len(slots)} placeholders, {len(values)} values")
    it = iter(values)
    return re.sub(r"\{[^{}]*\}", lambda m: str(next(it)), template)


def fmt_pct(x: float | None) -> str:
    """A proportion (or difference of proportions) in percent or points, one decimal."""
    if x is None:
        return "n/a"
    s = f"{100 * x:.1f}"
    if s in ("-0.0", "0.0"):
        return "0.0"
    return MINUS + s[1:] if s.startswith("-") else s


def _ci_pair(ci) -> list[str]:
    return [fmt_pct(x) for x in (ci if ci else [None, None])]


def headline(acc: dict, sec: dict, hyp: dict) -> dict:
    """The three headline numbers of PROTOCOL 10, for L1, A1 and A2."""
    sy, df = acc["systems"], acc["differences"]
    prim = None
    if "A2" in sy and "L1" in sy:
        prim = {"A2": sy["A2"]["macro"], "L1": sy["L1"]["macro"], "difference": df["A2-L1"],
                "A1-L1": df.get("A1-L1"), "A2-A1": df.get("A2-A1"),
                "verdict": verdict(df["A2-L1"]["ci"])}
    keep = ("k", "n", "rate", "ci")
    return {
        "primary": prim,
        "answered_anyway": {s: {k: sec[s]["c1_fabricated_certainty"][k] for k in keep}
                            for s in LLM_SYSTEMS if s in sec},
        "changed_when_must_not": {s: {k: sec[s]["answer_change"]["pooled"][k] for k in keep}
                                  for s in LLM_SYSTEMS if s in sec},
    }


def weakest(acc: dict, system: str = "A2") -> dict | None:
    if system not in acc["systems"]:
        return None
    bc = acc["systems"][system]["by_condition"]
    order = rank_conditions(bc)
    if not order:
        return None
    k = order[0]
    return {"condition": k, **{x: bc[k][x] for x in ("k", "n", "est", "ci")}}


def sentences(acc: dict, sec: dict, hyp: dict, head: dict, weak: dict | None,
              flags: dict) -> dict:
    """The fixed sentences of PROTOCOL 10 with the numbers filled in."""
    out = {"primary": None, "ceiling": None, "schema_invalid": [], "weakest": None}
    p = head["primary"]
    if p is not None:
        a1l1, a2a1 = p.get("A1-L1"), p.get("A2-A1")
        vals = [fmt_pct(p["A2"]["est"]), fmt_pct(p["L1"]["est"]),
                fmt_pct(p["difference"]["est"]), *_ci_pair(p["difference"]["ci"])]
        if p["verdict"] != "unresolved":
            for d in (a1l1, a2a1):
                vals += [fmt_pct(d["est"] if d else None), *_ci_pair(d["ci"] if d else None)]
        out["primary"] = fill(SENTENCES[p["verdict"]], vals)
    if flags["ceiling"]:
        out["ceiling"] = SENTENCE_CEILING
    for s in LLM_SYSTEMS:
        f = flags["no_valid_output"].get(s)
        if f and f["over_threshold"]:
            out["schema_invalid"].append(fill(SENTENCE_INVALID, [f["k"], f["n"], s]))
    h2, h3 = hyp.get("H2"), hyp.get("H3")
    if weak is not None and h2 is not None and h3 is not None:
        out["weakest"] = fill(SENTENCE_WEAKEST, [
            weak["condition"], weak["k"], weak["n"], fmt_pct(weak["est"]),
            *_ci_pair(weak["ci"]), fmt_pct(h2["est"]), *_ci_pair(h2["ci"]),
            h2["verdict"], h3["verdict"]])
    return out


def result_flags(acc: dict, sec: dict) -> dict:
    sy = acc["systems"]
    ceiling = all(s in sy and sy[s]["macro"]["est"] is not None
                  and sy[s]["macro"]["est"] >= CEILING - EPS for s in ("L1", "A2"))
    inv = {}
    for s in LLM_SYSTEMS:
        if s in sec:
            v = sec[s]["no_valid_output"]
            inv[s] = {**v, "over_threshold": bool(v["n"] and v["k"] / v["n"] > INVALID_SHARE)}
    return {"ceiling": bool(ceiling), "no_valid_output": inv}


RESULT_KEYS = ("schema", "case_set", "run", "n_cases", "headline", "hypotheses", "weakest_A2",
               "flags", "sentences")


def results(s: dict) -> dict:
    """The page-facing subset of a score dict (keys documented in the module docstring)."""
    return {k: s[k] for k in RESULT_KEYS}


# ---------------------------------------------------------------- driver
def run_case_ids(run_dir: str | Path) -> list[str] | None:
    """The case ids a run was started on (manifest `case_ids`), or None if the manifest
    does not list them (then the whole case set is scored)."""
    p = Path(run_dir) / "manifest.json"
    if not p.exists():
        return None
    ids = json.loads(p.read_text()).get("case_ids")
    return None if ids is None else list(ids)


def scored_cases(case_dir: str | Path, run_dir: str | Path) -> list[dict]:
    """The cases a run is scored on: those its manifest lists, else the whole set."""
    case_list = CS.read_cases(case_dir)
    ids = run_case_ids(run_dir)
    if ids is not None:
        unknown = set(ids) - {c["case_id"] for c in case_list}
        if unknown:
            raise ValueError(f"run lists cases not in the case set: {sorted(unknown)[:5]}")
        keep = set(ids)
        case_list = [c for c in case_list if c["case_id"] in keep]
    return case_list


def score_design(design: Design, run: dict[str, dict[str, dict]],
                 case_dir: str | Path | None = None, n_boot: int = N_BOOT,
                 seed: int = BOOT_SEED) -> dict:
    """Every table of a score for one run on one design (no file names)."""
    case_list = design.cases
    W = design.weights(n_boot, seed)
    out = {}
    for variant in ("main", "S-A", "S-C"):
        ok = {s: np.array([correct(c, recs.get(c["case_id"]), variant) for c in case_list])
              for s, recs in run.items()}
        out[f"accuracy_{variant}"] = accuracy_table(design, ok, seed=seed, W=W)
    out["secondary"] = {s: secondary(design, recs, case_dir, W) for s, recs in run.items()}
    flags = {s: case_flags(design, recs) for s, recs in run.items()}
    acc, sec = out["accuracy_main"], out["secondary"]
    hyp = hypotheses(design, flags, acc, W)
    head = headline(acc, sec, hyp)
    weak = weakest(acc)
    rf = result_flags(acc, sec)
    out.update({"headline": head, "hypotheses": hyp, "weakest_A2": weak, "flags": rf,
                "sentences": sentences(acc, sec, hyp, head, weak, rf)})
    return out


def score(case_dir: str | Path, run_dir: str | Path, extra_runs=(), n_boot: int = N_BOOT,
          seed: int = BOOT_SEED) -> dict:
    """Score a run on the cases it was started on (all of them, if the manifest does not
    say). A case of that list with no record for a system counts as wrong."""
    n_set = len(CS.read_cases(case_dir))
    case_list = scored_cases(case_dir, run_dir)
    design = Design(case_list)
    run = load_run(run_dir)
    out = {"schema": RESULTS_SCHEMA, "case_set": Path(case_dir).name, "run": Path(run_dir).name,
           "n_cases": len(case_list), "n_cases_in_set": n_set,
           **score_design(design, run, case_dir, n_boot=n_boot, seed=seed)}
    if extra_runs:
        others = [load_run(r) for r in extra_runs]
        ids = [c["case_id"] for c in case_list]
        W = design.weights(n_boot, seed)
        out["stability"] = {s: stability([run[s]] + [o.get(s, {}) for o in others], ids,
                                         design, W)
                            for s in run if s in LLM_SYSTEMS}
    return out
