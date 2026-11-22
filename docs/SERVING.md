# Serving the model and running run 1

Run 1 (`PROTOCOL.md` sec. 11) runs L1, A1 and A2 on the test set against one open-weights model served by llama.cpp on one rented GPU, then adds R0 and R1 locally and scores. This page gives the exact server command, what the client sends, how the chat template is obtained, and how to launch, stop and resume the run. Every frozen value comes from [`configs/run1.json`](../configs/run1.json), which `gpu/session.py` reads; the values below are copied from it. The code is in [`gpu/`](../gpu/README.md).

## 1. Where things run

| Where | What |
|---|---|
| Local machine | `gpu/session.py` (orchestration), `gpu/relay.py` (local relay on 127.0.0.1), `gpu/watchdog.py` (detached budget guard), one `scripts/run_systems.py` process per case from a clean worktree of the run commit, then R0/R1 and scoring |
| RunPod pod (Community Cloud by default, Secure Cloud only with `--cloud SECURE`; one NVIDIA RTX PRO 6000 Blackwell Server Edition, the same image, 150 GB container disk, port 8080/http only, no SSH) | `gpu/pod_start.sh`: download, checks, `llama-server` |

The relay exists because the RunPod HTTPS proxy closes a request that has sent no response bytes for 100 s. It sends each non-streaming chat request upstream with `stream: true`, `stream_options: {"include_usage": true}` and `return_progress: true` (bytes flow during prompt processing), rebuilds the ordinary `chat.completion` JSON (content, tool calls, finish reason, usage, server timings) and returns it. Other requests pass through unchanged. The relay adds the server's throwaway token; the harness sends none.

## 2. Model, engine and image

| | Value |
|---|---|
| Model | `unsloth/Qwen3.8-Flash-Next-GGUF` at revision `766911a6b7369840a91dbcd95f9f997acaab6cd6`, folder `UD-Q4_K_XL` (GGUF of `Qwen/Qwen3.8-Flash-Next`) |
| Files (bytes, sha256) | `Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf` (10,946,624; `4448186216b3af4cc558bbce2c3213f01608f8f8b2e5267a9767971dd3ec8082`), `-00002-` (49,859,583,136; `3f342f1c1580473f1ee94ddd5b28206e8c07a70fa1a366f59d1d6c922919a6c9`), `-00003-` (49,376,141,504; `56758f40269cad5cd9b0d3d6fbae0f40f6d5be6de49e4ab392dbe83157d9cbd3`), `-00004-` (12,087,983,520; `753bda48b98ba4f1636134a90a967de1b2d3908a236c026e464777342e53510a`) |
| Engine | llama.cpp `llama-server` b12464 (commit `d3c3e242`), CUDA server image |
| Image | `ghcr.io/ggml-org/llama.cpp@sha256:ae96e52f8b2106ea82bad786ee2d08bb9b0a8c93403df9c27ae1bf065a118089` (by digest, never by tag) |

The pod downloads each file from `https://huggingface.co/<repo>/resolve/<revision>/<folder>/<file>` in parallel byte ranges (each range retried up to 8 times), checks every sha256, re-downloads a mismatched file once, and refuses to start the server if any file still differs.

## 3. Chat template

The template is not stored in this repository and not sent in the pod request. It is the string value of the GGUF metadata key `tokenizer.chat_template` in the first file, which the pod has just checked against its sha256 above:

- `gpu/pod_start.sh` (`template_from_gguf`) extracts it to `/models/chat-template.jinja` and checks its sha256 against `12827f24b742ea4e80cdc12dbcf9622227056b9f797252a3149263d4f9aaadce` (9,993 bytes). On a mismatch, or if the key is missing, it writes `/models/logs/FATAL` and starts no server; `gpu/session.py` reads that file through the RunPod exec API while it waits for the server, collects the pod logs and terminates the pod.
- The server is given the extracted file with `--chat-template-file`. Before any model call the session checks again that the template the running server reports (`/props`, `chat_template`) has the same sha256.
- To obtain the same file locally from a downloaded first file: `uv run --locked python gpu/extract_template.py <path to -00001-of-00004.gguf> --out chat-template.jinja` (exit status 1 and nothing written on a sha256 mismatch). It is a full GGUF header reader; the shell extraction in the pod gives the same bytes (tested in `tests/gpu/test_gpu_template.py`).

## 4. The llama-server command

With the values of run 1 filled in (`NP` = 8 slots, `CTX_PER_SLOT` = 49,152, `EXTRA_ARGS` = `--no-cache-idle-slots`), `gpu/pod_start.sh` runs:

```
LLAMA_API_KEY=<throwaway token> \
/app/llama-server -m /models/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf \
  --alias qwen3.8-flash-next-gpu --host 0.0.0.0 --port 8080 \
  -ngl 999 -fa on -np 8 -c 393216 --no-kv-unified -b 2048 -ub 2048 \
  -ctk f16 -ctv f16 --fit off \
  --jinja --chat-template-file /models/chat-template.jinja --reasoning-format deepseek \
  --chat-template-kwargs '{"reasoning_effort": "medium"}' \
  --temp 1 --top-p 0.95 --top-k 20 --min-p 0 --repeat-penalty 1 --presence-penalty 0 \
  --predict 8192 --seed 1 --no-context-shift --cache-ram 8192 --metrics --no-webui \
  --no-cache-idle-slots
```

| Flag | Meaning |
|---|---|
| `-m` | first file of the split GGUF; llama.cpp loads the other three from the same folder |
| `--alias` | the model name requests use |
| `-ngl 999`, `-fa on` | every layer on the GPU; flash attention |
| `-np 8 -c 393216 --no-kv-unified` | 8 request slots, each with its own 49,152-token context |
| `-b 2048 -ub 2048` | logical and physical batch size |
| `-ctk f16 -ctv f16` | KV cache in f16 (not quantized) |
| `--fit off` | no automatic change of the settings to fit memory |
| `--jinja --chat-template-file` | the extracted template, rendered by the Jinja engine |
| `--reasoning-format deepseek` | any thinking text is returned in `reasoning_content`, not in `content` (thinking is off in run 1) |
| `--chat-template-kwargs`, `--temp`, `--top-p`, `--top-k`, `--min-p`, `--repeat-penalty`, `--presence-penalty`, `--predict`, `--seed` | server defaults for requests that do not set them (sec. 5) |
| `--no-context-shift` | a request longer than its slot is rejected (HTTP 400), never truncated |
| `--cache-ram 8192` | 8 GiB host-memory prompt cache |
| `--metrics`, `--no-webui` | Prometheus endpoint on; web interface off |
| `--no-cache-idle-slots` | idle slots are not saved to the prompt cache |

No speculative or multi-token decoding flag is given, so both are off.

## 5. What the client sends

Every chat request of L1, A1 and A2 (`gapcheck.backends.OpenAICompatBackend`, configured from `decoding` in `configs/run1.json`):

| Field | Server default (command line) | Sent by the client | In effect |
|---|---|---|---|
| `model` | – | `qwen3.8-flash-next-gpu` | |
| `temperature` | 1 | 0 | 0 |
| `seed` | 1 | 1 | 1 |
| `max_tokens` | 8192 (`--predict`) | 4096 | 4096 |
| `top_p`, `top_k`, `min_p` | 0.95, 20, 0 | not sent | the server's values; at temperature 0 the most probable token is chosen, and none of these filters removes that token |
| `repeat_penalty`, `presence_penalty` | 1, 0 (off) | not sent | off |
| `chat_template_kwargs` | `{"reasoning_effort": "medium"}` | `{"enable_thinking": false}` | thinking off; `reasoning_effort` is not sent by the client |
| `parallel_tool_calls` | – | `true` | |
| `tools`, `tool_choice` | – | the system's tools, `"auto"` (L1: `prevent_risk` only) | |
| `stream`, `stream_options`, `return_progress` | – | added by the relay (sec. 1) | |

Client time-out per request: 1,800 s (the relay's upstream time-out is the same). The client's own context pre-check is off. The calibration renders every L1 prompt with the server's `/apply-template` using the same request fields, so the prompt it counts is the one the server builds.

**One request at a time.** The client concurrency is 1: one case process at a time, and inside it the systems run one after another (L1, then the agent run that gives A1 and A2), so the server never has two requests in flight. The server keeps 8 slots, as in the GPU smoke test whose one-request-at-a-time runs were identical twice (`PROTOCOL.md` sec. 6).

**Harness settings** (`run` in the config; every field of `gapcheck.systems.Settings` is given, so no library default enters run 1, and the preflight checks that the harness at the run commit reads them back unchanged): `max_steps` 8, `round_counter` true, `rounds_left_notice` 0, `prompt_set` "default", `repair_steps` 8, `l1_tool_rounds` 2, `page_size` 20, `context_limit` 45,056, `chars_per_token` 2.26.

## 6. Context checks (no setting is derived)

- **Before any pod** (`gpu/calibrate.py --local`): the harness's L1 build stop on every test case with `chars_per_token` 2.26 and `context_limit` 45,056: every row the oracle uses and every planted sentence is in the serialization, and the estimated prompt fits. A failure stops the session before anything is rented.
- **On the pod, before any model call** (`gpu/calibrate.py`): the template's sha256, 8 slots of 49,152 tokens, and every L1 prompt rendered and counted by the server plus `max_tokens` 4,096 within 49,152. If a prompt does not fit, the session stops with the cases listed and makes no model call. `PROTOCOL.md` sec. 5 then raises the served context and logs it in `RUNS.md`; that is done by a person (a new run config), never by the code.

## 7. Running run 1

Prerequisites: `make setup`; a RunPod API key in `~/.config/runpod/api_key` (mode 600; read only inside `gpu/rpapi.py`, never printed, logged or put in a pod); the test set built by the tag commit (sec. 10) with its `manifest.json` sha256 committed to `RUNS.md`; the run commit checked out, with `gpu/` and `configs/run1.json` as committed (the preflight refuses otherwise).

```
# plan only: read-only checks, GPU stock and price, balance; creates nothing
uv run --locked python gpu/session.py --cases data/cases/<test set> --run-dir data/runs/run1 --cap 45

# launch, detached (the session survives the terminal)
setsid nohup uv run --locked python gpu/session.py --cases data/cases/<test set> \
    --run-dir data/runs/run1 --cap 45 --go --protocol-tag <tag> > data/runs/run1.out 2>&1 < /dev/null &

# or wait for Community stock and launch once
CASES=data/cases/<test set> RUN_DIR=data/runs/run1 CAP=45 EXTRA="--protocol-tag <tag>" \
    setsid nohup gpu/wait_launch.sh > /dev/null 2>&1 < /dev/null &

# follow
tail -f data/runs/run1/logs/timeline.txt

# graceful stop (the case in flight finishes, then teardown)
touch data/runs/run1/STOP

# resume after any stop: the same command with --resume (remove STOP first)
setsid nohup uv run --locked python gpu/session.py --cases data/cases/<test set> \
    --run-dir data/runs/run1 --cap 45 --go --resume >> data/runs/run1.out 2>&1 < /dev/null &
```

`--cap` is the budget in USD for all pods of the run together.

**Secure Cloud fallback** (off by default): `--cloud SECURE --max-hourly <USD>` creates the next pod on Secure Cloud with the same GPU type, image, server command and client settings; only the provider tier and price change. `--max-hourly` is required there (on Community it defaults to 1.80 USD per GPU hour). A resume may switch cloud, for example a run started on Community and resumed on Secure; a pod that is still running is reused on its own cloud. The cloud of every launch and of every pod is written to the timeline, `session.json`, `ledger.json` and `status.json`, and the pod deadline, the watchdog and the estimate use that pod's price.

A dry run takes the same path with a fake RunPod API and a fake llama-server and no network: `... --cases data/cases/<dev set> --run-dir data/runs/dry1 --cap 12 --go --dry --dry-cases 6`. The plan prints a cost estimate only when given pod seconds per case (`--est-case-s LO,HI`).

Outputs in the run directory: `session.json` (state, commit, config, case ids, launches), `ledger.json` (pods, prices, times), `logs/timeline.txt`, `run1/cases/<case id>/` (one harness run per case), `run1/phase.json` (attempts and timing), `status.json` (what is complete), and, once complete, `R/` (R0, R1) and `run1/merged/` (all five systems, `manifest.json`, `score.json`, `results.json`).

**No interim analysis.** The finalize step merges and scores only when every case has a usable record (sec. 9) for L1, A1 and A2. Otherwise it writes `status.json` with the missing case ids and their attempts, and nothing is merged or scored. `--finalize-only` repeats this step without a pod and refuses in the same way.

**Cost.** Two times are kept, and only the first is a cost:

- *Billed GPU hours*: for every pod of the run (every session and resume), from its creation to its termination, as RunPod bills; a pod the watchdog or the pod itself terminated while no session was running ends at the time the watchdog logged it. This sum is passed to scoring as `--gpu-hours`, with `--usd-per-hour` = the billed USD estimate divided by these hours (each pod at its own price, disk included). `status.json`: `billed_gpu_hours`, `billed_usd_estimate`, `pods` (cloud, price and hours of each).
- *Queue wall time*: the time the case queue ran, summed over sessions; it leaves out download, loading, calibration and teardown. `status.json`: `queue_wall_hours`. Not a cost.

## 8. Guards and budget

- Watchdog (detached, started before the pod): terminates the pod at its deadline (the pod hours the budget left buys at the highest accepted price, 1.80 USD/h plus disk, rounded down to 3 minutes), when estimated spend or the account balance drop since the first launch reaches cap − 0.5 USD, at the hard deadline, or when the session's heartbeat is 600 s old.
- In the pod: self-termination 3 minutes after the deadline, and after 1,200 s without a server task once the server is up; both use the pod-scoped key RunPod injects.
- The session stops launching cases at cap − 1 USD of estimated spend, 15 minutes before the pod deadline, on a STOP file, on a signal (a second signal kills the case in flight) or when the pod is gone. A pod above `--max-hourly` is refused (or terminated at once if billed above it). A GPU that is not available on the chosen cloud stops the session; the cloud is never switched automatically.
- After a budget stop the same cap is refused before a pod is created; the cap must be raised to go on.

## 9. Retry and failure rule

As implemented (`src/gapcheck/backends.py`, `src/gapcheck/systems.py`, `gpu/relay.py`, `gpu/queue_run.py`; numbers from `client` in the config):

1. **HTTP 4xx from the server** (a request it rejects, such as a prompt longer than its slot): the backend raises `BackendError`; the system's conversation ends with no answer and no re-ask, the error is logged in the record (`error: "backend: HTTP 400: ..."`), and the case counts as having a record for that system: scored as no valid output (for A2, a rejection in the repair round gives the forced fallback). Only HTTP 400 with llama-server's JSON error body is accepted as such a rejection (`accept_http_4xx`).
2. **Any other 4xx** (401, 403, 404, 408, 429 or a 400 that is not llama-server's JSON, which come from the token, the proxy or the network, not from the model): the case attempt counts as failed (point 4), so it is run again rather than scored.
3. **HTTP 5xx, a connection failure or a time-out** (1,800 s per request): not caught by the harness; the case process exits with an error and writes no record for any system of that case.
4. **Failed attempt** (exit code ≠ 0, records incomplete, an error of point 2, or the case killed after 2,400 s): the case directory is moved to `run1/aborted/`, the error text is kept (the last line of the case log, the infrastructure errors, or the time-out), and the case is queued again at the end, until it has had 2 attempts in the session; attempts are counted in `run1/phase.json` across sessions, so each later resume gives such a case one more attempt. An attempt the runner kills itself because of a second signal or a pod that is gone (budget, deadline) is not counted. After 4 failed attempts in a row the session stops launching (server or relay down) and tears down.
5. **Exhausted attempts** (`max_total_attempts` 4): when a case's 4th counted attempt fails, the runner itself (`gpu/queue_run.py`, not a person) writes a record for every system of that case with `output: null`, `schema_valid: false` and `error: "attempts exhausted (4): <last error text>"`, and the errors of all 4 attempts under `exhausted`. It is logged in the timeline and listed under `exhausted` in `status.json`; scoring reads it as no valid output (incorrect). The case then counts as complete.
6. **Relay**: a request on a pooled connection that the upstream had closed is sent once more on a fresh connection; any other upstream failure is answered with HTTP 502 (point 3).
7. **Invalid reply** (not one schema-valid JSON object): one re-ask inside the same tool-round limit; then scored as no valid output (harness rule, `PROTOCOL.md` sec. 5).

Run 1 is complete, and is scored, when every case has a record for every system: the harness's own, or the record of point 5.

## 10. Resume

Resume is the same command with `--resume` and the same run directory. It continues only with the same commit, the same run config (compared in full) and the same case set (path, manifest sha256 and case ids); otherwise it refuses.

- A case with a usable record for every system is never run again. Any other case directory (in flight at a crash, killed, or failed) is moved to `run1/aborted/` and the case is run again.
- Processes a killed session left behind are stopped first: its relay, and any case process still running (pids in `run1/running.json`). A pod that is still running is reused, with its deadline, and so is a watchdog that is still alive; otherwise a new pod is created under the budget that is left (the cap counts every pod in `ledger.json`). On a new pod the calibration must give the same template and the same token count for every case as on the first.
- A budget stop (watchdog or soft stop), a deadline, a STOP file, a signal or a crash are all resumed the same way; after a budget stop with a larger `--cap`. A resume may use another cloud (`--cloud`); the run config and the backend record stay the same, so the parts still merge.

## 11. Building the test case set

From a clean working tree at the tag commit, once the test seed is derived (`PROTOCOL.md` sec. 13). The same steps as the development set (`Makefile`), with the test split, 40,000 adults and 40 base patients per stratum:

```
make tools                                                   # JDK 17.0.21 and the Synthea v4.0.0 jar, sha256-checked
SEED=<test seed>
POP=data/test/s${SEED}_p40000_utc
THREADS=1 scripts/generate_synthea.sh $SEED 40000 $POP
uv run --locked python scripts/build_db.py $POP --force
uv run --locked python scripts/gate.py $POP/gapcheck.duckdb --name test_s${SEED}_p40000_utc
uv run --locked python scripts/build_cases.py $POP/gapcheck.duckdb --split test --n-per-stratum 40
sha256sum data/cases/test_s${SEED}_n40/manifest.json          # into RUNS.md before any model call
```

`build_db.py` takes the seed from the generator's `run_manifest.json`, and `gate.py` and `build_cases.py` take it from the database, so every sub-seed follows from the test seed. `build_cases.py` names the set `test_s<seed>_n40` and uses the test phrasings (`src/gapcheck/phrases/test.json`).
