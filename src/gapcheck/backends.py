"""Model backends behind one interface: `chat(messages, tools) -> Response`.

Messages use one neutral format:
  {"role": "system" | "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
An assistant message may also carry "raw": the provider's own content (kept
so that, for example, thinking blocks are sent back unchanged). Tools are
[{"name", "description", "parameters"}] (JSON schema).

Backends:
- MockBackend: scripted responses (a list, or a function of the messages).
- OpenAICompatBackend: an OpenAI-compatible chat endpoint such as a vLLM
  server (standard library HTTP; base_url, model, revision, temperature,
  seed from the config).
- AnthropicBackend: the Messages API through the `anthropic` SDK (imported
  only when used; the SDK finds credentials itself). The model id comes from
  the config and is never hard-coded.

`load_config(path)` reads a JSON file (or YAML if PyYAML is installed);
`make_backend(config)` builds the backend.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    latency_s: float = 0.0
    stop_reason: str | None = None
    raw: object = None  # provider content for the assistant message (not logged)

    def message(self) -> dict:
        m = {"role": "assistant", "content": self.text,
             "tool_calls": [t.to_dict() for t in self.tool_calls]}
        if self.raw is not None:
            m["raw"] = self.raw
        return m


class Backend:
    name = "base"

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> Response:
        raise NotImplementedError

    def describe(self) -> dict:
        """Settings for the run manifest (no secrets)."""
        return {"backend": self.name}


# ---------------------------------------------------------------- mock
class MockBackend(Backend):
    """Scripted model. `script` is a list of Response (or text, or dicts with text and
    tool_calls), used in order, or a function (messages, tools) -> Response."""
    name = "mock"

    def __init__(self, script: list | Callable, label: str = "scripted"):
        self.script = script
        self.label = label
        self.calls: list[dict] = []
        self._i = 0

    def bind(self, env) -> None:
        """Give a policy object the case it is about to see (test doubles only)."""
        b = getattr(self.script, "bind", None)
        if b:
            b(env)

    def chat(self, messages, tools=None) -> Response:
        t0 = time.perf_counter()
        self.calls.append({"messages": list(messages), "tools": [t["name"] for t in tools or []]})
        if callable(self.script):
            r = self.script(messages, tools)
        else:
            if self._i >= len(self.script):
                raise RuntimeError("mock script exhausted")
            r = self.script[self._i]
            self._i += 1
        if isinstance(r, str):
            r = Response(r)
        elif isinstance(r, dict):
            r = Response(r.get("text", ""), [c if isinstance(c, ToolCall) else
                                             ToolCall(c.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                                      c["name"], c.get("arguments", {}))
                                             for c in r.get("tool_calls", [])])
        n_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        r.usage = {"input_tokens": n_in, "output_tokens": len(r.text) // 4 + 20 * len(r.tool_calls)}
        r.latency_s = time.perf_counter() - t0
        return r

    def describe(self) -> dict:
        return {"backend": self.name, "label": self.label}


# ---------------------------------------------------------------- OpenAI-compatible
def to_openai(messages: list[dict], tools: list[dict] | None) -> tuple[list, list | None]:
    out = []
    for m in messages:
        if m["role"] == "assistant":
            d = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                d["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"],
                                                 "arguments": json.dumps(c["arguments"])}}
                                   for c in m["tool_calls"]]
            out.append(d)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                        "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    t = None
    if tools:
        t = [{"type": "function", "function": {"name": x["name"], "description": x["description"],
                                               "parameters": x["parameters"]}} for x in tools]
    return out, t


def from_openai(data: dict) -> Response:
    ch = data["choices"][0]
    msg = ch["message"]
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_unparsed": fn.get("arguments")}
        if not isinstance(args, dict):
            args = {"_unparsed": fn.get("arguments")}
        calls.append(ToolCall(c.get("id") or f"call_{uuid.uuid4().hex[:8]}", fn["name"], args))
    u = data.get("usage") or {}
    return Response(msg.get("content") or "", calls,
                    {"input_tokens": u.get("prompt_tokens", 0),
                     "output_tokens": u.get("completion_tokens", 0)},
                    stop_reason=ch.get("finish_reason"))


def _http_post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class OpenAICompatBackend(Backend):
    """Chat completions on an OpenAI-compatible server (e.g. vLLM).

    Config keys: base_url, model, revision (recorded; the server must serve
    that revision), temperature, seed, max_tokens, timeout, extra (dict of
    further request fields), api_key_env (name of an env var, optional).
    """
    name = "openai_compat"

    def __init__(self, cfg: dict, transport: Callable | None = None):
        for k in ("base_url", "model"):
            if not cfg.get(k):
                raise ValueError(f"openai_compat config needs {k}")
        self.cfg = cfg
        self.transport = transport or _http_post

    def chat(self, messages, tools=None) -> Response:
        msgs, t = to_openai(messages, tools)
        c = self.cfg
        payload = {"model": c["model"], "messages": msgs,
                   "max_tokens": c.get("max_tokens", 4096)}
        for k in ("temperature", "seed", "top_p"):
            if c.get(k) is not None:
                payload[k] = c[k]
        if t:
            payload["tools"] = t
            payload["tool_choice"] = "auto"
        payload.update(c.get("extra") or {})
        headers = {}
        if c.get("api_key_env") and os.environ.get(c["api_key_env"]):
            headers["Authorization"] = "Bearer " + os.environ[c["api_key_env"]]
        t0 = time.perf_counter()
        data = self.transport(c["base_url"].rstrip("/") + "/chat/completions", payload, headers,
                              c.get("timeout", 600))
        r = from_openai(data)
        r.latency_s = time.perf_counter() - t0
        return r

    def describe(self) -> dict:
        keep = ("base_url", "model", "revision", "temperature", "seed", "top_p", "max_tokens",
                "extra")
        return {"backend": self.name, **{k: self.cfg.get(k) for k in keep}}


# ---------------------------------------------------------------- Anthropic
def _block_dict(b) -> dict:
    if isinstance(b, dict):
        return b
    for m in ("to_dict", "model_dump"):
        f = getattr(b, m, None)
        if f:
            return f()
    return dict(vars(b))


def to_anthropic(messages: list[dict], tools: list[dict] | None) -> tuple[str, list, list | None]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "assistant":
            if m.get("raw") is not None:
                content = m["raw"]
            else:
                content = []
                if m.get("content"):
                    content.append({"type": "text", "text": m["content"]})
                content += [{"type": "tool_use", "id": c["id"], "name": c["name"],
                             "input": c["arguments"]} for c in m.get("tool_calls") or []]
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                     "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)  # all results of one turn in one message
            else:
                out.append({"role": "user", "content": [block]})
        else:
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append({"type": "text", "text": m["content"]})
            else:
                out.append({"role": "user", "content": m["content"]})
    t = None
    if tools:
        t = [{"name": x["name"], "description": x["description"], "input_schema": x["parameters"]}
             for x in tools]
    return system, out, t


def from_anthropic(msg) -> Response:
    texts, calls, raw = [], [], []
    for b in msg.content:
        d = _block_dict(b)
        raw.append(d)
        if d.get("type") == "text":
            texts.append(d.get("text", ""))
        elif d.get("type") == "tool_use":
            calls.append(ToolCall(d["id"], d["name"], dict(d.get("input") or {})))
    u = msg.usage
    usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
             "output_tokens": getattr(u, "output_tokens", 0) or 0}
    for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if getattr(u, k, None):
            usage[k] = getattr(u, k)
    return Response("".join(texts), calls, usage, stop_reason=getattr(msg, "stop_reason", None),
                    raw=raw)


class AnthropicBackend(Backend):
    """Messages API with client tools.

    Config keys: model (required, e.g. a dated snapshot id), max_tokens,
    effort (output_config.effort), thinking (dict, sent as given), temperature
    (sent only if set; newer models reject sampling parameters), timeout.
    Credentials: the SDK's default lookup only.
    """
    name = "anthropic"

    def __init__(self, cfg: dict, client=None):
        if not cfg.get("model"):
            raise ValueError("anthropic config needs model")
        self.cfg = cfg
        if client is None:
            import anthropic  # optional dependency: uv add anthropic
            client = anthropic.Anthropic(timeout=cfg.get("timeout", 600))
        self.client = client

    def chat(self, messages, tools=None) -> Response:
        system, msgs, t = to_anthropic(messages, tools)
        c = self.cfg
        kw = {"model": c["model"], "max_tokens": c.get("max_tokens", 16000), "messages": msgs}
        if system:
            kw["system"] = system
        if t:
            kw["tools"] = t
        if c.get("effort"):
            kw["output_config"] = {"effort": c["effort"]}
        if c.get("thinking"):
            kw["thinking"] = c["thinking"]
        if c.get("temperature") is not None:
            kw["temperature"] = c["temperature"]
        t0 = time.perf_counter()
        msg = self.client.messages.create(**kw)
        r = from_anthropic(msg)
        r.latency_s = time.perf_counter() - t0
        return r

    def describe(self) -> dict:
        keep = ("model", "max_tokens", "effort", "thinking", "temperature")
        return {"backend": self.name, **{k: self.cfg.get(k) for k in keep}}


# ---------------------------------------------------------------- config
def load_config(path: str | Path) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml  # only if installed
        return yaml.safe_load(text)
    return json.loads(text)


def make_backend(cfg: dict, **kw) -> Backend:
    b = cfg.get("backend")
    if b == "openai_compat":
        return OpenAICompatBackend(cfg, **kw)
    if b == "anthropic":
        return AnthropicBackend(cfg, **kw)
    if b == "mock":
        from .mock_agents import policy  # offline policies for dry runs
        return MockBackend(policy(cfg.get("policy", "abstain")), label=cfg.get("policy", "abstain"))
    raise ValueError(f"unknown backend {b!r}")
