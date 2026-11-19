"""Backends: message conversion and request building, offline (fake transport/client)."""
import json
from types import SimpleNamespace

import pytest

from gapcheck import backends as B
from gapcheck.tools import tool_specs

MSGS = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "Review."},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "t1", "name": "get_demographics", "arguments": {}},
        {"id": "t2", "name": "read_note", "arguments": {"note_id": "n1"}}]},
    {"role": "tool", "tool_call_id": "t1", "name": "get_demographics", "content": "{\"age\":55}"},
    {"role": "tool", "tool_call_id": "t2", "name": "read_note", "content": "{\"text\":\"x\"}"},
    {"role": "user", "content": "The tool limit is reached."},
]


def test_openai_conversion_and_request():
    msgs, tools = B.to_openai(MSGS, tool_specs(("read_note",)))
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[2]["tool_calls"][1]["function"] == {"name": "read_note",
                                                    "arguments": json.dumps({"note_id": "n1"})}
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "{\"age\":55}"}
    assert tools[0]["type"] == "function" and tools[0]["function"]["name"] == "read_note"

    seen = {}

    def transport(url, payload, headers, timeout):
        seen.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c9", "type": "function",
             "function": {"name": "read_note", "arguments": "{\"note_id\": \"n2\"}"}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 7}}

    cfg = {"backend": "openai_compat", "base_url": "http://127.0.0.1:8000/v1/", "model": "m",
           "revision": "abc123", "temperature": 0, "seed": 7, "max_tokens": 512}
    b = B.make_backend(cfg, transport=transport)
    r = b.chat(MSGS, tool_specs(("read_note",)))
    assert seen["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    p = seen["payload"]
    assert p["model"] == "m" and p["temperature"] == 0 and p["seed"] == 7 and p["max_tokens"] == 512
    assert p["tool_choice"] == "auto" and "Authorization" not in seen["headers"]
    assert r.tool_calls[0].to_dict() == {"id": "c9", "name": "read_note",
                                         "arguments": {"note_id": "n2"}}
    assert r.usage == {"input_tokens": 100, "output_tokens": 7} and r.stop_reason == "tool_calls"
    assert b.describe()["revision"] == "abc123"


def test_openai_bad_arguments_kept():
    r = B.from_openai({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "a", "function": {"name": "read_note", "arguments": "{oops"}}]}}]})
    assert r.tool_calls[0].arguments == {"_unparsed": "{oops"}


def test_anthropic_conversion():
    system, msgs, tools = B.to_anthropic(MSGS, tool_specs(("read_note",)))
    assert system == "SYS"
    assert msgs[0] == {"role": "user", "content": "Review."}
    assert [b["type"] for b in msgs[1]["content"]] == ["tool_use", "tool_use"]
    # both results in one user message, then the text
    assert msgs[2]["role"] == "user"
    assert [b["type"] for b in msgs[2]["content"]] == ["tool_result", "tool_result", "text"]
    assert len(msgs) == 3
    assert tools[0]["input_schema"]["required"] == ["note_id"]


class _FakeClient:
    def __init__(self):
        self.kw = None
        self.messages = self

    def create(self, **kw):
        self.kw = kw
        return SimpleNamespace(
            content=[{"type": "thinking", "thinking": "", "signature": "s"},
                     {"type": "text", "text": "Checking."},
                     {"type": "tool_use", "id": "tu1", "name": "get_demographics", "input": {}}],
            usage=SimpleNamespace(input_tokens=50, output_tokens=9, cache_read_input_tokens=0),
            stop_reason="tool_use")


def test_anthropic_backend_request_and_replay():
    fake = _FakeClient()
    cfg = {"backend": "anthropic", "model": "model-id-from-config", "max_tokens": 2000,
           "effort": "high"}
    b = B.make_backend(cfg, client=fake)
    r = b.chat(MSGS[:2], tool_specs(("get_demographics",)))
    kw = fake.kw
    assert kw["model"] == "model-id-from-config" and kw["system"] == "SYS"
    assert kw["output_config"] == {"effort": "high"} and "temperature" not in kw
    assert r.text == "Checking." and r.tool_calls[0].name == "get_demographics"
    assert r.usage == {"input_tokens": 50, "output_tokens": 9}
    # the assistant turn is sent back with its own blocks (thinking included)
    m = r.message()
    _, msgs, _ = B.to_anthropic(MSGS[:2] + [m, {"role": "tool", "tool_call_id": "tu1",
                                                "name": "get_demographics", "content": "{}"}],
                                None)
    assert msgs[1]["content"][0]["type"] == "thinking"
    assert msgs[2]["content"][0]["tool_use_id"] == "tu1"
    with pytest.raises(ValueError):
        B.AnthropicBackend({"backend": "anthropic"}, client=fake)


def test_mock_backend_and_config(tmp_path):
    m = B.MockBackend([{"text": "a"}, {"text": "", "tool_calls": [{"name": "get_allergies"}]}])
    assert m.chat([{"role": "user", "content": "hi"}]).text == "a"
    r = m.chat([{"role": "user", "content": "hi"}])
    assert r.tool_calls[0].name == "get_allergies" and r.tool_calls[0].id
    with pytest.raises(RuntimeError):
        m.chat([])
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"backend": "mock", "policy": "abstain", "run": {"max_steps": 3}}))
    cfg = B.load_config(p)
    mb = B.make_backend(cfg)
    assert json.loads(mb.chat([{"role": "user", "content": "x"}]).text)["status"] == \
        "CANNOT_DETERMINE"
    with pytest.raises(ValueError):
        B.make_backend({"backend": "other"})


# ---------------------------------------------------------------- real-server behaviour
OVERFLOW_BODY = (b'{"error":{"code":400,"message":"request (34561 tokens) exceeds the available '
                 b'context size (32768 tokens), try increasing it",'
                 b'"type":"exceed_context_size_error"}}')
OK_REPLY = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 3,
                      "prompt_tokens_details": {"cached_tokens": 80}},
            "timings": {"prompt_n": 10, "prompt_ms": 120.0, "predicted_n": 3,
                        "predicted_ms": 300.0}}
CFG = {"backend": "openai_compat", "base_url": "http://127.0.0.1:8088/v1", "model": "m",
       "max_tokens": 4096}


def _http_error(code, body):
    import io
    import urllib.error
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def test_openai_4xx_is_a_backend_error_and_5xx_is_not():
    """A rejected request (a prompt over the context window, as a real llama.cpp server
    answers it) is a BackendError the run can record; a server failure still raises."""
    import urllib.error

    def reject(url, payload, headers, timeout):
        raise _http_error(400, OVERFLOW_BODY)

    with pytest.raises(B.BackendError) as e:
        B.make_backend(CFG, transport=reject).chat(MSGS)
    assert e.value.status == 400 and "exceeds the available context size" in str(e.value)

    def crash(url, payload, headers, timeout):
        raise _http_error(500, b"boom")

    with pytest.raises(urllib.error.HTTPError):
        B.make_backend(CFG, transport=crash).chat(MSGS)


def test_openai_usage_keeps_cache_and_server_timings():
    r = B.make_backend(CFG, transport=lambda *a: OK_REPLY).chat(MSGS)
    assert r.usage["input_tokens"] == 90 and r.usage["cached_tokens"] == 80
    assert r.usage["server_prompt_n"] == 10 and r.usage["server_predicted_ms"] == 300.0


def test_llamacpp_preflight_caps_max_tokens_and_refuses_overflow():
    sent = []

    def transport(n_prompt):
        def t(url, payload, headers, timeout):
            sent.append(url)
            if url.endswith("/apply-template"):
                assert payload["messages"] and payload["chat_template_kwargs"]
                return {"prompt": "rendered"}
            if url.endswith("/tokenize"):
                assert payload == {"content": "rendered"}
                return {"tokens": list(range(n_prompt))}
            assert url.endswith("/v1/chat/completions")
            sent.append(payload["max_tokens"])
            return OK_REPLY
        return t

    cfg = {**CFG, "preflight": "llamacpp", "context_window": 32768,
           "extra": {"chat_template_kwargs": {"enable_thinking": False}}}
    r = B.make_backend(cfg, transport=transport(30000)).chat(MSGS)
    assert sent == ["http://127.0.0.1:8088/apply-template", "http://127.0.0.1:8088/tokenize",
                    "http://127.0.0.1:8088/v1/chat/completions", 2768]
    assert r.usage["preflight_prompt_tokens"] == 30000 and r.usage["max_tokens_sent"] == 2768

    sent.clear()
    with pytest.raises(B.BackendError) as e:
        B.make_backend(cfg, transport=transport(32600)).chat(MSGS)
    assert "not sent" in str(e.value)
    assert not any(isinstance(x, str) and x.endswith("/chat/completions") for x in sent)

    with pytest.raises(ValueError):
        B.make_backend({**CFG, "preflight": "llamacpp"})  # needs context_window
