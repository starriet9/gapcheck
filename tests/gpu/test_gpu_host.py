"""Offline tests of the host checks (no network, no RunPod, no GPU).

  pod_start.sh (functions sourced with POD_START_TEST=1, nvidia-smi stubbed): gpu_busy fails a
    host whose GPU is busy before use in every sample, not one that is idle in any sample;
    dl_too_slow fails a slow download after DL_CHECK_S, never before, and at DL_MAX_S
  wait_launch.sh (uv stubbed): after a host problem it launches a new pod with --resume, up to
    HOST_RETRIES times; any other end of a launch that created a pod ends the waiter; a launch
    that created no pod keeps it waiting
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GPU = REPO / "gpu"


def sourced(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = f"set -u\nsource {GPU / 'pod_start.sh'}\n{body}\n"
    e = dict(os.environ, POD_START_TEST="1", POD_DIR="/nonexistent", **(env or {}))
    return subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True, timeout=30)


def busy(tmp: Path, samples: list[str]) -> subprocess.CompletedProcess:
    """gpu_busy with nvidia-smi giving the samples in turn (a counter file: it runs in a subshell)."""
    lines = "\n".join(f"{i}) echo '{x}';;" for i, x in enumerate(samples, 1))
    counter = tmp / f"n_{len(list(tmp.iterdir()))}"
    body = f"""
nvidia-smi() {{ n=$(( $(cat {counter} 2>/dev/null || echo 0) + 1 )); echo $n > {counter}; case $n in
{lines}
esac; }}
why=$(gpu_busy) && echo "BUSY $why" || echo IDLE
"""
    return sourced(body, {"GPU_CHECK_SAMPLES": str(len(samples)), "GPU_CHECK_PAUSE": "0"})


def test_gpu_busy_in_every_sample_fails_the_host(tmp_path):
    r = busy(tmp_path, ["100, 0", "100, 0", "99, 0"])
    assert r.stdout.startswith("BUSY GPU busy before use"), r.stdout + r.stderr
    assert "100%/0MiB" in r.stdout


def test_gpu_idle_in_one_sample_or_in_use_passes(tmp_path):
    assert busy(tmp_path, ["100, 0", "0, 0", "100, 0"]).stdout.strip() == "IDLE"
    assert busy(tmp_path, ["100, 2048", "100, 2048", "100, 2048"]).stdout.strip() == "IDLE"  # memory in use
    assert busy(tmp_path, ["", "", ""]).stdout.strip() == "IDLE"  # no reading: not a reason to refuse


def slow(got: int, total: int, el: int) -> str:
    r = sourced(f'why=$(dl_too_slow {got} {total} {el}) && echo "SLOW $why" || echo OK',
                {"DL_CHECK_S": "600", "DL_MAX_S": "2400"})
    return r.stdout.strip()


def test_download_speed_rule():
    total = 111_000_000_000
    assert slow(0, total, 599) == "OK"  # before the check time
    assert slow(0, total, 600) == "SLOW download too slow: 0 bytes after 600s"
    assert slow(total // 3, total, 600) == "OK"  # 1800 s projected
    assert slow(total // 5, total, 600).startswith("SLOW")  # 3000 s projected
    out = sourced(f"dl_too_slow {total // 5} {total} 600", {"DL_CHECK_S": "600", "DL_MAX_S": "2400"}).stdout
    assert "projected 3000s > 2400s" in out and "37 MB/s" in out, out
    assert slow(total - 1, total, 2400).startswith("SLOW")  # limit reached
    assert slow(total, total, 9999) == "OK"  # done


def waiter(tmp: Path, statuses: list[str], retries: int = 2, pods_per_launch: list[int] | None = None) -> tuple[str, list[str]]:
    root = tmp / "repo"
    (root / "gpu").mkdir(parents=True)
    shutil.copy(GPU / "wait_launch.sh", root / "gpu" / "wait_launch.sh")
    run = root / "data" / "runs" / "w"
    bin_ = tmp / "bin"
    bin_.mkdir()
    (tmp / "statuses.json").write_text(json.dumps(statuses))
    (tmp / "pods.json").write_text(json.dumps(pods_per_launch or [1] * len(statuses)))
    fake = f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
a = sys.argv[1:]
if "gpu/gpu_check.py" in a:
    print("community HIGH"); sys.exit(0)
run = Path({str(run)!r}); tmp = Path({str(tmp)!r})
calls = tmp / "calls.txt"
n = len(calls.read_text().splitlines()) if calls.exists() else 0
with calls.open("a") as f: f.write(" ".join(a) + "\\n")
sts = json.loads((tmp / "statuses.json").read_text()); add = json.loads((tmp / "pods.json").read_text())
led = run / "ledger.json"
pods = json.loads(led.read_text())["pods"] if led.exists() else []
pods += [{{"id": f"p{{len(pods) + i}}"}} for i in range(add[min(n, len(add) - 1)])]
led.write_text(json.dumps({{"pods": pods}}))
(run / "session.json").write_text(json.dumps({{"status": sts[min(n, len(sts) - 1)]}}))
"""
    (bin_ / "uv").write_text(fake)
    (bin_ / "uv").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", CASES="data/cases/x", RUN_DIR="data/runs/w",
               CAP="3", POLL="0", HOURS="1", HOST_RETRIES=str(retries))
    subprocess.run(["bash", str(root / "gpu" / "wait_launch.sh")], env=env, cwd=root, timeout=60, check=True)
    calls = (tmp / "calls.txt").read_text().splitlines()
    return (run / "wait.log").read_text(), calls


def test_waiter_retries_a_new_pod_after_a_host_problem(tmp_path):
    log, calls = waiter(tmp_path, ["stopped: pod refused to start the server: host: GPU busy before use (x)",
                                   "stopped: server not healthy after 3000 s", "done"])
    assert len(calls) == 3 and "--resume" not in calls[0] and all("--resume" in c for c in calls[1:])
    assert "retry 1 of 2" in log and "retry 2 of 2" in log and log.rstrip().endswith("waiter done")


def test_waiter_stops_after_host_retries_and_on_other_ends(tmp_path):
    host = "stopped: server not healthy after 3000 s"
    log, calls = waiter(tmp_path / "a", [host, host, host, "done"], retries=2)
    assert len(calls) == 3 and "waiter done" in log
    log, calls = waiter(tmp_path / "b", ["stopped: pod refused to start the server: chat template sha256 x != y"])
    assert len(calls) == 1 and "retry" not in log  # not a host problem: no new pod
    log, calls = waiter(tmp_path / "c", ["stopped: budget (cap reached; resume with a larger --cap)"])
    assert len(calls) == 1


def test_waiter_keeps_waiting_when_no_pod_was_created(tmp_path):
    log, calls = waiter(tmp_path, ["stopped: GPU not available on COMMUNITY", "done"], pods_per_launch=[0, 1])
    assert len(calls) == 2 and "--resume" in calls[1] and "waiter done" in log
