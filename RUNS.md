# RUNS: release, seed, test set and run log

Append-only log for `PROTOCOL.md` (secs. 11, 13 and 14). Times are UTC. A `pending` field is filled once, when the event happens, in its own commit; nothing else is edited afterwards. Corrections are added as new dated lines, not by rewriting.

**Status (2026-11-20): protocol not released. No test seed, no test set, no test run.**

## 1. Protocol release

| Field | Value |
|---|---|
| Tag | pending |
| Tag commit (40 hex characters, lowercase) | pending |
| GitHub Release URL | pending |
| Release `published_at` (UTC, from the GitHub API) | pending |
| External registration (optional) | pending |

## 2. Beacon pulse and test seed

Rule: `PROTOCOL.md` sec. 13.

| Field | Value |
|---|---|
| T = `published_at` rounded up to a whole minute, plus 2 minutes (UTC) | pending |
| T in milliseconds since the Unix epoch | pending |
| Source used (primary NIST Randomness Beacon 2.0; fallback drand quicknet) | pending |
| Request URL | pending |
| Pulse URI (NIST) or round number (drand) | pending |
| Pulse `timeStamp` (NIST) or round time (drand) | pending |
| Previous pulse `timeStamp` (must be earlier than T) | pending |
| `beacon_value` (NIST `outputValue`, or drand `randomness`) | pending |
| `sha256(tag commit + beacon_value)` | pending |
| Test seed = first 8 hex characters as an integer | pending |
| Retrieved at; number of attempts; failed attempts with HTTP status | pending |

## 3. Test set

Built by the tag commit, unchanged, before any model is called.

| Field | Value |
|---|---|
| Commit used for the build (must equal the tag commit) | pending |
| Working tree clean (`git status --porcelain` empty) | pending |
| Generation batch: seed, `-p` (40000, one batch), sha256 of its `run_manifest.json` | pending |
| Cohort size (alive, aged 30–79 at index) | pending |
| Gate report | pending (`reports/gate_test_*.md`) |
| Clusters by stratum × statin status | pending |
| Cases by condition (C0, C1, C2a, C2b, C3a, C3b, C4, C5) | pending |
| Shortfalls by cell; rejected twins | pending |
| L1 build-stop check (largest prompt in tokens; context limit) | pending |
| Phrase file and its sha256 | pending |
| **Test set sha256** (sha256 of the case set's `manifest.json`, which lists the sha256 of every file) | pending |
| Commit that recorded this section (must precede every model call) | pending |

## 4. Model and server

Chosen by the rule in `PROTOCOL.md` sec. 6. Filled before run 1.

| Field | Value |
|---|---|
| Model repository, revision, file sha256 | pending |
| Serving engine, version or commit, image digest | pending |
| GPU, provider and tier | pending |
| Context length served; `context_limit`; `chars_per_token` | pending |
| Launch command (as run) | pending |
| Client concurrency | pending |
| Determinism check at temperature 0 (cases repeated; differing final statuses) | pending |
| Prompts and conventions page sha256 (must equal `PROTOCOL.md` sec. 5) | pending |

## 5. Runs

Each run writes a `manifest.json` (git commit, clean-tree flag, prompt and conventions hashes, test set sha256, backend and settings); its sha256 is entered here.

| Run | Role | Systems | Temperature, seed | Started | Finished | Commit | Run manifest sha256 | Status |
|---|---|---|---|---|---|---|---|---|
| 1 | Primary analysis | R0, R1, L1, A1, A2 | 0, seed 1 | – | – | – | – | pending |
| 2 | Stability only | L1, A1, A2 | per protocol, seed 2 | – | – | – | – | pending |
| 3 | Stability only | L1, A1, A2 | per protocol, seed 3 | – | – | – | – | pending |

Per run, after it finishes: cases with an output per system; schema-invalid outputs; backend errors; forced fallbacks; tool-call parser failures; GPU-hours and cost.

## 6. Crashes and resumed runs

None as of 2026-11-20.

Entry format: date; run; commit; what failed; which case ids had no output; how the run was resumed. Cases that already have an output are never run again.

## 7. Analysis outputs

| Field | Value |
|---|---|
| Scoring commit | pending |
| Command | `python scripts/score_runs.py --cases <test set> --run <run 1>` |
| `score.json` sha256 | pending |
| Figure 1 file and sha256 | pending |
| Date the results were first published | pending |

## 8. Development-set runs before the release

Runs on the development set `dev_s20261120_utc_n10` (200 cases) that choose a setting. Each rule was written before its run. Both runs sent 8 requests at a time to a server with 8 slots; on that setup 2 to 4 of 20 statuses changed between identical runs (GPU smoke test), so both rules were decided under that noise. Run 1 sends one request at a time (`PROTOCOL.md` sec. 6). Development accuracy is not a result (`PROTOCOL.md` sec. 10); the outputs are released with the rest.

**dev1 (2026-11-21), `max_steps` 16 (D) against 8 (T).** One GPU session, configuration D (L1, A1, A2; 16 rounds) then T (A1, A2; 8 rounds), the same cases, round counter on. Rule (all five must hold to choose 8): (1) A1 and A2 correct counts each T ≥ D − 3; (2) C3a A2 correct T ≥ D − 2; (3) A1 answers with missing claims in T ≤ 4; (4) cases with r_req > 6 in D ≤ 2; (5) schema-invalid plus forced fallbacks outside C1 in T ≤ D + 2. Outcome: all five held; `max_steps` = 8. For (4), 3 cases had no r_req because the A1 loop ended after a text-only turn (the re-ask then had no tool rounds, a harness defect fixed in 12e665a); they were counted as not late; the same cases had r_req 2 under 8 rounds.

**T2 (2026-11-21), repair prompt candidate `repair2`.** In dev1, repairs turned correct C2a answers into CANNOT_DETERMINE after the verifier rejected a risk computed from a value not in the record, on a row that needs no risk. Candidate: prompt set `repair2`, the repair prompt plus two sentences (delete such a risk claim; a failed check on a claim the row does not need never changes the status or the row). Run: A1 and A2 on all 200 cases, `max_steps` 8, round counter on, `repair_steps` 8, with the re-ask fix (12e665a); L1 is not run again. Rule, written before the run (all seven must hold to keep `repair2`):

| # | Rule |
|---|---|
| V1 | Cases with A1 correct and A2 wrong ≤ 2 |
| V2 | Of `case_7052a1f791e2` and `case_f9c8a915a027`, those with A1 correct and the first draft rejected: at least one has A2 correct |
| V3 | C2a: A2 correct ≥ A1 correct − 1 |
| V4 | All cases: A2 correct ≥ A1 correct − 1 |
| V5 | Forced fallbacks outside C1 plus A2 schema-invalid ≤ 5, and 0 check-2 failures after repair |
| V6 | C1: A2 correct ≥ A1 correct |
| V7 | A1 correct ≥ 185 |

2026-11-21, before the run: V2 holds if neither case qualifies (the rule is about repairs of rejected correct drafts). The rule is computed by the dev kit's `judge_t2.py`; on the dev1 T run it gives V1 5, V2 0 of 2, V3 −4, V4 −3, V5 5 and 0, V6 equal, V7 189, as counted by hand.

If any fails, or the run does not finish, the current `repair.txt` is kept and there is no third run; if only V7 fails, the run is not used to decide (same outcome). Either way `max_steps` stays 8. Kept, `repair2` replaces `repair.txt` before the release. Checked but not part of the rule: no case gets the tool-limit error before its 8 rounds are used.

Outcome (2026-11-21, session t2 at commit 4aa6afb, 200 of 200 cases for A1 and A2): all seven held, so `repair2` is kept. V1 1 case (a C3a case whose repair failed check 4 and was forced); V2 2 of 2 qualifying cases correct; V3 C2a A1 28, A2 28; V4 A1 193, A2 192; V5 1 forced fallback outside C1, 0 schema-invalid, 0 check-2 failures after repair; V6 C1 19 and 19; V7 193. No case got the tool-limit error before its 8 rounds. The text of `repair2` replaced `repair.txt` and the prompt set was removed in the commit that adds this paragraph.
