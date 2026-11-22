# gapcheck: pre-registration protocol

**Status: DRAFT (2026-11-20). Not frozen. The test set does not exist.**

This file is frozen by a tagged commit and a GitHub Release before the test set is determined (secs. 13–14). `SPEC.md` is the design and holds the full text of rules that are summarized here; at the tag the two must agree, and where they differ this file governs. Everything changed after the tag goes in `DEVIATIONS.md`. What happens after the tag is logged in `RUNS.md`.

Open fields carry the word FILL in square brackets and are completed before the tag. At the tag this paragraph and the status line above are replaced by the frozen status, and a search for "FILL" in this file finds nothing.

Synthetic patients. Ground truth is a guideline encoded as a decision table, not clinical judgment. The test-case mix is designed, so no count is a prevalence. Research prototype, not medical advice.

## 1. Question and hypotheses

When a statin care-gap agent says "statin indicated and not prescribed", is each part of that statement documented in the patient's record? And does the answer change when the deciding record entry changes, and only then?

Test: take synthetic longitudinal records, make controlled edits, and check whether each system's answer follows the edit. Ground truth comes from the guideline written as a decision table in code, plus a ledger of every edit. No LLM grades any output. This is an evaluation of an existing model inside three system designs; no model is trained or tuned.

Hypotheses, reported whatever the result:

- **H1.** A2 (agent with a code verifier) is more accurate than L1 (one call on the serialized record) on the primary outcome (sec. 7).
- **H2.** On C3a, C3b and C4 together (the conditions that plant a sentence in a note), A2 is more accurate than the keyword baseline R1. Measure: exact-status accuracy macro-averaged over those three conditions, A2 minus R1, paired.
- **H3.** A2 has a lower C1 fabricated-certainty rate than A1, and its over-abstention rate is not more than 5 percentage points higher than A1's in C2a and in C2b.

Verdict words, fixed here. Each comparison is a paired difference with a 95% interval (sec. 7). "Supported": the interval excludes 0 in the stated direction. "Contradicted": it excludes 0 in the other direction. "Unresolved": it includes 0. For the second part of H3: "met" if the upper limit of the interval of (A2 − A1) is at most 5 points in both conditions, "not met" if a lower limit is above 5, otherwise "unresolved". H3 is supported only if its first part is supported and its second part is met. The study is sized for estimation, not for a formal test; no multiplicity correction is applied and none is claimed.

Expectation, written before any test output: rule baselines should win wherever the answer sits in structured data. The agent can add value only on notes, and may still lose to the keyword rule there, because templated notes favor keywords.

## 2. Data and patches

**Generator.** Synthea v4.0.0 release jar `synthea-with-dependencies.jar`, sha256 `ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1`, unmodified, on JDK 17 (Temurin 17.0.21). Run only through `scripts/generate_synthea.sh <seed> <n_alive> <out_dir>`, which fixes every argument: `-s <seed> -cs <seed> -r 20261118 -e 20261118 -p <n_alive> -a 30-79`, Massachusetts, one generator thread, JVM time zone UTC, CSV export only, `exporter.years_of_history=0`. It writes `run_manifest.json` (jar hash, Java version, arguments, sha256 of every CSV). Deceased patients are excluded. Index date = date of the last encounter.

**Patches** (after generation, seeded, every added or removed row in `augmentation_ledger.csv`; full rules in `SPEC.md` sec. 4.1):

- P0: rows of the eGFR series in mL/min, which contradicts the diagnoses, are removed at load.
- P1: adults with no eGFR and no kidney-related diagnosis get an eGFR row at each wellness encounter with a BP panel, drawn for a healthy adult of the patient's age and sex (CKD-EPI reference values of Ma et al. 2021; `SPEC.md` sec. 4.1) and kept within 60–140. A patient with a P1 value can be a base patient only if the indication row is the same for every eGFR from 60 to 140, so no test label depends on an imputed value.
- P2: within each indication row of S2–S4, a seeded random half receives an active statin.

Order: generate → P0 → P1 → indication group → P2 within groups → oracle label. Notes are rendered by the harness from the final structured state plus the planted sentences; the generator's own notes are not used. Generator limits that are not patched are listed in `SPEC.md` secs. 4.1 and 11.

**Sets.** Development: fixed seeds 20261118, 20261119 and 20261120 (10,000 adults at seed 20261120 with the final settings; base patients sampled at 10 per stratum). A sizing batch (seed 5020261121, above the range of any test seed; 50,000 adults) was generated only to count eligible patients; no case set was written and no system was run on it. Everything seen so far, including every model output, comes from these sets. Test: 40,000 living adults, one batch, from the derived seed (sec. 13): `scripts/generate_synthea.sh <test seed> 40000 <out_dir>`.

**Seeds.** Sub-seed for label L (`P1`, `P2`, `sample`, `edits`, `phrases`) = `int(sha256("<seed>:<L>").hexdigest()[:16], 16)`. Base patients are taken in ascending order of `sha256("<sample sub-seed>:<stratum>:<patient id>")`.

**One batch.** No further batch is generated. A cell that is short keeps the patients it has; the shortfall is reported in `RUNS.md` and is not filled from another cell. At the rates measured before the tag (34 treated and 33 untreated eligible patients at risk ≥ 10% in 60,000 UTC adults), every cell fills with probability above 0.99.

## 3. Oracle (ground truth)

**Source.** 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on the Management of Dyslipidemia, J Am Coll Cardiol 2026;87(19):2624–2757, doi:10.1016/j.jacc.2025.11.016. Scope: starting a statin, yes or no. Risk: own implementation of the PREVENT base equations, 10-year and 30-year ASCVD (Khan et al., Circulation 2024), cross-checked as in `SPEC.md` sec. 3.5.

**Status values** (same for the oracle and every system): ON_THERAPY; GAP (a class 1 recommendation to start applies, no active statin, no documented reason); DOCUMENTED_REASON (would be GAP, but a reason is documented within 365 days before index); DISCUSS (only a class 2a or 2b recommendation applies); NOT_INDICATED; CANNOT_DETERMINE (a deciding input is absent or out of range; the missing inputs are named).

**Decision table** (first matching row wins; ages 30–79). Each row's guideline section, recommendation number, class, level of evidence and quoted sentence are in `reports/oracle_rules.md`, generated from the code.

| Row | Rule | Label |
|---|---|---|
| 0 | Active statin at index | ON_THERAPY |
| 1 | Clinical ASCVD, any age | GAP |
| 2 | Latest LDL-C ≥ 190 mg/dL, any age | GAP |
| 3 | Diabetes, age 40–75 | GAP |
| 3b | CKD stage 3 or 4, age 40–75, LDL-C 70–189 | GAP |
| 4 | Diabetes, age 76–79 | DISCUSS |
| 5 | Diabetes, age 30–39: a listed feature, or 10-year risk ≥ 3%, or 30-year risk ≥ 10%, or LDL-C 160–189 | DISCUSS, else NOT_INDICATED |
| 6 | Age 76–79, no ASCVD, no diabetes, LDL-C < 190 | DISCUSS |
| 7 | Latest LDL-C < 70 | NOT_INDICATED |
| 8 | 10-year risk ≥ 10% | GAP |
| 9 | 10-year risk 5% to < 10% | GAP |
| 10 | 10-year risk 3% to < 5% | DISCUSS |
| 11a | 10-year risk < 3%, and LDL-C 160–189 or (ages 30–59) 30-year risk ≥ 10% | DISCUSS |
| 11b | All other < 3% | NOT_INDICATED |

Encoding choices where the guideline leaves room, the reason taxonomy, the open- and closed-world input rules and the definition of a deciding input are fixed as written in `SPEC.md` secs. 3.2–3.4. Two choices are tested in sensitivity analyses (sec. 9). The others cannot affect a test label, because the sampling rules keep those patients out (sec. 4).

**Frozen version.** The oracle is the code at the tag commit. sha256 of the files that define ground truth, at the tag:

| File | sha256 |
|---|---|
| `src/gapcheck/oracle.py` | `[FILL]` |
| `src/gapcheck/prevent.py` | `[FILL]` |
| `src/gapcheck/prevent_coefficients.json` | `[FILL]` |
| `src/gapcheck/codes.py` | `[FILL]` |
| `src/gapcheck/facts.py` | `[FILL]` |
| `reports/oracle_rules.md` | `[FILL]` |

Last commit that changed the decision table before the tag: `d60c5a2` (2026-11-19, oracle row 5).

**Author checks of the oracle.** Two checks by the author, reported whatever they show:

1. Before the tag, the author read each row of the decision table, each documented-reason category and each convention in `reports/oracle_rules.md` against the guideline text (2026-11-22; [FILL: result and any change, with its commit]). An LLM agent had earlier listed the places where the guideline can be read more than one way; the author decided each of them (sec. 9 re-scores the main ones the other way).
2. After the tag and before any run 1 output is read, the author labels 30 development cases by hand, blind to the oracle label, the row, the condition and the edit. The cases come from `data/cases/dev_s20261120_utc_n10`: in ascending order of `sha256("20261122:handlabel:<case id>")`, a case is taken unless its expected status already has 5 or its base patient is already taken, so the 30 cases are 5 per status from 30 different patients (near-identical cards of one patient would show what was edited). They are shown in ascending order of `sha256("20261122:handlabel-order:<case id>")`. Each is shown as a card made from the case record by `scripts/hand_label_cards.py` (released with the results), with the PREVENT risks that the `prevent_risk` tool returns. Reported: agreement with the label of the tagged oracle (k of 30), and each disagreement with its cause: an error in the code, a row the author reads differently, or an author error. The primary analysis uses the oracle as tagged. If a disagreement is an error in the code, run 1 is also re-scored with the corrected oracle, reported as a deviation (sec. 12) with the number of test cases whose label changes.

## 4. Cohort, conditions and expected counts

**Base patients** (40 per stratum; rules and every exclusion in `SPEC.md` sec. 5.1):

| Stratum | Indication group | On statin | Off statin |
|---|---|---:|---:|
| S1 | Row 1, clinical ASCVD | 20 | 20 (at most half stroke-only) |
| S2 | Row 2, LDL-C ≥ 190 now and at least once before | 20 | 20 |
| S3 | Row 3, diabetes at 40–75 | 20 | 20 |
| S4 | Rows 8–9, 10-year risk ≥ 5% | 10 at ≥ 10%, 10 at 5 to < 10% | 10 and 10 |
| S5 | Rows 7 or 11b, NOT_INDICATED | – | 40 |

**Threshold sources** (not cases themselves; each gives two edited twins): 20 untreated patients within 1.5 points of the 5% risk cut and 20 of the 3% cut (stratum NT), and 20 untreated patients with latest LDL-C 160 to < 185 and no LDL-C ≥ 190 at any time (stratum LT).

**Conditions** (full definitions in `SPEC.md` sec. 5.2). Every edited case has a ledger row with the rows deleted or added, the sentence planted, and the correct label, computed by the oracle on the edited record and checked against the condition's expected direction.

| Condition | Edit | Correct behavior | Group |
|---|---|---|---|
| C0 | None | Oracle label | – |
| C1 | Every instance of one deciding input deleted (lipid panel, SBP or smoking status) | CANNOT_DETERMINE, with the deleted input in `missing` | Must change |
| C2a | The same deletion where an earlier row makes the input irrelevant | Unchanged | Must not change |
| C2b | Smoking status deleted where the label is the same for both values and PREVENT is still needed | Unchanged | Must not change |
| C3a | One sentence stating a reason, in one note within 365 days of index; GAP patients | DOCUMENTED_REASON | Must change |
| C3b | The same kind of sentence; NOT_INDICATED patients | Unchanged | Must not change |
| C4 | A distractor sentence in a note: negation, a relative's statin, deferral, stable liver disease; GAP and NOT_INDICATED patients | Unchanged | Must not change |
| C5 | One value moved across a cut: risk 5% or 3% (SBP by at most ±30 mm Hg or TC by at most ±50 mg/dL; twins land 0.5 to 1.0 point from the cut), LDL-C to 185 and 195, or smoking set to current | The new oracle label; a pair is used only if its two labels differ | Must change |

C1 and C2a use one deletion per base patient; if several inputs qualify, one is chosen by the `edits` sub-seed. C2b is kept.

**Phrases.** 30 test phrasings per planted fact type, each used at most once, disjoint from the 20 development phrasings per type. A phrasing whose words state the patient's sex (a pronoun, a spouse, pregnancy) is planted only in patients of that sex (`SPEC.md` sec. 5.2). Test phrasings are written by an author who reads neither the prompts, the R1 rules nor any run output, and the authors of the prompts and R1 never read the test phrasings; R1 was frozen before they were written (its last change predates the phrase file's first commit). Test phrasings are used in no run before the tag. The test phrasings were written by a separate agent (Claude Opus 5.5, commit `e8d3352`) that was told not to open the prompts, the R1 rules or any run output. Two reviews for clinical plausibility by separate LLM agents (Claude Fable 5.2) under the same rule read all 300, and no clinician reviewed them: the first reworded 17 (`61d892a`); the second, made before reading the first, found none wrong and 4 open to clinical dispute, and one of the 4 was reworded by a third such agent (`b05c7f5`). None was removed. The agent that wrote the prompts has read none of them; the project author, who wrote none of the prompts, was shown the 4 disputed phrasings in the second review (2026-11-20). Phrase file `src/gapcheck/phrases/test.json` (version `test-1`), sha256 at the tag: `[FILL]`.

**Expected counts.** The exact counts are fixed by the tagged code and the seed, not by the author, and are entered in `RUNS.md` before any model call.

| | C0 | C1 | C2a | C2b | C3a | C3b | C4 | C5 | Cases | Clusters |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Every cell filled | 200 | about 80 | 120 | about 40 | 80 | 40 | 120 | about 126 | about 805 | 260 |
| Simulated on the 10,000-adult development set (seed 20261120) | 186 | 71 | 114 | 36 | 72 | 40 | 112 | 125 | 756 | 246 |

In the simulation the two S4 cells at risk ≥ 10% were short by 6 (treated) and 8 (untreated) of 10; no other cell was short, and 3 twins were rejected by the build checks. In the 50,000-adult sizing batch (sec. 2, counts only) no cell was short: 803 cases in 260 clusters (C1 78, C2b 39, C5 126), with 9 twins rejected by the build checks. Clusters are base patients plus threshold sources.

C2b and C1-by-smoking are small; their intervals will be wide and are reported as such.

## 5. Systems and frozen prompts

| System | What it is |
|---|---|
| R0 | The oracle on structured data only (no notes). Deterministic. |
| R1 | R0 plus keyword and regular-expression rules over notes for reasons, with simple negation handling. Written from development phrasings only; frozen before test phrasings are written, by authors who never read them. |
| L1 | One conversation on a serialized record, with `prevent_risk` as its only tool. |
| A1 | Tool-using agent with nine tools (record queries, note search, `prevent_risk`, `guideline_lookup`). Each record tool is a tested, parameterized query; there is no free SQL. |
| A2 | A1 plus the code verifier. A failed check gives one repair round; a second failure forces CANNOT_DETERMINE with `missing` unchanged. A1 is A2's answer before verification, from the same run. |

R0 and R1 contain the oracle, so they are correct by construction on C0, C1, C2a, C2b and C5. This is stated wherever their numbers appear.

All systems get the same conventions page (decision table, encoding choices, input rules, code lists, reason taxonomy) and return the same output schema (`SPEC.md` sec. 6.1). The verifier checks provenance and arithmetic only (`SPEC.md` sec. 6.2): cited rows and quotes exist and match; the inputs required by the row the system itself names are covered; each computed risk is reproduced within 0.1 percentage point; each absence claim is confirmed by a fixed query. It never evaluates the decision table and never reads the ledger. On failure the system is told only the number of the failed check and the index of the claim.

A reply that is not one schema-valid JSON object gets one re-ask; the re-ask may use the rounds of tool calls left in the same limit. A case with no valid output after that, or with a request the server rejects, is scored as incorrect for that system. In A2, a repair that ends this way is a failed repair: the answer is the forced CANNOT_DETERMINE, scored like any forced fallback. A request the server rejects means HTTP 400 from llama-server. Any other failure of a request (another 4xx, a 5xx, a lost connection, no reply within 1,800 s) or a case not finished within 2,400 s is a failed attempt: the case is run again from its start, at most 2 attempts per invocation of the runner (`docs/SERVING.md`). A case whose 4th counted attempt fails (attempts are counted across resumes; an attempt the runner itself interrupts because of a stop signal or a pod that is gone is not counted) gets, written by the runner, a record for every system of that case with no output, schema_valid false and the error "attempts exhausted (4): <last error text>". It is logged in the timeline and listed in `status.json`, and it is scored as no valid output (incorrect).

**Harness settings** (the task prompts state the same limits to the model):

| Setting | Value |
|---|---|
| A1 rounds of tool calls (`max_steps`) | 8, chosen over 16 on the development set by a rule written before that run (`RUNS.md` sec. 8) |
| Round counter: after each round of tool calls in A1 and in the A2 repair, one message "Round k of N used." (`round_counter`) | on |
| Rounds-left notice (`rounds_left_notice`) | 0 (off) |
| A2 repair rounds of tool calls (`repair_steps`) | 8 |
| L1 rounds of `prevent_risk` calls (`l1_tool_rounds`) | 2 |
| Tool page size | 20 |
| L1 serialization | As in `SPEC.md` sec. 6: demographics; conditions and procedures by code; medications by drug; allergies; the last three values of each observation code on the conventions page; notes within 365 days before index |

**L1 build stop.** Before any model call, every test case is checked: each row the oracle uses and each planted sentence must be inside the L1 serialization, and the prompt must fit the served context. If a prompt does not fit, the served context length is raised until every test case fits, before any model call; this is logged in `RUNS.md` and is not a deviation. If the model cannot serve that length, the cases that do not fit are scored as incorrect for L1 and counted, and the primary analysis is repeated without them as a deviation.

**Frozen prompts.** sha256 at the tag. Every run manifest records the prompt and conventions hashes; the tag commit pins every other file, including `configs/run1.json` (the run-1 settings), `gpu/` (the serving and run scripts), `src/gapcheck/systems.py`, `schema.py` and `backends.py`:

| File | sha256 |
|---|---|
| `src/gapcheck/prompts/system.txt` | `[FILL]` |
| `src/gapcheck/prompts/task_l1.txt` | `[FILL]` |
| `src/gapcheck/prompts/task_agent.txt` | `[FILL]` |
| `src/gapcheck/prompts/reask.txt` | `[FILL]` |
| `src/gapcheck/prompts/repair.txt` | `[FILL]` |
| `src/gapcheck/prompts/limit.txt` | `[FILL]` |
| `src/gapcheck/prompts/round_counter.txt` | `[FILL]` |
| `src/gapcheck/prompts/rounds_left.txt` (loaded, not used: `rounds_left_notice` is 0) | `[FILL]` |
| Conventions page (`gapcheck.conventions.sha256()`) | `[FILL]` |
| `src/gapcheck/baselines.py` (R0, R1) | `[FILL]` |
| `src/gapcheck/verifier.py` | `[FILL]` |
| `src/gapcheck/tools.py` | `[FILL]` |
| `src/gapcheck/serialize.py` | `[FILL]` |
| `src/gapcheck/score.py` | `[FILL]` |

Prompts were developed on the development sets only. The repair prompt was revised once on the development set, by a rule written before that run (`RUNS.md` sec. 8).

## 6. Model: selection rule and decoding settings

One open-weights model with native tool calling, pinned by revision and file hash, on one rented GPU. The same model and settings serve L1, A1 and A2.

**Selection rule, written before the GPU test** (except the G4 amount, set on 2026-11-22 after the projection was known). Candidates in order of preference:

1. Qwen3.8-Flash-Next, GGUF build `unsloth/Qwen3.8-Flash-Next-GGUF`, quantization UD-Q4_K_XL, revision `766911a6b7369840a91dbcd95f9f997acaab6cd6`, served by llama.cpp (CUDA) on one RTX PRO 6000 (96 GB).
2. `Qwen/Qwen3.8-27B-FP8`, revision `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, served by vLLM on one L40S (48 GB).
3. Fallback if neither passes: one hosted model pinned to a dated snapshot (synthetic data only).

The first candidate that passes every gate is used. Candidate 1 is first because the same weights also run on a local machine, which the planned work on real notes requires (`SPEC.md` sec. 12). Gates, measured on the development set (the GPU smoke test of 2026-11-20 used that day's prompts; the default prompts changed twice after it, `b468daf` and `RUNS.md` sec. 8, and G2 was checked again in the development runs):

- G1. The pinned build loads, its file hashes match, and the served context holds the largest development L1 prompt plus the output allowance.
- G2. A schema-valid final answer on at least 95% of development cases, for L1 and for the agent run.
- G3. Concurrency check: on a fixed prompt of about 18,000 tokens that asks for 37 identifiers scattered through it, the number returned correctly is the same when the request is sent alone and when it is sent among concurrent requests.
- G4. Projected cost of run 1 on the test set is within USD 45.

Accuracy of any system on any set is not a criterion. Development accuracy is recorded and does not enter the choice.

**Chosen model** (filled after the GPU test):

| Field | Value |
|---|---|
| Candidate chosen and gate results (G1–G4, for each candidate tested) | Candidate 1; candidates 2 and 3 not tested. GPU smoke test, 2026-11-20, 60 development cases: G1 pass (the 4 file hashes match; largest development L1 prompt 32,197 tokens + 4,096 output ≤ 49,152 served); G2 pass (L1 60/60, agent 59/60; in the development runs of 2026-11-21, L1 200/200, A1 and A2 200/200 at 8 rounds in both runs and 198/200 at 16 rounds); G3 pass (37/37 alone and among 3 or 7 concurrent requests, 18,058-token prompt); G4 pass (run 1 projected at USD 37–41 with one request at a time) |
| Model repository, revision, quantization, file sha256 | `unsloth/Qwen3.8-Flash-Next-GGUF` at `766911a6b7369840a91dbcd95f9f997acaab6cd6`, UD-Q4_K_XL (GGUF of `Qwen/Qwen3.8-Flash-Next`), 4 files: `-00001-of-00004.gguf` `4448186216b3af4cc558bbce2c3213f01608f8f8b2e5267a9767971dd3ec8082`; `-00002` `3f342f1c1580473f1ee94ddd5b28206e8c07a70fa1a366f59d1d6c922919a6c9`; `-00003` `56758f40269cad5cd9b0d3d6fbae0f40f6d5be6de49e4ab392dbe83157d9cbd3`; `-00004` `753bda48b98ba4f1636134a90a967de1b2d3908a236c026e464777342e53510a` |
| License | Qwen Community License 1.0 (`qwen-community-1.0`, both repositories) |
| Training data cut-off stated by the publisher | Not stated on either model card (checked 2026-11-22) |
| Serving engine, version or commit, container image digest | llama.cpp `llama-server` b12464 (commit `d3c3e242`), CUDA server image `ghcr.io/ggml-org/llama.cpp@sha256:ae96e52f8b2106ea82bad786ee2d08bb9b0a8c93403df9c27ae1bf065a118089`; f16 KV cache, flash attention on, context shift off, multi-token prediction off |
| GPU, provider and tier | One NVIDIA RTX PRO 6000 Blackwell Server Edition (96 GB), rented on RunPod; the cloud tier of each pod is recorded in `RUNS.md` |
| Chat template file and sha256 | The metadata key `tokenizer.chat_template` of the first model file, extracted on the pod by `gpu/pod_start.sh` (no template file is shipped; the server refuses to start on a mismatch), 9,993 bytes, sha256 `12827f24b742ea4e80cdc12dbcf9622227056b9f797252a3149263d4f9aaadce`, passed to the server with `--chat-template-file`; the same bytes as in the development runs |
| Served context length; `context_limit`; `chars_per_token` | 49,152 tokens per request slot (8 slots, one request at a time); 45,056; 2.26 |

**Decoding settings:**

| Setting | Run 1 (primary) | Runs 2 and 3 |
|---|---|---|
| Temperature | 0 | 0.7 (the model card's non-thinking value) |
| top_p, top_k | not sent | 0.8 and 20 (model card) |
| Presence or repetition penalty | none | none (a penalty works against exact quotes and repeated row ids) |
| Seed | 1 | 2 and 3 |
| Thinking | off (`enable_thinking: false`) | off |
| Output limit (`max_tokens`) | 4096 | same |
| Parallel tool calls | allowed | allowed |
| Speculative or multi-token decoding | off | off |
| Client concurrency | 1 (one request at a time) | same |

Temperature 0 departs from the model card's sampling advice; it is chosen so that run 1 has no sampling noise. Temperature 0 is not assumed to be deterministic on these engines: before the tag, 4 development cases were run twice with one request at a time (GPU smoke test, 2026-11-20); the final outputs and the model's tool-call traces were identical. With concurrent requests on the same server, 2 to 4 of 20 statuses changed between runs, which is why run 1 sends one request at a time. Both are reported in `RUNS.md` with the results.

Tool-call parser failures are counted per case and reported: a reply that ends with a tool-call signal and no call; a call with empty arguments for a tool with required parameters; arguments that do not parse.

## 7. Primary outcome and analysis

**Scoring.** Exact status against the oracle label (C0) or the ledger label (C1–C5). C1 is correct only if the status is CANNOT_DETERMINE and the deleted input is in `missing`. Code: `src/gapcheck/score.py`, run by `scripts/score_runs.py`; frozen at the tag (sec. 5).

**Primary outcome.** Exact-status accuracy, macro-averaged over the eight conditions (C0, C1, C2a, C2b, C3a, C3b, C4, C5; each weighted equally, so the designed case mix does not drive the number).

**Primary comparison.** A2 minus L1, paired, on run 1. A2 − L1 combines retrieval by tools and the verifier, so A1 − L1 and A2 − A1 are reported beside it.

**Interval.** 95% percentile interval from a cluster bootstrap:

- A cluster is one base patient with all of its cases, or one threshold source with its two twins.
- Clusters are resampled with replacement within stratum (S1–S5, NT, LT) × statin status at base, each cell to its own size.
- In every resample, each condition's accuracy and the macro-average are recomputed for all systems from the same resampled clusters; a condition with no case in a resample is left out of that resample's average.
- 10,000 resamples; NumPy `default_rng(20261120)`. Point estimates come from the full set.

**Precision.** Expected half-width of the interval of A2 − L1, simulated from development results: about ±2.5 points (260 clusters drawn with replacement from the 64 development clusters, A2 from the second GPU development run and L1 from the first; 4,000 draws; on the 64 development clusters themselves, ±5.2). For scale: a condition with 80 cases gives a single-proportion half-width of about ±7 points at 0.90 and ±11 at 0.50.

## 8. Secondary outcomes

All pre-specified, all with 95% intervals from the same bootstrap, all on run 1 unless stated:

1. Accuracy per condition for R0, R1, L1, A1, A2 (Figure 1).
2. Sensitivity and specificity for GAP over all cases.
3. C1 fabricated-certainty rate: a schema-valid status other than CANNOT_DETERMINE although the deciding input is absent. C2a and C2b over-abstention rates: status CANNOT_DETERMINE.
4. C5 pair consistency: both members of a pair correct.
5. C3a miss rate (status other than DOCUMENTED_REASON). Answer-change rate in C2a, C2b, C3b and C4, each and pooled: the status differs from the same system's status on the same patient's C0 case; an invalid or missing output on either case counts as changed. C3a citation overlap with the planted sentence.
6. Unsupported-claim rate: share of DOCUMENTED and ABSENT claims in final answers that fail verifier check 1 or 4. Beside it, the share of final answers that pass all four verifier checks, for L1, A1 and A2 (A2's answers pass or are forced, by construction).
7. Run-to-run stability: share of cases with the same status in runs 1, 2 and 3, for L1, A1 and A2 (sec. 11).
8. Tool calls, tokens and seconds per case; schema-invalid outputs; forced fallbacks per condition; parser failures; verifier first-round rejections by check number, and how many were repaired or forced; cost per case: GPU-hours times the hourly rate, divided by the number of cases. GPU-hours are the billed hours of every pod of run 1, over all sessions and resumes, each from its creation to its termination; the rate is the billed USD estimate (each pod at its own price, disk included) divided by those hours. The case-queue wall time is reported separately and is not a cost.
9. Sensitivity analyses (sec. 9).
10. The H2 and H3 comparisons of sec. 1.

## 9. Sensitivity analyses

Both re-score the saved run 1 outputs; no model is run again. Truth and prediction are relabelled the same way; a prediction is relabelled by the row the system names in `benefit_group`.

- **S-A.** Row 8 (10-year risk ≥ 10%) relabelled DISCUSS. Reason: the ≥ 10% recommendation is class 1 but worded conditionally ("in whom LLT is initiated").
- **S-C.** Strict GAP: GAP only for rows 1, 2, 3 and 3b; rows 8 and 9 relabelled DISCUSS.

Reported for each: the primary outcome and comparison, and accuracy per condition, in a table.

## 10. Reporting rules

The primary outcome (sec. 7) is reported wherever results are reported, whatever its value.

Counts are written with their denominator first and then the rate: "k of n (p%, 95% CI lo to hi)". No rate is shown without k and n. Wherever R0 or R1 appears, the "correct by construction" note appears with it.

Display. Results are shown first as three rows that follow one example patient's edits: record as is (C0), one sentence added to a note (C3a) and lipid panel deleted (C1, any deciding input). On the two structured-data rows the agent (A2) gives "k of n" and the rule column is labelled as the answer key, not scored, because the rule is correct there by construction. The note row gives "k of n" for the rule (R0), the keyword rule (R1) and the agent (A2). Results are not shown as a bar chart of the five systems.

Wherever A2's result on a condition is shown, the number of A2's forced fallbacks on that condition is shown with it. A forced CANNOT_DETERMINE is correct on C1 and incorrect on every other condition.

Wherever A2's accuracy is shown, A1's (the same run before verification) is shown with it, and the difference A2 − A1 on the primary outcome is reported wherever H1 is reported. The condition on which A2 is weakest is named directly under the three rows, with k of n and its forced fallbacks.

Wording. A2 is called an agent with a code verifier, not self-checking or self-correcting: code re-checks the cited rows, quotes and computed risks, and the model gets one repair. An answer that passes the checks is described as having its evidence re-checked, not as correct; the verifier does not evaluate the decision table.

Examples shown with the results are chosen by the author and labelled as such. All outputs are released (sec. 15).

No development-set accuracy is reported as a result.

## 11. Run plan and stopping rule

- **Run 1 is the primary analysis.** All five systems, the whole test set, once, at temperature 0. Its results are published whatever they show. Every outcome in this protocol refers to run 1, except stability.
- **Only run 1 is reported first.** The first report of results covers run 1 and is published before runs 2 and 3 are made.
- **Runs 2 and 3 come later** and serve only secondary outcome 7 (stability). L1, A1 and A2, non-zero temperature, seeds 2 and 3 (sec. 6; `configs/run2.json`, `configs/run3.json`). Until they are done, `RUNS.md` lists them as pending. They never replace or change a run 1 number. If they are not run, `RUNS.md` says so and outcome 7 is reported as not done.
- **No interim analysis.** Test outputs are not scored until run 1 is complete for all systems.
- **Complete** means every test case has, for every system, an output or a logged error, the records of exhausted cases (sec. 5) included.
- **Crashes.** If a run stops (lost connection, server error, machine failure, a budget stop), it is resumed with the same commit, model, server settings and prompts, and the event is logged in `RUNS.md`. A case with a usable record for every system (sec. 5: an output, an HTTP 400 record or an exhausted record) is never run again; any other attempt is run again from its start, and its files are kept in `aborted/` and released.
- **Order.** The test set's sha256 is committed to `RUNS.md` before the first model call (sec. 14).

## 12. Deviations

- Every departure from this protocol after the tag is entered in `DEVIATIONS.md` with: the protocol sentence, what was done, why, when it was decided (before the seed; after the seed and before any model output; after outputs were seen), what it affects, and the commit.
- Where both can be computed, the result under the protocol and the result under the deviation are reported side by side.
- A change to the protocol before the test seed is derived needs a new tag and Release. The seed then comes from the new commit and the pulse that follows the new Release; the earlier tag stays public and is listed in `RUNS.md`. Once a seed has been derived, the protocol is not re-released to obtain another.
- A code change after the tag that alters the test set is a deviation, reported with both results. A code change that alters neither the test set nor the scoring (for example a crash fix in the runner) is logged in `RUNS.md` and `DEVIATIONS.md` with its commit. Such a fix may change only `gpu/`, `tests/gpu/`, `docs/`, `RUNS.md`, `DEVIATIONS.md` and `results/`; the runner refuses to resume run 1 at a commit that changes anything else (`--runner-fix`, `docs/SERVING.md`).
- If there are no deviations, `DEVIATIONS.md` says "None as of <date>".

## 13. Test-set seed

The seed is fixed by two things the author cannot choose: the tag commit and a public random value published after the Release.

```
test_seed = int( sha256( protocol_commit_sha + beacon_value ).hexdigest()[:8], 16 )
```

- `protocol_commit_sha`: the 40-character lowercase hexadecimal id of the commit the Release tag points to.
- `beacon_value`: defined below, exactly as published.
- The two strings are joined with nothing between them and hashed as ASCII. Shell form: `printf '%s%s' "$SHA" "$VALUE" | sha256sum | cut -c1-8`, read as a base-16 integer.
- T = the Release's `published_at` time, as returned by the GitHub API, rounded up to a whole minute, plus 2 minutes (UTC). For example, `published_at` 14:03:27Z gives T = 14:06:00.000Z, and 14:03:00Z gives 14:05:00.000Z.
- Reason: the pulse must be published after the protocol commit and the Release are public, so nobody, the author included, could know it when the protocol was fixed; the 2 minutes cover clock differences between GitHub and NIST.

**Primary beacon: NIST Randomness Beacon, version 2.0** (`https://beacon.nist.gov/beacon/2.0`, one pulse per minute).

- Pulse: the pulse with the smallest `timeStamp` at or after T, on the chain the endpoint serves at that time (chain 2 on 2026-11-19). Normally its `timeStamp` equals T.
- Request: `GET https://beacon.nist.gov/beacon/2.0/pulse/time/<T in milliseconds since the Unix epoch>`. The pulse before it (same chain, `pulseIndex` − 1) is fetched as a check; its `timeStamp` must be earlier than T.
- `beacon_value` = the pulse's `outputValue`: 128 hexadecimal characters, upper case as published.
- The pulse `uri`, `timeStamp`, `statusCode` and `outputValue` are copied into `RUNS.md`.

**Fallback beacon: drand, League of Entropy `quicknet` chain** (chain hash `52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971`, genesis 1692803367, one round every 3 seconds).

- Used only if no request made between T and T + 12 hours returns a NIST pulse that meets the rule above (at least three attempts, at least one hour apart, each logged in `RUNS.md` with its time and HTTP status).
- Round: r = (T in seconds − 1692803367) / 3 + 1, the round emitted at T (the division is exact for any whole minute).
- Request: `GET https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/<r>`.
- `beacon_value` = the `randomness` field: 64 hexadecimal characters, lower case as published. It equals the sha256 of the round's signature, which is checked.

Once a seed has been derived from either source it is final. The first source that meets its rule decides; the other is not consulted.

Both beacons were checked on 2026-11-19 (UTC): each returned its latest pulse and the pulse addressed by time as above. Worked example of the arithmetic, not the test seed: commit id of 40 zeros and the NIST pulse of 2026-11-19T00:00:00.000Z (chain 2, pulse 2033839) give a digest beginning `19aa106d`, so the seed would be 430575725.

## 14. Freeze and release: order of work

Visible in the commit history and in `RUNS.md`:

1. Development phrasings; R1 frozen; test phrasings written by a separate author who does not read the prompts, R1 or run outputs (prompt authors never read the test phrasings); prompts frozen. Test phrasings are used in no run before the tag.
2. Model chosen by the rule in sec. 6; every `[FILL]` in this file completed.
3. Tag on the protocol commit, pushed to the public repository, and published as a GitHub Release (server-side time stamp).
4. Beacon pulse at T; seed derived; both recorded in `RUNS.md`.
5. Test set built by the tag commit, unchanged, from a clean working tree; its sha256 (the sha256 of the case set's `manifest.json`, which lists the sha256 of every file) committed to `RUNS.md` and pushed before any model call. Commands: `docs/SERVING.md` sec. 11.
6. Run 1, and during it the author's hand labels (sec. 3), finished before any run 1 output is read; scoring; results published with `RUNS.md` and `DEVIATIONS.md` updated.

External registration: none.

Approved by the author: `[FILL: name, date]`.

## 15. Released with the results

The synthetic test set, the augmentation and edit ledgers, all model outputs and traces, run manifests, `score.json`, the hand-label cards with the author's labels and their comparison with the oracle (sec. 3), and the code. `make reproduce` rebuilds data, patches and edits, runs R0 and R1, re-scores the saved model outputs and redraws the figures from a clean clone. Re-running the LLM systems needs a GPU and is a separate target. Reporting follows TRIPOD-LLM (`docs/TRIPOD-LLM.md`).

## References

- Blumenthal RS, et al. 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on the Management of Dyslipidemia. J Am Coll Cardiol 2026;87(19):2624–2757. doi:10.1016/j.jacc.2025.11.016.
- Khan SS, et al. Development and validation of the American Heart Association's PREVENT equations. Circulation 2024;149(6):430–449. doi:10.1161/CIRCULATIONAHA.123.067626.
- Walonoski J, et al. Synthea. J Am Med Inform Assoc 2018;25(3):230–238. doi:10.1093/jamia/ocx079.
- Ma Y, Zhan J, Xu G. Reference values of glomerular filtration rate for healthy adults in southern China: a cross-sectional survey. Ther Adv Chronic Dis 2021;12:20406223211035287. doi:10.1177/20406223211035287.
- Gallifant J, et al. The TRIPOD-LLM reporting guideline for studies using large language models. Nat Med 2025;31(1):60–69. doi:10.1038/s41591-024-03425-5.
- NIST Randomness Beacon, version 2.0. https://beacon.nist.gov/beacon/2.0 (format: NIST Interagency Report 8213, draft, "A Reference for Randomness Beacons: Format and Protocol Version 2").
- drand, League of Entropy, `quicknet` chain. https://api.drand.sh (chain information at `/<chain hash>/info`).
- Prior work and what this adds: `SPEC.md` sec. 2.
