"""One GPU session of a pre-registered run (run 1: configs/run1.json), from pod creation to
scoring. Every frozen setting comes from the run config; the case set is given with --cases.

Steps (each logged to <run dir>/logs/timeline.txt; state in <run dir>/session.json):
  preflight   real run: --protocol-tag given and the run commit is the tagged commit or
              follows it only by commits to RUNS.md, DEVIATIONS.md, results/; the run commit is
              HEAD and the checkout is clean; a resume at a later commit only with --runner-fix
              (changes in gpu/, docs/, RUNS.md, DEVIATIONS.md, results/ only, a logged
              deviation). Always: worktree of the commit (clean); the harness at that commit
              accepts the run settings exactly; case set: every file matches its manifest,
              split as the config says (a --rehearsal: dev, under data/runs/rehearsal/),
              manifest sha256 in RUNS.md at the commit; relay self-test; the harness's L1 build
              stop on every case (local, before any pod); API key file present (never read
              here). Nothing is created before this passes.
  gpu check   read-only: GPU availability and price on the chosen cloud (--cloud; default the
              config's, Community; Secure needs --max-hourly), balance >= budget left + 0.5.
              Not available there: stop (the cloud is never switched automatically).
  watchdog    detached (own session): terminates the pod at its deadline, on estimated spend
              or balance drop >= cap - margin, at the hard deadline, or when this process
              stops writing its heartbeat for --client-stale seconds
  pod         chosen cloud, pinned image, 8080/http only, a throwaway llama-server token; the
              pod downloads and sha256-checks the GGUF files, extracts the chat template from
              file 1 and checks its sha256 (gpu/pod_start.sh), and self-terminates at its
              deadline + 3 min and after --idle-seconds without a task
  relay       local streaming relay (RunPod's proxy cuts non-streaming replies at 100 s)
  health      wait for the server; the pod's fatal-error file is read through the exec API
  calibrate   check only: template sha256, slots, context per slot; every L1 prompt counted
              by the server fits one slot with max_tokens; otherwise stop before any model call
  run         the case queue (queue_run.py): client concurrency from the config (run 1: one
              request at a time); soft stop at cap - reserve or near the pod deadline, STOP
              file or a signal (graceful; a second signal kills the case in flight)
  teardown    pod logs, terminate (DELETE, confirmed 404), watchdog stop, relay stop, token
              deleted
  finalize    local, after the pod is gone. No interim analysis: only when every case has a
              usable record for every system (a case whose attempts are exhausted gets one
              written by the queue: no valid output) are R0/R1 run, the parts merged and the
              run scored (scripts/score_runs.py, with the billed GPU hours of every pod of the
              run). Otherwise <run dir>/status.json lists what is missing and nothing is
              merged or scored; resume with the same command.

Usage (from the repo root):
  plan only (read-only, creates nothing):
    uv run --locked python gpu/session.py --cases data/cases/<test set> --run-dir data/runs/run1 --cap 45 \\
        --protocol-tag <tag>
  launch (detached; survives the terminal):
    setsid nohup uv run --locked python gpu/session.py --cases data/cases/<test set> \\
        --run-dir data/runs/run1 --cap 45 --protocol-tag <tag> --go > data/runs/run1.out 2>&1 < /dev/null &
  resume after any stop (crash, budget, deadline, signal, STOP file): the same command with
  --resume (same commit, config and case set; usable cases are kept). After a budget stop
  the cap must be raised, since the cap counts every pod of the run.
  graceful stop: touch data/runs/run1/STOP
  dry run (no RunPod, no model: fake API and fake llama-server, same code path):
    ... --cases data/cases/<dev set> --run-dir data/runs/dry1 --cap 12 --go --dry --first-cases 6
Run files store repository-relative paths only; the account (balance, other pods, the watchdog
log) stays in <run dir>/account/, and <run dir>/RELEASE.txt lists the files meant for release.
The RunPod API key is read only inside rpapi.py and never printed, logged or passed on.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT))
import collect  # noqa: E402
import gpu_check  # noqa: E402
import queue_run  # noqa: E402
import rpapi  # noqa: E402

REPO = KIT.parent
DISK_RATE = 0.10 / 730.0  # USD per GB-hour, container disk of a running pod
DRY_TEMPLATE = "{#- gapcheck dry run: a stand-in, not the model's chat template -#}\n"
# After the protocol tag only these may change before run 1 (the test set's sha256 goes into RUNS.md).
AFTER_TAG = ("RUNS.md", "DEVIATIONS.md", "results/")
# A resume at a later commit (--runner-fix, a logged deviation) may change only these: never the
# harness (src/), the scripts, the run config or the prompts.
RUNNER_FIX = ("gpu/", "docs/", "RUNS.md", "DEVIATIONS.md", "results/")
SETTINGS_FIELDS = ("max_steps", "repair_steps", "l1_tool_rounds", "context_limit", "page_size",
                   "chars_per_token", "prompt_set", "rounds_left_notice", "round_counter")

SIG = {"n": 0}
LOGF = None
SESS: Path = None  # type: ignore
CFG: dict = {}


# ---------------------------------------------------------------- small helpers
def utc(t=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def log(msg: str) -> None:
    line = f"{utc()} {msg}"
    print(line, flush=True)
    if LOGF:
        LOGF.write(line + "\n")
        LOGF.flush()


def timeline(msg: str) -> None:
    """A line of logs/timeline.txt (released): pod ids are written as pod#1, pod#2, ...; amounts
    of the account balance never go here (account_log)."""
    log(msg)
    for i, p in enumerate(load_ledger()["pods"], 1):
        msg = msg.replace(p["id"], f"pod#{i}")
    with open(SESS / "logs" / "timeline.txt", "a") as f:
        f.write(f"{utc()} {msg}\n")


def account_dir() -> Path:
    """<run dir>/account/: the account's balance and pods, the watchdog log. Never released."""
    d = SESS / "account"
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o700)
    return d


def account_log(msg: str) -> None:
    log(msg)
    with open(account_dir() / "account.log", "a") as f:
        f.write(f"{utc()} {msg}\n")


def load_account() -> dict:
    p = account_dir() / "account.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_account(d: dict) -> None:
    write_json(account_dir() / "account.json", d)


def watchdog_log() -> Path:
    return account_dir() / "watchdog.log"


def write_json(p: Path, d) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1) + "\n")
    os.replace(tmp, p)


def load_state() -> dict:
    p = SESS / "session.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(st: dict) -> None:
    write_json(SESS / "session.json", st)


def load_ledger() -> dict:
    p = SESS / "ledger.json"
    return json.loads(p.read_text()) if p.exists() else {"pods": []}


def save_ledger(led: dict) -> None:
    write_json(SESS / "ledger.json", led)


def spent_estimate(now=None) -> float:
    now = now or time.time()
    return sum(p["hourly"] * ((p.get("terminated_epoch") or now) - p["created_epoch"]) / 3600
               for p in load_ledger()["pods"])


def billed(now=None) -> dict:
    """Billed GPU hours of the run: every pod in the ledger (every session and resume), from
    creation to termination, as RunPod bills them; with each pod's cloud and hourly price."""
    now = now or time.time()
    pods = []
    for i, p in enumerate(load_ledger()["pods"], 1):
        h = max((p.get("terminated_epoch") or now) - p["created_epoch"], 0) / 3600
        pods.append({"pod": i, "cloud": p.get("cloud"), "usd_per_hour": round(p["hourly"], 4),
                     "billed_hours": round(h, 4), "usd": round(h * p["hourly"], 4),
                     "terminated": bool(p.get("terminated_epoch"))})
    hours = sum(x["billed_hours"] for x in pods)
    usd = sum(x["usd"] for x in pods)
    return {"billed_gpu_hours": round(hours, 4), "billed_usd_estimate": round(usd, 3),
            "usd_per_billed_hour": round(usd / hours, 4) if hours else None, "pods": pods}


def pid_alive(pid) -> bool:
    """True for a running process (an exited child not yet reaped, a zombie, is not)."""
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def cmdline(pid) -> str:
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, TypeError, ValueError):
        return ""


def kill_stale(pid, *must: str) -> bool:
    """Kill a process an earlier launch of this session left (a relay or fake server after
    the session process was killed); only if its command line has every string in `must`."""
    cmd = cmdline(pid)
    if not pid or not pid_alive(pid) or not all(m in cmd for m in must):
        return False
    os.kill(int(pid), signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    if pid_alive(pid):
        os.kill(int(pid), signal.SIGKILL)
    log(f"stopped process {pid} left by an earlier launch ({must[0]})")
    return True


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_get(url: str, timeout: float = 20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # connection refused, reset, timeout
        return None, f"{type(e).__name__}"


def wt_env(wt: Path) -> dict:
    return dict(os.environ, PYTHONPATH=str(wt / "src"))


def git(*a, cwd=REPO) -> str:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def under(path: str, prefixes) -> bool:
    return any(path == x or (x.endswith("/") and path.startswith(x)) for x in prefixes)


def touched(c0: str, c1: str, cwd=REPO) -> list[str]:
    """Every path a commit in c0..c1 touches, each commit's own diff included (a change that a
    later commit reverts is still listed). c0 must be an ancestor of c1; merges are refused."""
    if subprocess.run(["git", "merge-base", "--is-ancestor", c0, c1], cwd=cwd).returncode:
        return [f"<{c1[:10]} does not descend from {c0[:10]}>"]
    out = set(git("log", "--format=", "--name-only", f"{c0}..{c1}", cwd=cwd).split())
    out |= set(git("diff", "--name-only", c0, c1, cwd=cwd).split())
    if git("rev-list", "--merges", f"{c0}..{c1}", cwd=cwd):
        out.add("<merge commits>")
    return sorted(out)


def tag_check(tag: str, commit: str, cwd=REPO) -> tuple[str | None, list[str]]:
    """(tag commit, problems): the run commit is the tagged commit, or follows it by commits
    that touch only RUNS.md, DEVIATIONS.md and results/."""
    try:
        tc = git("rev-parse", "--verify", f"{tag}^{{commit}}", cwd=cwd)
    except subprocess.CalledProcessError:
        return None, [f"protocol tag {tag!r} does not exist here"]
    if tc == commit:
        return tc, []
    bad = [f for f in touched(tc, commit, cwd) if not under(f, AFTER_TAG)]
    return tc, ([f"commits after the tag {tag} ({tc[:10]}) touch {bad}; only {list(AFTER_TAG)} may change"]
                if bad else [])


def runner_fix_check(c0: str, c1: str, cwd=REPO) -> tuple[list[str], list[str]]:
    """(files the commits c0..c1 change, those outside the runner-fix paths)."""
    files = touched(c0, c1, cwd)
    return files, [f for f in files if not under(f, RUNNER_FIX)]


def same_code(first: str):
    """For merge(): two parts' git records are the same run code if equal, or if both are clean
    and every commit is the first run commit or a runner fix of it."""
    ok = {first: True}

    def allowed(c: str) -> bool:
        if c not in ok:
            try:
                ok[c] = not runner_fix_check(first, c)[1]
            except subprocess.CalledProcessError:
                ok[c] = False
        return ok[c]

    def equiv(g0: dict | None, g: dict | None) -> bool:
        if g0 == g:
            return True
        return bool(g0 and g and not g0.get("dirty") and not g.get("dirty")
                    and allowed(g0.get("commit")) and allowed(g.get("commit")))
    return equiv


def rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def disk_gb() -> int:
    return int(CFG["gpu"]["disk_gb"])


def systems() -> list[str]:
    return list(CFG["systems"])


def accept_4xx() -> tuple:
    return tuple(CFG["client"]["accept_http_4xx"])


# ---------------------------------------------------------------- run config
def load_config(path: Path) -> dict:
    c = json.loads(path.read_text())
    errs = []
    for k in ("name", "systems", "case_split", "run", "decoding", "client", "server", "model", "image", "gpu"):
        if k not in c:
            errs.append(f"missing {k}")
    if errs:
        raise SystemExit(f"{path}: {'; '.join(errs)}")
    if not str(c["name"]).isalnum():
        errs.append(f"name {c['name']!r} must be alphanumeric")
    if not c["systems"] or not set(c["systems"]) <= {"L1", "A1", "A2"}:
        errs.append("systems must be among L1, A1, A2")
    if set(c["run"]) != set(SETTINGS_FIELDS):
        errs.append(f"run must give every harness setting exactly once: {sorted(SETTINGS_FIELDS)}")
    d, cl, sv = c["decoding"], c["client"], c["server"]
    if c["run"].get("context_limit", 0) + d.get("max_tokens", 0) > sv.get("ctx_per_slot", 0):
        errs.append("context_limit + max_tokens must fit ctx_per_slot")
    if not 1 <= int(cl.get("concurrency", 0)) <= int(sv.get("slots", 0)):
        errs.append("client concurrency must be between 1 and the server slots")
    for k in ("request_timeout_s", "case_timeout_s", "max_attempts", "max_total_attempts", "fail_limit",
              "accept_http_4xx"):
        if k not in cl:
            errs.append(f"client.{k} missing")
    if not int(cl.get("max_total_attempts", 0)) >= int(cl.get("max_attempts", 1)) >= 1:
        errs.append("client: max_total_attempts >= max_attempts >= 1")
    if not all(400 <= int(x) < 500 for x in cl.get("accept_http_4xx", [])):
        errs.append("client.accept_http_4xx: 4xx statuses only")
    files = c["model"].get("files") or []
    if not files or any(set(f) != {"name", "bytes", "sha256", "download_ranges"} for f in files):
        errs.append("model.files: name, bytes, sha256, download_ranges for each file")
    if len(c["model"].get("chat_template", {}).get("sha256", "")) != 64:
        errs.append("model.chat_template.sha256 missing")
    if errs:
        raise SystemExit(f"{path}: {'; '.join(errs)}")
    return c


def revision_text(dry: bool) -> str:
    if dry:
        return f"DRY RUN: fake llama server (gpu/fake_server.py), no model, no GPU; slots {CFG['server']['slots']}"
    m, s = CFG["model"], CFG["server"]
    return (f"{m['repo']}@{m['revision']} {m['quant']} ({len(m['files'])} files sha256-checked); "
            f"{CFG['engine']} {CFG['image']}; RunPod {CFG['gpu']['id']} (cloud of each pod in the ledger); "
            f"-np {s['slots']} ctx/slot {s['ctx_per_slot']} --no-kv-unified {s['extra_args']}; f16 KV; "
            f"-b 2048 -ub 2048; MTP off; context-shift off; chat template sha256 "
            f"{m['chat_template']['sha256'][:8]} from the GGUF metadata; via local streaming relay; "
            f"client concurrency {CFG['client']['concurrency']}; client preflight off")


def template_sha(a) -> str:
    return hashlib.sha256(DRY_TEMPLATE.encode()).hexdigest() if a.dry else CFG["model"]["chat_template"]["sha256"]


# ---------------------------------------------------------------- preflight
def case_set_check(cases: Path, commit: str, split: str, check_runs_md: bool = True) -> dict:
    """Integrity of the case set and its registration. Returns facts and problems."""
    out = {"path": rel(cases), "problems": []}
    try:
        man = json.loads((cases / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"case set {cases}: no readable manifest.json ({e})")
    for f, want in sorted((man.get("files") or {}).items()):
        p = cases / f
        if not p.exists() or sha256_file(p) != want:
            out["problems"].append(f"case set file {f} missing or changed (sha256 differs from manifest.json)")
    out["manifest_sha256"] = sha256_file(cases / "manifest.json")
    out["split"] = man.get("split")
    if man.get("split") != split:
        out["problems"].append(f"case set split {man.get('split')!r} != {split!r}")
    try:
        runs_md_text = git("show", f"{commit}:RUNS.md")
    except subprocess.CalledProcessError:
        runs_md_text = ""
    out["in_runs_md"] = out["manifest_sha256"] in runs_md_text
    if check_runs_md and not out["in_runs_md"]:
        out["problems"].append(f"manifest.json sha256 {out['manifest_sha256']} is not in RUNS.md at the run "
                               f"commit {commit[:10]} (PROTOCOL.md sec. 14: recorded before any model call)")
    ids = [json.loads(x)["case_id"] for x in (cases / "cases.jsonl").read_text().splitlines() if x.strip()]
    if len(ids) != len(set(ids)) or not ids:
        out["problems"].append("cases.jsonl: empty or duplicate case ids")
    out["case_ids"] = ids
    return out


def code_check(commit: str, config: Path) -> list[str]:
    """The orchestration that runs (this checkout's gpu/ and the run config) is the commit's."""
    paths = [rel(KIT), rel(config)]
    if Path(paths[1]).is_absolute():
        return [f"the run config {config} is not a file of this repository"]
    probs = []
    diff = subprocess.run(["git", "diff", "--name-only", commit, "--", *paths], cwd=REPO,
                          capture_output=True, text=True).stdout.split()
    untracked = git("ls-files", "--others", "--exclude-standard", "--", *paths).split()
    if diff or untracked:
        probs.append(f"the checkout differs from the run commit {commit[:10]} in {sorted(set(diff + untracked))}")
    return probs


def worktree(a, commit: str) -> Path:
    if a.use_checkout:
        return REPO
    wt = SESS / "wt"
    if not wt.exists():
        git("worktree", "add", "--detach", str(wt), commit)
    dirty = git("status", "--porcelain", cwd=wt)
    if dirty:
        raise SystemExit(f"worktree is not clean:\n{dirty}")
    if git("rev-parse", "HEAD", cwd=wt) != commit:
        if not a.runner_fix:
            raise SystemExit(f"worktree {rel(wt)} is not at {commit[:10]}")
        git("checkout", "-q", "--detach", commit, cwd=wt)  # a checked runner fix (preflight)
    return wt


def preflight(a, st: dict) -> dict:
    """Local checks; the worktree (under data/, git-ignored) is created unless plan-only."""
    out = {"warnings": [], "runner_fix": None}
    head = git("rev-parse", "HEAD")
    commit = git("rev-parse", a.commit)
    first = st.get("commit") or commit
    if not a.dry and commit != head:
        raise SystemExit("a real run runs the checked-out commit: leave --commit at HEAD")
    if commit != first:
        if not a.runner_fix:
            raise SystemExit(f"resume: the session ran commit {first[:10]}, this checkout is at {commit[:10]}; a "
                             f"later commit is allowed only with --runner-fix (changes in {list(RUNNER_FIX)} only)")
        files, bad = runner_fix_check(first, commit)
        if bad:
            raise SystemExit(f"--runner-fix refused: the commits since {first[:10]} change {bad}, outside "
                             f"{list(RUNNER_FIX)}")
        out["runner_fix"] = {"t_utc": utc(), "from": first, "to": commit, "files": files}
    out["commit"], out["first_commit"] = commit, first
    cs = case_set_check(Path(a.cases), commit, "dev" if a.rehearsal else CFG["case_split"],
                        check_runs_md=not a.rehearsal)
    probs = list(cs.pop("problems")) + code_check(commit, Path(a.config))
    out["case_set"] = {k: v for k, v in cs.items() if k != "case_ids"}
    ids = cs["case_ids"][:a.first_cases] if a.first_cases else cs["case_ids"]
    out["case_ids"] = ids
    out["tag"] = {"protocol_tag": a.protocol_tag, "tag_commit": None}
    if a.protocol_tag:
        tc, tp = tag_check(a.protocol_tag, first)
        out["tag"]["tag_commit"] = tc
        probs += tp
    elif not a.dry and not a.rehearsal:
        probs.append("--protocol-tag <tag> is required for a real run")
    if not a.dry:
        dirty = git("status", "--porcelain")
        if dirty:
            probs.append(f"the checkout has uncommitted changes: {dirty.splitlines()[:5]}")
        if not rpapi.key_present():
            probs.append("no RunPod API key file (~/.config/runpod/api_key)")
    hard = []
    if a.rehearsal:  # a rehearsal never runs on the test set, and stays in its own folder
        if cs["split"] != "dev":
            hard.append(f"--rehearsal needs a development case set, not split {cs['split']!r}")
        if not SESS.is_relative_to(REPO / "data" / "runs" / "rehearsal"):
            hard.append("--rehearsal writes under data/runs/rehearsal/ only")
    if a.plan:
        out["problems"] = hard + probs
        return out
    if hard or (probs and not a.dry):
        raise SystemExit("preflight:\n  " + "\n  ".join(hard + probs))
    out["warnings"] = [f"dry run, not enforced: {p}" for p in probs]
    wt = worktree(a, commit)
    chk = subprocess.run([sys.executable, "-c",
                          "import dataclasses, json, sys, gapcheck; from gapcheck import systems as s; "
                          "run = json.loads(sys.argv[1]); st = s.Settings.from_dict(run); "
                          "d = dataclasses.asdict(st); "
                          "assert set(d) == set(run) and all(d[k] == run[k] and type(d[k]) is type(run[k]) "
                          "for k in d), (d, run); "
                          "print(json.dumps([gapcheck.__file__, s.prompt_record(st.prompt_set)]))",
                          json.dumps(CFG["run"])],
                         cwd=wt, env=wt_env(wt), capture_output=True, text=True)
    if chk.returncode:
        raise SystemExit(f"the harness at the run commit does not accept the run settings exactly:\n{chk.stderr[-800:]}")
    f, prompts = json.loads(chk.stdout)
    if not f.startswith(str(wt)):
        raise SystemExit(f"gapcheck imported from {f}, not from {wt}")
    out["prompts"] = prompts
    rt = subprocess.run([sys.executable, str(KIT / "relay_selftest.py")], cwd=wt, env=wt_env(wt),
                        capture_output=True, text=True, timeout=120)
    if rt.returncode:
        raise SystemExit(f"relay self-test failed:\n{rt.stdout[-800:]}{rt.stderr[-800:]}")
    out["relay_test"] = rt.stdout.strip().splitlines()[-1]
    out["build_stop"] = local_build_stop(a, wt, commit, cs["manifest_sha256"], ids)
    out["wt"] = str(wt)
    return out


def local_build_stop(a, wt: Path, commit: str, manifest_sha: str, ids: list[str]) -> str:
    res = SESS / "results" / "build_stop_local.json"
    key = {"commit": commit, "manifest_sha256": manifest_sha, "n": len(ids), "run": CFG["run"]}
    try:
        prev = json.loads(res.read_text())
        if prev.get("ok") and prev.get("key") == key:
            return f"L1 build stop passed earlier ({prev['n']} cases)"
    except (OSError, json.JSONDecodeError):
        pass
    r = CFG["run"]
    cmd = [sys.executable, str(KIT / "calibrate.py"), "--local", "--cases", str(Path(a.cases).resolve()),
           "--out", str(res), "--chars-per-token", str(r["chars_per_token"]),
           "--context-limit", str(r["context_limit"]), "--l1-rounds", str(r["l1_tool_rounds"]),
           "--page-size", str(r["page_size"]), "--prompt-set", r["prompt_set"]]
    if a.first_cases:
        cmd += ["--case-ids", ",".join(ids)]
    p = subprocess.run(cmd, cwd=wt, env=wt_env(wt), capture_output=True, text=True, timeout=7200)
    (SESS / "logs" / "build_stop_local.out").write_text(p.stdout + p.stderr)
    if not res.exists():
        raise SystemExit(f"local build stop failed (rc {p.returncode}); see logs/build_stop_local.out")
    d = json.loads(res.read_text())
    d["key"] = key
    write_json(res, d)
    if not d["ok"]:
        raise SystemExit(f"L1 build stop: {len(d['problems'])} cases fail before any pod: "
                         + "; ".join(d["problems"][:5]))
    return f"L1 build stop ok on {d['n']} cases (estimate max {d['estimate_max']})"


# ---------------------------------------------------------------- processes
class Procs:
    relay = None
    fake = None
    watchdog = None


def heartbeat_loop(path: Path, stop: threading.Event) -> None:
    while not stop.is_set():
        path.write_text(f"{time.time():.0f}\n")
        stop.wait(20)


def start_watchdog(a, st: dict, hard_deadline: float) -> None:
    hb = SESS / "logs" / "watchdog.heartbeat"
    wlog = watchdog_log()
    if st.get("watchdog_pid") and pid_alive(st["watchdog_pid"]):
        age = time.time() - float(hb.read_text()) if hb.exists() else 1e9
        if age < 3 * a.watchdog_poll + 30:
            log(f"watchdog pid {st['watchdog_pid']} alive (heartbeat {age:.0f} s): kept")
            return
    stopf = SESS / "watchdog.stop"
    stopf.unlink(missing_ok=True)
    cmd = [sys.executable, "-I", str(KIT / "watchdog.py"), "--ledger", str(SESS / "ledger.json"),
           "--prefix", st["prefix"], "--hard-deadline", str(hard_deadline), "--budget", str(a.cap),
           "--margin", str(a.margin), "--poll", str(a.watchdog_poll), "--balance0", str(load_account()["balance0"]),
           "--log", str(wlog), "--stop-file", str(stopf), "--heartbeat", str(hb),
           "--client-heartbeat", str(SESS / "logs" / "session.heartbeat"),
           "--client-stale", str(a.client_stale)]
    if a.dry:
        cmd += ["--dry-state", str(SESS / "fake_runpod.json")]
    n0 = len(wlog.read_text().splitlines()) if wlog.exists() else 0
    p = subprocess.Popen(["nohup", *cmd], stdin=subprocess.DEVNULL,
                         stdout=open(account_dir() / "watchdog.stdout", "a"), stderr=subprocess.STDOUT,
                         start_new_session=True, cwd=str(KIT))
    Procs.watchdog = p
    st["watchdog_pid"] = p.pid
    save_state(st)
    for _ in range(120):
        lines = wlog.read_text().splitlines()[n0:] if wlog.exists() else []
        if any(" START " in x for x in lines) and hb.exists():
            timeline(f"watchdog started pid={p.pid} cap=${a.cap - a.margin:.2f} hard_deadline={utc(hard_deadline)}")
            return
        if p.poll() is not None:
            break
        time.sleep(0.5)
    raise SystemExit("watchdog did not start (see account/watchdog.stdout)")


def stop_watchdog(st: dict, wait_s: float = 150) -> None:
    (SESS / "watchdog.stop").write_text(utc() + "\n")
    pid = st.get("watchdog_pid")
    t0 = time.time()
    if Procs.watchdog is not None:  # started by this process: reap it
        try:
            Procs.watchdog.wait(wait_s)
        except subprocess.TimeoutExpired:
            pass
    while pid and pid_alive(pid) and time.time() - t0 < wait_s:
        time.sleep(1)
    alive = bool(pid and pid_alive(pid))
    timeline(f"watchdog {'still running (it exits by itself once no pod remains)' if alive else 'exited'}")


def wall_hours(left_usd: float, hourly: float) -> float:
    """Pod hours the budget left buys, rounded down to 0.05 h (3 min)."""
    return math.floor(max(left_usd, 0) / hourly * 20) / 20


def model_files_env() -> str:
    return "\n".join(f"{f['name']} {f['bytes']} {f['sha256']} {f['download_ranges']}" for f in CFG["model"]["files"])


def cloud_offer(g: dict, cloud: str) -> tuple[bool, float | None, dict | None]:
    """(available, GPU price per hour, catalog row) of the run's GPU on one cloud."""
    c = g["clouds"][cloud].get(CFG["gpu"]["id"])
    ok = bool(c and c["availability"] not in (None, "NONE") and (c["max_count"] or 0) > 0)
    return ok, (c or {}).get("price"), c


def create_pod(a, st: dict, token: str, upstream_fake: str | None, price: float | None) -> dict:
    led = load_ledger()
    n = len(led["pods"]) + 1
    hourly_guess = (price or a.max_hourly) + disk_gb() * DISK_RATE
    left = a.cap - a.margin - spent_estimate()
    max_wall_h = min(a.max_hours, wall_hours(left, hourly_guess)) if a.max_hours else wall_hours(left, hourly_guess)
    if max_wall_h < 0.5:
        raise SystemExit(f"budget left ${left:.2f} buys less than half a pod hour: raise --cap to go on")
    now = time.time()
    self_destruct = int(now + max_wall_h * 3600 + 180)
    start = (KIT / "pod_start.sh").read_bytes()
    g, m, s = CFG["gpu"], CFG["model"], CFG["server"]
    body = {
        "name": f"{st['prefix']}-{n}", "cloud": a.cloud,
        "gpu": {"id": g["id"], "count": 1, "minCudaVersion": g["min_cuda"], "minRamPerGpu": g["min_ram_gb"],
                "minVcpuCountPerGpu": g["min_vcpu"]},
        "image": CFG["image"], "entrypoint": ["/bin/bash", "-c"],
        "cmd": ['echo "$START_B64" | base64 -d > /start.sh && exec bash /start.sh'],
        "disk": disk_gb(), "ports": ["8080/http"], "startSsh": False, "startJupyter": False,
        "env": {"LLAMA_API_KEY": token, "START_B64": base64.b64encode(start).decode(),
                "SELF_DESTRUCT_AT": str(self_destruct), "NP": str(s["slots"]),
                "CTX_PER_SLOT": str(s["ctx_per_slot"]), "EXTRA_ARGS": s["extra_args"], "ALIAS": s["alias"],
                "IDLE_SECONDS": str(a.idle_seconds), "MODEL_REPO": m["repo"], "MODEL_REV": m["revision"],
                "MODEL_QUANT": m["quant"], "MODEL_FILES": model_files_env(),
                "TEMPLATE_SHA256": m["chat_template"]["sha256"]},
    }
    shown = json.loads(json.dumps(body))
    shown["env"] = {k: (f"<{len(v)} chars>" if k in ("LLAMA_API_KEY", "START_B64") else v)
                    for k, v in body["env"].items()}
    shown["start_sh_sha256"] = hashlib.sha256(start).hexdigest()
    (SESS / "logs" / f"create_pod_{n}.json").write_text(json.dumps(shown, indent=1) + "\n")
    timeline(f"create pod {body['name']} (cloud {a.cloud}, {g['id']}, max wall {max_wall_h} h)")
    code, d = rpapi.call("POST", "/v2/pods", body, timeout=120)
    if code not in (200, 201) or not isinstance(d, dict) or not d.get("id"):
        msg = json.dumps(d)[:600] if not isinstance(d, str) else d[:600]
        timeline(f"CREATE FAILED HTTP {code}: {msg}")
        raise SystemExit(3)
    cost = float(d.get("cost") or 0)
    created = rpapi.iso_to_epoch(d["createdAt"]) if d.get("createdAt") else now
    if cost + disk_gb() * DISK_RATE > hourly_guess:  # billed above the catalog price: a shorter deadline
        max_wall_h = min(max_wall_h, wall_hours(left, cost + disk_gb() * DISK_RATE))
    rec = {"id": d["id"], "name": body["name"], "created_epoch": created, "created_iso": d.get("createdAt"),
           "cost_api": cost, "hourly": cost + disk_gb() * DISK_RATE, "max_wall_h": max_wall_h,
           "deadline_epoch": created + max_wall_h * 3600, "self_destruct_at": self_destruct,
           "gpu": g["id"], "cloud": a.cloud, "image": CFG["image"], "disk_gb": disk_gb(),
           "launch": len(st["launches"]),
           "upstream": upstream_fake or f"https://{d['id']}-8080.proxy.runpod.net"}
    led["pods"].append(rec)
    save_ledger(led)
    keep = {k: d.get(k) for k in ("id", "name", "status", "cost", "cloud", "dataCenterId", "cudaVersion",
                                  "createdAt", "disk", "ports", "mounts", "gpu", "image")}
    (SESS / "logs" / f"create_pod_{n}.out.json").write_text(json.dumps(keep, indent=1) + "\n")
    timeline(f"pod {d['id']} created on {a.cloud} at ${cost}/h (+disk); deadline {utc(rec['deadline_epoch'])}")
    if cost > a.max_hourly:
        timeline(f"COST {cost} > max {a.max_hourly}: terminating at once")
        terminate_pod(rec)
        raise SystemExit(3)
    return rec


def live_pod() -> dict | None:
    for rec in reversed(load_ledger()["pods"]):
        if rec.get("terminated_epoch"):
            continue
        code, d = rpapi.get_pod(rec["id"])
        if code == 200:
            return rec
        if code == 404:
            mark_terminated(rec["id"])
    return None


def termination_time(pid: str) -> float:
    """When a pod stopped being billed: now, or earlier if the watchdog terminated it or saw
    it gone first (its log), e.g. while this session was not running."""
    t = time.time()
    try:
        lines = watchdog_log().read_text().splitlines()
    except OSError:
        lines = []
    for x in lines:
        if f"TERMINATE {pid}: " in x or f"pod {pid} no longer listed" in x:
            try:
                t = min(t, rpapi.iso_to_epoch(x[:20]))
            except ValueError:
                pass
            break
    return t


def mark_terminated(pid: str) -> None:
    led = load_ledger()
    for p in led["pods"]:
        if p["id"] == pid and not p.get("terminated_epoch"):
            p["terminated_epoch"] = termination_time(pid)
    save_ledger(led)


def terminate_pod(rec: dict) -> bool:
    for i in range(4):
        r = rpapi.terminate(rec["id"])
        time.sleep(2 if not rpapi.is_fake() else 0.2)
        code, _ = rpapi.get_pod(rec["id"])
        timeline(f"TERMINATE {rec['id']} -> {r}; GET -> {code}")
        if code == 404:
            mark_terminated(rec["id"])
            return True
    return False


def start_relay(a, st: dict, upstream: str) -> None:
    if Procs.relay and Procs.relay.poll() is None:
        return
    kill_stale(st.get("relay_pid"), "relay.py", str(SESS))
    if port_open(a.relay_port):
        raise SystemExit(f"port {a.relay_port} is in use (another relay?)")
    Procs.relay = subprocess.Popen([sys.executable, "-I", str(KIT / "relay.py"), "--listen",
                                    f"127.0.0.1:{a.relay_port}", "--upstream", upstream, "--token-file",
                                    str(SESS / "secret" / "llama_api_key"), "--log", str(SESS / "logs" / "relay.log"),
                                    "--timeout", str(CFG["client"]["request_timeout_s"])],
                                   stdout=open(SESS / "logs" / "relay.stdout", "a"), stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
    st["relay_pid"] = Procs.relay.pid
    save_state(st)
    for _ in range(40):
        if port_open(a.relay_port):
            timeline(f"relay started on 127.0.0.1:{a.relay_port}")
            return
        time.sleep(0.25)
    raise SystemExit("relay did not start")


def stop_proc(p) -> None:
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def watchdog_reason(pod_id: str) -> str:
    """Why the watchdog terminated a pod (its log), or that it was not the watchdog."""
    try:
        lines = watchdog_log().read_text().splitlines()
    except OSError:
        lines = []
    for x in reversed(lines):
        if f"TERMINATE {pod_id}: " in x:
            why = x.split(f"TERMINATE {pod_id}: ", 1)[1].rsplit(" -> ", 1)[0]
            if "balance" in why:  # no account amounts outside account/
                why = "balance drop since the first launch >= cap - margin"
            return "the watchdog terminated it: " + why
    return "terminated outside the watchdog (the pod's own guards or RunPod)"


def pod_fatal(rec: dict) -> str | None:
    """The pod's fatal-error file (gpu/pod_start.sh refused to start the server), if any."""
    code, d = rpapi.exec_cmd(rec["id"], ["bash", "-c", "sed 's/^/fatal: /' /models/logs/FATAL 2>/dev/null; "
                                                       "true # gapcheck-fatal-check"], 20)
    if code != 200 or not isinstance(d, dict):
        return None
    lines = [x[7:] for x in (d.get("stdout") or "").splitlines() if x.startswith("fatal: ")]
    return "; ".join(lines) or None


def wait_health(a, rec: dict) -> None:
    url = f"http://127.0.0.1:{a.relay_port}/health"
    t0 = time.time()
    last, t_fatal = None, 0.0
    while time.time() - t0 < a.health_timeout:
        code, body = http_get(url)
        if code == 200:
            timeline(f"server healthy after {time.time() - t0:.0f} s")
            return
        if code != last:
            log(f"health: {code} {body[:80]}")
            last = code
        if SIG["n"]:
            raise SystemExit("signal while waiting for the server")
        c, _ = rpapi.get_pod(rec["id"])
        if c == 404:
            raise SystemExit("the pod disappeared while waiting for the server")
        if time.time() - t_fatal >= a.fatal_poll:
            t_fatal = time.time()
            why = pod_fatal(rec)
            if why:
                timeline(f"POD REFUSED TO START THE SERVER: {why}")
                raise SystemExit(f"pod refused to start the server: {why}")
        time.sleep(a.health_poll)
    raise SystemExit(f"server not healthy after {a.health_timeout} s")


def calibrate(a, wt: Path, n: int, ids: list[str]) -> dict:
    out = SESS / "results" / f"calibration_{n}.json"
    r, d, s = CFG["run"], CFG["decoding"], CFG["server"]
    cmd = [sys.executable, str(KIT / "calibrate.py"), "--cases", str(Path(a.cases).resolve()), "--relay",
           f"http://127.0.0.1:{a.relay_port}", "--out", str(out), "--np", str(s["slots"]),
           "--ctx-per-slot", str(s["ctx_per_slot"]), "--max-tokens", str(d["max_tokens"]),
           "--template-sha256", template_sha(a), "--chars-per-token", str(r["chars_per_token"]),
           "--context-limit", str(r["context_limit"]), "--l1-rounds", str(r["l1_tool_rounds"]),
           "--page-size", str(r["page_size"]), "--prompt-set", r["prompt_set"], "--model", s["alias"],
           "--extra", json.dumps(d["extra"]), "--agent-config", f"{r['max_steps']}:{r['prompt_set']}"]
    if a.first_cases:
        cmd += ["--case-ids", ",".join(ids)]
    p = subprocess.run(cmd, cwd=wt, env=wt_env(wt), capture_output=True, text=True, timeout=3600)
    (SESS / "logs" / f"calibrate_{n}.out").write_text(p.stdout + p.stderr)
    if not out.exists():
        raise SystemExit(f"calibration failed (rc {p.returncode}); see logs/calibrate_{n}.out")
    cal = json.loads(out.read_text())
    timeline(f"calibration {n} (check only): max L1 {cal['max']} tokens ({cal['max_case']}) + "
             f"{d['max_tokens']} vs {s['ctx_per_slot']} served; estimate max {cal['estimate_max']} vs "
             f"context_limit {cal['context_limit']}; problems {cal['problems']}; notes {cal['notes']}")
    if not cal["ok"]:
        raise SystemExit("calibration: stopped before any model call: " + " | ".join(cal["problems"]))
    first = SESS / "results" / "calibration_1.json"
    if n > 1 and first.exists():
        c1 = json.loads(first.read_text())
        same = (c1["health"]["chat_template_sha256"], {k: v["tokens"] for k, v in c1["cases"].items()}) == \
               (cal["health"]["chat_template_sha256"], {k: v["tokens"] for k, v in cal["cases"].items()})
        if not same:
            raise SystemExit("calibration differs from the first pod's (template or token counts): not the same server?")
    return cal


def backend_config(a) -> dict:
    d = CFG["decoding"]
    return {"backend": "openai_compat", "base_url": f"http://127.0.0.1:{a.relay_port}/v1",
            "model": CFG["server"]["alias"], "revision": revision_text(a.dry),
            "temperature": d["temperature"], "seed": d["seed"], "max_tokens": d["max_tokens"],
            "timeout": CFG["client"]["request_timeout_s"], "preflight": None, "extra": d["extra"],
            "run": dict(CFG["run"])}


def write_backend(a) -> Path:
    p = SESS / f"backend_{CFG['name']}.json"
    cfg = backend_config(a)
    if p.exists() and json.loads(p.read_text()) != cfg:
        raise SystemExit(f"{p.name} differs from what this launch would write (settings or relay port changed?)")
    p.write_text(json.dumps(cfg, indent=1) + "\n")
    return p


# ---------------------------------------------------------------- finalize (local)
def run_rules(wt: Path, cases: Path, ids: list[str]) -> Path:
    rdir = SESS / "R"
    try:
        m = json.loads((rdir / "manifest.json").read_text())
        if m.get("finished_utc") and m["case_ids"] == ids:
            return rdir
    except (OSError, json.JSONDecodeError):
        pass
    rid = str(rdir.relative_to(queue_run.runs_root(SESS)))
    cmd = [sys.executable, str(wt / "scripts" / "run_systems.py"), "--cases", str(cases), "--systems", "R0,R1",
           "--run-id", rid, "--out", str(queue_run.runs_root(SESS)), "--force", "--case-ids", ",".join(ids)]
    subprocess.run(cmd, cwd=wt, env=wt_env(wt), check=True, capture_output=True, text=True)
    return rdir


def add_copies(merged: Path, sources: dict[str, Path]) -> None:
    man = json.loads((merged / "manifest.json").read_text())
    keep = set(man["case_ids"])
    copied = {}
    for s, src in sources.items():
        recs = [x for x in (src / f"{s}.jsonl").read_text().splitlines()
                if x.strip() and json.loads(x)["case_id"] in keep]
        (merged / f"{s}.jsonl").write_text("\n".join(recs) + ("\n" if recs else ""))
        copied[s] = str(src.relative_to(queue_run.runs_root(SESS)))
    man["copied_systems"] = copied
    man["systems"] = sorted(set(man.get("systems") or []) | set(sources), key=["R0", "R1", "L1", "A1", "A2"].index)
    (merged / "manifest.json").write_text(json.dumps(man, indent=1, sort_keys=True) + "\n")


def finalize(a, st: dict, wt: Path) -> dict:
    """Score only a complete run; otherwise write what is missing and stop."""
    name, ids, sy = CFG["name"], st["case_ids"], systems()
    cases = Path(a.cases).resolve()
    missing = [c for c in ids if not queue_run.usable(queue_run.case_dir(SESS, name, c), sy, c, accept_4xx())]
    ph = queue_run.load_phase(SESS, name)
    queue_h = sum((s["end"] or s["start"]) - s["start"] for s in ph["segments"]) / 3600
    exhausted = {}
    for c in ids:
        info = queue_run.exhausted_info(queue_run.case_dir(SESS, name, c), sy, c)
        if info:
            exhausted[c] = {"attempts": info["attempts"], "last_error": (info["errors"] or [{}])[-1].get("error")}
    b = billed()
    stt = {"t_utc": utc(), "run": name, "systems": sy, "case_set": rel(cases), "n_cases": len(ids),
           "n_complete": len(ids) - len(missing), "complete": not missing, "scored": False,
           "missing": missing, "failed_attempts": {c: t for c, t in ph["tasks"].items() if c in set(missing)},
           "exhausted": exhausted, "billed_gpu_hours": b["billed_gpu_hours"],
           "billed_usd_estimate": b["billed_usd_estimate"], "queue_wall_hours": round(queue_h, 4),
           "pods": b["pods"], "clouds": [x.get("cloud") for x in st.get("launches", [])],
           "protocol_tag": st.get("protocol_tag"), "tag_commit": st.get("tag_commit"),
           "first_run_commit": st.get("commit"), "run_commit": st.get("run_commit", st.get("commit")),
           "runner_fixes": st.get("runner_fixes", []),
           "deviation": "runner fix" if st.get("runner_fixes") else None,
           "rehearsal": a.rehearsal, "dry": a.dry}
    write_release()
    if missing:
        stt["next"] = ("not scored (no interim analysis, PROTOCOL.md sec. 11); resume with the same command "
                       "and --resume" + (" and a larger --cap" if any(w in str(st.get("status")) for w in
                                                                      ("budget", "spend", "balance")) else ""))
        write_json(SESS / "status.json", stt)
        timeline(f"finalize: NOT SCORED: {len(missing)} of {len(ids)} cases lack a usable record for every "
                 f"system ({', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}); see status.json")
        return stt
    timeline("finalize: every case complete: R0/R1, merge, score (local)")
    rdir = run_rules(wt, cases, ids)
    if a.dry or a.rehearsal:
        note = "; ".join(x for x in (
            f"DRY RUN ({rel(cases)}): fake server, no model" if a.dry else "",
            f"REHEARSAL on development cases ({rel(cases)})" if a.rehearsal else "") if x) + "; not a result"
    else:
        note = f"{name}: pre-registered run (PROTOCOL.md sec. 11); merged from one process per case"
    if st.get("runner_fixes"):
        note += "; deviation: runner fix " + ", ".join(f"{x['from'][:10]}->{x['to'][:10]}" for x in st["runner_fixes"])
    m = queue_run.merge(SESS, name, sy, ids, note, accept_4xx(), git_equiv=same_code(st["commit"]))
    merged = SESS / name / "merged"
    if m["complete"] != len(ids) or m["mismatch"]:
        stt.update(next="not scored: the parts differ in " + ", ".join(sorted(m["mismatch"])) +
                   " (or a case was lost); report before doing anything else")
        write_json(SESS / "status.json", stt)
        timeline(f"finalize: NOT SCORED: merge mismatch {sorted(m['mismatch'])}, {m['complete']} of {len(ids)}")
        return stt
    add_copies(merged, {"R0": rdir, "R1": rdir})
    if exhausted:
        timeline(f"finalize: {len(exhausted)} cases scored with records written after exhausted attempts: "
                 f"{', '.join(sorted(exhausted))}")
    if not b["billed_gpu_hours"]:
        stt.update(next="not scored: no billed GPU hours in the ledger")
        write_json(SESS / "status.json", stt)
        return stt
    cmd = [sys.executable, str(wt / "scripts" / "score_runs.py"), "--cases", str(cases), "--run", str(merged),
           "--gpu-hours", f"{b['billed_gpu_hours']:.4f}", "--usd-per-hour", f"{b['usd_per_billed_hour']:.4f}"]
    if a.dry:
        cmd += ["--n-boot", str(a.dry_n_boot)]
    r = subprocess.run(cmd, cwd=wt, env=wt_env(wt), capture_output=True, text=True)
    (SESS / "logs" / f"score_{name}.out").write_text(r.stdout + r.stderr)
    stt.update(scored=r.returncode == 0, score_rc=r.returncode, merged=rel(merged),
               score=rel(merged / "score.json"), gpu_hours_passed=b["billed_gpu_hours"],
               usd_per_hour_passed=b["usd_per_billed_hour"])
    write_json(SESS / "status.json", stt)
    timeline(f"finalize: merged {m['complete']} cases; score rc {r.returncode} ({rel(merged / 'score.json')})")
    write_release()
    return stt


RELEASE = (  # run files meant for release, relative to the run directory
    ("session.json", "state: commits, tag, config, case ids, launches (cloud, arguments)"),
    ("status.json", "completeness, exhausted cases, billed GPU hours and cost, clouds, commits"),
    ("logs/timeline.txt", "every step (pod ids as pod#n, no account amounts)"),
    ("wait.log", "stock checks before the launch (gpu/wait_launch.sh)"),
    ("backend_run1.json", "the backend configuration the harness read"),
    ("results/build_stop_local.json", "the local L1 build stop"),
    ("results/calibration_*.json", "server checks and token counts, per pod"),
    ("run1/phase.json", "attempts, their errors and timing, per case"),
    ("run1/cases/", "one harness run per case: manifest.json and one record per system"),
    ("R/", "R0 and R1"),
    ("run1/merged/", "all five systems merged, manifest.json, score.json, results.json"),
)
NOT_RELEASED = ("account/ (account balance, the account's pods, the watchdog log); ledger.json (pod ids; "
                "the billed hours and prices of each pod are in status.json); logs/ except timeline.txt "
                "(pod ids, proxy host, process output); podlogs/; run1/aborted/ and run1/logs/ (failed "
                "attempts, process output); secret/; wt/; session.lock; watchdog.stop; STOP; dry-run files")


def write_release() -> None:
    lines = ["Run files meant for release (paths relative to this directory). Generated by gpu/session.py.", ""]
    for pat, what in RELEASE:
        found = sorted(SESS.glob(pat.rstrip("/"))) if "*" in pat else [SESS / pat.rstrip("/")]
        for f in found:
            if f.exists():
                lines.append(f"{f.relative_to(SESS)}{'/' if f.is_dir() else ''}\t{what}")
    lines += ["", "Not for release: " + NOT_RELEASED, ""]
    (SESS / "RELEASE.txt").write_text("\n".join(lines))


# ---------------------------------------------------------------- main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", required=True, help="case set directory, e.g. data/cases/<test set>")
    ap.add_argument("--run-dir", required=True, help="session directory under data/runs/")
    ap.add_argument("--config", default=str(REPO / "configs" / "run1.json"))
    ap.add_argument("--cap", type=float, required=True, help="USD for all pods of the run; the watchdog "
                                                             "terminates at cap - margin")
    ap.add_argument("--go", action="store_true", help="create the pod (without it: plan only)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--finalize-only", action="store_true", help="no pod: the finalize step only")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--protocol-tag", help="required for a real run: the run commit is the tagged commit or "
                                           "follows it only by commits to RUNS.md, DEVIATIONS.md, results/")
    ap.add_argument("--runner-fix", action="store_true",
                    help="resume at a later commit that changes only gpu/, docs/, RUNS.md, DEVIATIONS.md, "
                         "results/ (a logged deviation)")
    ap.add_argument("--rehearsal", action="store_true",
                    help="a real-pod rehearsal on development cases under data/runs/rehearsal/; no tag needed")
    ap.add_argument("--cloud", choices=("COMMUNITY", "SECURE"),
                    help="RunPod cloud of a new pod (default: the run config's, Community); same GPU and image")
    ap.add_argument("--max-hourly", type=float, help="refuse a pod above this $/h (GPU part); default 1.80 on "
                                                     "Community, required with --cloud SECURE")
    ap.add_argument("--max-hours", type=float, default=0, help="pod wall limit (default: what the cap buys)")
    ap.add_argument("--launch-guard", type=float, default=900,
                    help="no case is launched this many seconds before the pod deadline")
    ap.add_argument("--margin", type=float, default=0.5)
    ap.add_argument("--soft-reserve", type=float, default=1.0, help="stop launching cases at cap - this")
    ap.add_argument("--idle-seconds", type=int, default=1200, help="in-pod idle self-terminate (0 off)")
    ap.add_argument("--client-stale", type=float, default=600)
    ap.add_argument("--relay-port", type=int)
    ap.add_argument("--health-timeout", type=float, default=3000)
    ap.add_argument("--health-poll", type=float, default=15)
    ap.add_argument("--fatal-poll", type=float, default=120, help="seconds between reads of the pod's fatal file")
    ap.add_argument("--watchdog-poll", type=float, default=30)
    ap.add_argument("--pod-poll", type=float, default=60, help="seconds between pod-alive checks during the run")
    ap.add_argument("--est-case-s", help="plan: LO,HI pod seconds per case, for a cost estimate")
    ap.add_argument("--est-setup-h", type=float, default=0.5, help="plan: pod hours outside the case queue")
    ap.add_argument("--dry", action="store_true", help="fake RunPod API and fake llama-server; no network")
    ap.add_argument("--first-cases", "--dry-cases", dest="first_cases", type=int, default=0,
                    help="dry or rehearsal: the first N cases of the case set only")
    ap.add_argument("--use-checkout", action="store_true", help="dry: this checkout instead of a worktree")
    ap.add_argument("--dry-availability", default="LOW")
    ap.add_argument("--dry-balance", type=float, default=20.0)
    ap.add_argument("--dry-speed", type=float, default=1.0)
    ap.add_argument("--dry-delay", type=float, default=0.05)
    ap.add_argument("--dry-n-boot", type=int, default=200)
    ap.add_argument("--dry-fail-text", help="dry: the fake server answers 503 to chat requests with this text")
    a = ap.parse_args(argv)
    a.plan = not a.go and not a.finalize_only
    if a.first_cases and not (a.dry or a.rehearsal):
        ap.error("--first-cases needs --dry or --rehearsal (run 1 runs every case)")
    if a.use_checkout and not a.dry:
        ap.error("--use-checkout needs --dry")
    if a.relay_port is None:
        a.relay_port = 18090 if a.dry else 18080
    if a.dry:
        a.health_poll, a.watchdog_poll = min(a.health_poll, 1.0), min(a.watchdog_poll, 2.0)
        a.fatal_poll, a.pod_poll = min(a.fatal_poll, 2.0), min(a.pod_poll, 2.0)
    return a


def plan(a) -> None:
    pf = preflight(a, {})
    g = gpu_check.check(CFG["gpu"]["id"])
    ok, gpu_price, row = cloud_offer(g, a.cloud)
    price = (gpu_price or a.max_hourly) + disk_gb() * DISK_RATE
    est = None
    if a.est_case_s:
        lo, hi = (float(x) for x in a.est_case_s.split(","))
        n = len(pf["case_ids"])
        h = [a.est_setup_h + n * lo / 3600, a.est_setup_h + n * hi / 3600]
        est = {"hours": [round(x, 2) for x in h], "usd": [round(x * price, 2) for x in h], "usd_per_hour": round(price, 4)}
    print(json.dumps({"plan_only": True, "commit": pf["commit"], "config": CFG["name"],
                      "case_set": pf["case_set"], "n_cases": len(pf["case_ids"]), "problems": pf["problems"],
                      "cloud": a.cloud, "gpu": row, "available": ok, "max_hourly": a.max_hourly,
                      "balance": g["balance"],
                      "pods_now": g["pods"], "balance_needed": a.cap + 0.5,
                      "balance_ok": g["balance"] >= a.cap + 0.5, "estimate": est,
                      "run_dir_exists": SESS.exists()}, indent=1))


def main(argv=None):
    global LOGF, SESS, CFG
    a = parse_args(argv)
    CFG = load_config(Path(a.config))
    a.cloud = a.cloud or CFG["gpu"]["cloud"]
    if a.max_hourly is None:
        if a.cloud != "COMMUNITY":
            raise SystemExit(f"--cloud {a.cloud} needs --max-hourly (USD per hour for the GPU; it costs more there)")
        a.max_hourly = 1.80
    SESS = Path(a.run_dir).resolve()
    try:
        queue_run.runs_root(SESS)  # run ids are paths relative to data/runs
    except ValueError as e:
        raise SystemExit(f"--run-dir: {e}")
    # run files store paths relative to the repository only
    for what, path in (("--run-dir", SESS), ("--cases", Path(a.cases).resolve()), ("--config", Path(a.config).resolve())):
        if not path.is_relative_to(REPO):
            raise SystemExit(f"{what} must be inside the repository ({REPO.name}/...)")

    if a.plan:
        if a.dry:
            raise SystemExit("--dry needs --go")
        plan(a)
        return

    st = {}
    if (SESS / "session.json").exists():
        if not a.resume:
            raise SystemExit(f"{SESS} exists: add --resume to continue it, or use a new --run-dir")
        st = load_state()
        if bool(st.get("dry")) != a.dry:
            raise SystemExit("resume: --dry differs from the session's")
        if st.get("config") != CFG:
            raise SystemExit("resume: the run config differs from the session's (see session.json)")
        if st.get("cases") != rel(Path(a.cases)):
            raise SystemExit(f"resume: --cases {a.cases} differs from the session's {st.get('cases')}")
        if bool(st.get("rehearsal")) != a.rehearsal:
            raise SystemExit("resume: --rehearsal differs from the session's")
        if st.get("protocol_tag") != a.protocol_tag:
            raise SystemExit(f"resume: --protocol-tag {a.protocol_tag} differs from the session's {st.get('protocol_tag')}")
    elif a.finalize_only:
        raise SystemExit(f"{SESS}: no session to finalize")
    SESS.mkdir(parents=True, exist_ok=True)
    for d in ("logs", "results", "secret"):
        (SESS / d).mkdir(exist_ok=True)
    os.chmod(SESS / "secret", 0o700)
    lock = SESS / "session.lock"
    if lock.exists() and pid_alive(lock.read_text().strip()) and lock.read_text().strip() != str(os.getpid()):
        raise SystemExit(f"another session process is running (pid {lock.read_text().strip()})")
    lock.write_text(str(os.getpid()))
    LOGF = open(SESS / "logs" / "session.log", "a")
    if a.dry:
        fstate = SESS / "fake_runpod.json"
        rpapi.use_fake(str(fstate))
        fs = json.loads(fstate.read_text())
        if not st:
            fs.update(balance0=a.dry_balance, speed=a.dry_speed)
        fs.update(availability=a.dry_availability)
        fstate.write_text(json.dumps(fs, indent=1))

    def on_signal(signum, frame):  # no file I/O here: the main thread may be writing the log
        SIG["n"] += 1
        os.write(2, f"{utc()} signal {signum} ({SIG['n']}): "
                    f"{'stop launching' if SIG['n'] == 1 else 'kill the running case'}\n".encode())
    for s_ in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s_, on_signal)

    hb_stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(SESS / "logs" / "session.heartbeat", hb_stop),
                     daemon=True).start()
    status = "error"
    wt = None
    try:
        timeline(f"session {SESS.name} start pid={os.getpid()} {'DRY ' if a.dry else ''}config={CFG['name']} "
                 f"cloud={a.cloud} max_hourly=${a.max_hourly} cap=${a.cap} resume={a.resume} "
                 f"finalize_only={a.finalize_only}")
        pf = preflight(a, st)
        wt = Path(pf["wt"])
        if not st:
            st = {"run_dir": rel(SESS), "dry": a.dry, "rehearsal": a.rehearsal, "created_utc": utc(),
                  "commit": pf["commit"], "run_commit": pf["commit"], "protocol_tag": a.protocol_tag,
                  "tag_commit": pf["tag"]["tag_commit"], "runner_fixes": [],
                  "prefix": f"gapcheck-{SESS.name}-{secrets.token_hex(3)}", "config": CFG,
                  "config_file": rel(Path(a.config)), "cases": rel(Path(a.cases)), "case_set": pf["case_set"],
                  "case_ids": pf["case_ids"], "prompts": pf["prompts"], "launches": []}
        if st["case_ids"] != pf["case_ids"] or st["case_set"]["manifest_sha256"] != pf["case_set"]["manifest_sha256"]:
            raise SystemExit("resume: the case set (or --dry-cases) differs from the session's")
        args = {k: (rel(Path(v)) if k in ("cases", "run_dir", "config") else v) for k, v in vars(a).items()}
        st["launches"].append({"t": utc(), "pid": os.getpid(), "cloud": a.cloud, "commit": pf["commit"],
                               "args": args, "warnings": pf["warnings"]})
        if pf["runner_fix"]:
            st["runner_fixes"].append(pf["runner_fix"])
            st["run_commit"] = pf["commit"]
        st["status"] = "running"
        save_state(st)
        if pf["runner_fix"]:
            fx = pf["runner_fix"]
            timeline(f"deviation: runner fix: resumed at commit {fx['to']} (first run commit {fx['from']}); "
                     f"files changed: {fx['files']}")
        if a.protocol_tag:
            timeline(f"protocol tag {a.protocol_tag} -> {st['tag_commit']}; first run commit {st['commit']}")
        for w in pf["warnings"]:
            timeline(f"WARNING {w}")
        timeline(f"preflight ok: commit {pf['commit'][:10]}, {len(pf['case_ids'])} cases, {pf['relay_test']}; "
                 f"{pf['build_stop']}")

        if a.finalize_only:
            status = "finalized"
            return

        # ---- GPU, balance, watchdog, pod
        tokf = SESS / "secret" / "llama_api_key"
        if not tokf.exists():
            tokf.write_text(secrets.token_urlsafe(32) + "\n")
            os.chmod(tokf, 0o600)
        token = tokf.read_text().strip()
        fake_up = None
        if a.dry:
            kill_stale(st.get("fake_pid"), "fake_server.py", str(SESS))
            tpl = SESS / "dry_template.jinja"
            tpl.write_text(DRY_TEMPLATE)
            port = free_port()
            Procs.fake = subprocess.Popen(
                [sys.executable, "-I", str(KIT / "fake_server.py"), "--port", str(port),
                 "--np", str(CFG["server"]["slots"]), "--ctx-per-slot", str(CFG["server"]["ctx_per_slot"]),
                 "--delay", str(a.dry_delay), "--ready-after", "3", "--template", str(tpl),
                 "--token-file", str(tokf)] + (["--fail-503-text", a.dry_fail_text] if a.dry_fail_text else []),
                stdout=open(SESS / "logs" / "fake_server.out", "a"), stderr=subprocess.STDOUT)
            st["fake_pid"] = Procs.fake.pid
            fake_up = f"http://127.0.0.1:{port}"
            fs = json.loads((SESS / "fake_runpod.json").read_text())
            fs["upstream"] = fake_up
            (SESS / "fake_runpod.json").write_text(json.dumps(fs, indent=1))
        g = gpu_check.check(CFG["gpu"]["id"])
        (account_dir() / f"gpu_check_{len(st['launches'])}.json").write_text(json.dumps(g, indent=1) + "\n")
        acct = load_account()
        acct.setdefault("balance0", g["balance"])
        save_account(acct)
        rec = live_pod()
        if rec is None:
            ok, price, row = cloud_offer(g, a.cloud)
            if not ok:
                timeline(f"STOP: {CFG['gpu']['id']} not available on {a.cloud} ({row}); no automatic switch "
                         "of cloud (--cloud chooses it)")
                status = f"stopped: GPU not available on {a.cloud}"
                return
            price = price or 0
            if price > a.max_hourly:
                timeline(f"STOP: {a.cloud} price ${price}/h > max ${a.max_hourly}/h")
                status = "stopped: price"
                return
            drop = acct["balance0"] - g["balance"]
            if drop >= a.cap - a.margin:  # the watchdog's balance rule, before a pod exists
                account_log(f"STOP: balance drop ${drop:.2f} since the first launch >= cap ${a.cap} - margin ${a.margin}")
                timeline("STOP: budget: the balance drop since the first launch reached cap - margin; raise --cap "
                         "to go on (amounts in account/)")
                status = "stopped: budget (balance drop reached the cap; resume with a larger --cap)"
                return
            left = a.cap - spent_estimate()
            if g["balance"] < left + 0.5:
                account_log(f"STOP: balance ${g['balance']:.2f} < budget left ${left:.2f} + $0.50")
                timeline("STOP: the account balance is below the budget left + $0.50 (amounts in account/)")
                status = "stopped: balance below cap"
                return
            wall = wall_hours(a.cap - a.margin - spent_estimate(), price + disk_gb() * DISK_RATE)
            if a.max_hours:
                wall = min(wall, a.max_hours)
            if wall < 0.5:
                timeline(f"STOP: budget: estimated spend ${spent_estimate():.2f} leaves less than half a pod hour "
                         f"under cap ${a.cap}; raise --cap to go on")
                status = "stopped: budget (cap reached; resume with a larger --cap)"
                return
            start_watchdog(a, st, time.time() + wall * 3600 + 600)
            rec = create_pod(a, st, token, fake_up, price)
        else:
            timeline(f"reusing live pod {rec['id']} on {rec.get('cloud')} (deadline {utc(rec['deadline_epoch'])})"
                     + (f"; --cloud {a.cloud} applies to the next new pod" if rec.get("cloud") != a.cloud else ""))
            st["launches"][-1]["cloud"] = rec.get("cloud")
            save_state(st)
            if fake_up:
                rec["upstream"] = fake_up  # dry: the fake server of this launch
            start_watchdog(a, st, rec["deadline_epoch"] + 600)
        start_relay(a, st, rec["upstream"])
        wait_health(a, rec)
        n_pod = len(load_ledger()["pods"])
        calibrate(a, wt, n_pod, st["case_ids"])
        backend = write_backend(a)

        # ---- the case queue
        alive = {"t": 0.0, "ok": True}

        def pod_gone() -> bool:
            if time.time() - alive["t"] > a.pod_poll:
                c, _ = rpapi.get_pod(rec["id"])
                alive.update(t=time.time(), ok=c != 404)
            return not alive["ok"]

        why_stop: dict[str, str] = {}

        def abort() -> bool:
            if SIG["n"] > 1:
                why_stop.setdefault("why", "second signal: the running case was killed")
                return True
            if pod_gone():
                why_stop.setdefault("why", f"pod gone ({watchdog_reason(rec['id'])})")
                return True
            return False

        def may_launch() -> str | None:
            if SIG["n"]:
                return "signal"
            if (SESS / "STOP").exists():
                return "STOP file"
            sp = spent_estimate()
            if sp >= a.cap - a.soft_reserve:
                return f"budget: estimated spend ${sp:.2f} >= cap ${a.cap} - reserve ${a.soft_reserve}"
            if time.time() >= rec["deadline_epoch"] - a.launch_guard:
                return f"pod deadline in less than {a.launch_guard / 60:.0f} min"
            if Procs.relay.poll() is not None:
                log("relay exited; restarting it once")
                Procs.relay = None
                start_relay(a, st, rec["upstream"])
            if pod_gone():
                return f"pod gone ({watchdog_reason(rec['id'])})"
            return None
        name, cl = CFG["name"], CFG["client"]
        timeline(f"run {name} start ({', '.join(systems())}; {CFG['run']}; client concurrency {cl['concurrency']})")
        res = queue_run.run_phase(SESS, name, systems(), backend, st["case_ids"], int(cl["concurrency"]), wt,
                                  Path(a.cases).resolve(), may_launch, log, max_attempts=int(cl["max_attempts"]),
                                  fail_limit=int(cl["fail_limit"]), task_timeout=float(cl["case_timeout_s"]),
                                  abort=abort, accept_4xx=accept_4xx(),
                                  max_total_attempts=int(cl["max_total_attempts"]), note=timeline)
        stop = why_stop.get("why") if res["stop"] == "aborted" else res["stop"]
        timeline(f"run {name} end: {res['complete']}/{res['total']} complete" + (f"; stopped: {stop}" if stop else ""))
        if stop:
            status = f"stopped: {stop}"
        elif res["complete"] < res["total"]:
            status = f"stopped: {res['total'] - res['complete']} cases failed every attempt"
        else:
            status = "done"
    except SystemExit as e:
        status = f"stopped: {e.code}" if e.code not in (None, 0) else status
        log(f"SystemExit: {e.code}")
    except Exception as e:  # anything else: still tear down
        status = f"error: {type(e).__name__}: {e}"
        log(status)
        import traceback
        log(traceback.format_exc())
    finally:
        try:
            pod = live_pod() if (SESS / "ledger.json").exists() else None
            if pod:
                c = collect.collect(SESS, pod["id"], 60 if a.dry else 240)
                timeline(f"pod logs collected: {c.get('ok')} {len(c.get('files') or {})} files")
                ok = terminate_pod(pod)
                if not ok:
                    timeline(f"WARNING: pod {pod['id']} not confirmed terminated; the watchdog keeps trying")
            if st.get("watchdog_pid"):
                stop_watchdog(st)
            stop_proc(Procs.relay)
            stop_proc(Procs.fake)
            kill_stale(st.get("relay_pid"), "relay.py", str(SESS))  # left by a killed launch
            kill_stale(st.get("fake_pid"), "fake_server.py", str(SESS))
            (SESS / "secret" / "llama_api_key").unlink(missing_ok=True)
            timeline(f"teardown done; estimated spend ${spent_estimate():.3f}")
        except Exception as e:
            log(f"TEARDOWN ERROR {type(e).__name__}: {e}")
        st["status"] = status
        if st.get("case_ids") and wt is not None:
            try:
                fin = finalize(a, st, wt)
                st["scored"] = fin["scored"]
                st["n_complete"] = fin["n_complete"]
            except Exception as e:
                log(f"FINALIZE ERROR {type(e).__name__}: {e}")
        if not a.finalize_only and (SESS / "ledger.json").exists():  # a pod existed: the account after it
            try:
                g = gpu_check.check(CFG["gpu"]["id"])
                acct = load_account()
                acct.update(balance_end=g["balance"], pods_end=g["pods"], t_end=utc())
                if acct.get("balance0") is not None:
                    acct["balance_drop"] = round(acct["balance0"] - g["balance"], 4)
                save_account(acct)
                account_log(f"session end: balance drop {acct.get('balance_drop')}")
            except Exception as e:
                log(f"final balance check failed: {e}")
        st["finished_utc"] = utc()
        st["estimated_spend"] = round(spent_estimate(), 3) if (SESS / "ledger.json").exists() else 0.0
        if st.get("commit"):
            save_state(st)
        hb_stop.set()
        timeline(f"session end: est spend ${st.get('estimated_spend')}; scored {st.get('scored')}; {status}")
        lock.unlink(missing_ok=True)
        if wt is None or status.startswith("error"):  # refused in the preflight, or an error
            raise SystemExit(1)


if __name__ == "__main__":
    main()
