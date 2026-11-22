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
  exhausted attempts    -> one case's requests always get HTTP 503: not scored after its 2nd
                           and 3rd attempts; at the 4th the runner writes its records (no valid
                           output) and the run is scored
The budget-stop resume moves to Secure Cloud (--cloud SECURE), which the ledger and
status.json record per pod; scoring gets the billed GPU hours of every pod.

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
from gapcheck import serialize  # noqa: E402
from gapcheck.tools import CaseEnv  # noqa: E402

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
    # cost: billed GPU hours of the pod (creation to termination), not the queue wall time
    led0 = json.loads((run_dir / "ledger.json").read_text())["pods"][0]
    billed_h = (led0["terminated_epoch"] - led0["created_epoch"]) / 3600
    assert abs(stt["billed_gpu_hours"] - billed_h) < 1e-3 and stt["queue_wall_hours"] < stt["billed_gpu_hours"]
    assert abs(score["cost"]["gpu_hours"] - stt["billed_gpu_hours"]) < 1e-3
    assert stt["pods"][0]["cloud"] == "COMMUNITY" and stt["clouds"] == ["COMMUNITY"]
    assert stt["exhausted"] == {}
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

    # resumed on Secure Cloud (same GPU and image), with its own price ceiling
    p3 = run(cmd(run_dir, port, 12, n, "--resume", "--cloud", "SECURE", "--max-hourly", "3"))
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


def unique_l1_text(ids: list[str], target: str) -> str:
    """A line of the target case's L1 serialization that no other case of the run has."""
    tx = {}
    for c in ids:
        env = CaseEnv.from_case(REPO / DEV, c, page_size=RUN1["run"]["page_size"])
        tx[c] = serialize.serialize(env)
        env.close()
    others = "\n".join(v for k, v in tx.items() if k != target)
    return next(x for x in tx[target].splitlines() if len(x) > 15 and x not in others)


def test_exhausted_attempts_get_a_record_and_the_run_is_scored(run_dir):
    port, n = session.free_port(), 4
    ids = case_ids(n)
    bad = ids[2]
    c = cmd(run_dir, port, 12, n, "--dry-fail-text", unique_l1_text(ids, bad))
    p1 = run(c)  # attempts 1 and 2 fail
    assert p1.returncode == 0, p1.stdout[-3000:]
    stt = json.loads((run_dir / "status.json").read_text())
    assert not stt["scored"] and stt["missing"] == [bad] and stt["failed_attempts"][bad]["attempts"] == 2
    p2 = run(c + ["--resume"])  # attempt 3 fails
    stt = json.loads((run_dir / "status.json").read_text())
    assert p2.returncode == 0 and not stt["scored"] and stt["failed_attempts"][bad]["attempts"] == 3
    p3 = run(c + ["--resume"])  # attempt 4 fails: the runner writes the records
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
    man = json.loads((merged / "manifest.json").read_text())
    assert man["n_cases"] == n and not man["mismatch"]
    score = json.loads((merged / "score.json").read_text())
    assert score["n_cases"] == n
    assert "attempts exhausted (4)" in (run_dir / "logs" / "timeline.txt").read_text()
    # the other cases ran once each
    phase = json.loads((run_dir / "run1" / "phase.json").read_text())
    assert all(phase["tasks"][x]["attempts"] == 1 for x in ids if x != bad)
    assert not nothing_left_running(run_dir)


def test_plan_and_dry_flags_are_checked(run_dir):
    p = run([sys.executable, str(GPU / "session.py"), "--cases", DEV, "--run-dir", str(run_dir), "--cap", "1",
             "--dry"])
    assert p.returncode != 0 and "--dry needs --go" in p.stderr
    p = run([sys.executable, str(GPU / "session.py"), "--cases", DEV, "--run-dir", "/tmp/elsewhere", "--cap", "1",
             "--go", "--dry"])
    assert p.returncode != 0 and "not under data/runs" in p.stderr
    p = run([sys.executable, str(GPU / "session.py"), "--cases", DEV, "--run-dir", str(run_dir), "--cap", "1",
             "--go", "--dry", "--cloud", "SECURE"])
    assert p.returncode != 0 and "needs --max-hourly" in p.stderr and not run_dir.exists()
