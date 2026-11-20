"""Run systems on a case set and write one JSONL per system (SPEC 6, 7).

Usage:
  python scripts/run_systems.py --cases data/cases/dev_s20261120_utc_n10 \
      --systems R0,R1,L1,A1,A2 --backend configs/vllm.json --run-id dev_r1 \
      [--out data/runs] [--limit N] [--case-ids id1,id2] [--score]

Writes <out>/<run-id>/<system>.jsonl (case id, output, trace, tokens,
seconds, verifier results, forced-fallback flag, and an `audit`: verifier
checks on the final answer, never shown to any model) and manifest.json
(git commit, prompt set with each prompt role's file and sha256, conventions
sha256, case-set sha256, backend and run settings). R0 and R1 need no
backend. A1 and A2 come from one agent run: A1 is A2's pre-verification
answer. The backend config's "run" block sets the harness settings
(`systems.Settings`), including `prompt_set` and `rounds_left_notice`.

Before L1 runs, every case passes the build-stop check (serialize.build_stop):
rows the oracle uses and planted sentences must be in the serialization, and
the prompt must fit the context limit.

`runs/` is not in .gitignore, so the default output is under data/ (ignored).
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from gapcheck import backends, cases, conventions, serialize, systems, verifier
from gapcheck.tools import CaseEnv

ROOT = Path(__file__).resolve().parents[1]


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return p.name


def git_state() -> dict:
    def run(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True,
                                  check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def audit(out, env) -> dict | None:
    if out is None:
        return None
    rep = verifier.verify(out, env)
    return {"ok": rep.ok, "failures": [f.to_dict() for f in rep.failures],
            "unsupported": list(verifier.unsupported(rep))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--systems", default="R0,R1")
    ap.add_argument("--backend", help="backend config (JSON, or YAML if PyYAML is installed)")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "runs"))
    ap.add_argument("--limit", type=int, help="first N cases only (smoke runs)")
    ap.add_argument("--case-ids", help="comma-separated case ids")
    ap.add_argument("--score", action="store_true", help="score the run when done")
    ap.add_argument("--force", action="store_true", help="overwrite an existing run directory")
    a = ap.parse_args()

    names = [s.strip() for s in a.systems.split(",") if s.strip()]
    bad = set(names) - set(systems.SYSTEMS)
    if bad:
        sys.exit(f"unknown systems: {sorted(bad)}")
    need_model = bool(set(names) & {"L1", "A1", "A2"})
    cfg = backends.load_config(a.backend) if a.backend else {}
    if need_model and not cfg:
        sys.exit("L1, A1 and A2 need --backend")
    backend = backends.make_backend(cfg) if need_model else None
    st = systems.Settings.from_dict(cfg.get("run"))

    case_dir = Path(a.cases)
    case_list = cases.read_cases(case_dir)
    if a.case_ids:
        want = set(a.case_ids.split(","))
        case_list = [c for c in case_list if c["case_id"] in want]
    if a.limit:
        case_list = case_list[:a.limit]

    out_dir = Path(a.out) / a.run_id
    if out_dir.exists() and any(out_dir.iterdir()) and not a.force:
        sys.exit(f"{rel(out_dir)} exists; use --force or a new --run-id")
    out_dir.mkdir(parents=True, exist_ok=True)

    # L1 build stop, over every case before any model call
    l1_sizes = {}
    if "L1" in names:
        for c in case_list:
            env = CaseEnv.from_case(case_dir, c["case_id"], page_size=st.page_size)
            ser_t, prompt_t = systems.l1_prompt_tokens(env, st.chars_per_token,
                                                       st.l1_tool_rounds, st.prompt_set)
            serialize.build_stop(case_dir, c["case_id"], serialize.serialize(env), prompt_t,
                                 st.context_limit)
            l1_sizes[c["case_id"]] = {"serialization_tokens": ser_t, "prompt_tokens": prompt_t}
            env.close()

    manifest = {
        "run_id": a.run_id, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git": git_state(), "systems": names, "n_cases": len(case_list),
        "case_ids": [c["case_id"] for c in case_list],
        "case_set": {"path": rel(case_dir),
                     "manifest_sha256": hashlib.sha256(
                         (case_dir / "manifest.json").read_bytes()).hexdigest()},
        **systems.prompt_record(st.prompt_set),  # prompt_set, prompt_files, prompts_sha256
        "conventions_sha256": conventions.sha256(),
        "backend": backend.describe() if backend else None,
        "settings": systems.Settings.from_dict(cfg.get("run")).__dict__,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    files = {s: (out_dir / f"{s}.jsonl").open("w") for s in names}
    t0 = time.time()
    for n, c in enumerate(case_list, 1):
        cid = c["case_id"]
        env = CaseEnv.from_case(case_dir, cid, page_size=st.page_size)
        res = {}
        for s in ("R0", "R1"):
            if s in names:
                res[s] = systems.run_rule(s, env)
        if "L1" in names:
            env.calls.clear()
            res["L1"] = systems.run_l1(env, backend, st)
            res["L1"]["l1_size"] = l1_sizes.get(cid)
        if {"A1", "A2"} & set(names):
            env.calls.clear()
            a1, a2 = systems.run_agent(env, backend, st, verify="A2" in names)
            res["A1"], res["A2"] = a1, a2
        for s in names:
            r = res[s]
            r["audit"] = audit(r["output"], env)
            files[s].write(json.dumps({"case_id": cid, "system": s, **r},
                                      ensure_ascii=False, default=str) + "\n")
            files[s].flush()  # a run stopped early keeps every finished case
        env.close()
        if n % 20 == 0 or n == len(case_list):
            print(f"{n}/{len(case_list)} cases, {time.time() - t0:.0f} s", flush=True)
    for f in files.values():
        f.close()
    manifest["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"run": rel(out_dir), "systems": names, "cases": len(case_list)}))

    if a.score:
        from gapcheck import score
        s = score.score(case_dir, out_dir)
        (out_dir / "score.json").write_text(json.dumps(s, indent=1) + "\n")
        for name, v in s["accuracy_main"]["systems"].items():
            by = {k: (None if x["est"] is None else round(100 * x["est"], 1))
                  for k, x in v["by_condition"].items()}
            print(name, "macro", round(100 * v["macro"]["est"], 1), by)


if __name__ == "__main__":
    main()
