"""Offline tests of the GPU session's guards (no network, no RunPod, no model).

  watchdog (fake RunPod API): terminates our pod on each trigger and then exits: estimated
    spend >= cap, balance drop >= cap, pod deadline, hard deadline, stale client heartbeat;
    leaves a pod alone when nothing triggers; never touches a pod that is not ours
  pod_start.sh guards (functions sourced with POD_START_TEST=1, curl stubbed): the idle
    guard terminates after IDLE_SECONDS without server-log activity, not while active
  queue_run: complete(), usable() and the infrastructure-error rule, merge() mismatch
    detection, orphaned case processes killed on resume
  session: the run config is checked; configs/run1.json holds the frozen values; the tag
    rule and the runner-fix rule on a small git repository; parts at a runner-fix commit merge
  gpu_check --summary: one line, no account data, exit status by cloud
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GPU = REPO / "gpu"
sys.path.insert(0, str(GPU))
import fakerp  # noqa: E402
import queue_run  # noqa: E402
import session  # noqa: E402

PY = sys.executable

# name: (kwargs, expect_terminate)
SCENARIOS = {
    "est_spend": ({"created_ago": 2 * 3600, "budget": 3}, True),
    "balance_drop": ({"created_ago": 5, "speed": 7200, "budget": 3}, True),
    "pod_deadline": ({"ledger_extra": {"deadline_epoch": time.time() - 1}}, True),
    "hard_deadline": ({"hard_in": -1}, True),
    "client_heartbeat": ({"heartbeat_age": 1000}, True),
    "no_trigger": ({"other_pod": True}, False),
    "other_pod_kept": ({"created_ago": 2 * 3600, "budget": 3, "other_pod": True}, True),
}


def start_scenario(tmp: Path, name: str, *, created_ago=60.0, ledger_extra=None, speed=1.0, budget=12.0,
                   hard_in=3600.0, heartbeat_age=None, other_pod=False):
    d = tmp / name
    d.mkdir()
    state = d / "fake.json"
    fake = fakerp.Fake(str(state))
    s = json.loads(state.read_text())
    s.update(balance0=20.0, speed=speed)
    state.write_text(json.dumps(s))
    _, pod = fake.call("POST", "/v2/pods", {"name": "gapcheck-t-abc-1", "cloud": "COMMUNITY",
                                            "gpu": {"id": "G"}, "disk": 150})
    other = None
    if other_pod:
        _, other = fake.call("POST", "/v2/pods", {"name": "someone-else", "cloud": "COMMUNITY",
                                                  "gpu": {"id": "G"}, "disk": 0})
    s = json.loads(state.read_text())
    s["pods"][0]["created"] -= created_ago
    state.write_text(json.dumps(s))
    rec = {"id": pod["id"], "name": "gapcheck-t-abc-1", "created_epoch": s["pods"][0]["created"],
           "hourly": 1.7105, **(ledger_extra or {})}
    (d / "ledger.json").write_text(json.dumps({"pods": [rec]}))
    (d / "stop").write_text("x")  # exit as soon as no pod of ours remains
    hb = d / "client.hb"
    hb.write_text("x")
    if heartbeat_age is not None:
        os.utime(hb, (time.time() - heartbeat_age, time.time() - heartbeat_age))
    cmd = [PY, "-I", str(GPU / "watchdog.py"), "--ledger", str(d / "ledger.json"), "--prefix", "gapcheck-t-abc",
           "--hard-deadline", str(time.time() + hard_in), "--budget", str(budget), "--margin", "0.5",
           "--poll", "0.5", "--balance0", "20.0", "--log", str(d / "wd.log"), "--stop-file", str(d / "stop"),
           "--heartbeat", str(d / "wd.hb"), "--client-heartbeat", str(hb), "--client-stale", "600",
           "--dry-state", str(state)]
    return {"d": d, "state": state, "pod": pod, "other": other, "p": subprocess.Popen(cmd, cwd=str(GPU))}


def test_watchdog_triggers(tmp_path):
    runs = {n: start_scenario(tmp_path, n, **kw) for n, (kw, _) in SCENARIOS.items()}
    t0 = time.time()
    while time.time() - t0 < 10 and any(r["p"].poll() is None for n, r in runs.items() if SCENARIOS[n][1]):
        time.sleep(0.2)
    time.sleep(max(0.0, 3 - (time.time() - t0)))  # the no-trigger watchdog polls a few times
    bad = []
    for n, r in runs.items():
        alive = r["p"].poll() is None
        if alive:
            r["p"].terminate()
            r["p"].wait()
        log = (r["d"] / "wd.log").read_text()
        s = json.loads(r["state"].read_text())
        ours = next(x for x in s["pods"] if x["id"] == r["pod"]["id"])
        if SCENARIOS[n][1]:
            if not (ours["terminated"] and not alive and "TERMINATE" in log):
                bad.append((n, "not terminated", log[-300:]))
        elif ours["terminated"] or "TERMINATE" in log:
            bad.append((n, "terminated without a trigger", log[-300:]))
        if r["other"]:
            o = next(x for x in s["pods"] if x["id"] == r["other"]["id"])
            if o["terminated"]:
                bad.append((n, "terminated a pod that is not ours", ""))
    assert not bad, bad


def idle_guard(tmp: Path, active: bool) -> str:
    d = tmp / f"idle_{active}"
    (d / "logs").mkdir(parents=True)
    slog = d / "logs" / "server_np8_000000.log"
    slog.write_text("start\n")
    script = f"""
set -u
source {GPU / 'pod_start.sh'}
curl() {{ case "$*" in *health*) echo 200;; *) echo "$*" >> {d}/curl_calls; echo 204;; esac; }}
idle_guard
echo GUARD_RETURNED
"""
    env = dict(os.environ, POD_START_TEST="1", POD_DIR=str(d), IDLE_SECONDS="3", IDLE_POLL="0.5",
               SELF_TERMINATE_PAUSE="0", RUNPOD_POD_ID="podx", RUNPOD_API_KEY="podscoped")
    p = subprocess.Popen(["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while time.time() - t0 < (6 if active else 10) and p.poll() is None:
        if active:
            slog.write_text(f"launch_slot_ {time.time()}\n")
        time.sleep(0.3)
    if p.poll() is None:
        p.terminate()
        p.wait()
    out = p.stdout.read()
    calls = (d / "curl_calls").read_text() if (d / "curl_calls").exists() else ""
    return out + "\nCALLS:" + calls


def test_idle_guard_terminates_when_idle(tmp_path):
    out = idle_guard(tmp_path, active=False)
    assert "GUARD_RETURNED" in out and "-X DELETE" in out and "pods/podx" in out, out[-500:]
    assert "Bearer podscoped" in out  # the pod-scoped key only


def test_idle_guard_waits_while_active(tmp_path):
    out = idle_guard(tmp_path, active=True)
    assert "GUARD_RETURNED" not in out and "-X DELETE" not in out, out[-300:]


def write_case(root: Path, cid: str, commit="a", recs=None, finished=True):
    cd = root / "X" / "cases" / cid
    cd.mkdir(parents=True)
    (cd / "manifest.json").write_text(json.dumps({"case_ids": [cid], "started_utc": "t", "git": {"commit": commit},
                                                  **({"finished_utc": "t"} if finished else {})}))
    for s, r in (recs or {"A1": {}}).items():
        (cd / f"{s}.jsonl").write_text(json.dumps({"case_id": cid, **r}) + "\n")
    return cd


def test_complete_and_merge(tmp_path):
    root = tmp_path / "data" / "runs" / "s"
    write_case(root, "c1", "a")
    write_case(root, "c2", "b")
    c3 = write_case(root, "c3", finished=False)
    assert queue_run.complete(root / "X" / "cases" / "c1", ["A1"], "c1")
    assert not queue_run.complete(root / "X" / "cases" / "c1", ["A1", "A2"], "c1")
    assert not queue_run.complete(c3, ["A1"], "c3")
    m = queue_run.merge(root, "X", ["A1"], ["c1", "c2", "c3"], "t")
    assert "git" in m["mismatch"] and m["complete"] == 2


def err(status: int, body: str) -> dict:
    msg = f"HTTP {status}: {body}"
    return {"output": None, "error": f"backend: {msg}", "trace": [{"type": "error", "error": msg}]}


def test_infrastructure_errors_make_a_case_unusable(tmp_path):
    root = tmp_path / "data" / "runs" / "s"
    ok400 = '{"error": {"code": 400, "message": "request exceeds the available context size"}}'
    cases = {
        "valid": ({"L1": {"output": {"status": "GAP"}, "error": None, "trace": []}}, True),
        "schema_invalid": ({"L1": {"output": None, "error": "schema: bad", "trace": []}}, True),
        "server_400": ({"L1": err(400, ok400)}, True),
        "proxy_400_html": ({"L1": err(400, "<html>Bad Request</html>")}, False),
        "token_401": ({"L1": err(401, '{"error": {"code": 401}}')}, False),
        "proxy_404": ({"L1": err(404, "")}, False),
        "repair_429": ({"L1": {"output": {"status": "GAP"}, "trace": [
            {"type": "error", "error": "HTTP 429: slow down"}]}}, False),
    }
    for cid, (recs, want) in cases.items():
        d = write_case(root, cid, recs=recs)
        assert queue_run.complete(d, ["L1"], cid)
        assert queue_run.usable(d, ["L1"], cid) is want, (cid, queue_run.infra_errors(d, ["L1"], cid))
    assert queue_run.infra_errors(root / "X" / "cases" / "token_401", ["L1"], "token_401") == ["L1: HTTP 401"]
    m = queue_run.merge(root, "X", ["L1"], list(cases), "t")
    assert m["complete"] == sum(w for _, w in cases.values())


def test_orphaned_case_process_is_killed(tmp_path):
    sess = tmp_path / "data" / "runs" / "s"
    (sess / "X").mkdir(parents=True)
    fake = tmp_path / "run_systems.py"  # a stand-in whose command line looks like a case process
    fake.write_text("import time\ntime.sleep(60)\n")
    p = subprocess.Popen([PY, str(fake), "--case-ids", "case_1"])
    other = subprocess.Popen([PY, "-c", "import time; time.sleep(60)", "case_2"])
    try:
        (sess / "X" / "running.json").write_text(json.dumps({"case_1": p.pid, "case_2": other.pid}))
        killed = queue_run.kill_orphans(sess, "X", lambda m: None)
        assert killed == [p.pid] and p.wait(5) != 0
        assert other.poll() is None  # not a case process: left alone
        assert not (sess / "X" / "running.json").exists()
    finally:
        for x in (p, other):
            if x.poll() is None:
                x.kill()
                x.wait()


def test_run1_config_holds_the_frozen_values():
    c = session.load_config(REPO / "configs" / "run1.json")
    assert c["systems"] == ["L1", "A1", "A2"] and c["case_split"] == "test"
    assert c["run"] == {"max_steps": 8, "round_counter": True, "rounds_left_notice": 0, "prompt_set": "default",
                        "repair_steps": 8, "l1_tool_rounds": 2, "page_size": 20, "context_limit": 45056,
                        "chars_per_token": 2.26}
    assert c["decoding"] == {"temperature": 0, "seed": 1, "max_tokens": 4096,
                             "extra": {"chat_template_kwargs": {"enable_thinking": False},
                                       "parallel_tool_calls": True}}
    assert c["client"]["concurrency"] == 1 and c["server"]["slots"] == 8 and c["server"]["ctx_per_slot"] == 49152
    assert c["client"]["max_attempts"] == 2 and c["client"]["max_total_attempts"] == 4
    assert c["gpu"]["cloud"] == "COMMUNITY"
    assert [f["sha256"][:8] for f in c["model"]["files"]] == ["44481862", "3f342f1c", "56758f40", "753bda48"]
    assert c["model"]["revision"] == "766911a6b7369840a91dbcd95f9f997acaab6cd6"
    assert c["image"].endswith("@sha256:ae96e52f8b2106ea82bad786ee2d08bb9b0a8c93403df9c27ae1bf065a118089")
    # every harness setting is given, so no library default enters run 1
    from dataclasses import asdict
    from gapcheck import systems
    assert set(c["run"]) == set(asdict(systems.Settings())) == set(session.SETTINGS_FIELDS)
    assert asdict(systems.Settings.from_dict(c["run"])) == c["run"]


@pytest.mark.parametrize("change, msg", [
    (lambda c: c["run"].pop("max_steps"), "every harness setting"),
    (lambda c: c["run"].update(extra=1), "every harness setting"),
    (lambda c: c["client"].update(concurrency=9), "client concurrency"),
    (lambda c: c["run"].update(context_limit=46000), "fit ctx_per_slot"),
    (lambda c: c["client"].update(accept_http_4xx=[500]), "4xx statuses only"),
    (lambda c: c["model"]["files"][0].pop("sha256"), "model.files"),
    (lambda c: c["client"].update(max_total_attempts=1), "max_total_attempts >= max_attempts"),
])
def test_bad_run_config_is_refused(tmp_path, change, msg):
    c = json.loads((REPO / "configs" / "run1.json").read_text())
    change(c)
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(c))
    with pytest.raises(SystemExit, match=msg):
        session.load_config(p)


def test_pod_start_has_no_template_payload():
    """The chat template is extracted on the pod, never shipped in the request."""
    text = (GPU / "pod_start.sh").read_text() + (GPU / "session.py").read_text()
    assert "CT_B64" not in text and "--chat-template-file $D/chat-template.jinja" in text
    assert shutil.which("bash")


GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@localhost"]


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for f, text in files.items():
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(text)
    subprocess.run(["git", *GIT_ID, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *GIT_ID, "commit", "-q", "-m", msg], cwd=repo, check=True)
    return session.git("rev-parse", "HEAD", cwd=repo)


def test_tag_and_runner_fix_rules(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    base = commit(r, {"src/gapcheck/a.py": "1", "gpu/session.py": "1", "RUNS.md": "", "docs/x.md": ""}, "base")
    subprocess.run(["git", *GIT_ID, "tag", "-a", "-m", "t", "v1"], cwd=r, check=True)
    assert session.tag_check("v1", base, cwd=r) == (base, [])
    assert session.tag_check("nope", base, cwd=r)[1]
    c1 = commit(r, {"RUNS.md": "test set sha256", "results/a.json": "{}", "DEVIATIONS.md": "None"}, "after tag")
    assert session.tag_check("v1", c1, cwd=r) == (base, [])
    c2 = commit(r, {"src/gapcheck/a.py": "2"}, "harness")
    c3 = commit(r, {"src/gapcheck/a.py": "1"}, "reverted")  # same tree as c1, but a commit touched src
    for c in (c2, c3):
        tc, probs = session.tag_check("v1", c, cwd=r)
        assert probs and "src/gapcheck/a.py" in probs[0]
    # runner fix: gpu/ and docs/ only
    subprocess.run(["git", "reset", "-q", "--hard", c1], cwd=r, check=True)
    f1 = commit(r, {"gpu/session.py": "2", "docs/x.md": "fix"}, "runner fix")
    assert session.runner_fix_check(c1, f1, cwd=r) == (["docs/x.md", "gpu/session.py"], [])
    for files in ({"src/gapcheck/a.py": "3"}, {"configs/run1.json": "{}"}, {"scripts/s.py": "x"},
                  {"tests/gpu/t.py": "x"}):
        subprocess.run(["git", "reset", "-q", "--hard", f1], cwd=r, check=True)
        bad = commit(r, files, "not a runner fix")
        assert session.runner_fix_check(c1, bad, cwd=r)[1] == list(files)
    # not a descendant
    subprocess.run(["git", "checkout", "-q", "--orphan", "other"], cwd=r, check=True)
    lone = commit(r, {"x": "1"}, "elsewhere")
    assert session.runner_fix_check(c1, lone, cwd=r)[1]


def test_merge_accepts_runner_fix_commits_only_through_git_equiv(tmp_path):
    root = tmp_path / "data" / "runs" / "s"
    write_case(root, "c1", "a")
    write_case(root, "c2", "b")
    m = queue_run.merge(root, "X", ["A1"], ["c1", "c2"], "t")
    assert "git" in m["mismatch"]
    m = queue_run.merge(root, "X", ["A1"], ["c1", "c2"], "t", git_equiv=lambda g0, g: True)
    assert not m["mismatch"]
    man = json.loads((root / "X" / "merged" / "manifest.json").read_text())
    assert man["git_commits"] == ["a", "b"]
    m = queue_run.merge(root, "X", ["A1"], ["c1", "c2"], "t", git_equiv=lambda g0, g: g0 == g)
    assert "git" in m["mismatch"]


def test_gpu_check_summary_has_no_account_data(tmp_path):
    state = tmp_path / "fake.json"
    fakerp.Fake(str(state))
    for cloud, avail, rc in (("COMMUNITY", "LOW", 0), ("COMMUNITY", "NONE", 3), ("SECURE", "NONE", 0)):
        s = json.loads(state.read_text())
        s["availability"] = avail  # the fake's Community stock; Secure stays MEDIUM
        state.write_text(json.dumps(s))
        p = subprocess.run([PY, "-I", str(GPU / "gpu_check.py"), "--summary", "--cloud", cloud,
                            "--dry-state", str(state)], capture_output=True, text=True)
        assert p.returncode == rc, (cloud, avail, p.stdout)
        line = p.stdout.strip()
        assert "community" in line and "secure" in line and "balance" not in line and "pods" not in line
