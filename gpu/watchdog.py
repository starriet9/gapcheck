"""Budget watchdog for a GPU session (gpu/session.py). session.py starts it with setsid nohup BEFORE creating
a pod, so it runs in its own session and outlives the session process and this terminal.

It TERMINATES (never stops) every pod of ours when any of these holds:
  - now >= that pod's deadline (ledger deadline_epoch, else createdAt + max wall time)
  - estimated total spend of our pods (+ 2 polls ahead) >= budget - margin
  - account balance has dropped by >= budget - margin since balance0
  - now >= the global hard deadline
  - the client heartbeat file (written by session.py every 20 s) is older than --client-stale
    seconds: the session process died without cleaning up (killed, crashed, PC asleep)
"Ours" = pod id in the ledger, or pod name starting with --prefix.
Exits only when the stop file exists (or the hard deadline passed) AND no pod of ours remains.

Usage: watchdog.py --ledger L --prefix P --hard-deadline EPOCH --budget 12 [--margin 0.5]
          --log F --stop-file F [--heartbeat F] [--balance0 X] [--poll 30] [--dry-state F]
          [--client-heartbeat F --client-stale 600]
       watchdog.py --selftest --log F  (harmless reads + DELETE of a nonexistent pod id)
The API key is read inside rpapi only; nothing secret is logged.
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rpapi  # noqa: E402

DISK_RATE = 0.10 / 730.0  # USD per GB-hour, container disk on a running pod


def log(f, msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg
    f.write(line + "\n")
    f.flush()


def load_ledger(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"pods": []}


def hourly_of(pod: dict, led: dict | None) -> float:
    if led and led.get("hourly"):
        return float(led["hourly"])
    disk = pod.get("disk") or 0
    return float(pod.get("cost") or 0) + disk * DISK_RATE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger")
    ap.add_argument("--prefix")
    ap.add_argument("--hard-deadline", type=float)
    ap.add_argument("--budget", type=float)
    ap.add_argument("--margin", type=float, default=0.5)
    ap.add_argument("--poll", type=float, default=30)
    ap.add_argument("--balance0", type=float, help="balance at the first launch (resumed sessions)")
    ap.add_argument("--log", required=True)
    ap.add_argument("--stop-file")
    ap.add_argument("--heartbeat")
    ap.add_argument("--client-heartbeat", help="file the session touches; stale -> terminate")
    ap.add_argument("--client-stale", type=float, default=600)
    ap.add_argument("--dry-state", help="use the offline fake RunPod API (fakerp.py)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.dry_state:
        rpapi.use_fake(a.dry_state)
    lf = open(a.log, "a")
    tag = "DRY " if a.dry_state else ""

    if a.selftest:
        pods = rpapi.list_pods()
        log(lf, f"{tag}SELFTEST list_pods ok: {len(pods)} pods: "
                + json.dumps([{k: p.get(k) for k in ('id', 'name', 'status')} for p in pods]))
        log(lf, f"{tag}SELFTEST balance ok: {rpapi.balance():.4f}")
        r = rpapi.terminate("nonexistent0selftest")
        log(lf, f"{tag}SELFTEST terminate(nonexistent) -> {r}")
        print("\n".join(open(a.log).read().splitlines()[-3:]))
        return
    for k in ("ledger", "prefix", "hard_deadline", "budget", "stop_file"):
        if getattr(a, k) is None:
            ap.error(f"--{k.replace('_', '-')} is required")

    cap = a.budget - a.margin
    ledger_p = Path(a.ledger)
    stop_p = Path(a.stop_file)
    bal0 = a.balance0
    for _ in range(10):
        if bal0 is not None:
            break
        try:
            bal0 = rpapi.balance()
        except Exception as e:
            log(lf, f"balance error at start: {e}")
            time.sleep(5)
    log(lf, f"{tag}START pid={os.getpid()} prefix={a.prefix} cap=${cap:.2f} hard_deadline="
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(a.hard_deadline))} balance0={bal0} "
            f"key_file={rpapi.key_file_shown()}")
    seen = {}  # id -> {"created", "hourly", "last_seen", "gone"}
    while True:
        now = time.time()
        if a.heartbeat:
            Path(a.heartbeat).write_text(f"{now:.0f}\n")
        led = load_ledger(ledger_p)
        led_by_id = {p["id"]: p for p in led.get("pods", [])}
        try:
            pods = rpapi.list_pods()
            api_ok = True
        except Exception as e:
            log(lf, f"API ERROR list_pods: {e}")
            pods, api_ok = [], False
        ours = [p for p in pods if p.get("id") in led_by_id
                or (a.prefix and str(p.get("name", "")).startswith(a.prefix))]
        if api_ok:
            ids_now = {p["id"] for p in ours}
            for pid, s in seen.items():
                if pid not in ids_now and not s["gone"]:
                    s["gone"] = True
                    log(lf, f"pod {pid} no longer listed (terminated); billed ~{s['last_seen'] - s['created']:.0f}s")
        for p in ours:
            pid = p["id"]
            created = rpapi.iso_to_epoch(p["createdAt"]) if p.get("createdAt") else now
            s = seen.setdefault(pid, {"created": created, "hourly": hourly_of(p, led_by_id.get(pid)),
                                      "last_seen": now, "gone": False})
            s["last_seen"] = now
            s["hourly"] = hourly_of(p, led_by_id.get(pid))
        # estimated spend: all pods ever seen (+ ledger pods never seen), billed from creation
        for pid, lp in led_by_id.items():
            if pid not in seen and lp.get("created_epoch"):
                seen[pid] = {"created": lp["created_epoch"], "hourly": float(lp.get("hourly") or 0),
                             "last_seen": now if not lp.get("terminated_epoch") else lp["terminated_epoch"],
                             "gone": bool(lp.get("terminated_epoch"))}
        spent = sum(s["hourly"] * (s["last_seen"] - s["created"]) / 3600 for s in seen.values())
        live_rate = sum(s["hourly"] for s in seen.values() if not s["gone"])
        projected = spent + live_rate * 2 * a.poll / 3600
        bal = None
        try:
            bal = rpapi.balance()
        except Exception as e:
            log(lf, f"API ERROR balance: {e}")
        bal_spent = (bal0 - bal) if (bal is not None and bal0 is not None) else None
        reasons = {}
        for p in ours:
            pid = p["id"]
            s = seen[pid]
            lp = led_by_id.get(pid) or {}
            if lp.get("deadline_epoch"):
                dl = float(lp["deadline_epoch"])
            else:
                hrs = math.floor(max(cap - (spent - s["hourly"] * (now - s["created"]) / 3600), 0)
                                 / max(s["hourly"], 1e-6))
                dl = s["created"] + hrs * 3600
            if now >= dl:
                reasons[pid] = f"pod deadline {time.strftime('%H:%M:%SZ', time.gmtime(dl))} reached"
        if projected >= cap:
            for p in ours:
                reasons.setdefault(p["id"], f"estimated spend ${projected:.2f} >= cap ${cap:.2f}")
        if bal_spent is not None and bal_spent >= cap:
            for p in ours:
                reasons.setdefault(p["id"], f"balance drop ${bal_spent:.2f} >= cap ${cap:.2f}")
        if now >= a.hard_deadline:
            for p in ours:
                reasons.setdefault(p["id"], "global hard deadline reached")
        if a.client_heartbeat and ours:
            try:
                hb_age = now - os.path.getmtime(a.client_heartbeat)
            except OSError:
                hb_age = None
            if hb_age is not None and hb_age >= a.client_stale:
                for p in ours:
                    reasons.setdefault(p["id"], f"client heartbeat {hb_age:.0f} s old >= {a.client_stale:.0f} s")
        log(lf, "poll pods=" + json.dumps([{"id": p["id"], "name": p.get("name"), "status": p.get("status"),
                                             "cost": p.get("cost"),
                                             "age_s": round(now - seen[p["id"]]["created"])} for p in ours])
            + f" est_spent=${spent:.3f} balance={bal} balance_drop={None if bal_spent is None else round(bal_spent, 4)}")
        for pid, why in reasons.items():
            r = rpapi.terminate(pid)
            log(lf, f"{tag}TERMINATE {pid}: {why} -> {r}")
        if (stop_p.exists() or now >= a.hard_deadline) and api_ok and not ours:
            log(lf, f"{tag}EXIT: stop={stop_p.exists()} no pods of ours remain; est_spent=${spent:.3f} "
                    f"balance={bal} balance_drop={bal_spent}")
            return
        time.sleep(min(10, a.poll) if reasons or not api_ok else a.poll)


if __name__ == "__main__":
    main()
