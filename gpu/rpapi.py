"""Minimal RunPod REST v2 client (stdlib). The API key is read from
~/.config/runpod/api_key, or from the file named by GAPCHECK_RUNPOD_KEY_FILE (another RunPod
account), inside the process and never printed, logged or put in argv.

Dry mode: `use_fake(state_path)` switches every function in this module to an offline
fake (fakerp.py) that keeps pods and balance in a JSON file. Nothing is sent to RunPod.
"""
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.runpod.io"
KEY_ENV = "GAPCHECK_RUNPOD_KEY_FILE"  # runner fix 2026-11-26: the key file of another account
KEYF = os.path.expanduser(os.environ.get(KEY_ENV) or "~/.config/runpod/api_key")
# a relative path would name a different file in each process's working folder (the watchdog runs in gpu/)
KEYF_OK = os.path.isabs(KEYF)
UA = "gapcheck/1"
_FAKE = None


def use_fake(state_path: str) -> None:
    global _FAKE
    import fakerp
    _FAKE = fakerp.Fake(state_path)


def is_fake() -> bool:
    return _FAKE is not None


def _key() -> str:
    if not KEYF_OK:
        raise ValueError(f"{KEY_ENV} must be an absolute path or start with ~/")
    with open(KEYF) as f:
        return f.read().strip()


def key_file_shown() -> str:
    """The key file's path for messages and logs, with the home folder as ~ (never the key)."""
    home = os.path.expanduser("~")
    return "~" + KEYF[len(home):] if KEYF.startswith(home + os.sep) else KEYF


def key_present() -> bool:
    if not KEYF_OK:
        return False
    try:
        return os.path.getsize(KEYF) > 0
    except OSError:
        return False


def call(method: str, path: str, body=None, timeout: float = 60):
    """Returns (status, parsed_json_or_text). Never raises on HTTP errors."""
    if _FAKE is not None:
        return _FAKE.call(method, path, body)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _key())
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            raw = e.read().decode(errors="replace")
        except Exception:
            raw = ""
    try:
        return code, (json.loads(raw) if raw.strip() else None)
    except json.JSONDecodeError:
        return code, raw[:2000]


def list_pods():
    code, d = call("GET", "/v2/pods")
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"list pods failed: {code} {str(d)[:300]}")
    return d.get("pods") or []


def balance():
    code, d = call("GET", "/v2/billing/balance")
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"balance failed: {code} {str(d)[:300]}")
    return float(d["balanceAmount"])


def terminate(pod_id: str):
    """DELETE, falling back to the terminate action. Returns list of (how, status)."""
    out = []
    code, _ = call("DELETE", f"/v2/pods/{pod_id}")
    out.append(("DELETE", code))
    if code not in (200, 202, 204, 404):
        code2, _ = call("POST", f"/v2/pods/{pod_id}/action", {"action": "terminate"})
        out.append(("action.terminate", code2))
    return out


def get_pod(pod_id: str):
    return call("GET", f"/v2/pods/{pod_id}")


def exec_cmd(pod_id: str, command: list, timeout_s: int = 20):
    return call("POST", f"/v2/pods/{pod_id}/exec",
                {"command": command, "timeoutSeconds": timeout_s}, timeout=timeout_s + 40)


def gpu_catalog(cloud: str):
    """GPU types with availability and prices for one cloud (COMMUNITY or SECURE)."""
    code, d = call("GET", f"/v2/catalog/gpus?include=AVAILABILITY&product=POD&cloud={cloud}")
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"gpu catalog failed: {code} {str(d)[:300]}")
    return d.get("gpus") or []


def iso_to_epoch(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


if __name__ == "__main__":
    print(json.dumps({"t": time.time(), "pods": [
        {k: p.get(k) for k in ("id", "name", "status", "cost", "createdAt")} for p in list_pods()],
        "balance": balance()}, indent=1))
