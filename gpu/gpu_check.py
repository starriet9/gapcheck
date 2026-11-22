"""Read-only check: is the GPU available on the Community cloud, and at what price?
Also reads the account balance and the pods that exist. Creates nothing, spends nothing.

Usage: python3 -I gpu/gpu_check.py [--gpu ID] [--out results/gpu_check.json] [--dry-state F]
Exit code 0 if the GPU is listed on COMMUNITY with availability other than NONE, else 3.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rpapi  # noqa: E402

GPU = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
WATCH = ("NVIDIA RTX PRO 6000 Blackwell Server Edition",
         "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
         "NVIDIA H100 NVL")


def check(gpu: str = GPU) -> dict:
    out = {"t_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "gpu": gpu, "clouds": {}}
    for cloud in ("COMMUNITY", "SECURE"):
        rows = {g["id"]: g for g in rpapi.gpu_catalog(cloud)}
        out["clouds"][cloud] = {
            gid: ({"availability": rows[gid].get("availability"),
                   "price": (rows[gid].get("price") or {}).get(cloud.lower()),
                   "max_count": (rows[gid].get("maxCount") or {}).get(cloud.lower()),
                   "cuda": [c.get("version") for c in rows[gid].get("cudaVersions") or []
                            if c.get("available")],
                   "memory_gb": rows[gid].get("memory")} if gid in rows else None)
            for gid in WATCH}
    c = out["clouds"]["COMMUNITY"].get(gpu)
    out["community_available"] = bool(c and c["availability"] not in (None, "NONE")
                                      and (c["max_count"] or 0) > 0)
    out["community_price"] = c["price"] if c else None
    out["balance"] = rpapi.balance()
    out["pods"] = [{k: p.get(k) for k in ("id", "name", "status", "cost")} for p in rpapi.list_pods()]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", default=GPU)
    ap.add_argument("--out")
    ap.add_argument("--dry-state")
    a = ap.parse_args()
    if a.dry_state:
        rpapi.use_fake(a.dry_state)
    res = check(a.gpu)
    print(json.dumps(res, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1) + "\n")
    sys.exit(0 if res["community_available"] else 3)


if __name__ == "__main__":
    main()
