"""Copy pod-side logs to <session>/podlogs/ before termination (exec API, 900 kB per call).
Best effort and time-limited: never delays the termination by more than `max_seconds`."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rpapi  # noqa: E402


def collect(sess: Path, pod_id: str, max_seconds: float = 240) -> dict:
    out = sess / "podlogs"
    out.mkdir(exist_ok=True)
    t0 = time.time()
    got = {}

    def ex(cmd, to=25):
        code, d = rpapi.exec_cmd(pod_id, ["bash", "-c", cmd], to)
        if code != 200 or not isinstance(d, dict):
            return None
        return (d.get("stdout") or "") + (("[stderr]" + d["stderr"]) if d.get("stderr") else "")

    listing = ex("ls /models/logs")
    if listing is None:
        return {"ok": False, "why": "exec failed"}
    for f in listing.split():
        if time.time() - t0 > max_seconds or "/" in f:
            break
        size_s = (ex(f"wc -c < /models/logs/{f}") or "0").strip()
        size = int(size_s) if size_s.isdigit() else 0
        chunks, off = [], 0
        while off < size and off < 40 * 900000 and time.time() - t0 < max_seconds:
            chunks.append(ex(f"tail -c +{off + 1} /models/logs/{f} | head -c 900000") or "")
            off += 900000
        (out / f).write_text("".join(chunks))
        got[f] = size
    (out / "nvidia-smi.txt").write_text(ex("nvidia-smi; free -g; df -h /") or "")
    return {"ok": True, "files": got, "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    s = Path(sys.argv[1])
    pid = sys.argv[2] if len(sys.argv) > 2 else json.loads((s / "ledger.json").read_text())["pods"][-1]["id"]
    print(json.dumps(collect(s, pid), indent=1))
