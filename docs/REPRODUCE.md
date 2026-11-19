# Reproducing gapcheck

Everything below runs on a local CPU. No model is called unless you run `make run-llm` with your own backend config. Commands are run from the repository root. Each `make` target is a thin wrapper; the exact commands it runs are listed here and in the `Makefile`.

## Requirements

- Linux x86-64 (the pinned JDK build is for x64 Linux), bash, GNU make, git, curl, tar, sha256sum and jq (`scripts/generate_synthea.sh` writes its manifest with jq).
- [uv](https://docs.astral.sh/uv/) (CI uses 0.12.24). uv downloads CPython 3.12 if no 3.12 interpreter is found. The Makefile keeps uv's cache, its Python and the virtual environment inside the checkout (`tools/uv-cache`, `tools/uv-python`, `.venv`).
- For the data pipeline: up to 32 GB of heap for the Synthea JVM (`JAVA_XMX`, default `32g`) and about 9 GB of disk under `data/` (ignored by git).

## 1. Python environment and unit tests

```
make setup    # uv sync --locked
make test     # uv run --locked pytest -q
```

The tests run on a small fixture in `tests/fixtures/synthea_mini` and need no network, data download or model. Two groups skip unless their source files are supplied, because those files are not redistributed: the guideline-quote checks (`GAPCHECK_GUIDELINE_TEXT`, a text extraction of the guideline) and the PREVENT coefficient check (`GAPCHECK_PREVENT_SUPPLEMENT`, the supplement spreadsheet of Khan et al. 2024). The cross-check against `pyprevent` 0.1.7 always runs. CI (`.github/workflows/test.yml`) runs `make setup` and `make test` on every push and pull request.

## 2. Tools (not in git)

```
make tools
```

downloads both files once, checks their sha256, and refuses a mismatch:

| Tool | Source | sha256 |
|---|---|---|
| Eclipse Temurin JDK 17.0.21+9, x64 Linux (`tools/jdk/`) | `https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.21%2B9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.21_9.tar.gz` | `8b9b611d917e3dfd8419d3566b5949226f69b052fed7e109c1368add1dfe8711` |
| Synthea v4.0.0 release jar (`tools/synthea/v4.0.0/synthea-with-dependencies.jar`) | `https://github.com/synthetichealth/synthea/releases/download/v4.0.0/synthea-with-dependencies.jar` | `ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1` |

The jar is used unmodified. `make check-tools` re-checks the jar hash, the JDK version and jq. Nothing is installed system-wide.

## 3. Dev set, end to end

```
make reproduce
```

runs these steps; each is a file target and is skipped if its output exists:

```
# Synthea: seed 20261120, 10,000 living adults aged 30-79, Massachusetts, one thread,
# reference and end date 2026-11-18, JVM time zone UTC (settings in the script)
THREADS=1 scripts/generate_synthea.sh 20261120 10000 data/dev/s20261120_p10000_utc
# DuckDB load, patches P0 and P1
uv run --locked python scripts/build_db.py data/dev/s20261120_p10000_utc --force
# P2, oracle, strata; writes reports/gate_dev_s20261120_p10000_utc.md
uv run --locked python scripts/gate.py data/dev/s20261120_p10000_utc/gapcheck.duckdb \
    --name dev_s20261120_p10000_utc
# dev case set: 10 base patients per stratum, twins, notes, edit ledger, manifest
uv run --locked python scripts/build_cases.py data/dev/s20261120_p10000_utc/gapcheck.duckdb \
    --split dev --n-per-stratum 10 --name dev_s20261120_utc_n10
# R0, R1 and the mock L1/A1/A2, then scoring
uv run --locked python scripts/run_systems.py --cases data/cases/dev_s20261120_utc_n10 \
    --systems R0,R1,L1,A1,A2 --backend configs/mock_reference.json \
    --run-id repro_mock_reference --force --score
# Figure 1 from that score file
uv run --locked python scripts/figure1.py --score data/runs/repro_mock_reference/score.json \
    --out-dir data/runs/repro_mock_reference/figures
```

The mock backend (`configs/mock_reference.json`) is not a model. It returns fixed reference answers so that the tools, the verifier, the repair loop and the scoring run end to end without a GPU. Its scores say nothing about any model.

From a clean clone on the development machine (2026-11-20, other jobs running): Synthea 24 min, database 22 s, gate about 2.5 min, case set 4.6 min, mock runs and scoring 30 s; 32 min in all.

## 4. Verify

```
make verify
```

checks, for the default settings above:

1. `sha256sum -c docs/REPRODUCE.sha256`: 14 generator CSV files, the augmentation ledger and strata file, the case-set files (cases, deltas, edit ledger, base tables and notes), and the accuracy sections of the mock run's `score.json` (written as sorted JSON to `accuracy_sections.json`).
2. `observations.csv` up to the simulation end date (header and every row dated 2026-11-18 or earlier) against `docs/REPRODUCE.observations.sha256`.
3. The regenerated gate report against the committed `reports/gate_dev_s20261120_p10000_utc.md`, without its run-time section.

The accuracy hash depends on the scoring code (`src/gapcheck/score.py`) and the mock policy as well as on the data, so a commit that changes either updates `docs/REPRODUCE.sha256` (`sha256sum data/runs/repro_mock_reference/accuracy_sections.json` after `make mock-run verify`) and says why.

All three passed on a clean clone (generated 2026-11-19 16:44 UTC) against the original dev set (generated 11:06 UTC the same day).

Known differences that are not checked:

- `observations.csv` as a whole depends on when it is generated. Synthea writes yearly QALY, DALY and QOLS rows dated after the simulation end date (`-e 20261118`), and which of them appear depends on the wall-clock time of the run: the clean clone had 7 more patients with such a row dated 2026-11-19 (21 lines), all timed between the two runs' start times. The pipeline drops every row dated after a patient's index date, which is on or before the end date, so nothing downstream changes; every derived file above matched.
- `payer_transitions.csv` is not used by the pipeline and differs between runs (line order; SPEC sec. 4).
- `gapcheck.duckdb` is not compared byte for byte; the tables derived from it are.
- Run outputs (`*.jsonl`, `manifest.json`) record seconds, start times and the git commit; the gate report records run times.
- More than one generator thread (`THREADS`) changes row order.
- The day-0 pilot sets were generated in the machine's local zone before the zone was pinned and cannot be reproduced byte for byte (SPEC sec. 4). Their gate report is kept for the record.

## 5. Scoring saved outputs and real model runs

```
make score RUN=data/runs/<id>                       # re-score saved outputs (no model call)
make figure RUN=data/runs/<id>                      # Figure 1 from data/runs/<id>/score.json
make run-llm BACKEND=configs/<file>.json RUN_ID=<id> [LLM_SYSTEMS=L1,A1,A2]
```

`run-llm` needs a backend config; `configs/vllm_example.json` (an OpenAI-compatible server, model pinned by revision) and `configs/anthropic_example.json` show the fields. The case set is the dev set unless `CASE_SET` is given. Test-set runs follow `PROTOCOL.md` and are logged in `RUNS.md`; the test set's sha256 is recorded there before any model is run.

## 6. Clean-clone check

```
make clean-clone-test
```

clones the committed `HEAD` into `data/clean_clone_test/gapcheck` and runs `make setup test` there from an empty environment (only `HOME`, `PATH` and `LANG`), with its own venv, uv cache and Python. It shows whether anything depends on files that are not in git.
