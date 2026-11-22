"""Dry runs of gpu/session.py in run-1 mode (configs/run1.json) against the fake RunPod API
(gpu/fakerp.py) and the fake llama-server (gpu/fake_server.py): the same code path as a real
run, with no network, no GPU and no model.

  complete              -> every case usable, R0/R1 added, merged and scored
  budget stop           -> the watchdog terminates the pod mid-run (balance drop >= cap);
                           nothing is merged or scored, status.json lists the missing cases;
                           a resume with a larger cap runs only those and then scores
  crash (SIGKILL)       -> no teardown, no status, no score; the resume stops the relay and
                           fake server the dead session left, kills its case process, reuses
                           the live pod and the watchdog, and then scores

They need the development case set (built by `make cases`); without it they are skipped.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GPU = REPO / "gpu"
sys.path.insert(0, str(GPU))
import queue_run  # noqa: E402
import session  # noqa: E402

DEV = "data/cases/dev_s20261120_utc_n10"
RUN1 = json.loads((REPO / "configs" / "run1.json").read_text())
pytestmark = pytest.mark.skipif(not (REPO / DEV / "manifest.json").exists(),
                                reason="needs the development case set (make cases)")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "data" / "runs" / "dry"
    yield d
    # leave nothing running and no worktree registered, whatever happened
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
        if pid and str(d) in session.cmdline(pid) or (pid and "run_systems.py" in session.cmdline(pid)):
            os.kill(int(pid), signal.SIGKILL)
    if (d / "wt").exists():  # only this test's own worktree
        subprocess.run(["git", "worktree", "remove", "--force", str(d / "wt")], cwd=REPO, capture_output=True)


def cmd(d: Path, port: int, cap: float, n: int, *extra: str) -> list[str]:
    return [sys.executable, str(GPU / "session.py"), "--cases", DEV, "--run-dir", str(d), "--cap", str(cap),
            "--go", "--dry", "--dry-cases", str(n), "--relay-port", str(port), *extra]


def run(c: list[str], timeout: float = 420) -> subprocess.CompletedProcess:
    p = subprocess.run(c, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    return p


def usable_cases(d: Path, n_ids: list[str]) -> list[str]:
    return [c for c in n_ids if queue_run.usable(queue_run.case_dir(d, "run1", c), RUN1["systems"], c)]


def case_ids(n: int) -> list[str]:
    lines = (REPO / DEV / "cases.jsonl").read_text().splitlines()
    return [json.loads(x)["case_id"] for x in lines if x.strip()][:n]


def started(d: Path, cid: str) -> str:
    return json.loads((queue_run.case_dir(d, "run1", cid) / "manifest.json").read_text())["started_utc"]


def nothing_left_running(d: Path) -> list:
    st = json.loads((d / "session.json").read_text())
    return [(k, st[k]) for k in ("watchdog_pid", "relay_pid", "fake_pid") if st.get(k) and session.pid_alive(st[k])]


def wait_for(cond, timeout: float, what: str):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def test_complete_run_is_scored(run_dir):
    port = session.free_port()
    p = run(cmd(run_dir, port, 12, 4))
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
    assert score["cost"] is not None
    st = json.loads((run_dir / "session.json").read_text())
    assert st["status"] == "done" and st["scored"]
    led = json.loads((run_dir / "ledger.json").read_text())
    assert len(led["pods"]) == 1 and led["pods"][0]["terminated_epoch"]
    created = json.loads((run_dir / "logs" / "create_pod_1.json").read_text())
    assert created["env"]["LLAMA_API_KEY"].startswith("<") and "CT_B64" not in created["env"]
    assert created["env"]["TEMPLATE_SHA256"] == RUN1["model"]["chat_template"]["sha256"]
    assert created["env"]["NP"] == "8" and created["image"] == RUN1["image"]
    assert not (run_dir / "secret" / "llama_api_key").exists()
    assert not nothing_left_running(run_dir)
    # one request at a time: the fake server never saw two chat requests overlap
    phase = json.loads((run_dir / "run1" / "phase.json").read_text())
    spans = sorted((t["start"], t["end"]) for t in phase["tasks"].values())
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_budget_stop_is_not_scored_and_resumes(run_dir):
    port, n = session.free_port(), 8
    ids = case_ids(n)
    p = subprocess.Popen(cmd(run_dir, port, 5, n, "--dry-delay", "0.1"), cwd=REPO,
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
    assert "TERMINATE" in (run_dir / "logs" / "watchdog.log").read_text()
    t_done = {c: started(run_dir, c) for c in done1}

    # the same cap again: refused before any pod, still not scored
    p2 = run(cmd(run_dir, port, 5, n, "--resume"))
    assert p2.returncode == 0
    assert not json.loads((run_dir / "status.json").read_text())["scored"]
    assert len(json.loads((run_dir / "ledger.json").read_text())["pods"]) == 1

    p3 = run(cmd(run_dir, port, 12, n, "--resume"))
    assert p3.returncode == 0, p3.stdout[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"], stt
    assert {c: started(run_dir, c) for c in done1} == t_done  # finished cases were not run again
    assert len(json.loads((run_dir / "ledger.json").read_text())["pods"]) == 2
    assert json.loads((run_dir / "run1" / "merged" / "manifest.json").read_text())["case_ids"] == ids
    assert not nothing_left_running(run_dir)


def test_crash_is_not_scored_and_resumes(run_dir):
    port, n = session.free_port(), 4
    ids = case_ids(n)
    p = subprocess.Popen(cmd(run_dir, port, 12, n, "--dry-delay", "0.3"), cwd=REPO,
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

    p2 = run(cmd(run_dir, port, 12, n, "--resume"))
    assert p2.returncode == 0, p2.stdout[-3000:]
    log = p2.stdout
    assert "left by an earlier launch (relay.py)" in log and "reusing live pod" in log, log[-3000:]
    assert all(not session.pid_alive(x) or "run_systems.py" not in session.cmdline(x) for x in orphan)
    stt = json.loads((run_dir / "status.json").read_text())
    assert stt["complete"] and stt["scored"], stt
    assert {c: started(run_dir, c) for c in done1} == t_done
    assert len(json.loads((run_dir / "ledger.json").read_text())["pods"]) == 1
    assert not session.pid_alive(st["relay_pid"]) and not nothing_left_running(run_dir)


def test_plan_and_dry_flags_are_checked(run_dir):
    p = run([sys.executable, str(GPU / "session.py"), "--cases", DEV, "--run-dir", str(run_dir), "--cap", "1",
             "--dry"])
    assert p.returncode != 0 and "--dry needs --go" in p.stderr
    p = run([sys.executable, str(GPU / "session.py"), "--cases", DEV, "--run-dir", "/tmp/elsewhere", "--cap", "1",
             "--go", "--dry"])
    assert p.returncode != 0 and "not under data/runs" in p.stderr
