"""Case queue of one GPU run (gpu/session.py).

The harness is unchanged: each case is one `scripts/run_systems.py --case-ids <id>` process,
run from the clean worktree of the run commit (PYTHONPATH=<wt>/src), writing
<session>/<run>/cases/<case_id>/{manifest.json,<system>.jsonl}. At most `np` processes run
at once (client concurrency; run 1: one), taking cases in the given order. Inside a process
the systems run one after another, so with np 1 the server sees one request at a time.

- Per-case flush: run_systems.py writes a case's records after all its systems are done,
  then marks the manifest finished. A finished case is a finished directory; an
  interruption loses only the case in flight.
- Usable case: finished manifest, one record per system, and no infrastructure error
  (`infra_errors`): a backend error whose HTTP status is not in `accept_4xx` (run 1:
  400 with a JSON body, the server's own rejection of the request). Any other 4xx (401,
  404 or 429 from the token or the proxy, say) says nothing about the model, so that
  attempt counts as failed, like a crash.
- Resume: a usable case is never run again; any other case directory is moved to
  <run>/aborted/ and the case is run again. Case processes left running by a session that
  was killed (pids in <run>/running.json) are killed first.
- Retry: a process that fails (exit code != 0, unusable output, or killed after
  `task_timeout` seconds) is queued again at the end, until it has had `max_attempts`
  attempts; the count is kept in <run>/phase.json across resumes, so a later session gives
  such a case one more attempt. An attempt the queue itself kills on `abort()` (second
  signal, pod gone) is not counted. After `fail_limit` failures in a row the queue stops
  launching (server or relay down). Each failed attempt's error text (the last line of the
  case log, the infrastructure errors, or the time-out) is kept in phase.json.
- Exhausted: with `max_total_attempts` = N, a case whose N-th counted attempt fails gets a
  record for every system written by this queue (`write_exhausted`): output null,
  schema_valid false, error "attempts exhausted (N): <last error>", the attempts' errors
  under "exhausted". Scoring reads it as no valid output. The case is then complete.
- `may_launch()` is asked before each launch; a non-empty string (budget, deadline, stop
  file, signal) stops launching. Cases in flight are allowed to finish (or killed after
  `task_timeout` seconds, or at once when `abort()` turns true).
- Timing of each segment goes to <run>/phase.json (segments add up across resumes).

`merge()` gathers usable cases into <run>/merged/ (case-set order) with one manifest,
after checking that every part ran the same commit, prompts, settings and backend. With
`git_equiv`, parts at different commits are accepted when it says their code is the same
(gpu/session.py: a logged runner fix that leaves the harness, scripts and config alone).
Error texts kept in phase.json and in exhausted records have the repository and home paths
replaced by relative ones.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

KEYS_SAME = ("git", "prompt_set", "prompt_files", "prompts_sha256", "conventions_sha256",
             "settings", "backend", "case_set", "systems")
HTTP = re.compile(r"HTTP (\d{3}): ?(.*)", re.S)


def case_dir(sess: Path, run: str, cid: str) -> Path:
    return sess / run / "cases" / cid


def _records(d: Path, systems: list[str], cid: str) -> dict[str, dict] | None:
    try:
        m = json.loads((d / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not m.get("finished_utc") or m.get("case_ids") != [cid]:
        return None
    out = {}
    for s in systems:
        try:
            lines = [x for x in (d / f"{s}.jsonl").read_text().splitlines() if x.strip()]
            rec = json.loads(lines[0]) if len(lines) == 1 else None
        except (OSError, json.JSONDecodeError):
            return None
        if not rec or rec.get("case_id") != cid:
            return None
        out[s] = rec
    return out


def complete(d: Path, systems: list[str], cid: str) -> bool:
    """Finished manifest and one record per system (an output or a logged error)."""
    return _records(d, systems, cid) is not None


def http_errors(rec: dict) -> list[tuple[int, str]]:
    """(status, message) of every backend error in one system record: the trace's error
    entries (systems.answer logs a BackendError as "HTTP <status>: <body>") and an error
    field "backend: HTTP ..."; other error texts (schema messages, say) are not read."""
    msgs = [e.get("error") or "" for e in rec.get("trace") or [] if e.get("type") == "error"]
    if str(rec.get("error") or "").startswith("backend: "):
        msgs.append(rec["error"][len("backend: "):])
    out = []
    for m in msgs:
        h = HTTP.match(m)
        if h:
            out.append((int(h.group(1)), h.group(2)))
    return out


def infra_errors(d: Path, systems: list[str], cid: str, accept_4xx=(400,)) -> list[str]:
    """Backend errors that are not the server's own rejection of a request: an HTTP status
    outside `accept_4xx`, or an accepted status whose body is not llama-server's JSON."""
    recs = _records(d, systems, cid) or {}
    bad = []
    for s, rec in recs.items():
        for status, body in http_errors(rec):
            if status not in accept_4xx or not body.lstrip().startswith("{"):
                bad.append(f"{s}: HTTP {status}")
    return sorted(set(bad))


def usable(d: Path, systems: list[str], cid: str, accept_4xx=(400,)) -> bool:
    return complete(d, systems, cid) and not infra_errors(d, systems, cid, accept_4xx)


def _set_aside(sess: Path, run: str, cid: str, why: str) -> None:
    d = case_dir(sess, run, cid)
    if d.exists():
        dest = sess / run / "aborted" / f"{cid}.{time.strftime('%Y%m%dT%H%M%S')}.{why}"
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{dest.name}.{n}")
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))


def runs_root(sess: Path) -> Path:
    """data/runs (run ids are paths relative to it)."""
    for p in sess.parents:
        if p.name == "runs" and p.parent.name == "data":
            return p
    raise ValueError(f"{sess} is not under data/runs")


def load_phase(sess: Path, run: str) -> dict:
    p = sess / run / "phase.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"config": run, "segments": [], "tasks": {}}


def save_phase(sess: Path, run: str, ph: dict) -> None:
    p = sess / run / "phase.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ph, indent=1) + "\n")
    os.replace(tmp, p)


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def scrub(sess: Path, text: str) -> str:
    """No absolute local path in a text kept with the run: the repository root and the home
    folder are replaced by '.' and '~'."""
    repo = str(runs_root(sess).parents[1])
    return text.replace(repo + "/", "").replace(repo, ".").replace(str(Path.home()), "~")


def last_log_line(sess: Path, run: str, cid: str) -> str:
    try:
        lines = [x.strip() for x in (sess / run / "logs" / f"{cid}.log").read_text(errors="replace").splitlines()]
    except OSError:
        return ""
    lines = [x for x in lines if x]
    return scrub(sess, lines[-1])[:300] if lines else ""


def _base_manifest(sess: Path, run: str, cid: str, systems: list[str]) -> dict:
    """The run-level fields of a case manifest: from this case's latest failed attempt
    (run_systems.py writes its manifest before any model call), else from a usable case."""
    cands = sorted((sess / run / "aborted").glob(f"{cid}.*/manifest.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    cands += sorted((sess / run / "cases").glob("*/manifest.json"))
    for p in cands:
        try:
            m = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if all(k in m for k in KEYS_SAME):
            return {k: m[k] for k in KEYS_SAME}
    return {k: None for k in KEYS_SAME}


def write_exhausted(sess: Path, run: str, cid: str, systems: list[str], errors: list[dict]) -> str:
    """Records of a case whose attempts are exhausted, written by the runner: for every
    system, no output, schema_valid false, the error. Returns the error text."""
    root = runs_root(sess)
    n = sum(1 for e in errors if e.get("counted", True))
    last = next((e["error"] for e in reversed(errors) if e.get("error")), "no error text")
    msg = f"attempts exhausted ({n}): {last}"
    d = case_dir(sess, run, cid)
    if d.exists():
        _set_aside(sess, run, cid, "before_exhausted")
    d.mkdir(parents=True)
    info = {"attempts": n, "errors": errors, "written_by": "gpu/queue_run.py", "t_utc": _utc()}
    for s in systems:
        rec = {"case_id": cid, "system": s, "output": None, "schema_valid": False, "error": msg, "trace": [],
               "tokens": {"input_tokens": 0, "output_tokens": 0}, "model_calls": 0, "tool_calls": 0,
               "seconds": 0.0, "verifier": [] if s == "A2" else None, "forced_fallback": False,
               "audit": None, "exhausted": info}
        (d / f"{s}.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    man = {**_base_manifest(sess, run, cid, systems), "run_id": str(d.relative_to(root)), "case_ids": [cid],
           "n_cases": 1, "started_utc": info["t_utc"], "finished_utc": info["t_utc"], "exhausted": info}
    (d / "manifest.json").write_text(json.dumps(man, indent=1, sort_keys=True) + "\n")
    return msg


def exhausted_info(d: Path, systems: list[str], cid: str) -> dict | None:
    recs = _records(d, systems, cid) or {}
    return next((r["exhausted"] for r in recs.values() if r.get("exhausted")), None)


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def kill_orphans(sess: Path, run: str, log: Callable[[str], None]) -> list[int]:
    """Kill case processes a killed session left running (recorded in running.json)."""
    p = sess / run / "running.json"
    try:
        running = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    killed = []
    for cid, pid in running.items():
        cmd = _cmdline(int(pid))
        if "run_systems.py" in cmd and cid in cmd:
            log(f"{run}: killing case process {pid} ({cid}) left by an earlier session")
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
            killed.append(int(pid))
    t0 = time.time()
    while killed and time.time() - t0 < 10 and any(_cmdline(x) for x in killed):
        time.sleep(0.1)
    p.unlink(missing_ok=True)
    return killed


def _save_running(sess: Path, run: str, running: dict) -> None:
    p = sess / run / "running.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({cid: v[0].pid for cid, v in running.items()}) + "\n")
    os.replace(tmp, p)


def run_phase(sess: Path, run: str, systems: list[str], backend: Path, order: list[str],
              np: int, wt: Path, cases: Path, may_launch: Callable[[], str | None],
              log: Callable[[str], None], python: str = sys.executable,
              max_attempts: int = 2, fail_limit: int = 4, task_timeout: float = 2400,
              poll: float = 1.0, abort: Callable[[], bool] = lambda: False,
              accept_4xx=(400,), max_total_attempts: int | None = None,
              note: Callable[[str], None] | None = None) -> dict:
    note = note or log
    root = runs_root(sess)
    (sess / run / "cases").mkdir(parents=True, exist_ok=True)
    (sess / run / "logs").mkdir(parents=True, exist_ok=True)
    kill_orphans(sess, run, log)
    ph = load_phase(sess, run)
    todo = []
    for cid in order:
        d = case_dir(sess, run, cid)
        if usable(d, systems, cid, accept_4xx):
            continue
        if d.exists():
            _set_aside(sess, run, cid, "incomplete")
        task = ph["tasks"].get(cid, {})
        if max_total_attempts and task.get("attempts", 0) >= max_total_attempts:
            msg = write_exhausted(sess, run, cid, systems, task.get("errors", []))
            note(f"{run}: {cid}: {msg} (record written by the runner)")
            continue
        todo.append(cid)
    n_total, n_done0 = len(order), len(order) - len(todo)
    seg = {"start": time.time(), "end": None, "n_done": 0, "n_failed": 0, "np": np,
           "stop_reason": None, "resumed_with_done": n_done0}
    ph["segments"].append(seg)
    save_phase(sess, run, ph)
    log(f"{run}: {n_done0} of {n_total} cases already complete; {len(todo)} to run, np {np}")
    pending = deque(todo)
    running: dict[str, tuple] = {}
    attempts = {cid: ph["tasks"].get(cid, {}).get("attempts", 0) for cid in todo}
    streak, stop = 0, None
    gave_up = []
    interrupted = set()
    while pending or running:
        if running and abort():
            log(f"{run}: abort: killing {len(running)} running cases (they will be re-run on resume)")
            for cid, (p, _, _) in running.items():
                p.kill()
                interrupted.add(cid)
            stop = stop or "aborted"
        for cid, (p, t0, lf) in list(running.items()):
            rc = p.poll()
            timed_out = False
            if rc is None and time.time() - t0 > task_timeout:
                p.kill()
                rc = p.wait()
                timed_out = True
                log(f"{run}: {cid} killed after {task_timeout:.0f} s")
            if rc is None:
                continue
            lf.close()
            del running[cid]
            _save_running(sess, run, running)
            t1 = time.time()
            d = case_dir(sess, run, cid)
            infra = infra_errors(d, systems, cid, accept_4xx) if rc == 0 else []
            ok = rc == 0 and complete(d, systems, cid) and not infra
            errors = list(ph["tasks"].get(cid, {}).get("errors", []))
            if not ok:
                counted = cid not in interrupted
                if not counted:
                    attempts[cid] -= 1
                if not counted:
                    why = "interrupted by the runner (not counted)"
                elif timed_out:
                    why = f"killed after {task_timeout:.0f} s (case time-out)"
                elif infra:
                    why = "infrastructure errors: " + ", ".join(infra)
                elif rc != 0:
                    why = last_log_line(sess, run, cid) or f"exit code {rc}"
                else:
                    why = "exit code 0 but the records are incomplete"
                errors.append({"attempt": attempts[cid] + (0 if counted else 1), "counted": counted, "rc": rc,
                               "error": why, "t_utc": _utc()})
            ph["tasks"][cid] = {"start": t0, "end": t1, "rc": rc, "ok": ok, "attempts": attempts[cid],
                                "errors": errors}
            if infra:
                ph["tasks"][cid]["infra_errors"] = infra
            if ok:
                seg["n_done"] += 1
                streak = 0
            else:
                seg["n_failed"] += 1
                streak += 1 if cid not in interrupted else 0
                _set_aside(sess, run, cid, "infra" if infra else f"rc{rc}")
                if max_total_attempts and attempts[cid] >= max_total_attempts:
                    msg = write_exhausted(sess, run, cid, systems, errors)
                    ph["tasks"][cid]["exhausted"] = True
                    seg["n_done"] += 1
                    note(f"{run}: {cid}: {msg} (record written by the runner)")
                elif attempts[cid] < max_attempts and stop is None:
                    pending.append(cid)
                else:
                    gave_up.append(cid)
            interrupted.discard(cid)
            done = n_done0 + seg["n_done"]
            log(f"{run}: {'ok  ' if ok else 'FAIL'} {cid} rc={rc} {t1 - t0:.0f} s"
                + (f" infrastructure errors {infra}" if infra else "")
                + f"; {done}/{n_total} done, {len(running)} running, {len(pending)} queued")
            save_phase(sess, run, ph)
        if stop is None and streak >= fail_limit:
            stop = f"{streak} failures in a row"
            log(f"{run}: STOP launching: {stop}")
        while stop is None and pending and len(running) < np:
            why = may_launch()
            if why:
                stop = why
                log(f"{run}: STOP launching: {why}")
                break
            cid = pending.popleft()
            attempts[cid] += 1
            rid = str(case_dir(sess, run, cid).relative_to(root))
            cmd = [python, str(wt / "scripts" / "run_systems.py"), "--cases", str(cases),
                   "--systems", ",".join(systems), "--backend", str(backend), "--run-id", rid,
                   "--out", str(root), "--case-ids", cid]
            lf = open(sess / run / "logs" / f"{cid}.log", "a")
            env = dict(os.environ, PYTHONPATH=str(wt / "src"))
            running[cid] = (subprocess.Popen(cmd, cwd=wt, env=env, stdout=lf, stderr=subprocess.STDOUT),
                            time.time(), lf)
            _save_running(sess, run, running)
        if stop is not None and pending:
            gave_up += list(pending)
            pending.clear()
        if not running and not pending:
            break
        time.sleep(poll)
    seg["end"] = time.time()
    seg["stop_reason"] = stop
    done = sum(usable(case_dir(sess, run, c), systems, c, accept_4xx) for c in order)
    ph["complete"] = done
    save_phase(sess, run, ph)
    log(f"{run}: segment {seg['end'] - seg['start']:.0f} s, {seg['n_done']} done, "
        f"{seg['n_failed']} failed; {done}/{n_total} complete" + (f"; stopped: {stop}" if stop else ""))
    return {"complete": done, "total": n_total, "stop": stop, "not_run": gave_up}


def merge(sess: Path, run: str, systems: list[str], case_order: list[str], note: str,
          accept_4xx=(400,), git_equiv: Callable[[dict, dict], bool] | None = None) -> dict:
    """<run>/merged/{system}.jsonl over the usable cases, in case-set order."""
    root = runs_root(sess)
    recs = {s: {} for s in systems}
    mans = []
    for cid in case_order:
        d = case_dir(sess, run, cid)
        if not usable(d, systems, cid, accept_4xx):
            continue
        m = json.loads((d / "manifest.json").read_text())
        mans.append(m)
        for s in systems:
            r = json.loads((d / f"{s}.jsonl").read_text().splitlines()[0])
            recs[s][cid] = r
    if not mans:
        return {"complete": 0, "mismatch": {}}
    m0 = mans[0]
    mismatch = {k: sorted({json.dumps(m.get(k), sort_keys=True) for m in mans})
                for k in KEYS_SAME if len({json.dumps(m.get(k), sort_keys=True) for m in mans}) > 1}
    if "git" in mismatch and git_equiv and all(git_equiv(m0.get("git"), m.get("git")) for m in mans):
        del mismatch["git"]  # different commits, same run code
    out = sess / run / "merged"
    out.mkdir(parents=True, exist_ok=True)
    ids = [c for c in case_order if c in recs[systems[0]]]
    for s in systems:
        with (out / f"{s}.jsonl").open("w") as f:
            for c in ids:
                f.write(json.dumps(recs[s][c], ensure_ascii=False) + "\n")
    man = {k: m0.get(k) for k in KEYS_SAME}
    man.update(run_id=str(out.relative_to(root)), case_ids=ids, n_cases=len(ids),
               started_utc=min(m["started_utc"] for m in mans),
               finished_utc=max(m["finished_utc"] for m in mans),
               parts=len(mans), parts_dir=str((sess / run / "cases").relative_to(root)),
               git_commits=sorted({(m.get("git") or {}).get("commit") or "" for m in mans}),
               mismatch=mismatch, note=note)
    (out / "manifest.json").write_text(json.dumps(man, indent=1, sort_keys=True) + "\n")
    return {"complete": len(ids), "mismatch": mismatch, "dir": str(out)}
