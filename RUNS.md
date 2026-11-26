# RUNS: release, seed, test set and run log

Append-only log for `PROTOCOL.md` (secs. 11, 13 and 14). Times are UTC. A `pending` field is filled once, when the event happens, in its own commit; nothing else is edited afterwards. Corrections are added as new dated lines, not by rewriting.

**Status (2026-11-23): protocol released at tag `protocol-v1`; test seed derived; test set built. No test run.**

## 1. Protocol release

| Field | Value |
|---|---|
| Tag | `protocol-v1` |
| Tag commit (40 hex characters, lowercase) | `f2f30d9b76f626c9397d9624ae69867dee165063` |
| GitHub Release URL | https://github.com/starriet9/gapcheck/releases/tag/protocol-v1 |
| Release `published_at` (UTC, from the GitHub API) | 2026-11-23T11:57:17Z |
| External registration (optional) | None. Internet Archive snapshots: https://web.archive.org/web/20261123115830/https://github.com/starriet9/gapcheck/blob/protocol-v1/PROTOCOL.md ; https://web.archive.org/web/20261123115940/https://github.com/starriet9/gapcheck/releases/tag/protocol-v1 ; https://web.archive.org/web/20261123120023/https://github.com/starriet9/gapcheck/tree/protocol-v1 ; https://web.archive.org/web/20261123120004/https://starriet9.github.io/gapcheck/ |

## 2. Beacon pulse and test seed

Rule: `PROTOCOL.md` sec. 13.

| Field | Value |
|---|---|
| T = `published_at` rounded up to a whole minute, plus 2 minutes (UTC) | 2026-11-23T12:00:00.000Z |
| T in milliseconds since the Unix epoch | 1795435200000 |
| Source used (primary NIST Randomness Beacon 2.0; fallback drand quicknet) | NIST Randomness Beacon 2.0 (primary) |
| Request URL | https://beacon.nist.gov/beacon/2.0/pulse/time/1795435200000 |
| Pulse URI (NIST) or round number (drand) | https://beacon.nist.gov/beacon/2.0/chain/2/pulse/2040305 (`statusCode` 0) |
| Pulse `timeStamp` (NIST) or round time (drand) | 2026-11-23T12:00:00.000Z |
| Previous pulse `timeStamp` (must be earlier than T) | 2026-11-23T11:59:00.000Z (chain 2, pulse 2040304) |
| `beacon_value` (NIST `outputValue`, or drand `randomness`) | `72582BB1FD2799CFBB7AF5C0208FBB01C49CE2874BA9052A1E8B59D9151592B7310716E9F3752DD00F557AB208BEC47957DEED456DFE5961FEFACBB3ACAA3625` |
| `sha256(tag commit + beacon_value)` | `4e5b449c41cb1dccee3990af7a1ae4f77d6ba8a7af394165a78962bdb2c03ab6` |
| Test seed = first 8 hex characters as an integer | `4e5b449c` = 1314604188 |
| Retrieved at; number of attempts; failed attempts with HTTP status | 2026-11-23T12:01:24Z; 2 requests for the pulse at T: 12:00:20Z HTTP 404 ("Pulse Not Available": the latest pulse, fetched at 12:00:53Z, was 11:59:00), then 12:01:24Z HTTP 200. The previous pulse was fetched at 12:01:44Z as a check (HTTP 200) |

## 3. Test set

Built by the tag commit, unchanged, before any model is called.

| Field | Value |
|---|---|
| Commit used for the build (must equal the tag commit) | `f2f30d9b76f626c9397d9624ae69867dee165063` (the tag commit, checked out detached) |
| Working tree clean (`git status --porcelain` empty) | Yes, empty at 2026-11-23T12:03:25Z, before the build started |
| Generation batch: seed, `-p` (40000, one batch), sha256 of its `run_manifest.json` | seed 1314604188, `-p` 40000, one batch (2026-11-23T12:03Z to 13:12Z); `run_manifest.json` sha256 `8b8bdf494595cd5be5718354cb9d28b8a2712fab1684c24ba64045c681a1edd2` |
| Cohort size (alive, aged 30–79 at index) | 39,553 |
| Gate report | `results/gate_test_s1314604188_p40000_utc.md` (gate.py writes it to `reports/`; moved to `results/`, because commits after the tag may touch only the run records) |
| Clusters by stratum × statin status | 260: S1 20 on, 20 off; S2 20 on, 20 off; S3 20 on, 20 off; S4 20 on, 20 off; S5 40 off; near-threshold sources 40; LDL-C threshold sources 20 |
| Cases by condition (C0, C1, C2a, C2b, C3a, C3b, C4, C5) | 798: C0 200, C1 79, C2a 120, C2b 34, C3a 80, C3b 40, C4 120, C5 125 (65 C5 pairs) |
| Shortfalls by cell; rejected twins | No cell short. Rejected twins: C1 1, C2b 9 |
| L1 build-stop check (largest prompt in tokens; context limit) | Local check (`gpu/calibrate.py --local`, `chars_per_token` 2.26). With the tagged `context_limit` 45,056, 2 of 798 cases did not fit: `case_3566815f8f64` (C0, estimate 48,827 tokens) and `case_188db58f0c8f` (C2a, 48,237), both of one base patient; every row the oracle uses and every planted sentence was inside the serialization. The served context was raised under `PROTOCOL.md` sec. 5 before any model call (sec. 3a, item 3). With `context_limit` 49,152 all 798 cases pass (2026-11-24T04:34:32Z; largest estimate 48,827). This row was recorded with sec. 3a, after `235af37` and before any model call |
| Phrase file and its sha256 | `src/gapcheck/phrases/test.json` (version `test-1`), sha256 `e786194c6941e82dfdbc4c5730c3bc858c2c0bcc5d399777e0b160d8bf162995`, as in `PROTOCOL.md` sec. 4 |
| **Test set sha256** (sha256 of the case set's `manifest.json`, which lists the sha256 of every file) | `be6af5fd7e4cfcdb499a14037a83ec560fbbcce94a27ff10b5ad993ebb9b7df7` (`data/cases/test_s1314604188_n40/manifest.json`, 2026-11-23T13:43:10Z) |
| Commit that recorded this section (must precede every model call) | `235af37` |

**Hand labels** (`PROTOCOL.md` sec. 3).

| Field | Value |
|---|---|
| sha256 of the cards file (`scripts/hand_label_cards.py` at the tag commit), entered before labeling starts | `882a7ba533d68f515ccb5bf199934b3603ae0a31d789858282a10cad011e1f3b` (30 cards, built at the tag commit, 2026-11-23) |
| sha256 of the label file, and the commit that holds it (before the key is opened and before run 1 is complete) | `dc190648428f667dcb0abf9f5eeb21b0479f02f336f2ebd593de123611f2a7d1` (`results/hand_labels.json`, 30 labels), in `789d11c`; committed and pushed 2026-11-26, before the key was opened and before run 1 started |
| Time the key was opened | 2026-11-26T13:49:05Z, after the label file was pushed (`1784ebd`) |
| Commit that records the cause of each disagreement (before any run 1 output is read) | `7a27cf3`: no disagreement. Status agrees on 30 of 30 cards (30 of 30 without recognised cards: the author recognised none); the row agrees on all 25 cards where the author named one (`results/hand_label_comparison.json`). Recorded before run 1 started |

## 3a. Before any model output: records and the scoring rules as frozen (2026-11-24)

Written after the test set was built and before any model call on it. Nothing here changes the test set, the systems, the prompts, the scoring code or the analysis. The author asked for two independent reviews of the protocol's logic, both read on 2026-11-24 before any model output: Claude Fable 5.2 (an LLM agent) and a model from another vendor (OpenAI, in ChatGPT). Items 1, 2 and 4–6 come from those reviews.

1. **C1 and forced fallbacks.** Sec. 7: "C1 is correct only if the status is CANNOT_DETERMINE and the deleted input is in `missing`." Sec. 10: "A forced CANNOT_DETERMINE is correct on C1 and incorrect on every other condition." Sec. 5: a forced fallback leaves `missing` unchanged. The frozen scorer applies sec. 7 to every C1 answer, forced or not (`src/gapcheck/score.py` lines 134–141; the file is unchanged since the tag, sha256 `4ff7674c4c8ee20cee86a98575a2bc7c01552fa9b923ce9a53fe559205f19500`), and the fallback copies `missing` from the last answer (`src/gapcheck/systems.py` lines 339–342). So a forced CANNOT_DETERMINE on C1 is correct only if that answer named the deleted input. Sec. 7 and the frozen code are followed, and the sentence in sec. 10 is read with sec. 7's condition. This is the protocol as scored, so it is not a deviation.
2. **Where `SPEC.md` and `PROTOCOL.md` differ, `PROTOCOL.md` governs** (its preamble). C2b is kept (sec. 4), although `SPEC.md` sec. 5.2 still describes dropping it first if the schedule slips. A prompt that does not fit the served context is handled by sec. 5 (the context is raised before any model call), not only by the build stop of `SPEC.md` sec. 6.
3. **Served context raised (sec. 5).** The local L1 check with the tagged `context_limit` 45,056 failed on 2 of 798 cases (sec. 3, build-stop row). Run 1 therefore uses `results/run1_context49152.json`: `configs/run1.json` with `run.context_limit` 49,152 and `server.ctx_per_slot` 53,248 (49,152 plus `max_tokens` 4,096; 8 slots, `-c` 425,984). No other value changes. With it the local check passes on all 798 cases. On the pod, the server's own token count of every L1 prompt is checked before any model call (`docs/SERVING.md` sec. 6). This is the step sec. 5 prescribes, so it is not a deviation; if the model cannot serve this length, sec. 5's rule for cases that do not fit applies.
4. **C1 by deleted input.** Sec. 4: "C2b and C1-by-smoking are small; their intervals will be wide and are reported as such." The test set's 79 C1 cases are lipid panel 64, SBP 14 and smoking 1. The frozen scorer reports C1 as one condition. `results/c1_by_input.py`, written now, splits it by deleted input with the scorer's own case list, correctness rule and resamples, and changes no scored number. On a synthetic development run, its parts add up to the scorer's C1 counts for every system. With a single smoking case, that row is reported as k of 1 with no interval.
5. **Rates as the frozen scorer computes them** (secs. 7–8), so that each reported number is read as defined:
   - Macro-average: the mean of the condition accuracies. In a resample, a condition with no case is left out of that resample's mean. A macro-average has no single k of n, so each condition's k of n is shown with it.
   - Intervals: 2.5th and 97.5th percentiles of 10,000 stratified cluster resamples. If every resample gives the same value (for example, A1 and A2 never differ on C2b), the interval is a single point. It is then shown with the number of discordant cases and is not read as precision.
   - No valid output (schema-invalid, missing, or rejected by the server) is incorrect. It is not fabricated certainty on C1 and not over-abstention on C2a or C2b. It counts as a missed GAP for GAP sensitivity and as not GAP for GAP specificity, as a miss on C3a, and as a changed answer in the answer-change rates. Answers without a verifier audit are outside the "passes all four checks" share.
   - Fabricated certainty is any valid status other than CANNOT_DETERMINE on C1. A CANNOT_DETERMINE that does not name the deleted input is not fabricated certainty, but it is incorrect.
   - C1 and C3a accuracy are edited-case accuracy. A correct answer there does not show that the system changed its own C0 answer; C5 pair consistency and the answer-change rates are the pre-specified measures of change and of no change.
   - Unsupported claims are pooled over the DOCUMENTED and ABSENT claims of final answers that fail check 1 or 4. An answer with no such claim adds nothing to the denominator.
6. **Limits stated wherever the results are interpreted:**
   - The verifier checks provenance and arithmetic only. A2's final answers pass its checks or are forced, by construction, so A2's check results are not independent evidence that its answers are grounded. Whether every needed fact is cited, is the latest value, and supports the claim is not measured. The first part of the sec. 1 question is therefore answered only within those checks.
   - H1–H3 compare designs. None of them sets an absolute level a system must reach.
   - The primary outcome averages edits that must change the answer (C1, C3a, C5) and edits that must not (C2a, C2b, C3b, C4). The two groups are shown separately (Figure 1, sec. 8).
   - L1's prompt holds every row the oracle uses and every planted sentence (sec. 5 build stop), so A2 − L1 does not test retrieval when the record is too long to show in full.
   - R0 and R1 contain the oracle. H2 compares A2 with an oracle-plus-keyword baseline, not with keyword rules alone.
   - The 5-point margin of H3 part 2 is a design choice; the protocol does not justify its size.
   - Each edit is a controlled change of one fact, not of one record entry: an LDL-C twin also moves TC by the same amount, and a smoking edit also changes the notes rendered from the record.
   - Intervals cover resampling of these patients, for this model, configuration and run. They do not cover run-to-run variation. Runs 2 and 3, at temperature 0.7, measure agreement across decoding settings, not repeats of run 1.
   - "Unresolved" means the interval includes 0. It does not show that the systems are equal.
   - H1–H3 and their measures were written on 2026-11-20 (`9827757`), before the GPU development runs of 2026-11-21 (sec. 8). Since then, only the article "a" was added to H1 (`1975ec6`).
7. **How the results are shown** (the mapping from the sec. 1 question to the pre-specified measures, and the form of each verdict): added to this section before any run 1 output is read.

   Added 2026-11-25, before any model call. Each part of the sec. 1 question, the pre-specified measure that answers it, and the hypothesis that tests it. "None" means no hypothesis tests that measure. "Not measured" marks a gap. Three gaps have an exploratory measure, defined in item 8 before any model output (`DEVIATIONS.md` D-1); it is marked "exploratory" and has no verdict. This table is for reading the results; the first display is the one fixed in sec. 10.

   | Part of the sec. 1 question | Pre-specified measure (`PROTOCOL.md` section) | Hypothesis |
   |---|---|---|
   | Overall: does the status match the oracle across the eight conditions? | Exact-status accuracy, macro-averaged (sec. 7); A2 − L1, with A1 − L1 and A2 − A1 beside it | H1 |
   | Is the care-gap statement itself right? | GAP sensitivity and specificity over all cases (sec. 8.2) | None |
   | Is each part of it documented: do the cited record rows and absence claims check out? | Unsupported-claim rate (checks 1 and 4); share of final answers that pass all four checks, for L1, A1 and A2 (sec. 8.6). A2's answers pass or are forced by construction (item 6), and a forced answer keeps the rejected draft's evidence, so A2's two rates are not exactly 0% and 100% when it has forced answers. For A2 the informative counts are the first-round rejections by check and how many were repaired or forced (sec. 8.8) | None |
   | Is the row the system names the oracle's row? | Not pre-specified. The verifier never evaluates the decision table (`SPEC.md` sec. 6.2); sec. 9 relabels a prediction by the row it names. Exploratory: row agreement (item 8) | None |
   | Is each cited lab or vital value the latest on record? | Not pre-specified. The verifier does not check it, because feeding it back would leak the answer (`SPEC.md` sec. 6.2). Exploratory: latest entry (item 8) | None |
   | Is a deciding input that is absent named in `missing`? | C1 accuracy requires it (sec. 7); outside C1 no deciding input is absent | H1 through C1 accuracy |
   | Does a cited row or quote support the claim in meaning, and is every needed fact cited? | Check 1 compares values, codes and quotes; check 2 covers the required inputs of the row the system names. Beyond that: not measured | None |
   | Does the answer change when a deciding input is deleted (C1)? | C1 accuracy, also by deleted input (item 4); fabricated-certainty rate (sec. 8.3) | H3 part 1 (fabricated certainty, A2 vs A1); H1 through C1 accuracy |
   | ... when a reason is stated only in a note (C3a)? | C3a accuracy (the C3a miss rate of sec. 8.5 is its complement); citation overlap with the planted sentence (sec. 8.5) | H2 (with C3b and C4); H1 |
   | ... when one value crosses a threshold (C5)? | C5 accuracy (sec. 8.1); pair consistency, both members correct (sec. 8.4) | H1 through C5 accuracy |
   | ... and only then: an input deleted where it cannot decide (C2a, C2b)? | Accuracy (sec. 8.1); over-abstention rate (sec. 8.3); answer-change rate against the same patient's C0 case (sec. 8.5) | H3 part 2 (over-abstention, C2a and C2b each); H1 |
   | ... and only then: a reason sentence where no statin is indicated (C3b), a distractor in a note (C4)? | Accuracy (sec. 8.1); answer-change rate against C0 (sec. 8.5) | H2; H1 |
   | Does a system move from a correct C0 answer to the correct edited answer (C1, C3a)? | Not pre-specified: C1 and C3a are scored as edited-case accuracy (item 5). Exploratory: C1 and C3a pair consistency, the rule of sec. 8.4 applied to the C0 case and the edited case (item 8) | None |
   | Does the answer depend on the facts it cites (would removing a cited fact change it)? | Not measured (planned: `SPEC.md` sec. 12) | None |

   Stability (sec. 8.7) and compute, failures and cost (sec. 8.8) answer no part of the sec. 1 question. The sensitivity analyses (sec. 9) re-score the first two rows under the other encodings and answer no further part.

   Each verdict is written as the verdict word, then the paired difference in percentage points with its 95% interval, for example "H1: [verdict]; A2 − L1 = x points (95% CI lo to hi)". Each rate in it is shown with its k of n (sec. 10); a macro-average is shown with each condition's k of n (item 5). H2 is shown with the note that R1 contains the oracle and is correct by construction on structured data (sec. 10). H3 is written as its two parts and then its verdict (sec. 1): part 1, fabricated certainty on C1, A2 and A1 each as k of n (supported, contradicted or unresolved); part 2, over-abstention, A2 and A1 each as k of n with the A2 − A1 interval, for C2a and for C2b (met, not met or unresolved). A single-point interval is shown with the number of discordant cases (item 5). Sentences that interpret the results are written after run 1 is scored, from the scored numbers; none is written in advance.

8. **Three exploratory measures** (added 2026-11-25, before any model call; `DEVIATIONS.md` D-1). Not pre-specified. For L1, A1 and A2 on run 1, from the saved outputs, by `results/exploratory.py` (sha256 `6835d259b947f4165de99e0cdb145197005823ada37017d721b02e00d6c885f2`), with the scorer's case list, validity and correctness rules and resamples. Each is k of n with its interval (for latest entry n is claims, not cases, and the interval is computed as for the unsupported-claim rate, sec. 8.6); a single-point interval is marked. None has a verdict, no difference between systems is computed, and none is shown in the first display (sec. 10), in Figure 1 or on the README's first screen.
   - Row agreement. Cases with a single oracle row (`expected_row` not null). Agrees: a valid output whose `benefit_group` equals `expected_row`. An invalid or missing output, a null row and a forced fallback do not agree. Left out: every case whose oracle result names no single row: every C1 case, and a case in which more than one row with the same label remains possible after the deletion; in the test set these are C1 79 and C2a 8, no other condition. Shown by condition and pooled, with a 2 × 2 count of status correct by row agrees, the number of cases whose oracle row is 0 (ON_THERAPY, where the row is the status; 160 in the test set), and the number of forced fallbacks among the cases counted.
   - Latest entry. Every DOCUMENTED claim on the lipid panel, systolic BP, smoking status or eGFR in a valid final answer, forced answers included. Latest: every cited source is an observation row of that input (by code: TC, HDL-C or LDL-C; systolic BP; smoking status; eGFR in the unit the oracle reads) dated at the timestamp the oracle reads (`src/gapcheck/facts.py`). For the lipid panel that is the latest timestamp at which TC, HDL-C and LDL-C are all recorded, so a later row of one of them alone is another date. A row that shares that timestamp counts as latest; the claims that cite such a row other than the oracle's are counted (in the test set the oracle's eGFR has such a tie in 18 cases). Not latest, counted by kind, the first that applies: the oracle reads no entry for that input in the case's record; a source that is not such a row (a note, another table or code, an unknown id); rows from that date and another; rows from another date only. Also shown: the count per input. Pooled over all conditions. The claimed value is not compared (check 1 does that). L1 is shown the last three values per code (sec. 5), so its rate is not comparable with the agents'. "Latest" does not show that the entry supports the claim.
   - C1 and C3a pair consistency. The rule of C5 pair consistency (sec. 8.4) applied to the pairs (C0, C1) and (C0, C3a) of one patient: both cases correct under sec. 7, for the same system. Shown for C1 and for C3a, each with a 2 × 2 count; not pooled. Its k is a subset of the edited-case correct count: it re-reads the same outputs, it is not new evidence.

   The script stops without output if the case set breaks an assumption of these definitions (a C1 case with an oracle row; a C1 or C3a case without exactly one C0 case of the same patient, or with that case's expected status; a record whose patient or row ids are not as the oracle reads them) or if a record marked valid lacks a field it reads. The case-set stops were run on the test set before any model call (`--inputs-only`, counts only): none fired; C1 pairs 79, C3a pairs 80.

   Checked before any model call: two implementations written independently from the same definitions agree on every count (273 values) on three development runs (a mock run, an oracle-complete mock run and a run with 100 planted errors of 21 kinds, covering every kind of claim and every cell of the 2 × 2 counts), and each planted error moves the counts as predicted from the definitions before the script was run on it. Records with no output (rejected by the server, attempts exhausted, or absent) were checked with an in-memory fuzz of 12 runs in every record format the runners write: no stop, both implementations equal. The sentence at the top of this section ("Nothing here changes ... the analysis") holds for items 1–7.

9. **A known limit of the frozen harness** (recorded 2026-11-25, before any model call). An answer whose `benefit_group` or ABSENT-claim source is a list or an object, or whose COMPUTED inputs hold a list or an object, raises a `TypeError` in `src/gapcheck/schema.py` or `src/gapcheck/verifier.py` that `systems.parse_answer` does not catch. The case's process then fails, and after its four attempts (`max_total_attempts` in the run 1 configuration) the runner writes an "attempts exhausted" record for every LLM system of that case, although sec. 5 says that such a reply gets one re-ask and that only that system is scored incorrect. It did not occur in the development GPU runs (about 2,200 L1 and agent records, no exhausted record, no such error in their logs). The harness is not changed: a change to the systems after the tag would itself be a deviation. If it occurs in run 1, each such case is listed, with the error text of its exhausted record, in `DEVIATIONS.md` and with the results.

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
| Determinism check at temperature 0 (cases repeated; differing final statuses) | Before the tag, on development cases (GPU smoke test, 2026-11-20; `PROTOCOL.md` sec. 6): 4 cases run twice with one request at a time, 0 differing: the 12 final outputs (L1, A1, A2) and the 8 model traces were identical. With requests sent concurrently to the same server, 2 to 4 of 20 statuses differed between runs. Run 1 sends one request at a time and repeats no test case: a repeat would be a model call on the test set outside run 1 |
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

None as of 2026-11-24.

Entry format: date; run; commit; what failed; which case ids had no output; how the run was resumed. Cases with a usable record for every system are never run again; any other attempt is run again from its start (`PROTOCOL.md` sec. 11).

## 7. Analysis outputs

| Field | Value |
|---|---|
| Scoring commit | pending |
| Command | `python scripts/score_runs.py --cases <test set> --run <run 1> --gpu-hours <billed pod hours> --usd-per-hour <rate>`, run by `gpu/session.py` when run 1 is complete |
| `score.json` sha256 | pending |
| Figure 1 file and sha256 | pending |
| Date the results were first published | pending |

## 8. Development-set runs before the release

Runs on the development set `dev_s20261120_utc_n10` (200 cases) that choose a setting. Each rule was written before its run. Both runs used one pod on RunPod Community Cloud and sent 8 requests at a time to a server with 8 slots; on that setup 2 to 4 of 20 statuses changed between identical runs (GPU smoke test), so both rules were decided under that noise. Run 1 sends one request at a time (`PROTOCOL.md` sec. 6). Development accuracy is not a result (`PROTOCOL.md` sec. 10); the outputs are released with the rest.

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

**Rehearsals (no setting chosen).** A rehearsal runs the run-1 path on a real pod with the first 6 development cases (`--rehearsal`, `docs/SERVING.md`): download, checks, a killed session, a resume, scoring. Its records are not results.

- 2026-11-22, attempt 1 (`data/runs/rehearsal/r1`, commit d493abd, Community Cloud): the server never became healthy, and the session stopped after 3000 s and terminated the pod (0.84 billed pod hours, about $1.44). From the first reading the GPU showed 100 % utilization with no memory used (the pods of dev1 and T2 showed 0 %), and the download had not finished after 50 minutes (on the pods of dev1 and T2 the server was healthy after 18 and 21 minutes). The output of the start script was not kept. Changes before the next attempt: the pod refuses to start on a GPU that is busy before use or a download that is too slow; the start script's output goes to `start.log`; the waiter tries a new pod after such a host problem, up to 2 times.
- 2026-11-22, attempt 2 (`data/runs/rehearsal/r2`, commit a443cf0, Community Cloud): the first create failed because no instance was left (no pod, no cost) and the waiter launched again 33 s later. The pod's GPU was idle before use, the download (111 GB) finished in 1,732 s and the server was healthy after 1,796 s. After the first case the session was killed (SIGKILL); resumed 30 s later, it reused the live pod, completed the 6 cases and scored them, and the pod was terminated (DELETE 204, then 404). 0.57 billed pod hours, about $0.98. No runner change was needed.
