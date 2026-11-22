"""Dry runs of gpu/session.py in run-1 mode (configs/run1.json) against the fake RunPod API
(gpu/fakerp.py) and the fake llama-server (gpu/fake_server.py): the same code path as a real
run, with no network, no GPU and no model. Each test runs in a throwaway git repository made
from this working tree (tracked files, one commit) with a copy of the development case set,
so commits, tags and worktrees never touch this repository.

  complete              -> every case usable, R0/R1 added, merged and scored with the billed
                           GPU hours; the run files meant for release (RELEASE.txt) hold no
                           absolute path, no pod id and no account amount
  budget stop           -> the watchdog terminates the pod mid-run (balance drop >= cap);
                           nothing is merged or scored, status.json lists the missing cases;
                           a resume on Secure Cloud with a larger cap runs only those, scores
  crash (SIGKILL)       -> no teardown, no status, no score; the resume stops the relay and
                           fake server the dead session left, kills its case process, reuses
                           the live pod and the watchdog, and then scores
  exhausted attempts    -> one case's requests always get HTTP 503: not scored after its 2nd
                           and 3rd attempts; at the 4th the runner writes its records (no valid
                           output) and the run is scored
  runner fix            -> a resume at a later commit is refused without --runner-fix, and
                           with it when src/gapcheck/ changed; allowed when only gpu/ changed,
                           logged as a deviation, and the parts at two commits merge
  rehearsal             -> development cases only, under data/runs/rehearsal/ only
  real-run refusals     -> no tag, a commit after the tag that touches more than RUNS.md,
                           DEVIATIONS.md or results/, no API key: refused before any network

They need the development case set (built by `make cases`); without it they are skipped.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "gpu"))
import queue_run  # noqa: E402
import session  # noqa: E402
from gapcheck import serialize  # noqa: E402
from gapcheck.tools import CaseEnv  # noqa: E402

DEV = "data/cases/dev_s20261120_utc_n10"
RUN1 = json.loads((REPO / "configs" / "run1.json").read_text())
GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@localhost"]
pytestmark = pytest.mark.skipif(not (REPO / DEV / "manifest.json").exists(),
                                reason="needs the development case set (make cases)")


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", *GIT_ID, *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A git repository with this working tree's tracked files and the dev case set."""
    r = tmp_path / "repo"
    for f in subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True,
                            check=True).stdout.split("\0"):
        src = REPO / f
        if f and src.is_file():
            (r / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, r / f)
    git(r, "init", "-q")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    shutil.copytree(REPO / DEV, r / DEV)
    return r


def cleanup(d: Path) -> None:
    pids = []
    try:
        st = json.loads((d / "session.json").read_text())
        pids += [st.get(k) for k in ("watchdog_pid", "relay_pid", "fake_pid")]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        pids += list(json.loads((d / "run1" / "running.json").read_text()).values())
    except (OSError, json.JSONDecodeError):
        pass
    for pid in pids:
        cmd = session.cmdline(pid) if pid else ""
        if str(d) in cmd or "run_systems.py" in cmd:
            os.kill(int(pid), signal.SIGKILL)


@pytest.fixture
def run_dir(repo):
    d = repo / "data" / "runs" / "dry"
    yield d
    cleanup(d)


def cmd(repo: Path, d: Path, port: int, cap: float, n: int, *extra: str, dry: bool = True) -> list[str]:
    return [sys.executable, str(repo / "gpu" / "session.py"), "--cases", DEV, "--run-dir",
            str(d.relative_to(repo)), "--cap", str(cap), "--go", "--relay-port", str(port),
            *(["--dry", "--first-cases", str(n)] if dry else []), *extra]


def run(repo: Path, c: list[str], timeout: float = 420, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(c, cwd=repo, capture_output=True, text=True, timeout=timeout, env=env)


def usable_cases(d: Path, ids: list[str]) -> list[str]:
    return [c for c in ids if queue_run.usable(queue_run.case_dir(d, "run1", c), RUN1["systems"], c)]


def case_ids(n: int) -> list[str]:
    lines = (REPO / DEV / "cases.jsonl").read_text().splitlines()
    return [json.loads(x)["case_id"] for x in lines if x.strip()][:n]


def started(d: Path, cid: str) -> str:
    return json.loads((queue_run.case_dir(d, "run1", cid) / "manifest.json").read_text())["started_utc"]


def left_running(d: Path) -> list:
    st = json.loads((d / "session.json").read_text())
    return [(k, st[k]) for k in ("watchdog_pid", "relay_pid", "fake_pid") if st.get(k) and session.pid_alive(st[k])]


def wait_for(cond, timeout: float, what: str):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def stop_after_first_case(repo: Path, d: Path, c: list[str], ids: list[str]) -> None:
    """Launch, and touch STOP once the first case is usable (a graceful stop)."""
    p = subprocess.Popen(c, cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for(lambda: len(usable_cases(d, ids)) >= 1 or p.poll() is not None, 240, "the first case")
    (d / "STOP").write_text("x")
    assert p.wait(240) == 0


def release_files(d: Path) -> list[Path]:
    out = []
    for line in (d / "RELEASE.txt").read_text().split("\n\nNot for release")[0].splitlines()[2:]:
        f = d / line.split("\t")[0].rstrip("/")
        out += [x for x in f.rglob("*") if x.is_file()] if f.is_dir() else [f]
    return out


def test_complete_run_is_scored(repo, run_dir):
    port = session.free_port()
    p = run(repo, cmd(repo, run_dir, port, 12, 4))
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"] and stt["missing"] == [], stt
    merged = run_dir / "run1" / "merged"
    man = json.loads((merged / "manifest.json").read_text())
    assert man["systems"] == ["R0", "R1", "L1", "A1", "A2"] and man["n_cases"] == 4 and not man["mismatch"]
    assert man["settings"] == RUN1["run"]  # the harness ran with exactly the frozen settings
    assert man["backend"]["temperature"] == 0 and man["backend"]["extra"] == RUN1["decoding"]["extra"]
    score = json.loads((merged / "score.json").read_text())
    assert set(score["accuracy_main"]["systems"]) == {"R0", "R1", "L1", "A1", "A2"}
    # cost: billed GPU hours of the pod (creation to termination), not the queue wall time
    led = json.loads((run_dir / "ledger.json").read_text())["pods"]
    billed_h = (led[0]["terminated_epoch"] - led[0]["created_epoch"]) / 3600
    assert abs(stt["billed_gpu_hours"] - billed_h) < 1e-3 and stt["queue_wall_hours"] < stt["billed_gpu_hours"]
    assert abs(score["cost"]["gpu_hours"] - stt["billed_gpu_hours"]) < 1e-3
    assert stt["pods"][0]["cloud"] == "COMMUNITY" and stt["clouds"] == ["COMMUNITY"]
    assert stt["exhausted"] == {} and stt["deviation"] is None and stt["run_commit"] == stt["first_run_commit"]
    st = json.loads((run_dir / "session.json").read_text())
    assert st["status"] == "done" and st["scored"]
    assert len(led) == 1 and led[0]["terminated_epoch"]
    created = json.loads((run_dir / "logs" / "create_pod_1.json").read_text())
    assert created["env"]["LLAMA_API_KEY"].startswith("<") and "CT_B64" not in created["env"]
    assert created["env"]["TEMPLATE_SHA256"] == RUN1["model"]["chat_template"]["sha256"]
    assert created["env"]["NP"] == "8" and created["image"] == RUN1["image"]
    assert not (run_dir / "secret" / "llama_api_key").exists()
    assert not left_running(run_dir)
    # one request at a time: no two case processes overlapped
    phase = json.loads((run_dir / "run1" / "phase.json").read_text())
    spans = sorted((t["start"], t["end"]) for t in phase["tasks"].values())
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    # privacy of the run files: the account stays in account/; release files are clean
    acct = json.loads((run_dir / "account" / "account.json").read_text())
    assert "balance0" in acct and "balance_end" in acct and "pods_end" in acct
    assert (run_dir / "account" / "watchdog.log").exists() and (run_dir / "account" / "gpu_check_1.json").exists()
    rel = release_files(run_dir)
    names = {str(f.relative_to(run_dir)) for f in rel}
    assert {"session.json", "status.json", "logs/timeline.txt", "run1/merged/score.json"} <= names
    assert not any(n.startswith(("account/", "logs/session", "ledger")) for n in names)
    for f in rel:
        text = f.read_text(errors="replace")
        assert str(repo) not in text and str(Path.home()) not in text, f
        assert led[0]["id"] not in text, f  # pod ids stay in ledger.json and the full logs
        assert not re.search(r'"(balance0|balance_end|balance_drop|pods_end)"', text), f
    assert "pod#1" in (run_dir / "logs" / "timeline.txt").read_text()


def test_budget_stop_is_not_scored_and_resumes(repo, run_dir):
    port, n = session.free_port(), 8
    ids = case_ids(n)
    p = subprocess.Popen(cmd(repo, run_dir, port, 5, n, "--dry-delay", "0.1"), cwd=repo,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = []
    threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True).start()
    wait_for(lambda: len(usable_cases(run_dir, ids)) >= 1 or p.poll() is not None, 240, "the first case")
    # spending elsewhere on the account: the balance drops by more than cap - margin
    fs = run_dir / "fake_runpod.json"
    s = json.loads(fs.read_text())
    s["balance0"] -= 4.6
    tmp = fs.with_suffix(".test")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, fs)
    assert p.wait(240) == 0, "".join(out)[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    done1 = usable_cases(run_dir, ids)
    assert not stt["complete"] and not stt["scored"] and 1 <= len(done1) < n, "".join(out)[-3000:]
    assert sorted(stt["missing"]) == sorted(set(ids) - set(done1))
    assert "larger --cap" in stt["next"]
    assert not (run_dir / "run1" / "merged").exists() and not (run_dir / "R").exists()
    st = json.loads((run_dir / "session.json").read_text())
    assert "pod gone" in st["status"] and "balance drop" in st["status"], st["status"]
    assert "TERMINATE" in (run_dir / "account" / "watchdog.log").read_text()
    t_done = {c: started(run_dir, c) for c in done1}

    # the same cap again: refused before any pod, still not scored
    p2 = run(repo, cmd(repo, run_dir, port, 5, n, "--resume"))
    assert p2.returncode == 0
    assert not json.loads((run_dir / "status.json").read_text())["scored"]
    assert len(json.loads((run_dir / "ledger.json").read_text())["pods"]) == 1
    tl = (run_dir / "logs" / "timeline.txt").read_text()
    assert "balance drop since the first launch reached" in tl and not re.search(r"balance[^\n]*\$\d", tl)

    # resumed on Secure Cloud (same GPU and image), with its own price ceiling
    p3 = run(repo, cmd(repo, run_dir, port, 12, n, "--resume", "--cloud", "SECURE", "--max-hourly", "3"))
    assert p3.returncode == 0, p3.stdout[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"], stt
    assert {c: started(run_dir, c) for c in done1} == t_done  # finished cases were not run again
    pods = json.loads((run_dir / "ledger.json").read_text())["pods"]
    assert [x["cloud"] for x in pods] == ["COMMUNITY", "SECURE"] and pods[1]["hourly"] > 2.69
    assert [x["cloud"] for x in stt["pods"]] == ["COMMUNITY", "SECURE"]
    assert stt["clouds"] == ["COMMUNITY", "COMMUNITY", "SECURE"]  # one per launch
    assert abs(stt["billed_gpu_hours"] - sum(x["billed_hours"] for x in stt["pods"])) < 1e-3
    assert abs(stt["usd_per_hour_passed"] * stt["billed_gpu_hours"] - stt["billed_usd_estimate"]) < 1e-2
    assert "cloud SECURE" in (run_dir / "logs" / "timeline.txt").read_text()
    # the case killed by the budget stop was not charged an attempt: every case ran once
    phase = json.loads((run_dir / "run1" / "phase.json").read_text())
    assert all(x["attempts"] == 1 for x in phase["tasks"].values())
    assert all(not e["counted"] for x in phase["tasks"].values() for e in x.get("errors", []))
    assert json.loads((run_dir / "run1" / "merged" / "manifest.json").read_text())["case_ids"] == ids
    assert not left_running(run_dir)


def test_crash_is_not_scored_and_resumes(repo, run_dir):
    port, n = session.free_port(), 4
    ids = case_ids(n)
    p = subprocess.Popen(cmd(repo, run_dir, port, 12, n, "--dry-delay", "0.3"), cwd=repo,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for(lambda: len(usable_cases(run_dir, ids)) >= 1, 240, "the first case")
    wait_for(lambda: (run_dir / "run1" / "running.json").exists()
             and json.loads((run_dir / "run1" / "running.json").read_text() or "{}"), 60, "the next case")
    os.kill(p.pid, signal.SIGKILL)  # no teardown, no finalize
    p.wait()
    st = json.loads((run_dir / "session.json").read_text())
    orphan = list(json.loads((run_dir / "run1" / "running.json").read_text()).values())
    assert not (run_dir / "status.json").exists() and not (run_dir / "run1" / "merged").exists()
    assert session.pid_alive(st["relay_pid"]) and session.pid_alive(st["watchdog_pid"])
    done1 = usable_cases(run_dir, ids)
    t_done = {c: started(run_dir, c) for c in done1}

    p2 = run(repo, cmd(repo, run_dir, port, 12, n, "--resume"))
    assert p2.returncode == 0, p2.stdout[-3000:]
    log = p2.stdout
    assert "left by an earlier launch (relay.py)" in log and "reusing live pod" in log, log[-3000:]
    assert all(not session.pid_alive(x) or "run_systems.py" not in session.cmdline(x) for x in orphan)
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"], stt
    assert {c: started(run_dir, c) for c in done1} == t_done
    assert len(json.loads((run_dir / "ledger.json").read_text())["pods"]) == 1
    assert not session.pid_alive(st["relay_pid"]) and not left_running(run_dir)


def unique_l1_text(ids: list[str], target: str) -> str:
    """A line of the target case's L1 serialization that no other case of the run has."""
    tx = {}
    for c in ids:
        env = CaseEnv.from_case(REPO / DEV, c, page_size=RUN1["run"]["page_size"])
        tx[c] = serialize.serialize(env)
        env.close()
    others = "\n".join(v for k, v in tx.items() if k != target)
    return next(x for x in tx[target].splitlines() if len(x) > 15 and x not in others)


def test_exhausted_attempts_get_a_record_and_the_run_is_scored(repo, run_dir):
    port, n = session.free_port(), 4
    ids = case_ids(n)
    bad = ids[2]
    c = cmd(repo, run_dir, port, 12, n, "--dry-fail-text", unique_l1_text(ids, bad))
    p1 = run(repo, c)  # attempts 1 and 2 fail
    assert p1.returncode == 0, p1.stdout[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert not stt["scored"] and stt["missing"] == [bad] and stt["failed_attempts"][bad]["attempts"] == 2
    p2 = run(repo, c + ["--resume"])  # attempt 3 fails
    stt = json.loads((run_dir / "status.json").read_text())
    assert p2.returncode == 0 and not stt["scored"] and stt["failed_attempts"][bad]["attempts"] == 3
    p3 = run(repo, c + ["--resume"])  # attempt 4 fails: the runner writes the records
    assert p3.returncode == 0, p3.stdout[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"] and stt["missing"] == [], stt
    assert list(stt["exhausted"]) == [bad] and stt["exhausted"][bad]["attempts"] == 4
    assert "503" in stt["exhausted"][bad]["last_error"]
    merged = run_dir / "run1" / "merged"
    for s in RUN1["systems"]:
        r = next(json.loads(x) for x in (merged / f"{s}.jsonl").read_text().splitlines() if bad in x)
        assert r["output"] is None and r["schema_valid"] is False
        assert r["error"].startswith("attempts exhausted (4): ") and "503" in r["error"]
        assert r["exhausted"]["written_by"] == "gpu/queue_run.py" and len(r["exhausted"]["errors"]) == 4
        assert str(repo) not in json.dumps(r)
    man = json.loads((merged / "manifest.json").read_text())
    assert man["n_cases"] == n and not man["mismatch"]
    assert json.loads((merged / "score.json").read_text())["n_cases"] == n
    assert "attempts exhausted (4)" in (run_dir / "logs" / "timeline.txt").read_text()
    phase = json.loads((run_dir / "run1" / "phase.json").read_text())
    assert all(phase["tasks"][x]["attempts"] == 1 for x in ids if x != bad)  # the others ran once
    assert not left_running(run_dir)


def test_runner_fix_only_outside_the_harness(repo, run_dir):
    port, n = session.free_port(), 3
    ids = case_ids(n)
    stop_after_first_case(repo, run_dir, cmd(repo, run_dir, port, 12, n), ids)
    assert not json.loads((run_dir / "status.json").read_text())["scored"]
    first = git(repo, "rev-parse", "HEAD")
    done1 = usable_cases(run_dir, ids)
    (run_dir / "STOP").unlink()

    # a change to the harness: refused, with or without --runner-fix
    f = repo / "src" / "gapcheck" / "__init__.py"
    f.write_text(f.read_text() + "\n# changed after the first launch\n")
    git(repo, "commit", "-q", "-am", "harness change")
    p = run(repo, cmd(repo, run_dir, port, 12, n, "--resume"))
    assert p.returncode != 0 and "only with --runner-fix" in p.stdout + p.stderr
    p = run(repo, cmd(repo, run_dir, port, 12, n, "--resume", "--runner-fix"))
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "--runner-fix refused" in out and "src/gapcheck/__init__.py" in out, out[-2000:]
    assert not json.loads((run_dir / "status.json").read_text())["scored"]

    # only gpu/ changed: allowed, logged as a deviation, both commits merge
    git(repo, "reset", "-q", "--hard", first)
    g = repo / "gpu" / "README.md"
    g.write_text(g.read_text() + "\nA runner fix.\n")
    git(repo, "commit", "-q", "-am", "runner fix")
    fixed = git(repo, "rev-parse", "HEAD")
    p = run(repo, cmd(repo, run_dir, port, 12, n, "--resume", "--runner-fix"))
    assert p.returncode == 0, p.stdout[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"], stt
    assert stt["deviation"] == "runner fix" and stt["first_run_commit"] == first and stt["run_commit"] == fixed
    assert stt["runner_fixes"][0]["files"] == ["gpu/README.md"]
    man = json.loads((run_dir / "run1" / "merged" / "manifest.json").read_text())
    assert sorted(man["git_commits"]) == sorted([first, fixed]) and not man["mismatch"]
    assert "runner fix" in man["note"]
    tl = (run_dir / "logs" / "timeline.txt").read_text()
    assert "deviation: runner fix" in tl and first in tl and fixed in tl
    assert all(started(run_dir, c) for c in done1)
    assert not left_running(run_dir)


def test_rehearsal_is_dev_only_and_in_its_folder(repo):
    port = session.free_port()
    d = repo / "data" / "runs" / "rehearsal" / "r0"
    try:
        p = run(repo, cmd(repo, d, port, 12, 2, "--rehearsal"))
        assert p.returncode == 0, p.stdout[-3000:]
        stt = json.loads((d / "status.json").read_text())
        assert stt["scored"] and stt["rehearsal"] and stt["n_cases"] == 2
        assert "REHEARSAL" in json.loads((d / "run1" / "merged" / "manifest.json").read_text())["note"]
    finally:
        cleanup(d)
    elsewhere = repo / "data" / "runs" / "r1"
    p = run(repo, cmd(repo, elsewhere, port, 12, 2, "--rehearsal"))
    assert p.returncode != 0 and "data/runs/rehearsal/ only" in p.stdout
    m = repo / DEV / "manifest.json"
    m.write_text(m.read_text().replace('"split": "dev"', '"split": "test"'))
    p = run(repo, cmd(repo, repo / "data" / "runs" / "rehearsal" / "r2", port, 12, 2, "--rehearsal"))
    assert p.returncode != 0 and "development case set" in p.stdout


def test_real_run_refusals_before_any_network(repo, tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "home"))  # no API key file: nothing can reach RunPod
    port = session.free_port()
    d = repo / "data" / "runs" / "real"
    p = run(repo, cmd(repo, d, port, 45, 0, dry=False), env=env)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "--protocol-tag <tag> is required" in out and "no RunPod API key" in out
    assert not (d / "ledger.json").exists() and not (d / "account").exists()
    git(repo, "tag", "-a", "-m", "protocol", "v-test")
    p = run(repo, cmd(repo, d, port, 45, 0, "--protocol-tag", "v-test", dry=False), env=env)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "commits after the tag" not in out and "does not exist" not in out
    assert "split 'dev' != 'test'" in out and "is not in RUNS.md" in out  # the dev set is not the test set
    (repo / "RUNS.md").write_text((repo / "RUNS.md").read_text() + "\nentry\n")
    git(repo, "commit", "-q", "-am", "RUNS.md after the tag")
    p = run(repo, cmd(repo, d, port, 45, 0, "--protocol-tag", "v-test", dry=False), env=env)
    assert "commits after the tag" not in p.stdout + p.stderr
    f = repo / "scripts" / "score_runs.py"
    f.write_text(f.read_text() + "\n# changed after the tag\n")
    git(repo, "commit", "-q", "-am", "code after the tag")
    p = run(repo, cmd(repo, d, port, 45, 0, "--protocol-tag", "v-test", dry=False), env=env)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "commits after the tag v-test" in out and "scripts/score_runs.py" in out
    (repo / "untracked.txt").write_text("x")
    p = run(repo, cmd(repo, d, port, 45, 0, "--protocol-tag", "v-test", dry=False), env=env)
    assert "uncommitted changes" in p.stdout + p.stderr
    assert not (d / "ledger.json").exists()


def test_flags_are_checked(repo, run_dir):
    base = [sys.executable, str(repo / "gpu" / "session.py"), "--cases", DEV, "--cap", "1"]
    p = run(repo, base + ["--run-dir", "data/runs/dry", "--dry"])
    assert p.returncode != 0 and "--dry needs --go" in p.stderr
    p = run(repo, base + ["--run-dir", "/tmp/elsewhere", "--go", "--dry"])
    assert p.returncode != 0 and "not under data/runs" in p.stderr
    p = run(repo, base + ["--run-dir", "data/runs/dry", "--go", "--dry", "--cloud", "SECURE"])
    assert p.returncode != 0 and "needs --max-hourly" in p.stderr and not run_dir.exists()
    p = run(repo, base + ["--run-dir", "data/runs/dry", "--go", "--first-cases", "3"])
    assert p.returncode != 0 and "--first-cases needs --dry or --rehearsal" in p.stderr
