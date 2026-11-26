# DEVIATIONS

Departures from `PROTOCOL.md` after the protocol tag (policy: `PROTOCOL.md` sec. 12).

**2 entries as of 2026-11-26, both made before any model output: D-1 adds three exploratory measures; D-2 is a runner fix (not a departure from the protocol) so that run 1 can use a second RunPod account. No protocol-specified result changes.** This line is re-dated every time results or this file are updated. Records made after the tag and before any model output are in `RUNS.md` sec. 3a.

## What counts

- A deviation: anything done differently from the tagged protocol that touches the test set, the systems, the prompts, the model or its settings, the scoring, the analysis, or what is reported.
- Not a deviation, logged in `RUNS.md` instead: a crash and the resumed run; use of the fallback beacon under the rule in sec. 13; raising the served context length before any model call so that every test case fits (sec. 5).
- Entered here and in `RUNS.md`, marked "runner fix": a code change after the tag that changes only `gpu/`, `tests/gpu/` or `docs/` and alters neither the test set nor the scoring (`PROTOCOL.md` sec. 12).
- Not a deviation: changes made before the tag. They are in the commit history of `SPEC.md` and `PROTOCOL.md`.

## Entry format

```
### D-<n>: <short title> (<date, UTC>)
- Protocol says: <section and sentence>
- Done instead: <what>
- Reason: <why>
- Decided: before the test seed | after the seed, before any model output | after model outputs were seen
- Affects: test set | model outputs | scoring | analysis | reporting
- Effect on results: <the protocol-specified result and the deviating result, side by side, where both can be computed; otherwise why not>
- Commit: <sha>
```

Entries are numbered in the order they are written and are never removed. A deviation decided after model outputs were seen is marked as such wherever its result is shown.

## Entries

### D-1: Three exploratory measures added to what is reported (2026-11-25)
- Protocol says: sec. 8, "All pre-specified, all with 95% intervals from the same bootstrap, all on run 1 unless stated", followed by secondary outcomes 1–10. Secs. 7–9 name no other measure.
- Done instead: nothing pre-specified is changed or dropped. Three measures are added and reported as exploratory, for L1, A1 and A2 on run 1, each as k of n with the interval of sec. 7, with no verdict and no comparison between systems: (a) row agreement, the row the system names (`benefit_group`) is the oracle's row; (b) latest entry, a cited lipid, systolic BP, smoking or eGFR entry is the latest on record; (c) C1 and C3a pair consistency, the same system is correct on the patient's C0 case and on the edited case. Definitions: `RUNS.md` sec. 3a item 8. Code: `results/exploratory.py`, sha256 in `RUNS.md`.
- Reason: the sec. 1 question asks whether each part of the statement is documented and whether the answer changes with the deciding entry. Two reviews of the protocol's logic (2026-11-24) and the author's own reading found parts that the pre-specified measures leave open: correctness is scored on the status alone, the verifier does not check the named row or whether a cited value is the latest (`SPEC.md` sec. 6.2), and C1 and C3a are scored on the edited case alone. All three can be computed from the saved outputs without a model call.
- Decided: after the seed, before any model output. Known at the time: every development output (the definitions were checked on development runs) and counts of the test set that involve no model output: cases per condition and, from `--inputs-only`, the cases left out of row agreement, the row-0 cases, the cases with no entry per input, the cases with a tie, and the C1 and C3a pairs.
- Affects: analysis | reporting (additions only)
- Effect on results: none on any protocol-specified result. No pre-specified number, interval or verdict is computed differently, and `src/gapcheck/score.py` is unchanged. The added measures have no protocol-specified counterpart, so nothing is shown side by side. They are shown after the pre-specified outcomes, marked "exploratory, not pre-specified (D-1)".
- Commit: `b3413b5`

### D-2: Runner fix, so that run 1 can use a second RunPod account (2026-11-26)
- Protocol says: sec. 12, "A code change that alters neither the test set nor the scoring (for example a crash fix in the runner) is logged in `RUNS.md` and `DEVIATIONS.md` with its commit. Such a fix may change only `gpu/`, `tests/gpu/`, `docs/`, `RUNS.md`, `DEVIATIONS.md` and `results/`". The protocol names no RunPod account. This entry is that log; it is not a departure from the protocol.
- Done instead: (1) `gpu/rpapi.py` reads the API key from the file named by `GAPCHECK_RUNPOD_KEY_FILE` when it is set, and from `~/.config/runpod/api_key` otherwise, as before. A session records the file's path (never the key) and refuses a resume that reads another file. (2) The runner refused a first session at a commit that changes `gpu/` after the tag, and allowed such a commit only for a resume (`--runner-fix`). With `--runner-fix`, a first session now accepts commits after the tag that change only the paths sec. 12 allows, and records them (`tag_runner_fix` in `session.json` and `status.json`, and the merged manifest's note). A change to `src/`, `scripts/`, `configs/`, the prompts or the protocol is still refused. (3) Two related fixes: a rename after the tag is now checked under its old path as well as its new one (before, moving a file into `gpu/` would have been checked as a `gpu/` change only), and the key file must be an absolute path or start with `~/`, so that the watchdog, which runs in `gpu/`, reads the same file. (4) `wait.log`, released with the run, is scrubbed of local paths like the other released files. Tests in `tests/gpu/`; `docs/SERVING.md`, `gpu/README.md`.
- Reason: another project uses the RunPod account the runner read, and two runs on one account would mix the balance that each run's watchdog watches (`docs/SERVING.md`). Run 1 therefore uses a second account. Its key path was fixed in the code, and the runner's first-session check was stricter than sec. 12.
- Decided: after the seed, before any model output.
- Affects: none of the listed (the runner only: which RunPod account the pods are billed to)
- Effect on results: none. The test set, the systems, the prompts, the model, the server and decoding settings, the GPU type and the scoring are unchanged.
- Commit: b80be62
