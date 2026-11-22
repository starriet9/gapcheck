# gpu/: running the model systems of a pre-registered run on a rented GPU

`session.py` runs one session of run 1 (`configs/run1.json`, the only source of its frozen settings; runs 2 and 3 with `--config configs/run2.json` or `configs/run3.json`) on one RunPod pod: preflight checks, pod, local relay, the case queue, teardown, and scoring once every case is complete. The harness itself (`src/gapcheck`, `scripts/run_systems.py`) is unchanged and runs from a clean worktree of the run commit. How the server is started, what the client sends, how to launch, stop and resume, and the retry rule are in [`docs/SERVING.md`](../docs/SERVING.md).

| File | What it does |
|---|---|
| `session.py` | Orchestrates a session: preflight (commit, worktree, settings, case-set integrity and its RUNS.md entry, relay self-test, local L1 build stop), GPU check, watchdog, pod (Community Cloud; Secure only with `--cloud SECURE`), relay, health, calibration (check only), case queue, teardown, finalize (scores only a complete run, with the billed GPU hours of every pod; otherwise writes `status.json`). A real run needs `--protocol-tag` (sec. 7 of SERVING.md); `--runner-fix` allows a resume at a later commit that changes only `gpu/`, `docs/`, `RUNS.md`, `DEVIATIONS.md` or `results/`; `--rehearsal` runs development cases on a real pod; `--dry` runs the same path against the two fakes below. |
| `pod_start.sh` | Runs in the pod: downloads the pinned GGUF files with sha256 checks, extracts the chat template from the first file's metadata and checks its sha256, starts `llama-server`; self-terminates at its deadline or when idle. Refuses to start on any failed check. |
| `extract_template.py` | Pure-Python GGUF header reader: writes the `tokenizer.chat_template` value of a GGUF file after checking its sha256 (the same bytes `pod_start.sh` extracts). |
| `relay.py` | Local relay between the harness and the pod: sends each chat request upstream as a stream (the RunPod proxy cuts replies after 100 s) and rebuilds the ordinary JSON reply; adds the server token. |
| `relay_selftest.py` | Offline test of the relay with the harness's backend; run in the preflight. |
| `queue_run.py` | Case queue: one `run_systems.py` process per case, client concurrency from the config, retries, the rule for usable records, the record written after exhausted attempts, resume, merge. |
| `calibrate.py` | Context checks before any model call: `--local` runs the harness's L1 build stop on every case; server mode checks the template, slots and that every L1 prompt counted by the server fits one slot. Derives nothing. |
| `watchdog.py` | Detached budget watchdog: terminates the pod at its deadline, on estimated spend or balance drop at the cap, or when the session stops writing its heartbeat. |
| `rpapi.py` | Minimal RunPod REST client (standard library). Reads the API key from `~/.config/runpod/api_key` inside the process; never prints or passes it on. |
| `gpu_check.py` | Read-only: GPU availability and price on Community and Secure, balance, pods; `--summary` prints one line without account data. |
| `collect.py` | Copies the pod's logs before termination (exec API, time-limited). |
| `wait_launch.sh` | Waits for stock (one check every 5 minutes, up to 24 hours, each logged to `<run dir>/wait.log`), then launches `session.py` once with a pod. |
| `fakerp.py`, `fake_server.py` | Offline stand-ins for the RunPod API and llama-server, for dry runs and the tests in `tests/gpu/`. |

Tests are in [`tests/gpu/`](../tests/gpu/) and run with the rest of the suite (`make test`), offline: the watchdog triggers, the pod's idle guard and template extraction, the queue rules, the tag and runner-fix rules, and full dry runs of `session.py` (complete; budget stop, crash and exhausted attempts, each resumed; a runner fix; run 2's sampling; a rehearsal) in a throwaway copy of the repository. The dry runs need the development case set (`make cases`) and are skipped without it.
