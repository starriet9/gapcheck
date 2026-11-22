"""Context checks of a run before any model call. Nothing here derives a setting:
context_limit and chars_per_token come fixed from the run config (configs/run1.json).

--local (no server; gpu/session.py runs it before creating a pod):
  the harness's L1 build stop (serialize.build_stop) on every case, with the run's
  settings: every row the oracle uses and every planted sentence is in the serialization,
  and the estimated prompt (characters / chars_per_token) is within context_limit.

server mode (through the local relay, once the server is up):
  1. Server: chat template sha256, number of slots, context per slot, build.
  2. Every L1 prompt of the case set rendered and counted by the server itself
     (/apply-template, /tokenize, with the request fields of the run); also the first
     agent prompt.
  3. Problems (exit 2, no model call is made): template or slots differ; the largest L1
     prompt plus max_tokens does not fit one slot; the estimate of some case exceeds
     context_limit (the harness would stop that case). PROTOCOL.md sec. 5: the served
     context is then raised and logged, by a person, not by this script.
  The smallest characters/token ratio is recorded; if it is below chars_per_token the
  estimate undercounts some prompt, which is noted (the server count in 3 is the binding
  check).

Runs with the harness code of the run commit: PYTHONPATH=<worktree>/src.
Usage:
  calibrate.py --local --cases DIR --out F --chars-per-token X --context-limit N
      [--l1-rounds 2 --page-size 20 --prompt-set default] [--case-ids ID,ID]
  calibrate.py --cases DIR --relay URL --out F --np 8 --ctx-per-slot 49152 --max-tokens 4096
      --template-sha256 HEX --chars-per-token X --context-limit N --model ALIAS
      [--extra JSON] [--agent-config max_steps:prompt_set ...] [--workers 8]
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

from gapcheck import backends, cases, serialize, systems
from gapcheck.tools import CaseEnv, tool_specs


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return json.loads(r.read().decode())


def post(base, path, payload, timeout=120):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def count(base, a, msgs, tools) -> int:
    m, t = backends.to_openai(msgs, tools)
    p = {"model": a.model, "messages": m, "max_tokens": a.max_tokens, "temperature": 0, "seed": 1,
         **json.loads(a.extra)}
    if t:
        p["tools"], p["tool_choice"] = t, "auto"
    prompt = post(base, "/apply-template", p)["prompt"]
    return len(post(base, "/tokenize", {"content": prompt})["tokens"])


def case_list(a) -> list[dict]:
    cl = cases.read_cases(a.cases)
    if a.case_ids:
        want = set(a.case_ids.split(","))
        cl = [c for c in cl if c["case_id"] in want]
    return cl


def local(a) -> int:
    t0 = time.time()
    problems, sizes = [], {}
    for c in case_list(a):
        cid = c["case_id"]
        env = CaseEnv.from_case(a.cases, cid, page_size=a.page_size)
        try:
            _, prompt_t = systems.l1_prompt_tokens(env, a.chars_per_token, a.l1_rounds, a.prompt_set)
            serialize.build_stop(a.cases, cid, serialize.serialize(env), prompt_t, a.context_limit)
            sizes[cid] = prompt_t
        except Exception as e:  # the build stop raises on any failed item
            problems.append(f"{cid}: {type(e).__name__}: {str(e)[:300]}")
        finally:
            env.close()
    out = {"t_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": "local",
           "seconds": round(time.time() - t0, 1), "n": len(sizes) + len(problems),
           "chars_per_token": a.chars_per_token, "context_limit": a.context_limit,
           "estimate_max": max(sizes.values(), default=None), "problems": problems,
           "ok": not problems}
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0 if not problems else 2


def server(a) -> int:
    base = a.relay.rstrip("/")
    t0 = time.time()
    problems, notes = [], []

    props = get(base, "/props")
    slots = get(base, "/slots")
    tmpl = props.get("chat_template") or ""
    health = {"chat_template_sha256": hashlib.sha256(tmpl.encode()).hexdigest(),
              "chat_template_bytes": len(tmpl.encode()), "total_slots": props.get("total_slots"),
              "slot_n_ctx": sorted({s.get("n_ctx") for s in slots}), "n_slots": len(slots),
              "build_info": props.get("build_info"), "model_path": props.get("model_path")}
    if health["chat_template_sha256"] != a.template_sha256:
        problems.append(f"chat template sha256 {health['chat_template_sha256'][:12]} != {a.template_sha256[:12]}")
    if health["total_slots"] != a.np or health["n_slots"] != a.np:
        problems.append(f"slots {health['total_slots']}/{health['n_slots']} != {a.np}")
    if health["slot_n_ctx"] != [a.ctx_per_slot]:
        problems.append(f"slot context {health['slot_n_ctx']} != {a.ctx_per_slot}")

    tools = tool_specs(("prevent_risk",))

    def one(c):
        env = CaseEnv.from_case(a.cases, c["case_id"], page_size=a.page_size)
        try:
            msgs = systems.l1_messages(env, a.l1_rounds, a.prompt_set)
            _, chars = systems.l1_prompt_tokens(env, 1.0, a.l1_rounds, a.prompt_set)  # characters
            est = math.ceil(chars / a.chars_per_token)
            return c["case_id"], {"tokens": count(base, a, msgs, tools), "chars": chars, "estimate": est,
                                  "condition": c["condition"]}
        finally:
            env.close()
    cl = case_list(a)
    with cf.ThreadPoolExecutor(a.workers) as ex:  # tokenizing only; no completion is requested
        res = dict(ex.map(one, cl))
    agent = {}
    for spec in a.agent_config:
        ms, ps = spec.split(":")
        agent[spec] = count(base, a, systems.agent_messages(int(ms), ps), tool_specs())

    toks = sorted(v["tokens"] for v in res.values())
    big = max(res, key=lambda c: res[c]["tokens"])
    ratio = min(v["chars"] / v["tokens"] for v in res.values())
    est_max = max(v["estimate"] for v in res.values())
    if toks[-1] + a.max_tokens > a.ctx_per_slot:
        over = sorted(c for c, v in res.items() if v["tokens"] + a.max_tokens > a.ctx_per_slot)
        problems.append(f"L1 prompt does not fit: largest {toks[-1]} tokens ({big}) + max_tokens "
                        f"{a.max_tokens} > served context {a.ctx_per_slot}; {len(over)} cases over: "
                        f"{over[:10]}. PROTOCOL.md sec. 5: raise the served context and log it in "
                        "RUNS.md before any model call (not automated)")
    if est_max > a.context_limit:
        problems.append(f"build-stop estimate {est_max} > context_limit {a.context_limit} with "
                        f"chars_per_token {a.chars_per_token}")
    if ratio < a.chars_per_token:
        notes.append(f"smallest characters/token {ratio:.4f} < chars_per_token {a.chars_per_token}: "
                     "the estimate undercounts some prompts; the server count above is binding")
    out = {"t_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": "server",
           "seconds": round(time.time() - t0, 1), "health": health, "n": len(res), "max": toks[-1],
           "max_case": big, "median": toks[len(toks) // 2], "p90": toks[int(0.9 * len(toks))],
           "min": toks[0], "min_chars_per_token": ratio, "chars_per_token": a.chars_per_token,
           "context_limit": a.context_limit, "estimate_max": est_max,
           "agent_first_prompt_tokens": agent, "problems": problems, "notes": notes,
           "ok": not problems, "cases": res}
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "cases"}, indent=1))
    return 0 if not problems else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--case-ids", help="comma-separated: only these cases (default: all)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chars-per-token", type=float, required=True)
    ap.add_argument("--context-limit", type=int, required=True)
    ap.add_argument("--l1-rounds", type=int, default=2)
    ap.add_argument("--page-size", type=int, default=20)
    ap.add_argument("--prompt-set", default="default")
    ap.add_argument("--relay")
    ap.add_argument("--np", type=int)
    ap.add_argument("--ctx-per-slot", type=int)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--template-sha256")
    ap.add_argument("--model")
    ap.add_argument("--extra", default="{}", help="request fields of the run (JSON)")
    ap.add_argument("--agent-config", action="append", default=[],
                    help="max_steps:prompt_set (its first prompt is tokenized)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if not a.local:
        for k in ("relay", "np", "ctx_per_slot", "template_sha256", "model"):
            if getattr(a, k) is None:
                ap.error(f"--{k.replace('_', '-')} is required without --local")
    sys.exit(local(a) if a.local else server(a))


if __name__ == "__main__":
    main()
