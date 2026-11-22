"""Offline stand-in for the RunPod REST API (dry runs of gpu/session.py). No network.

State is one JSON file:
  {"balance0": 20.0, "speed": 1.0, "availability": "LOW", "price": 1.69, "secure_price": 2.69,
   "upstream": "http://127.0.0.1:PORT", "pods": [{id, name, cost, created, terminated}]}
Spend is computed, as on RunPod, from each pod's lifetime: hourly cost x seconds x speed.
`speed` makes a dry second count as `speed` seconds (watchdog budget tests).
Pods are "reachable" at `upstream` (a local fake llama-server) instead of a proxy URL.
Optional "pod_fatal": a message the pod's fatal-error file holds (gpu/pod_start.sh writes
one when it refuses to start the server), returned to the session's exec check.
"""
import json
import os
import time
import uuid
from pathlib import Path

DISK_RATE = 0.10 / 730.0


class Fake:
    def __init__(self, path: str):
        self.p = Path(path)
        if not self.p.exists():
            self._save({"balance0": 20.0, "speed": 1.0, "availability": "LOW", "price": 1.69,
                        "upstream": None, "pods": []})

    # ------------------------------------------------------------ state
    def _load(self) -> dict:
        for _ in range(20):
            try:
                return json.loads(self.p.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                time.sleep(0.05)
        return json.loads(self.p.read_text())

    def _save(self, s: dict) -> None:
        tmp = self.p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(s, indent=1))
        os.replace(tmp, self.p)

    def _spent(self, s: dict, now: float) -> float:
        tot = 0.0
        for p in s["pods"]:
            end = p.get("terminated") or now
            tot += p["hourly"] * max(end - p["created"], 0) * s.get("speed", 1.0) / 3600
        return tot

    @staticmethod
    def _iso(t: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"

    def _view(self, p: dict) -> dict:
        return {"id": p["id"], "name": p["name"], "status": "RUNNING", "cost": p["cost"],
                "disk": p["disk"], "createdAt": self._iso(p["created"]), "cloud": p["cloud"],
                "gpu": {"id": p["gpu"], "count": 1}, "image": p["image"], "ports": p["ports"],
                "fake_upstream": p.get("upstream")}

    # ------------------------------------------------------------ API
    def call(self, method: str, path: str, body=None):
        s = self._load()
        now = time.time()
        if method == "GET" and path == "/v2/pods":
            return 200, {"pods": [self._view(p) for p in s["pods"] if not p.get("terminated")]}
        if method == "GET" and path == "/v2/billing/balance":
            return 200, {"balanceAmount": s["balance0"] - self._spent(s, now)}
        if method == "GET" and path.startswith("/v2/catalog/gpus"):
            cloud = "community" if "cloud=COMMUNITY" in path else "secure"
            avail = s.get("availability", "LOW") if cloud == "community" else "MEDIUM"
            price = s.get("price", 1.69) if cloud == "community" else s.get("secure_price", 2.69)
            return 200, {"gpus": [{"id": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
                                   "availability": avail, "memory": 96,
                                   "price": {cloud: price}, "maxCount": {cloud: 9 if avail != "NONE" else 0},
                                   "cudaVersions": [{"available": avail != "NONE", "version": "12.8"}]}]}
        if method == "POST" and path == "/v2/pods":
            if s.get("availability", "LOW") == "NONE" and body.get("cloud") == "COMMUNITY":
                return 500, {"error": "There are no longer any instances available with the requested specifications."}
            pid = "dry" + uuid.uuid4().hex[:11]
            price = s.get("price", 1.69) if body.get("cloud") == "COMMUNITY" else s.get("secure_price", 2.69)
            rec = {"id": pid, "name": body["name"], "cost": price, "disk": body.get("disk", 0),
                   "hourly": price + body.get("disk", 0) * DISK_RATE, "created": now,
                   "terminated": None, "cloud": body.get("cloud"), "gpu": body["gpu"]["id"],
                   "image": body.get("image"), "ports": body.get("ports"), "upstream": s.get("upstream"),
                   "env_keys": sorted((body.get("env") or {}).keys())}
            s["pods"].append(rec)
            self._save(s)
            v = self._view(rec)
            v["startedAt"] = v["createdAt"]
            return 200, v
        if path.startswith("/v2/pods/"):
            pid = path.split("/")[3]
            pod = next((p for p in s["pods"] if p["id"] == pid and not p.get("terminated")), None)
            if method == "DELETE" or (method == "POST" and path.endswith("/action")):
                if pod is None:
                    return 404, None
                pod["terminated"] = now
                self._save(s)
                return 204, None
            if pod is None:
                return 404, {"error": "pod not found"}
            if method == "GET":
                return 200, self._view(pod)
            if method == "POST" and path.endswith("/exec"):
                cmd = " ".join(body.get("command") or [])
                if "gapcheck-fatal-check" in cmd and s.get("pod_fatal"):  # a pod that refused to start
                    return 200, {"stdout": f"fatal: {s['pod_fatal']}\n", "stderr": "", "exitCode": 0}
                return 200, {"stdout": f"[dry exec] {cmd[:200]}\n", "stderr": "", "exitCode": 0}
        return 404, {"error": f"fake: no route {method} {path}"}
