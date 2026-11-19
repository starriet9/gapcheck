"""Systems R0, R1, L1, A1 and A2 (SPEC 6) on one case.

- R0, R1: the rule baselines (`baselines.r0`, `baselines.r1`) on the case record.
- L1: one conversation on the serialized record (`serialize.serialize`) with
  prevent_risk as its only tool, at most `l1_tool_rounds` rounds of calls.
- A1: tool-using loop with every tool in `tools.TOOL_SPECS`, at most
  `max_steps` rounds of calls. The final reply must pass `schema.validate`;
  an invalid reply gets one re-ask, then the case is marked schema-invalid.
- A2: A1 plus the verifier. On failure the model is told only the failed
  check numbers and claim indices and gets one repair round (up to
  `repair_steps` rounds of calls); a second failure forces CANNOT_DETERMINE
  with `missing` unchanged. A1 is A2's pre-verification answer from the same
  run (`run_agent` returns both).

Every system gets the same system prompt and conventions page. Prompts are
plain text files in `prompts/`; their sha256 go into the run manifest. The task
prompts state the tool-round limits ({rounds}, {max_steps}), filled in from
`Settings` so the number the model reads is the harness limit.

A result is a dict: output (or None), schema_valid, error, trace, tokens,
model_calls, tool_calls, seconds, verifier (A2: one report per round),
forced_fallback.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import baselines, conventions, oracle, verifier
from . import schema as S
from .backends import Backend, BackendError
from .serialize import est_tokens, serialize
from .tools import CaseEnv, dumps, tool_specs

PROMPT_DIR = Path(__file__).with_name("prompts")
PROMPTS = ("system", "task_l1", "task_agent", "reask", "repair", "limit")
SYSTEMS = ("R0", "R1", "L1", "A1", "A2")


@dataclass
class Settings:
    max_steps: int = 20          # A1: rounds of tool calls
    repair_steps: int = 8        # A2 repair: rounds of tool calls
    l1_tool_rounds: int = 2
    context_limit: int = 32000   # estimated tokens (chars / chars_per_token), L1 build stop
    page_size: int = 20
    # Characters per token for the build-stop estimate. 4 is a rough English
    # figure; records full of ids, dates and numbers tokenize denser (a Qwen
    # tokenizer measured about 2.2-2.3 on dev L1 prompts). Calibrate it with
    # the server's tokenizer before trusting the build stop.
    chars_per_token: float = 4.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        d = d or {}
        return cls(**{k: d[k] for k in asdict(cls()) if k in d})


def prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def task_l1(rounds: int) -> str:
    """L1 task prompt with its tool-round limit (Settings.l1_tool_rounds)."""
    return prompt("task_l1").format(rounds=rounds)


def task_agent(max_steps: int) -> str:
    """Agent task prompt with its tool-round limit (Settings.max_steps)."""
    return prompt("task_agent").format(max_steps=max_steps)


def prompt_hashes() -> dict[str, str]:
    return {n: hashlib.sha256((PROMPT_DIR / f"{n}.txt").read_bytes()).hexdigest()
            for n in PROMPTS}


def system_message() -> dict:
    return {"role": "system", "content": prompt("system") + "\n\n" + conventions.render()}


# ---------------------------------------------------------------- parsing
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_answer(text: str | None) -> tuple[dict | None, str | None]:
    """(answer, None) if `text` holds one schema-valid JSON object, else (None, error)."""
    if not text or not text.strip():
        return None, "empty reply"
    t = _FENCE.sub("", text.strip())
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None, "no JSON object found"
    try:
        out = json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e.msg})"
    try:
        S.validate(out)
    except ValueError as e:
        return None, f"schema: {e}"
    return out, None


# ---------------------------------------------------------------- conversation
class Run:
    """Trace and usage of one system run."""

    def __init__(self):
        self.trace: list[dict] = []
        self.tokens = {"input_tokens": 0, "output_tokens": 0}
        self.model_calls = 0
        self.tool_calls = 0
        self.t0 = time.perf_counter()

    def model(self, r) -> None:
        self.model_calls += 1
        for k in self.tokens:
            self.tokens[k] += int(r.usage.get(k, 0) or 0)
        self.trace.append({"type": "model", "text": r.text,
                           "tool_calls": [c.to_dict() for c in r.tool_calls],
                           "usage": r.usage, "latency_s": round(r.latency_s, 3),
                           "stop_reason": r.stop_reason})

    def tool(self, name, args, result) -> None:
        self.tool_calls += 1
        self.trace.append({"type": "tool", "name": name, "args": args, "result": result})

    def user(self, kind: str, content: str) -> None:
        self.trace.append({"type": "user", "kind": kind, "content": content})

    def snapshot(self) -> dict:
        return {"trace": list(self.trace), "tokens": dict(self.tokens),
                "model_calls": self.model_calls, "tool_calls": self.tool_calls,
                "seconds": round(time.perf_counter() - self.t0, 3)}


def _refuse_calls(calls, messages: list[dict], run: Run) -> None:
    """Answer each tool call with the limit error, so every assistant tool call in the
    conversation has its tool result (APIs reject a tool call left unanswered)."""
    for c in calls:
        res = {"error": "tool limit reached"}
        run.tool(c.name, c.arguments, res)
        messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                         "content": dumps(res)})


def converse(backend: Backend, messages: list[dict], tools: list[dict], env: CaseEnv,
             max_rounds: int, run: Run) -> str | None:
    """Model turns until a reply without tool calls. After `max_rounds` rounds, tool calls
    get an error and the limit prompt once; a further tool call gets the error too and
    ends with None."""
    allowed = {t["name"] for t in tools}
    rounds, over = 0, False
    while True:
        r = backend.chat(messages, tools or None)
        run.model(r)
        messages.append(r.message())
        if not r.tool_calls:
            return r.text
        if rounds >= max_rounds:
            _refuse_calls(r.tool_calls, messages, run)
            if over:
                return None
            over = True
            messages.append({"role": "user", "content": prompt("limit")})
            run.user("limit", prompt("limit"))
            continue
        for c in r.tool_calls:
            res = env.call(c.name, c.arguments) if c.name in allowed else \
                {"error": f"tool {c.name!r} is not available"}
            run.tool(c.name, c.arguments, res)
            messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                             "content": dumps(res)})
        rounds += 1


def answer(backend, messages, tools, env, max_rounds, run) -> tuple[dict | None, str | None]:
    """Converse, parse and validate; one re-ask on an invalid reply. A request the
    server rejects (BackendError, e.g. context overflow) ends with no answer and no
    re-ask: a longer conversation would be rejected too."""
    try:
        text = converse(backend, messages, tools, env, max_rounds, run)
        out, err = parse_answer(text)
        if out is not None:
            return out, None
        if text is None:
            err = "a tool call after the tool limit"
        msg = prompt("reask").format(error=err)
        messages.append({"role": "user", "content": msg})
        run.user("reask", msg)
        text = converse(backend, messages, tools, env, 0, run)
    except BackendError as e:
        run.trace.append({"type": "error", "error": str(e)})
        return None, f"backend: {e}"
    out, err2 = parse_answer(text)
    if text is None:
        err2 = "a tool call after the tool limit"
    return (out, None) if out is not None else (None, err2)


def _result(out, err, snap, **kw) -> dict:
    return {"output": out, "schema_valid": out is not None, "error": err, **snap,
            "verifier": kw.get("verifier"), "forced_fallback": kw.get("forced_fallback", False)}


# ---------------------------------------------------------------- systems
def notes_of(env: CaseEnv) -> list[dict]:
    return [{"note_id": n, "date": d[:10], "text": t} for n, d, t in
            env.con.execute("select note_id, DATE, TEXT from notes order by DATE, note_id").fetchall()]


def run_rule(system: str, env: CaseEnv) -> dict:
    run = Run()
    f = env.facts()
    out = baselines.r0(f) if system == "R0" else baselines.r1(f, notes_of(env))
    return _result(out, None, run.snapshot())


def _bind(backend, env) -> None:
    b = getattr(backend, "bind", None)
    if b:
        b(env)


def l1_messages(env: CaseEnv, rounds: int = Settings.l1_tool_rounds) -> list[dict]:
    return [system_message(),
            {"role": "user", "content": task_l1(rounds) + "\n\n" + serialize(env)}]


def run_l1(env: CaseEnv, backend: Backend, st: Settings) -> dict:
    _bind(backend, env)
    run = Run()
    msgs = l1_messages(env, st.l1_tool_rounds)
    out, err = answer(backend, msgs, tool_specs(("prevent_risk",)), env, st.l1_tool_rounds, run)
    return _result(out, err, run.snapshot())


def agent_messages(max_steps: int = Settings.max_steps) -> list[dict]:
    return [system_message(), {"role": "user", "content": task_agent(max_steps)}]


FALLBACK_NOTE = ("The evidence for this answer did not pass the provenance checks after one "
                 "repair, so the status is CANNOT_DETERMINE. Review the record directly.")


def forced_fallback(last: dict) -> dict:
    return S.validate({"status": oracle.CANNOT_DETERMINE, "benefit_group": None,
                       "risk_10y": None, "missing": list(last["missing"]),
                       "evidence": last["evidence"], "note_for_clinician": FALLBACK_NOTE})


def run_agent(env: CaseEnv, backend: Backend, st: Settings, verify: bool = True
              ) -> tuple[dict, dict | None]:
    """A1 (pre-verification answer) and, with verify=True, A2 from the same run."""
    _bind(backend, env)
    run = Run()
    msgs = agent_messages(st.max_steps)
    tools = tool_specs()
    out, err = answer(backend, msgs, tools, env, st.max_steps, run)
    a1 = _result(out, err, run.snapshot())
    if not verify:
        return a1, None
    if out is None:
        return a1, _result(None, err, run.snapshot(), verifier=[])
    rep = verifier.verify(out, env)
    reports = [rep.to_dict()]
    run.trace.append({"type": "verifier", "round": 1, **rep.to_dict()})
    if rep.ok:
        return a1, _result(out, None, run.snapshot(), verifier=reports)
    msg = prompt("repair").format(failures=verifier.feedback(rep))
    msgs.append({"role": "user", "content": msg})
    run.user("repair", msg)
    out2, err2 = answer(backend, msgs, tools, env, st.repair_steps, run)
    if out2 is not None:
        rep2 = verifier.verify(out2, env)
        reports.append(rep2.to_dict())
        run.trace.append({"type": "verifier", "round": 2, **rep2.to_dict()})
        if rep2.ok:
            return a1, _result(out2, None, run.snapshot(), verifier=reports)
    last = out2 if out2 is not None else out
    return a1, _result(forced_fallback(last), err2, run.snapshot(), verifier=reports,
                       forced_fallback=True)


def l1_prompt_tokens(env: CaseEnv, chars_per_token: float = 4.0,
                     rounds: int = Settings.l1_tool_rounds) -> tuple[int, int]:
    """(serialization tokens, whole L1 prompt tokens with the tool definition),
    estimated as characters / chars_per_token."""
    msgs = l1_messages(env, rounds)
    ser = serialize(env)
    tools = json.dumps(tool_specs(("prevent_risk",)))
    return (est_tokens(ser, chars_per_token),
            est_tokens("".join(m["content"] for m in msgs) + tools, chars_per_token))
