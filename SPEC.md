# gapcheck: design specification

Status: draft v0.4 (2026-11-19). This file is the design. `PROTOCOL.md` (the pre-registration) is written from it and frozen before the test set is determined. Anything changed after that goes in `DEVIATIONS.md`.

## 1. Question

When a statin care-gap agent says "statin indicated and not prescribed", is each part of that statement documented in the patient's record? And does the answer change when the deciding record entry changes, and only then?

The test: take synthetic longitudinal records, make controlled edits (delete the deciding fact, put a reason for not prescribing in a note, add a distractor, move a value across a threshold), and check whether each system's answer follows the edit. Ground truth comes from the guideline written as a decision table in code, plus a ledger of every edit. No LLM grades any output.

What this is not: a prevalence estimate, a clinical performance claim, or a comparison with clinicians. Synthetic patients; ground truth is the guideline as encoded here, not clinical judgment; the mix of test cases is designed. Research prototype, not medical advice.

## 2. Prior work and what this adds

None of the parts is new on its own.

- Counterfactual, pre-registered edits to chronological patient packets, with a tool-using agent: Turk 2026, "Counterfactual Evaluation Reveals Hidden Capability Profiles in Clinical LLMs and Agents", arXiv 2605.30590 (oncology tumor-board cases).
- Claim-level evidence grounding and an "Indeterminate: Lack of Data" verdict over longitudinal EHR data, with adjudicated reference verdicts: CliniCARE-Bench, arXiv 2608.07796.
- Paired act/abstain twins outside medicine: AgentAbstain, arXiv 2607.10059.
- Counterfactual perturbation of clinical notes without agents: DeVisE, arXiv 2506.15339.
- Agent benchmarks on EHR environments: MedAgentBench (arXiv 2501.14654), PhysicianBench (arXiv 2605.02240).
- Reasons for statin non-use live in notes: Sarraju et al., Commun Med 2022;2:88; Gobbel et al., Am J Prev Cardiol 2022;9:100300.

This project extends counterfactual evaluation of clinical agents (Turk 2026) to one preventive-care decision: statin initiation under the 2026 ACC/AHA guideline, PREVENT-based. What differs from the two closest papers, as far as their public descriptions show:
1. Against Turk 2026: edits that must not change the answer (C2, C4) next to edits that must (C1, C3, C5); a correct abstention counts as correct; ground truth is computed by code from a quoted guideline table, not scored by an LLM judge.
2. Against CliniCARE-Bench, which scores claim-level grounding and a "lack of data" verdict on unedited records: controlled twins of the same patient with exact ground truth.
3. Each cited claim, including claims that something is absent, is checked against the record by code.
4. The protocol is published before the test set is determined (sec. 8).

## 3. Guideline and oracle

**Source.** 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on the Management of Dyslipidemia. J Am Coll Cardiol 2026;87(19):2624–2757, doi:10.1016/j.jacc.2025.11.016; Circulation, doi:10.1161/CIR.0000000000001423. Every oracle row in code carries the section, recommendation number, class of recommendation (COR), level of evidence (LOE) and the quoted sentence.

**Scope.** Initiation of a statin, yes or no. Dose intensity, LDL-C goals and non-statin therapy are out of scope. Not encoded, because the data cannot trigger them or they do not decide initiation: coronary artery calcium (sec. 4.2.3.6 recs 1, 3 and 5, COR 1; sec. 4.2.7); risk enhancers and reproductive risk markers (secs. 4.2.3.3 and 4.2.3.4, COR 2a; they personalize the discussion inside DISCUSS and never create a COR 1 start); familial hypercholesterolemia (sec. 4.2.4.4); Lp(a) (sec. 4.2.10); persistently elevated triglycerides (sec. 4.2.9 rec 5, COR 1: estimate PREVENT risk to guide the discussion, which the oracle does anyway); ischemic HFrEF (sec. 4.2.6 rec 8, COR 2b; no ejection-fraction data). Encoded in the oracle but excluded from the cohort: HIV (sec. 4.2.8.9 rec 1, COR 1) and end-stage renal disease, dialysis or kidney transplant (sec. 4.2.8.8 rec 3 covers only continuation; supportive text 3: starting a statin on dialysis "has not been shown to be beneficial"). The PREVENT base model is used; optional inputs (albuminuria, HbA1c, zip code) are not (sec. 11). `reports/oracle_rules.md`, generated from the code by `scripts/oracle_sheet.py`, lists every row with its quotes, effective scope, encoding choices and data limits.

**Status values** (same for the oracle and for every system).

| Status | Meaning |
|---|---|
| ON_THERAPY | An active statin prescription at the index date (not a claim about adherence). |
| GAP | A COR 1 recommendation to start a statin applies (table below), no active statin, no documented reason. |
| DOCUMENTED_REASON | Would be GAP, but a reason for not prescribing is documented (sec. 3.3). |
| DISCUSS | Only a COR 2a or 2b recommendation applies. Never counted as a gap. |
| NOT_INDICATED | No COR 1, 2a or 2b recommendation to start a statin applies. |
| CANNOT_DETERMINE | A deciding input is missing or outside its valid range (sec. 3.4). |

GAP is a prompt for review, not a finding of an error in care. The guideline asks for a clinician–patient discussion before lipid-lowering therapy in primary prevention (sec. 4.2.3.7 rec 1 and sec. 5.1 rec 1, both COR 1), and take-home message 3 says therapy "should be considered" at 5 to < 10%.

### 3.1 Decision table (first matching row wins; patients aged 30–79 at index)

| # | Group | Rule | Label | Guideline source |
|---|---|---|---|---|
| 0 | Any | Active statin at index | ON_THERAPY | — |
| 1 | Clinical ASCVD | Any qualifying code on or before index: ACS, MI, stable or unstable angina, coronary or other arterial revascularization, stroke, TIA, PAD including aortic aneurysm. No age limit. | GAP | Sec. 4.2.6 recs 1 and 4, COR 1, A ("high-intensity statin therapy should be initiated"); definition in the sec. 4.2.4.3 footnote. Table 1 shows the 2018 limit ("75 years of age or younger") removed in 2026. |
| 2 | Severe hypercholesterolemia | Latest LDL-C ≥ 190 mg/dL. No age limit. | GAP | Sec. 4.2.4.3 rec 2, COR 1, B-R ("treatment with maximally tolerated statin therapy is recommended"). Rec 1 (COR 1, B-NR) asks that secondary causes be excluded first; not encoded. |
| 3 | Diabetes, 40–75 | Diabetes, age 40–75 | GAP | Sec. 4.2.5 rec 1, COR 1, A ("moderate-intensity statin therapy is indicated"). No LDL-C floor: take-home message 8, "regardless of LDL-C level". |
| 3b | CKD, 40–75 | Active diagnosis of CKD stage 3 or 4, age 40–75, latest LDL-C 70–189 | GAP | Sec. 4.2.8.8 rec 1, COR 1, B-R ("an LDL-C of 70 to 189 mg/dL"). Defined by diagnosis code, not by a single eGFR value. Take-home message 8 says "regardless of LDL-C level"; see row 7. |
| 4 | Diabetes, > 75 | Diabetes, age 76–79 | DISCUSS | Sec. 4.2.5 rec 6, COR 2b, C-LD. Life-expectancy condition not encoded. |
| 5 | Diabetes, 30–39 | Diabetes, age 30–39. DISCUSS if any of: type 2 diabetes for ≥ 10 years, albuminuria or proteinuria, CKD stage 3 or higher, retinopathy, neuropathy (diagnosis codes); or 10-year risk ≥ 3%; or 30-year risk ≥ 10%; or LDL-C 160–189. Otherwise NOT_INDICATED (row 7 if LDL-C < 70, else row 11b). | DISCUSS, else NOT_INDICATED | Sec. 4.2.5 rec 7, COR 2b, C-LD (guideline range 20–39). Figure 9 (20–39 branch): "For adults ≥30 y of age, PREVENT ASCVD 10-y risk ≥3% or 30-y risk ≥10%" leads to a moderate-intensity statin in a COR 2a box, "No" to healthy behavior counseling (COR 1). Figure 6, low-risk branch: LDL-C 160–189 leads to a moderate-intensity statin (COR 2a). Encoding choice, sec. 3.2. ABI and type 1 diabetes do not occur in the data. |
| 6 | Older adults | Age 76–79, no ASCVD, no diabetes, LDL-C < 190 | DISCUSS | Sec. 4.2.8.3 rec 2, COR 2b, B-NR. Encoding choice, sec. 3.2. Life-expectancy condition not encoded. |
| 7 | LDL-C < 70 | Latest LDL-C < 70 mg/dL | NOT_INDICATED | No recommendation to start a statin applies: sec. 4.2.3.7 and Figure 6 cover LDL-C 70–189. Rec 12 (COR 3: No Benefit, B-NR) covers the subset with non-HDL-C < 100 and no other risk factors. Take-home message 8 recommends LDL-lowering therapy for CKD stage 3–4 at 40–75 "regardless of LDL-C level", sec. 4.2.8.8 rec 1 only at LDL-C 70–189; the oracle follows rec 1. Row-7 base patients are sampled only if robust (sec. 5.1). |
| 8 | PREVENT ≥ 10% | 10-year ASCVD risk ≥ 10% | GAP | Sec. 4.2.3.7 rec 7, COR 1, A, supportive text 7, and Figure 6. Encoding choice, sec. 3.2. |
| 9 | PREVENT 5 to < 10% | 10-year risk 5% to < 10% | GAP | Sec. 4.2.3.7 rec 5, COR 1, A ("at least a moderate-intensity statin is recommended") |
| 10 | PREVENT 3 to < 5% | 10-year risk 3% to < 5% | DISCUSS | Sec. 4.2.3.7 rec 4, COR 2a, A (conditional on a decision to treat) |
| 11a | PREVENT < 3%, elevated | 10-year risk < 3%, and LDL-C 160–189 (ages 30–75) or 30-year risk ≥ 10% (ages 30–59 only) | DISCUSS | Sec. 4.2.3.7 rec 3, COR 2a, C-LD; Table 1 and Figure 6 (low-risk branch) give the LDL-C 160–189 condition without an age limit. Encoding choice, sec. 3.2. |
| 11b | PREVENT < 3%, otherwise | All other < 3% (and diabetes at 30–39 not matched by row 5) | NOT_INDICATED | Sec. 4.2.3.7 rec 2 and Table 1, COR 1, A: health behavior counseling, no statin. Figure 6, low-risk branch: "No" leads to health behavior counseling (COR 1); for diabetes at 30–39, the "No" branches of Figure 9. |

Thresholds apply to the recorded LDL-C and the unrounded risk (LDL-C 189.6 is below 190). Excluded from the cohort (HIV; ESRD, dialysis, transplant) is checked before row 1. Given the rows before them, rows 8–11b are reached only at ages 30–75 with LDL-C 70–189, no ASCVD, no diabetes at 40–75 and no CKD stage 3–4 at 40–75; diabetes at 30–39 reaches only rows 2, 5, 7 and 11b.

### 3.2 Encoding choices, fixed here and in `PROTOCOL.md`

**PREVENT ≥ 10% is GAP.** Rec 5 (5 to < 10%) is an unconditional COR 1 statin recommendation. Rec 7 (≥ 10%) is COR 1 but worded conditionally: "in whom LLT is initiated for primary prevention, high-intensity statin therapy is recommended". Supportive text 7 calls high-intensity LLT "first-line LDL-C–lowering therapy for primary prevention of ASCVD in individuals at high risk (≥10%)", and the high-risk branch of Figure 6 reads "Start high-intensity statin" in a COR 1 box. Labelling a patient GAP at 5–9.9% but not at ≥ 10% would be incoherent.
Sensitivity analysis S-A: row 8 relabelled DISCUSS. It is small (about 10 untreated base patients at ≥ 10% and their twins).

**Ages 76–79 in primary prevention are DISCUSS.** Sec. 4.2.3.7 covers ages 30–79; the older-adults recommendation (COR 2b) covers every adult over 75. The guideline does not say which applies at 76–79. DISCUSS is the conservative choice. Rows 1–2 carry no age limit and still apply. Primary-prevention patients aged 76–79 are not sampled as base patients or twins, so this choice affects no test label; it stays in the oracle and the vignette tests.

**Strict GAP.** Sensitivity analysis S-C: GAP only for rows 1, 2, 3 and 3b; rows 8 and 9 relabelled DISCUSS.

**Diabetes at 30–39 follows Figure 9.** Supportive text 7 of sec. 4.2.5 and its synopsis send adults with diabetes over 30 to the primary-prevention algorithm (Figure 6), which would give GAP at ≥ 5%. Figure 9 draws the same age group differently: 10-year risk ≥ 3% or 30-year risk ≥ 10% leads to a moderate-intensity statin in a COR 2a box at any risk level, and "No" to healthy behavior counseling. Where the guideline reads two ways, the oracle takes DISCUSS, as for ages 76–79. The same rule covers LDL-C 160–189 at low risk (10-year < 3%, 30-year < 10%, no row-5 feature): Figure 9 gives counseling, Figure 6's low-risk branch a moderate-intensity statin (COR 2a), so DISCUSS. Diabetes outside 40–75 is not sampled, so this affects no test label.

**LDL-C 160–189 at low risk counts at any age 30–75 (row 11a).** Rec 3 of sec. 4.2.3.7 opens with "In adults aged 30 to 59 years" and repeats "(for those aged 30-59 years)" after the 30-year risk; Table 1 and Figure 6 give the LDL-C 160–189 condition without an age limit. The oracle reads the age limit as applying to the 30-year risk only. Patients and twins aged 60–75 with risk < 3% and LDL-C 160–189 are not used (sec. 5.1), so this affects no test label.

**Other conventions.** Thresholds are applied without rounding. "Latest" means the latest value on or before the index date, with no lookback limit in phase 1 (stale labs are a phase 2 condition). Rec 12 of sec. 4.2.3.7 reads "baseline untreated LDL-C"; the oracle uses the latest LDL-C even if measured during past statin use (sec. 11). If two values of a deciding input share the latest timestamp, the oracle takes the one with the smallest row id, and the patient is not sampled as a base patient. Diabetes excludes prediabetes. Ischemic heart disease code 414545008 counts as clinical ASCVD (an encoding choice). Statins are matched by ingredient, including ezetimibe/simvastatin. Code lists are published with the oracle.

### 3.3 Documented reasons

A documentation taxonomy, not a judgment that the reason is clinically valid. Categories follow Sarraju 2022 and Gobbel 2022, with the guideline section where it supports the category:
- statin intolerance or statin-attributed muscle symptoms (sec. 4.2.11 recs 1–2; sec. 4.2.5 rec 2);
- patient declined (no recommendation of its own; the sec. 4.2.3.7 synopsis lists "patient preferences" among the factors to consider);
- pregnancy, planned pregnancy or lactation, women aged 30–49 only (sec. 4.2.8.4 rec 1, COR 1; Table 25 lists lactation as a statin contraindication and notes "pregnancy is not a contraindication, but statins should be avoided while lactating");
- decompensated cirrhosis or acute liver failure (Table 25, statin contraindications: "Acute liver failure; decompensated cirrhosis"; stable chronic liver disease is not a reason: sec. 5.1 rec 3, COR 2a);
- hospice, palliative care or limited life expectancy (sec. 4.2.3.7 rec 11; sec. 4.2.8.3 rec 3).

A pregnancy reason is recognised whatever the patient's ASCVD risk, also in rows 1–2, although rec 1 is limited to persons "not at high risk for ASCVD" and rec 5 (COR 2b) allows continuing a statin in pregnancy with FH or clinical ASCVD: the taxonomy classifies what is documented, not whether stopping is right.

A reason needs an explicit refusal, intolerance, contraindication or care goal. Deferral ("will revisit next visit") is not a reason. A reason counts if it is dated within 365 days before the index date, in a note or in a structured entry: an allergy or intolerance record that names a statin ingredient, "statin(s)" or "HMG-CoA reductase inhibitor" as whole words (so nystatin or cilastatin do not count), a pregnancy condition, or a hospice or palliative-care encounter (305336008), procedure or referral (385763009, 306237005) or diagnosis (ICD-10-CM Z51.5, encounter for palliative care). Procedures that only use the word "palliative" for symptom relief, such as palliative dental treatment (D9110), do not count. A statin allergy record active at index (no stop date, or a stop date after index) counts whatever its start date; an ended one counts only if its stop date is within the 365 days. The 365-day window is a project convention: no guideline sentence sets it (the closest is sec. 4.2.8.4 supportive text 2, on deferring statins "for a period of 1 year" around pregnancy and lactation in HoFH). No structured lactation code exists in the generated data; lactation is documented only in notes. A note reason needs no structured corroboration. A reason changes GAP to DOCUMENTED_REASON only. The card still shows the patient: "gap with a documented reason, review", with the reason's date and source.

### 3.4 Inputs, deciding inputs and CANNOT_DETERMINE

**Closed-world inputs** (absence means "no"; never missing): clinical ASCVD (any qualifying code on or before index, stop dates ignored), diabetes, CKD stage, the row-5 features (active diagnosis codes), statin and BP treatment (start ≤ index and no stop or stop > index), type 2 diabetes duration (from the earliest type 2 diabetes code, in days / 365.25). Age and sex are always present.

**Open-world inputs** (absence means unknown): the lipid panel (TC, HDL-C and LDL-C of the latest panel, from one date; a later LDL-C without TC and HDL-C is not used), systolic BP, smoking status, eGFR. LDL-C has no valid range; if TC or HDL-C is outside its PREVENT range, only TC and HDL-C are unknown.

An open-world input X is **deciding** for a patient if the oracle returns more than one label when X is set to every admissible value while all other inputs keep their recorded values. Admissible values: smoking {current, not current}; SBP 90–200 by 1 mm Hg; eGFR 15–150 by 1; lipid panel: TC 130–320 and HDL-C 20–100 by 5 mg/dL, LDL-C from 30 to TC − HDL-C by 5, plus 69, 70, 159, 160, 189, 190. (Testing only the ends of a range is not enough: PREVENT is not monotone in SBP.) The 30-year risk is computed from the same inputs and is not an input of its own.

The oracle returns CANNOT_DETERMINE, naming every such X, when a deciding X is absent or outside its valid range. An input made irrelevant by an earlier row is never deciding. When several open-world inputs are missing, they are varied jointly over their admissible grids, and every input whose variation changes the label is named. A vignette test checks the consequence: the lipid panel is deciding for every patient not settled by rows 0, 1 or 3 (row 3b needs LDL-C 70–189).

### 3.5 PREVENT implementation

- Own implementation of the PREVENT base equations, 10-year and 30-year ASCVD (Khan et al., Circulation 2024;149:430–449). Coefficients transcribed from the paper's supplement (Supplemental Tables S12A, base 10-year, and S12F, base 30-year), with the table cited in code. The supplement's worked examples (Tables S12A and S12F) are the first unit tests. They fix smoking, statin and eGFR < 60 at zero and SBP above 110, so those terms are checked only against the packages and the AHA calculator.
- Inputs: sex, age, total cholesterol, HDL-C, systolic BP, BP treatment, statin use, diabetes, current smoking, eGFR. BMI is not in the ASCVD base equation, so BMI never causes CANNOT_DETERMINE.
- Valid ranges, as given in Tables S12A and S12F: age 30–79, TC 130–320 mg/dL, HDL-C 20–100, SBP 90–200, eGFR 15–150. The 30-year risk is used only at ages 30–59: the guideline uses it only there (sec. 4.2.3.7 recs 2–3), `pyprevent` rejects it above 59 and `preventr` flags it as questionable, although Table S12F lists 30–79. Both packages stop at eGFR 140, so cross-check rows with eGFR 141–150 are compared with the AHA calculator only.
- Cross-checks, all pinned:
  - `pyprevent` 0.1.7 (MIT) on 1,000 random valid inputs (with a valid BMI, which it validates).
  - `preventr` 0.12.2 (MIT, R) on the same inputs, in a project-local R environment, skipping SBP > 180 (which it rejects). Its public output is rounded to three decimals, so the comparison uses its internal full-precision path. If R cannot be run without a system install, this check is dropped and recorded as a deviation.
  - 10 hand entries in the AHA online PREVENT calculator.
  - Agreement: absolute difference < 0.1 percentage point on every row. A larger difference blocks the oracle until explained.
- At least 30 hand-written vignette tests of the decision table, including every row boundary.

## 4. Data

**Generator.** Synthea v4.0.0 release jar (`synthea-with-dependencies.jar`, sha256 `ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1`, tag commit `0185c09e`), run on JDK 17. The jar is not modified. Generated data is public domain per the Synthea wiki; the software is Apache-2.0.

**Settings.** `-a 30-79`, fixed `-s`, `-cs`, `-r` (reference date) and `-e` (simulation end date; without it open encounters take the wall-clock time), one generator thread (with several, row order changes between runs), Massachusetts. The JVM time zone is pinned to UTC (`TZ=UTC` and `-Duser.timezone=UTC`, recorded in `run_manifest.json`): Synthea writes date-only columns (birth and death dates, condition and allergy start and stop) in the JVM default zone and timestamps in UTC, and in some zones the generated patients themselves differ. With UTC every date in the data is a UTC date, the rule `load.py` and `facts.py` apply everywhere. `exporter.years_of_history = 0`, `exporter.csv.export = true`, `exporter.fhir.export = false`; claims files excluded. Deceased patients excluded. CSV tables (patients, encounters, conditions, observations, medications, procedures, allergies) loaded into DuckDB. Index date = last encounter date. A check confirms that two runs with the same arguments give byte-identical CSV files (verified: 16 of 16 files on a 2,500-person run; with the zone pinned, also between callers in different time zones, on the 10,000-person dev set).

**Record ids.** Every row gets an id from a hash of its content and its occurrence number among rows with identical content, so a deletion changes no other row's id, leaves no visible gap, and patched rows do not sit at the end. A mapping links base and twin ids for the dashboard toggle.

**Sets.** Dev: fixed seeds, including the day-0 pilots (10k, seed 20261119; 2.5k, seed 20261118), which shaped design decisions and are therefore development data. Dev base patients come from a 10,000-person set (seed 20261120) generated with the final settings, including the UTC time zone. The day-0 pilots were generated in the machine's local zone (Asia/Seoul) and cannot be reproduced byte for byte with the pinned zone. Test: 40,000 living adults in one batch (`-p 40000`) from the derived seed (sec. 8), with no further batch; a short cell keeps the patients it has (`PROTOCOL.md` sec. 2). On the 10,000-adult dev set the S4 ≥ 10% cells have 4 treated and 2 untreated eligible patients, too few for 10 each; a 50,000-adult sizing batch (counts only, no case set, no system run) had 30 and 31, and every other cell filled (`PROTOCOL.md` secs. 2 and 4).

### 4.1 Known Synthea limits (measured at day 0, 9,862 adults aged 30–79) and patches

All patches run after generation, from seeds, and write every added or removed row to `augmentation_ledger.csv`. Patch code and ledgers are public. Order of operations:
generate → P0 → P1 → indication group (first matching row among 1–11, ignoring row 0) → P2 within groups → oracle label. A stratum is an indication group; "on a statin" is the row-0 result.

| Limit | Consequence | Handling |
|---|---|---|
| Two eGFR series share LOINC 33914-3. The series in mL/min/{1.73_m2} (wellness and kidney-stage modules) follows the patient's kidney state. The series in mL/min (hospital, cardiac and veteran panels) is drawn from fixed ranges and contradicts the diagnoses: half of its latest values are < 15, also among adults with no CKD code. 81% of adults have no value in the first series. | PREVENT cannot be computed for most adults, and some values are wrong. | **P0:** rows of the mL/min series are removed at load and listed in the ledger. Creatinine is not an oracle input, and eGFR is never recomputed from it (the generator back-calculates creatinine, and CKD-EPI on it gives eGFR < 60 in 38% of adults with no kidney, diabetes or hypertension code). **P1:** for adults with no eGFR and no kidney-related diagnosis, add an eGFR row at each wellness encounter at age ≥ 18 that has a BP panel, in the format of the first series. Value: drawn for a healthy adult of the patient's age and sex, from the CKD-EPI eGFR mean and SD by age decade and sex in 20,930 healthy adults (Ma, Zhan and Xu 2021, Table 1; men 114.5 ± 10.4 at 20–29 and 79.6 ± 9.2 at ≥ 70, women 121.1 ± 8.3 and 82.2 ± 7.9), interpolated between decade midpoints. A patient keeps one level (within 2.5 SD) across visits, and each visit adds about half an SD of its own variation; a value outside 60–140 is redrawn (`patches.P1_REFERENCE`). The source is a southern Chinese population and does not name its CKD-EPI version; PREVENT uses CKD-EPI 2021. The generator's own draw for people without kidney damage (uniform at every age; men 77–160, women 56–161, `biometrics.yml`) was used before 2026-11-20 and gave a 72-year-old an eGFR of 122. A patient with a P1 value can be a base patient only if the indication row (not just the label) is the same for every eGFR from 60 to 140, so no test label depends on an imputed value. At 10k this removes 50 of 304 S4 and 276 of 3,143 S5 candidates. |
| Non-ASCVD statin users all come from one veteran-only module and share one pattern (diagnosis 55822004 Hyperlipidemia, simvastatin 10 mg, fixed-range labs). S2 has 6 statin users at 10k. | Treated and untreated patients differ in ways a model could exploit; S2 fails the gate. | Adults with 55822004 are not sampled into S2–S5. **P2:** within S2–S4, randomized separately within each indication row (so rows 8 and 9 are each split evenly), a seeded random half receives a statin (start at a past wellness encounter at age ≥ 30, active at index; drug and dose drawn from the generator's statin submodule; every column filled as in native rows). |
| ASCVD prevalence 25%, 96% on a statin. No codes for PAD, TIA, aortic aneurysm or stable angina are generated. Untreated ASCVD (94 at 10k): 43 stroke only, 37 revascularization without MI, 11 MI, 3 ischemic heart disease only. | Row 1 is tested on MI, revascularization, ischemic heart disease and stroke only. | Untreated S1 is sampled with at most half stroke-only; the types are reported. |
| About 8% of adults (826 of 9,883 in the dev set) have a diabetic-complication diagnosis (most often 127013003, disorder of kidney due to diabetes mellitus) or a glucose-lowering drug, but no diabetes code: the CKD module codes hypertension-driven nephropathy as diabetic kidney disease. | The record contradicts itself on diabetes; the oracle reads diabetes codes only, while a reader of the record may take the patient as diabetic. | Not patched. These patients are not sampled (sec. 5.1, `diabetes_evidence_without_diagnosis`); the oracle is unchanged. |
| No adult is a current smoker at index (the generator's quit rate is at least 1% per week). | Smoking never raises PREVENT risk as generated. | No population patch. Smoking enters through edits only (C1, C2b, C5). Stated as a limit. |
| Lipids are redrawn at every panel (within-patient LDL-C SD about 28 mg/dL); statins do not lower later values. 398 non-ASCVD adults had LDL-C ≥ 190 once and < 190 at the latest panel. | "Latest" and "ever" give different S2 groups. | Keep "latest". S2 base patients: latest LDL-C ≥ 190 and at least one earlier value ≥ 190. S3–S5 base patients and LDL-C twins: no LDL-C ≥ 190 at any time. |
| Synthea's note exporter holds no numeric results, repeats the end-of-simulation smoking sentence in every note, and (because of an inverted condition) leaves active chronic conditions and ongoing medications out of its lists. | Its notes cannot carry edits and misstate the record. | Notes are rendered by the harness from the final structured state (after patches and edits), in Synthea's section layout, listing conditions, medications and smoking status as of each encounter date, with no numeric results, plus planted sentences from the edit ledger. A test greps every C1 note for the deleted input. |
| LDL-C is labelled "direct assay" but computed with Friedewald (the guideline prefers Martin/Hopkins or Sampson, sec. 3.2). eGFR is generator-drawn. Cardiovascular events are generated with the Pooled Cohort Equations (`CardiovascularDiseaseModule` calls `ASCVD.ascvd10Year`). | Values are internally consistent but not physiological; who has ASCVD follows PCE, not PREVENT. | Not patched. Stated as limits. |

## 5. Cohort and conditions

### 5.1 Strata (base patients)

| Stratum | Definition (indication group) | Test n | Dev n |
|---|---|---|---|
| S1 | Row 1: clinical ASCVD | 40 | 10 |
| S2 | Row 2: LDL-C ≥ 190 (sec. 4.1 rule) | 40 | 10 |
| S3 | Row 3: diabetes, age 40–75 | 40 | 10 |
| S4 | Rows 8–9: age 30–75, LDL-C 70–189, 10-year risk ≥ 5% | 40 | 10 |
| S5 | Rows 7 or 11b: NOT_INDICATED | 40 | 10 |

Within S1–S4, half on a statin and half not (native in S1; P2 in S2–S4). S4 is split evenly between 5 to < 10% and ≥ 10%. DISCUSS-zone patients appear only as C5 twins.

**Not sampled** (each count reported): any PREVENT input out of range where PREVENT is needed (no clamping); native eGFR > 150 where PREVENT is needed; S2 patients who also meet row 3 (their lipid panel is not deciding, so C1 and C5 cannot change their label); primary prevention at 76–79; diabetes outside 40–75; row 3b (CKD); row-7 patients (LDL-C < 70) whose label is not NOT_INDICATED when LDL-C is set to 70, with a P1 eGFR at any eGFR 60–140 (their label rests on the gap between rec 12 and the 70–189 recommendations: 10-year risk ≥ 3%, 30-year risk ≥ 10% at 30–59, or CKD stage 3–4 at 40–75, for which take-home message 8 reads "regardless of LDL-C level"); ages 60–75 with risk < 3% and LDL-C 160–189 (row 11a by the encoding choice in sec. 3.2; also not used as C5 twins); any structured reason within 365 days of index, including pregnancy (sec. 4.2.8.4 rec 1 tells pregnant patients to stop a statin; excluded at any age or sex, so no base patient carries a pregnancy entry the oracle ignores) and hospice or palliative care, so every base label is set by the stratum rule and C3 alone adds reasons; HIV; ESRD, dialysis, transplant; diagnosis 55822004; P1 patients whose label depends on eGFR; patients whose record shows diabetes without a diabetes code (`diabetes_evidence_without_diagnosis`: a diabetic-complication diagnosis or a glucose-lowering drug, `codes.DIABETIC_COMPLICATIONS` and `codes.GLUCOSE_LOWERING_DRUGS`, on or before index, and no diabetes code 44054006 or 427089005 on or before index; a generator artifact, sec. 4.1; also not used as C5 sources).

**Day-0 gate** (result in `reports/gate_<set>.md`, aggregate counts only): every stratum ≥ 50 eligible patients after the exclusions, and S1–S4 ≥ 15 on and ≥ 15 off a statin. At 10,000 adults as generated, S1 and S3–S5 pass and S2 fails on statin users; P2 is the fix.

### 5.2 Conditions (record edits)

Each edited twin has a ledger row: base patient, condition, the rows deleted or added, note sentences planted, and the correct label after the edit (computed by the oracle on the edited record, then checked against the condition's expected direction).

| Condition | Edit | Applied to | Correct behavior | What it tests |
|---|---|---|---|---|
| C0 clean | None | All | Oracle label | Basic competence |
| C1 deciding fact deleted | Every instance of one open-world input (lipid panel, SBP, smoking status) removed, where it is deciding | S2, S4, S5 | CANNOT_DETERMINE with the deleted input in `missing` | Noticing versus filling in |
| C2a non-deciding fact deleted | Same deletion where an earlier row makes the input irrelevant | S1, S3, ON_THERAPY patients | Unchanged | Over-abstention |
| C2b non-deciding fact deleted, PREVENT still needed | Smoking status deleted where the label is the same for both values (for example S4 at risk ≥ 5% either way) | S4, S5 | Unchanged | Over-abstention when abstaining is tempting |
| C3a reason only in a note | One sentence states a reason (sec. 3.3) in a note chosen at random within 365 days of index. Structured data unchanged. | GAP patients | DOCUMENTED_REASON | Where structured rules are blind |
| C3b reason where it does not matter | Same sentence | NOT_INDICATED patients | Unchanged | Reason sentence alone does not decide |
| C4 distractor in a note | Negation ("denies muscle aches"), a relative's statin, "statin discussed, will revisit next visit" (reported on its own line), "fatty liver, liver tests stable" | GAP and NOT_INDICATED patients | Unchanged | Keyword-triggered errors |
| C5 threshold twin | One value moved across a cut: LDL-C of the latest panel 185 ↔ 195 (TC moved by the same amount); SBP or TC moved so risk crosses 5% or 3%; or every smoking row in the 3 years before index set to "Smokes tobacco daily" | S2, S4, S5 and near-threshold patients | The new oracle label | Computing versus guessing |

C5 rules: a pair (base and one twin, or two twins) is used only if the two labels differ. Each twin is at least 0.5 percentage point from a risk cut and at least 5 mg/dL from an LDL-C cut. LDL-C is never exactly 190. Near-threshold sources: untreated primary-prevention patients (rows 9, 10, 11b) whose 10-year risk is within 1.5 points of 5% or 3% give two twins, one on each side of the cut, by moving SBP (at most ±30 mm Hg) or TC (at most ±50 mg/dL); the source itself is not a case. LDL-C twins move the latest panel upward only (to 195), and only in patients with no LDL-C ≥ 190 at any time, so a twin never mixes the threshold with the latest-versus-ever convention.

Phrase library: 20 dev phrasings and 30 test phrasings per planted fact type (each test phrasing is used at most once, and the test scale needs about 30 per distractor type). Dev and test use disjoint phrasings. The library is reviewed by a clinician on a sample and then frozen.

Each phrasing records the patient sex its words state (`sex`: F, M or none); one with a sex is planted only in patients of that sex. Words that state it: a pronoun that refers to the patient ("She developed …", "his dad"), a spouse ("Pt's husband"), pregnancy and sex-specific anatomy. A pronoun or pregnancy word that can refer to a relative named before it states nothing ("Father stopped his statin", "Daughter is pregnant"). Every pregnancy reason is a woman's, and sentences of the pregnancy category (reasons and distractors) are planted only in women, as before. Loading a library checks every `sex` against these words (`gapcheck.phrases.SEX_WORDS`). Before 2026-11-20 only the pregnancy rule applied, and one dev note of a man read "She developed …". A draw takes the first unused phrasing, in a seeded order, that the patient may receive, so a type cannot run short if, for each sex, the phrasings that sex may receive are at least as many as all draws of the type. At the test scale (about 30 draws per distractor type and about 25 per reason type other than pregnancy, `PROTOCOL.md` sec. 4) that means 30 test distractor phrasings per type that any patient may receive (no sex, not the pregnancy category), and at most 5 per sex in each of those reason types.

Scope rule: if the schedule slips, C2b is the first condition dropped (recorded in `PROTOCOL.md` before the tag, not after); the limit then reads "C2 covers only inputs made irrelevant by an earlier row". C3b is kept.

Expected size: about 200 base patients and about 805 test cases if every cell is filled; 756 when simulated on the 10,000-adult dev set (counts per condition: `PROTOCOL.md` sec. 4). C2b and C1-by-smoking are small; their intervals will be wide and are reported as such. Exact counts are fixed by the tagged code and the test seed and entered in `RUNS.md` before any model call.

## 6. Systems

Every system receives the same conventions page: the decision table of sec. 3.1 with its conventions (sec. 3.2), the input rules of sec. 3.4, the code lists, and the reason taxonomy of sec. 3.3.

| System | What it is |
|---|---|
| R0 | The oracle on structured data only (no notes). Deterministic. |
| R1 | R0 plus keyword and regular-expression rules over notes for reasons, with simple negation handling. Written from dev phrasings only and frozen before test phrasings are written. |
| L1 | One model call on a serialized record, with `prevent_risk` as its only tool (at most two tool rounds). Serialization, fixed in the protocol: demographics; conditions and procedures, one line per code (first start, last stop); medications, one line per drug (first start, last stop, active or not); allergies; the last three values of each observation code on the conventions page; notes dated within 365 days before index. The build stops if, for any test case, a row used by the oracle or a planted sentence falls outside the serialization or the context limit. |
| A1 | Tool-using agent. Tools: `get_demographics`, `search_conditions`, `get_observations(code, since)`, `get_medications`, `get_allergies`, `search_notes`, `read_note`, `prevent_risk(inputs with source ids)`, `guideline_lookup(row)`. Each tool is a tested, parameterized DuckDB query (no free SQL). |
| A2 | A1 plus the code verifier (sec. 6.2). A failed check gives one repair round; a second failure forces CANNOT_DETERMINE with `missing` unchanged. A1 is A2's pre-verification answer from the same run. |

R0 and R1 are correct on C0, C1, C2 and C5 by construction, because they contain the oracle. This is stated wherever their numbers appear.

**Model.** One open-weights model with native tool calling, pinned by revision and file hash, served on one rented GPU (96 GB or 48 GB, by candidate). The candidates, their order and the gates that choose between them (schema-valid output on ≥ 95% of dev cases is one; accuracy is not a criterion) are in `PROTOCOL.md` sec. 6. Pre-specified fallback: if no candidate passes, use one hosted model pinned to a dated snapshot (synthetic data only). Run 1 at temperature 0; runs 2 and 3 at a fixed non-zero temperature with recorded seeds, for stability.

### 6.1 Output schema (every system)

```
status             one of the six values in sec. 3
benefit_group      oracle row id the system applies, or null
risk_10y           PREVENT 10-year ASCVD risk, if computed
missing            list of absent open-world inputs
evidence           list of {claim, support, source, value}
  support = DOCUMENTED | COMPUTED | ABSENT | INFERRED
  DOCUMENTED: a record row id, or a note id with the quoted text (exact substring)
  COMPUTED:   a prevent_risk call whose inputs are all DOCUMENTED
  ABSENT:     a named list (statins, ASCVD codes, diabetes codes, CKD codes, reasons within 365 days)
  INFERRED:   no source; allowed only outside the inputs required by benefit_group
note_for_clinician two sentences
```

### 6.2 Verifier

Code only. It checks provenance and arithmetic. It never evaluates the decision table and never reads the ledger.
1. Every DOCUMENTED source exists and its value or quoted text matches. For closed-world claims the conventions page defines the value as the codes of the cited rows (CKD: the stage number); in that format every code and stage must be a cited row's. Free text describing the cited rows is accepted and not compared, since the row-kind check already ties the claim to its rows.
2. For the row the system itself states in `benefit_group`, every input in that row's fixed list of required inputs has a DOCUMENTED, COMPUTED or ABSENT claim. The list per row is a static table. A required binary input that is missing may instead be covered by two `prevent_risk` calls (one per value, other inputs documented) whose results give the same label.
3. Every COMPUTED risk is recomputed from its cited inputs and matches within 0.1 percentage point.
4. Every input in `missing`, and every ABSENT claim, is confirmed by re-running the fixed query.

On failure the system is told only the number of the failed check and the index of the claim. The verifier does not check whether a cited value is the latest one, or whether an absent input was left out of `missing`; both would leak the answer.

## 7. Evaluation

**Ground truth.** Oracle label for C0; ledger label for C1–C5.

**Scoring.** Exact status. C1 is correct only if status is CANNOT_DETERMINE and the deleted input is in `missing`. C3a is scored on status; whether the cited text overlaps the planted sentence is a secondary outcome. Forced fallbacks are counted per condition.

**Primary outcome.** Exact-status accuracy, macro-averaged over the conditions (C0, C1, C2a, C2b, C3a, C3b, C4, C5, each weighted equally, so the designed case mix does not drive the number). Comparison: A2 minus L1, paired. 95% interval by cluster bootstrap: resample base patients within stratum × statin status, carry all their twins, recompute each condition's accuracy and the macro-average for both systems in the same resample; 10,000 resamples. Run 1 is the primary analysis. A2 − L1 combines retrieval by tools and the verifier, so A1 − L1 and A2 − A1 are reported beside it.

**Secondary outcomes** (all pre-specified, all with 95% intervals):
1. Accuracy per condition for R0, R1, L1, A1, A2.
2. Sensitivity and specificity for GAP.
3. C1 fabricated-certainty rate (a definite status although the deciding input is absent). C2a and C2b over-abstention rates.
4. C5 pair consistency (both members correct).
5. C3a miss rate; answer-change rates in C2a, C2b, C3b and C4, each and pooled (an invalid or missing output on either case counts as changed; `PROTOCOL.md` sec. 8); C3a citation overlap.
6. Unsupported-claim rate: share of DOCUMENTED and ABSENT claims failing verifier checks 1 or 4.
7. Run-to-run stability: share of cases with identical status across the three runs of L1, A1 and A2.
8. Tool calls, tokens and seconds per case; schema-invalid outputs; forced fallbacks per condition; tool-call parser failures (`PROTOCOL.md` sec. 6).
9. Sensitivity analyses S-A and S-C.

**Hypotheses** (reported either way):
- H1: A2 is more accurate than L1 on the primary outcome.
- H2: on C3a, C3b and C4 (accuracy macro-averaged over the three, paired; `PROTOCOL.md` sec. 1), A2 is more accurate than R1.
- H3: A2 has a lower C1 fabricated-certainty rate than A1, and its C2a and C2b over-abstention rates are not higher by more than 5 percentage points.

**Precision.** The study is sized for estimation, not for a formal test. The expected interval width of the primary paired difference is simulated on dev results and stated in `PROTOCOL.md`. For scale: a condition with 80 cases gives a single-proportion half-width of about ±7 points at 0.90 and ±11 at 0.50; with 80 C1 cases, H3 can detect a difference of about 20% against 5%.

**Expected result.** Rules should win wherever the answer sits in structured data. The agent can add value only on notes, and may still lose to the keyword rule there, because templated notes favor keywords. Whatever happens is reported.

## 8. Pre-registration and reproducibility

- `PROTOCOL.md`: question, oracle table with quotes, code lists, cohort and exclusions, conditions with counts, systems, conventions page, prompts, model choice, outcomes, hypotheses, sensitivity analyses, analysis code path, stopping rule.
- The protocol commit is pushed to the public repository and published as a GitHub Release (server-side timestamp).
- **Test seed** = `int(sha256(protocol_commit_sha + beacon_value)[:8], 16)`. `beacon_value` is the pulse of a public randomness beacon at T, the Release's publication time rounded up to a whole minute plus 2 minutes, so it is published after the protocol is public. `PROTOCOL.md` sec. 13 names the beacon (NIST Randomness Beacon 2.0, with drand `quicknet` as fallback) and this rule. The test set is not determined until then, so the author cannot try seeds.
- `PROTOCOL.md` also fixes: the Synthea jar sha256, the JDK version, every command-line argument, the sampling rule for base patients, and sub-seeds for P1, P2, edits and phrase assignment (sha256 of the test seed and a label). The test population is one batch of 40,000 living adults; no further batch is generated, and a short cell keeps the patients it has (`PROTOCOL.md` sec. 2).
- The test set is built by the tagged commit, unchanged. Its sha256 goes into `RUNS.md` before any model is run. A code change that alters the test set is a deviation, reported with both results.
- Order of work, visible in the commit history: dev phrasings; prompts and R1 frozen; test phrasings written; tag. Test phrasings are not used in any run before the tag.
- The test set is run once per system. Reruns only after a crash, logged in `RUNS.md` (commit, date, what failed). Every departure from the protocol goes in `DEVIATIONS.md`.
- The first report of results covers run 1 only. Runs 2 and 3 (stability) come later and are listed in `RUNS.md` as pending until they are done (`PROTOCOL.md` sec. 11).
- TRIPOD-LLM checklist (Gallifant et al., Nat Med 2025;31:60–69), filled in where items apply.
- `make reproduce` rebuilds data, patches, edits, runs R0/R1, re-scores the saved model outputs, and redraws the figures from a clean clone. Re-running the LLM systems needs a GPU and is a separate target.
- Released: the synthetic test set, the augmentation and edit ledgers, all model outputs and traces.

## 9. Outputs

- Figure 1: dot plot with 95% intervals, in two groups: edits that must change the answer (C1, C3a, C5) and edits that must not (C2a, C2b, C3b, C4), with C0 on top. One row per condition; marks for R1, L1, A1, A2; cells that are correct by construction marked as such; x axis percent correct. R0 in the table only.
- Static dashboard on GitHub Pages, built from result files (no server, no live model):
  - Patient card with two panels. "What matters": status, benefit group, 10-year risk, each input with a DOCUMENTED, COMPUTED or ABSENT tag linking to its record row. "What's missing": absent inputs and what they block.
  - A toggle between a base patient and its edited twin.
  - A verifier badge on any claim that failed a check.
  - Table of all test cases with filters, labelled "designed test set, not prevalence"; failure traces.
- One-page write-up in abstract form (Background, Methods, Results, Limitations).

## 10. Compute and cost

- Synthea, DuckDB, patches, oracle, R0/R1: local CPU.
- LLM systems: one rented GPU, chosen with the model under `PROTOCOL.md` sec. 6 (RTX PRO 6000 96 GB for candidate 1, L40S 48 GB for candidate 2); the cost cap for run 1 is set there (gate G4).
- Optional hosted-model arm: 200-case subsample, capped, priced before use.

## 11. Limits (fixed text for README, dashboard and write-up)

Synthetic patients (Synthea v4.0.0) with disclosed patches; ground truth is the 2026 guideline as encoded in a decision table, not clinical judgment; GAP is a prompt for review, not a finding of poor care; two encoding choices are tested in sensitivity analyses, and the others (sec. 3.2) are kept out of the test labels by the sampling rules; only the PREVENT base equations are used, although the guideline's Table 13 note says "all available information should be included in risk estimates derived from the PREVENT-ASCVD equations, including albuminuria, HbA1c, and zip code"; the oracle uses the latest LDL-C, while rec 12 of sec. 4.2.3.7 refers to "baseline untreated LDL-C" (the generator's statins do not lower later values, so this matters only for real records); below LDL-C 70 the guideline gives no start recommendation except through take-home message 8, so row-7 base patients are kept only where the label would not change at 70; the test-case mix is designed, so no count is a prevalence; no base patient is a current smoker, and smoking enters only through edits; the generator gives about 8% of adults a diabetic complication or a glucose-lowering drug without a diabetes diagnosis, and these patients are not sampled, so the test set has no record that contradicts itself on diabetes; PAD, TIA, aneurysm and stable angina do not occur in the data; notes are templated and favor keyword rules; statin initiation only, no dose intensity, CAC or Lp(a) rules; lipid and eGFR values are generator values, not physiological measurements; research prototype, not medical advice.

## 12. Phase 2 (planned, separate protocol)

1. Load-bearing citations: delete the entries the agent itself cited and rerun; an unchanged answer means the citation was decorative.
2. Real notes: MIMIC-IV and MIMIC-IV-Note (v2.2, credentialed). Local model only, following the PhysioNet LLM policy (2025-09-24). Label-free consistency edits on discharge summaries, then a hand-labelled set of 60–100 summaries (statin at discharge; reason documented and which). Single annotator, stated as a limitation. Only aggregate numbers are published.
3. More conditions: race-flip invariance (PREVENT is race-free), stale labs, statin recorded only in a note, note-versus-structured conflict, stale reasons.
4. Oracle check against NHANES and Anderson et al., JAMA 2026, within a tolerance fixed in advance, after checking which labels that paper counts as statin-eligible.

## 13. References

- Blumenthal RS, et al. 2026 ACC/AHA/AACVPR/ABC/ACPM/ADA/AGS/APhA/ASPC/NLA/PCNA Guideline on the Management of Dyslipidemia. J Am Coll Cardiol 2026;87(19):2624–2757. doi:10.1016/j.jacc.2025.11.016. Circulation doi:10.1161/CIR.0000000000001423.
- Khan SS, Matsushita K, Sang Y, et al. Development and validation of the American Heart Association's PREVENT equations. Circulation 2024;149(6):430–449. doi:10.1161/CIRCULATIONAHA.123.067626.
- Sarraju A, Coquet J, Zammit A, et al. Commun Med 2022;2:88. doi:10.1038/s43856-022-00157-w.
- Gobbel GT, Matheny ME, Reeves RR, et al. Am J Prev Cardiol 2022;9:100300. doi:10.1016/j.ajpc.2021.100300.
- Gallifant J, et al. TRIPOD-LLM. Nat Med 2025;31(1):60–69. doi:10.1038/s41591-024-03425-5.
- Anderson TS, Wilson LM, Sussman JB. JAMA 2026;336(7):557. doi:10.1001/jama.2026.11246.
- Walonoski J, Kramer M, Nichols J, et al. Synthea. J Am Med Inform Assoc 2018;25(3):230–238. doi:10.1093/jamia/ocx079.
- Ma Y, Zhan J, Xu G. Reference values of glomerular filtration rate for healthy adults in southern China: a cross-sectional survey. Ther Adv Chronic Dis 2021;12:20406223211035287. doi:10.1177/20406223211035287.
- Turk. arXiv 2605.30590 (2026). CliniCARE-Bench, arXiv 2608.07796 (2026). AgentAbstain, arXiv 2607.10059 (2026). DeVisE, arXiv 2506.15339. MedAgentBench, arXiv 2501.14654. PhysicianBench, arXiv 2605.02240.
