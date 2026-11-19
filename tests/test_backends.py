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
