# gapcheck: thin wrappers around scripts/. `make help` lists the targets.
#
# Python and its packages come from uv (uv.lock, Python 3.12). Synthea needs a
# JDK 17 and the Synthea v4.0.0 release jar under tools/; neither is in git.
# `make tools` downloads both and checks their sha256 (docs/REPRODUCE.md).
#
# `make reproduce` runs: Synthea -> DuckDB + patches P0/P1 -> gate (P2, oracle,
# strata) -> dev case set -> R0, R1 and the mock L1/A1/A2 -> scores.
# Steps whose output exists are skipped (file targets). No model is called;
# real LLM runs are the separate target `run-llm`, which needs BACKEND=<config>.

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# Keep uv's cache, its Python and the venv inside this checkout (as env.sh does).
# The venv is always this checkout's, even if another checkout's env.sh is sourced.
export UV_CACHE_DIR ?= $(CURDIR)/tools/uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/tools/uv-python
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv
UV_RUN := uv run --locked
PY := $(UV_RUN) python

# ---------------------------------------------------------------- tools (not in git)
JDK_URL := https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.21%2B9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.21_9.tar.gz
JDK_SHA256 := 8b9b611d917e3dfd8419d3566b5949226f69b052fed7e109c1368add1dfe8711
JAVA := tools/jdk/bin/java
SYNTHEA_URL := https://github.com/synthetichealth/synthea/releases/download/v4.0.0/synthea-with-dependencies.jar
SYNTHEA_SHA256 := ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1
SYNTHEA_JAR := tools/synthea/v4.0.0/synthea-with-dependencies.jar

# ---------------------------------------------------------------- dev set (SPEC 4, 5)
SEED ?= 20261120
N ?= 10000
N_PER_STRATUM ?= 10
POP := s$(SEED)_p$(N)_utc
POP_DIR := data/dev/$(POP)
DB := $(POP_DIR)/gapcheck.duckdb
GATE_NAME := dev_$(POP)
GATE_JSON := $(POP_DIR)/results/gate_$(GATE_NAME).json
CASE_SET ?= dev_s$(SEED)_utc_n$(N_PER_STRATUM)
CASES := data/cases/$(CASE_SET)
MOCK_BACKEND ?= configs/mock_reference.json
MOCK_RUN ?= repro_mock_reference
MOCK_DIR := data/runs/$(MOCK_RUN)

# Real model runs (optional): make run-llm BACKEND=configs/<yours>.json RUN_ID=<id>
BACKEND ?=
RUN_ID ?=
LLM_SYSTEMS ?= R0,R1,L1,A1,A2
RUN ?= $(MOCK_DIR)

# Figure 1 (SPEC 9) from a scored run.
FIGURE_SCRIPT ?= scripts/figure1.py
FIGURE_ARGS ?=

PYTEST_ARGS ?= -q

.PHONY: help setup test oracle-sheet tools check-tools synthea db gate cases mock-run \
        score run-llm figure reproduce verify leak-check export-public clean-clone-test

help:
	@echo "setup         uv sync (Python 3.12 and locked packages into .venv)"
	@echo "test          unit tests (no data, no network, no model)"
	@echo "oracle-sheet  regenerate reports/oracle_rules.md from the oracle code"
	@echo "tools         download JDK 17.0.21 and the Synthea v4.0.0 jar, sha256-checked"
	@echo "synthea       generate the dev population ($(POP_DIR)), about 20 min, 1 thread"
	@echo "db            load it into DuckDB and apply P0, P1"
	@echo "gate          P2, oracle, strata; writes reports/gate_$(GATE_NAME).md"
	@echo "cases         build the dev case set ($(CASES))"
	@echo "mock-run      R0, R1 and the mock L1/A1/A2 on the case set ($(MOCK_DIR))"
	@echo "score         score a run: make score RUN=data/runs/<id>"
	@echo "run-llm       real model run: make run-llm BACKEND=configs/<file>.json RUN_ID=<id>"
	@echo "figure        Figure 1 from a scored run ($(FIGURE_SCRIPT))"
	@echo "reproduce     synthea -> db -> gate -> cases -> mock-run -> score"
	@echo "verify        compare reproduced files with docs/REPRODUCE.sha256 and git"
	@echo "leak-check    scan tracked files and every commit for private strings"
	@echo "export-public history-preserving public export: make export-public OUT=<dir>"

setup:
	uv sync --locked

test:
	$(UV_RUN) pytest $(PYTEST_ARGS)

oracle-sheet:
	$(PY) scripts/oracle_sheet.py

# ---------------------------------------------------------------- tools
tools: $(JAVA) $(SYNTHEA_JAR)
	$(MAKE) --no-print-directory check-tools

$(SYNTHEA_JAR):
	mkdir -p tools/dl $(dir $@)
	curl -fL --retry 3 -o tools/dl/synthea-with-dependencies.jar.part '$(SYNTHEA_URL)'
	echo "$(SYNTHEA_SHA256)  tools/dl/synthea-with-dependencies.jar.part" | sha256sum -c -
	mv tools/dl/synthea-with-dependencies.jar.part $@

$(JAVA):
	mkdir -p tools/dl tools/jdk
	curl -fL --retry 3 -o tools/dl/jdk17.tar.gz.part '$(JDK_URL)'
	echo "$(JDK_SHA256)  tools/dl/jdk17.tar.gz.part" | sha256sum -c -
	tar -xzf tools/dl/jdk17.tar.gz.part -C tools/jdk --strip-components=1
	rm tools/dl/jdk17.tar.gz.part

check-tools:
	echo "$(SYNTHEA_SHA256)  $(SYNTHEA_JAR)" | sha256sum -c -
	$(JAVA) -version 2>&1 | head -1 | grep -q '"17.0.21"' \
	  || { echo "$(JAVA) is not JDK 17.0.21" >&2; exit 1; }
	command -v jq >/dev/null || { echo "jq is needed by scripts/generate_synthea.sh" >&2; exit 1; }

# ---------------------------------------------------------------- pipeline
synthea: $(POP_DIR)/run_manifest.json
db: $(DB)
gate: $(GATE_JSON)
cases: $(CASES)/manifest.json
mock-run: $(MOCK_DIR)/score.json

$(POP_DIR)/run_manifest.json: | $(JAVA) $(SYNTHEA_JAR)
	THREADS=1 scripts/generate_synthea.sh $(SEED) $(N) $(POP_DIR)

$(DB): $(POP_DIR)/run_manifest.json
	$(PY) scripts/build_db.py $(POP_DIR) --force

# gate.py applies P2 to the database in place; the touch keeps the report newer
# than the database so later steps are not redone.
$(GATE_JSON): $(DB)
	$(PY) scripts/gate.py $(DB) --name $(GATE_NAME)
	touch $@

$(CASES)/manifest.json: $(GATE_JSON)
	$(PY) scripts/build_cases.py $(DB) --split dev --n-per-stratum $(N_PER_STRATUM) \
	  --name $(CASE_SET)

$(MOCK_DIR)/score.json: $(CASES)/manifest.json
	$(PY) scripts/run_systems.py --cases $(CASES) --systems R0,R1,L1,A1,A2 \
	  --backend $(MOCK_BACKEND) --run-id $(MOCK_RUN) --force --score

score:
	$(PY) scripts/score_runs.py --cases $(CASES) --run $(RUN)

run-llm:
	@test -n "$(BACKEND)" -a -f "$(BACKEND)" || { echo "set BACKEND=<backend config file>" >&2; exit 2; }
	@test -n "$(RUN_ID)" || { echo "set RUN_ID=<new run id>" >&2; exit 2; }
	$(PY) scripts/run_systems.py --cases $(CASES) --systems $(LLM_SYSTEMS) \
	  --backend $(BACKEND) --run-id $(RUN_ID) --score

figure:
	@test -f $(FIGURE_SCRIPT) || { echo "$(FIGURE_SCRIPT) not found" >&2; exit 2; }
	$(PY) $(FIGURE_SCRIPT) $(FIGURE_ARGS)

reproduce: mock-run
	@echo "done: $(POP_DIR), $(CASES), $(MOCK_DIR); next: make verify"

# Byte-level checks of a reproduction (docs/REPRODUCE.md): generator CSVs and
# case-set files against docs/REPRODUCE.sha256, and the tracked gate report
# against git.
verify:
	sha256sum -c --quiet docs/REPRODUCE.sha256
	git diff --exit-code -- reports/
	@echo "verify: all listed files match"

# ---------------------------------------------------------------- publication
leak-check:
	$(PY) scripts/leak_check.py

OUT ?=
export-public:
	@test -n "$(OUT)" || { echo "set OUT=<new directory>" >&2; exit 2; }
	scripts/export_public.sh $(OUT)

# Clone HEAD into data/clean_clone_test/ (ignored) and run setup + test there
# with its own venv and caches, from a minimal environment.
clean-clone-test:
	rm -rf data/clean_clone_test/gapcheck
	mkdir -p data/clean_clone_test
	git clone -q --no-local . data/clean_clone_test/gapcheck
	env -i HOME="$$HOME" PATH="$$PATH" LANG=C.UTF-8 \
	  $(MAKE) -C data/clean_clone_test/gapcheck setup test
