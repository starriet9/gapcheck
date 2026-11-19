# TRIPOD-LLM checklist (draft)

Reporting guideline: Gallifant J, et al. The TRIPOD-LLM reporting guideline for studies using large language models. Nat Med 2025;31(1):60–69. doi:10.1038/s41591-024-03425-5. The item list below was checked against Table 2 (checklist) and Table 3 (abstracts) of the published statement on 2026-11-19. Item wording is abbreviated here; the statement has the full text.

**Status: draft, written before any test result.** "At tag" means the answer is fixed when the protocol is frozen. "With results" means it can only be filled after run 1. "Planned" marks a file that does not exist yet. `[FILL]` marks a statement only the author can make.

## Study type

- Research design (statement Table 1): **LLM evaluation (E)**, with an **LLM methods (M)** part (a way to evaluate an LLM system with controlled record edits). Not de novo development (D). Not an evaluation in a healthcare setting (H): no patients, no clinical workflow.
- LLM task: **classification (C)**. Each system assigns one of six status labels to a patient record.
- Items the statement limits to other designs or tasks are listed as "not applicable" with the reason. Three H-only items (14c, 14d, 15) are answered anyway.

## Checklist

| Item | Topic | Applies here | Where the repository answers it | Status |
|---|---|---|---|---|
| 1 | Title names the study as an LLM evaluation, with task, population and outcome | Yes | README title and one-page summary title: evaluation of an LLM-based statin care-gap system; synthetic adults aged 30–79; status label | Planned |
| 2 | Abstract | Yes | One-page summary in abstract form; see the abstract table below | Planned |
| 3a | Healthcare context, rationale, existing approaches | Yes | `SPEC.md` secs. 1–2; `PROTOCOL.md` sec. 1 | Answered |
| 3b | Target population, intended use and users in the care pathway | Yes (E) | `SPEC.md` sec. 1 ("What this is not"), sec. 3 (GAP is a prompt for review by a clinician), sec. 9 (patient card). Not deployed; no users were studied | Answered |
| 4 | Objectives; development, tuning or evaluation | Yes | `PROTOCOL.md` sec. 1: evaluation only; no model is trained or tuned | Answered |
| 5a | Data sources for training, tuning and evaluation; rationale | Yes | `PROTOCOL.md` sec. 2; `SPEC.md` sec. 4. No training or tuning data. Development and test sets are separate Synthea populations | Answered |
| 5b | Data points and their distribution; language, country | Yes | `reports/gate_*.md` (cohort and strata counts); `PROTOCOL.md` sec. 4 (designed case mix); `RUNS.md` sec. 3 (test set counts). English; synthetic residents of Massachusetts, USA | Dev: answered. Test: with results |
| 5c | Dates of the oldest and newest text | Yes | `SPEC.md` sec. 4 (records run from birth to the reference date 2026-11-18); notes are rendered by the harness; phrase library dates are in the commit history; model training cut-off in `PROTOCOL.md` sec. 6 | At tag |
| 5d | Preprocessing and quality checks | Yes | `SPEC.md` sec. 4 and 4.1 (P0, P1, P2, augmentation ledger, byte-identical regeneration check, note rendering); `tests/` | Answered |
| 5e | Missing and imbalanced data; reasons for omitting data | Yes | `SPEC.md` sec. 3.4 (open- and closed-world inputs, CANNOT_DETERMINE), sec. 5.1 ("Not sampled", every rule counted in the gate report); `PROTOCOL.md` sec. 7 (macro-average over conditions because the mix is designed) | Answered |
| 6a | LLM name, version, last date of training | Yes | `PROTOCOL.md` sec. 6 (model fields); `RUNS.md` sec. 4 | At tag |
| 6b | Model development process, architecture, alignment | No (M, D only; no model is built or tuned) | The model is used as released; repository, revision and file hash in `PROTOCOL.md` sec. 6 | Not applicable |
| 6c | How text was generated: prompts, consistency, inference settings | Yes | `PROTOCOL.md` secs. 5–6; `src/gapcheck/prompts/`; `src/gapcheck/conventions.py`; every run's `manifest.json` (prompt hashes, settings); run-to-run stability is secondary outcome 7 | At tag |
| 6d | Initial and post-processed output | Yes | `SPEC.md` sec. 6.1 (output schema); `src/gapcheck/systems.py` (`parse_answer`: one JSON object, one re-ask); A2 repair round and forced fallback (`SPEC.md` sec. 6.2). Raw replies and traces are released | Answered |
| 6e | Classification details and rationale; thresholds | Yes (C) | `SPEC.md` sec. 3 and `reports/oracle_rules.md` (six labels, decision table with guideline quotes). No model probabilities are used and no threshold is set on model output | Answered |
| 7a | Quality metrics for generated text | Not required for classification | Reported anyway: unsupported-claim rate and C3a citation overlap (`PROTOCOL.md` sec. 8) | With results |
| 7b | Relevance of the metrics to the task at deployment; agreement with human evaluation | Yes (E) | `PROTOCOL.md` secs. 7–8; `SPEC.md` secs. 1 and 11: accuracy against the encoded guideline, not a clinical performance claim; no human evaluation of outputs | Answered |
| 7c | Outcome definition, how predictions were computed, date of inference, metrics | Yes (E) | `PROTOCOL.md` sec. 7; `src/gapcheck/score.py`; inference dates in `RUNS.md` sec. 5 | Definition: answered. Dates: with results |
| 7d | Subjective outcome assessment: assessors, agreement | Yes | None. Scoring is by code against the oracle and the edit ledger; no human rater and no LLM judge (`PROTOCOL.md` sec. 7) | Answered |
| 7e | Comparison with other LLMs, humans, benchmarks | Yes | `PROTOCOL.md` secs. 5 and 7: rule baselines R0 and R1, single call L1, agent A1, agent with verifier A2, all on the same cases. One model; no human comparison | Answered |
| 8a | Annotation method and guidelines | Yes | No human annotation. Labels are computed: oracle on the record (C0) and edit ledger (C1–C5). `PROTOCOL.md` sec. 3; `reports/oracle_rules.md` | Answered |
| 8b | Number of annotators, double annotation, agreement | Yes | Not applicable to computed labels. The decision table was checked against the guideline text by the author; planted sentences were reviewed on a sample (`PROTOCOL.md` sec. 4) `[FILL: sample size and who reviewed]` | At tag |
| 8c | Annotators' background, or models involved in labelling | Yes | No model assigns a label. `[FILL: author's background; how the phrase library was written]` | At tag |
| 9a | Prompt design, curation and selection | Yes | `PROTOCOL.md` secs. 5 and 14 (order of work); commit history of `src/gapcheck/prompts/` | At tag |
| 9b | Data used to develop the prompts | Yes | Development sets only (fixed seeds 20261118–20261120, development phrasings). The test set does not exist when the prompts are frozen; test phrasings are disjoint | Answered |
| 10 | Preprocessing before summarization | No (summarization tasks only) | – | Not applicable |
| 11 | Instruction tuning or alignment | No (M, D only; none done) | – | Not applicable |
| 12 | Compute | Yes | `SPEC.md` sec. 10 (plan); `RUNS.md` sec. 5 (GPU-hours, cost); secondary outcome 8 (tool calls, tokens, seconds per case) | With results |
| 13 | Ethics approval and consent | Yes | Synthetic records only; no human participants or patient data; no ethics review was sought. Stated in the README `[FILL: confirm wording]` | Planned |
| 14a | Funding and role of funders | Yes | README `[FILL]` | Planned |
| 14b | Conflicts of interest | Yes | README `[FILL]` | Planned |
| 14c | Protocol access | H only; answered | `PROTOCOL.md` at the release tag; release URL in `RUNS.md` sec. 1 | At tag |
| 14d | Registration | H only; answered | Not in a trial or study registry. Time-stamped by a tagged commit and a GitHub Release; the test seed comes from a public randomness beacon pulse after the release (`PROTOCOL.md` sec. 13). Any external registration is listed in `RUNS.md` sec. 1 | At tag |
| 14e | Data availability | Yes | `SPEC.md` sec. 8: test set, augmentation and edit ledgers, model outputs and traces are released; generator data is public domain | With results |
| 14f | Code availability | Yes | This repository; `make reproduce` (`Makefile`) | Code: answered. Re-scoring of saved test outputs by `make reproduce`: with results |
| 15 | Patient and public involvement | H only; answered | None | Answered |
| 16a | Flow of records through the study; numbers per label | Yes (E) | Gate report (cohort, candidates, eligible, each exclusion rule); `RUNS.md` sec. 3 (clusters, cases per condition, expected label counts) | With results |
| 16b | Characteristics by data source and split; dates; sample size | Yes (E) | Gate reports for the development and test sets; characteristics table (age, sex, stratum, statin status) for both sets | Planned |
| 16c | Clinical variables compared between development and evaluation data | Yes (E) | Same table as 16b, development and test side by side | Planned |
| 16d | Number of records and events in each analysis | Yes (E) | `score.json` (`n_cases` per condition); every reported rate is shown as k of n (`PROTOCOL.md` sec. 10) | With results |
| 17 | Performance by the pre-specified metrics | Yes | Project page and README; Figure 1; `score.json`; pre-specified in `PROTOCOL.md` secs. 7–10 | With results |
| 18 | Results of model updating | No (no updating) | – | Not applicable |
| 19a | Interpretation, including fairness | Yes | One-page summary; fixed sentences in `PROTOCOL.md` sec. 10. Fairness across demographic groups is not assessed: the case mix is designed and no subgroup analysis is pre-specified; stated as a limit | With results |
| 19b | Limitations: bias, uncertainty, generalizability | Yes | `SPEC.md` sec. 11 (fixed text) | Answered |
| 19c | Known problems with the data for this task | Yes (E) | `SPEC.md` sec. 4.1 (generator limits, measured, with their handling) | Answered |
| 19d | Intended use, input, end user, autonomy and oversight | Yes (E) | `SPEC.md` secs. 1, 3 and 9: a flag for a clinician to review, never an action; research prototype, not for clinical use | Answered |
| 19e | Handling of poor or missing input at use | Yes (E) | `SPEC.md` sec. 3.4 (CANNOT_DETERMINE with the missing inputs named); condition C1 tests it | Answered |
| 19f | User interaction and expertise needed | Yes (E) | `SPEC.md` sec. 9 (patient card for a clinician). Not tested with users | Answered |
| 19g | Next steps | Yes | `SPEC.md` sec. 12 | Answered |

## Abstract items (statement Table 3), for the one-page summary

| Item | Topic | Applies here | Source for the summary | Status |
|---|---|---|---|---|
| 2a | Title | Yes | As item 1 | Planned |
| 2b | Context and rationale | Yes (E) | `SPEC.md` secs. 1–2 | Planned |
| 2c | Objectives | Yes | `PROTOCOL.md` sec. 1 | Planned |
| 2d | Study setting | Yes | Synthetic longitudinal records; no clinical setting | Planned |
| 2e | Data, splits, selective use | Yes | `PROTOCOL.md` secs. 2 and 4 | Planned |
| 2f | LLM name and version | Yes | `PROTOCOL.md` sec. 6 | Planned |
| 2g | Model-building steps | No (M, D only; none) | – | Not applicable |
| 2h | Task, inputs and outputs | Yes | `PROTOCOL.md` sec. 5; `SPEC.md` sec. 6.1 | Planned |
| 2i | Evaluation data, endpoint, held-out status, measures | Yes | `PROTOCOL.md` secs. 7 and 13 (test set created after the protocol) | Planned |
| 2j | Main results | Yes | The three headline numbers of `PROTOCOL.md` sec. 10 | With results |
| 2k | Implications and concerns | Yes | `SPEC.md` sec. 11 | With results |
| 2l | Registration | H only; answered | As item 14d | At tag |
