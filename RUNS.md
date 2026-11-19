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
| T = 00:00:00.000 UTC on the day after `published_at` | pending |
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
| Generation batches: seed, `-p`, sha256 of each `run_manifest.json` | pending |
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
