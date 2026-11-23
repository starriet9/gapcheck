# PREVENT check against the AHA online calculator

Date: 2026-11-23, 06:16–06:50 UTC. Calculator: the American Heart Association PREVENT online calculator, https://professional.heart.org/en/guidelines-and-statements/prevent-calculator ("Online Calculator" tab; page script `app.bundle.js?v=1.28.2.27619`).

- Base equations only: UACR, HbA1c and zip code were left empty, and every result read "The risk estimates were calculated using the base equations".
- The calculator also asks for BMI, which the ASCVD base equation does not use; 26 was entered for every row.
- The calculator shows each risk with one decimal. It shows the 30-year risk only at ages 30–59.
- Row 1 was entered twice, at the start and at the end; both showed the same values.

The oracle column is `gapcheck.prevent.ascvd_risk` (SPEC sec. 3.5), in percent.

| # | Sex | Age | TC | HDL-C | SBP | BP treated | Statin | Diabetes | Smoker | eGFR | AHA 10-year ASCVD | Oracle 10-year | AHA 30-year ASCVD | Oracle 30-year |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | female | 45 | 210 | 50 | 128 | no | no | no | yes | 95 | 1.9% | 1.942 | 11.4% | 11.399 |
| 2 | male | 55 | 190 | 42 | 145 | yes | yes | no | no | 80 | 4.8% | 4.844 | 23.6% | 23.604 |
| 3 | female | 65 | 230 | 60 | 135 | yes | no | yes | no | 52 | 11.7% | 11.697 | not shown | |
| 4 | male | 40 | 240 | 38 | 104 | no | no | no | no | 105 | 1.2% | 1.163 | 7.9% | 7.880 |
| 5 | female | 35 | 180 | 65 | 115 | no | no | no | no | 145 | rejected | 0.197 | rejected | 1.414 |
| 6 | male | 72 | 170 | 45 | 150 | yes | yes | yes | yes | 45 | 22.1% | 22.089 | not shown | |
| 7 | male | 50 | 260 | 35 | 160 | no | no | no | yes | 70 | 9.6% | 9.591 | 37.1% | 37.076 |
| 8 | female | 58 | 200 | 55 | 100 | no | yes | no | no | 148 | rejected | 1.606 | rejected | 8.917 |
| 9 | male | 30 | 220 | 48 | 122 | no | no | yes | no | 120 | 1.7% | 1.707 | 8.7% | 8.695 |
| 10 | female | 79 | 150 | 70 | 170 | yes | no | no | yes | 35 | 21.3% | 21.312 | not shown | |

TC, HDL-C in mg/dL; SBP in mm Hg; eGFR in mL/min/1.73 m².

**Result.** The 8 rows the calculator accepted agree within its display rounding. The largest difference is 0.044 percentage point for the 10-year risk and 0.024 for the 30-year risk, below the 0.1-point rule of SPEC sec. 3.5.

**eGFR above 140.** Rows 5 and 8 have eGFR 145 and 148. The calculator rejected both: "Estimated glomerular filtration rate (eGFR) must be between 15 - 140 ml/min/1.73m²". The page says an out-of-range value can be estimated with the closest in-range value. At eGFR 140:

| # | AHA | Oracle |
|---|---|---|
| 5 | 0.2% and 1.4% | 0.200 and 1.437 |
| 8 | 1.6% and 9.1% | 1.640 and 9.136 |

Both packages also stop at 140, so no external implementation checks eGFR 141–150. The PREVENT paper's Tables S12A and S12F allow up to 150, and the oracle computes with the recorded value. Patients with a native eGFR above 140 where PREVENT is needed are not sampled (SPEC sec. 5.1), so no test label rests on that range.
