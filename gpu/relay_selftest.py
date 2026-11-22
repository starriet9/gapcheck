"""Relay self-test, offline: fake llama-server (SSE) <- gpu/relay.py <- the harness's
OpenAICompatBackend. gpu/session.py runs it in its preflight with the harness of the run
commit (PYTHONPATH=<worktree>/src); tests/gpu/test_gpu_relay.py runs it too.

Checks: the streamed reply is rebuilt into one chat.completion (text, two tool calls with
split arguments, finish reason, usage with cached tokens and server timings); the relay adds
the token; the request fields the client sends pass through; stale pooled connections are
retried on a fresh one; a 400 from the server reaches the harness as BackendError.
Run: uv run --locked python gpu/relay_selftest.py"""
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gapcheck import backends

KIT = Path(__file__).resolve().parent
SEEN = []


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    close_idle = False

    def finish(self):
        super().finish()

    def handle(self):
        if Fake.close_idle:
            self.close_connection = True
            self.handle_one_request()  # one request per connection, then close (like an idle timeout)
        else:
            super().handle()

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.path == "/apply-template":
            return self._json(200, {"prompt": "x" * 100})
        if self.path == "/tokenize":
            return self._json(200, {"tokens": list(range(len(body["content"]) // 2))})
        if body.get("model") == "too-long":
            return self._json(400, {"error": {"code": 400, "message": "exceeds context", "type": "exceed_context_size_error"}})
        assert body.get("stream") is True and body.get("return_progress") is True
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()

        def ev(o):
            d = ("data: " + json.dumps(o) + "\n\n").encode()
            self.wfile.write(b"%x\r\n" % len(d) + d + b"\r\n"); self.wfile.flush()
        base = {"id": "chatcmpl-1", "model": "m", "object": "chat.completion.chunk", "created": 1}
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "content": None}}],
            "prompt_progress": {"total": 100, "cache": 0, "processed": 50, "time_ms": 10}})
        time.sleep(0.05)
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"content": "Hel"}}]})
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"content": "lo"}}]})
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "prevent_risk", "arguments": "{\"age\": 6"}}]}}]})
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1, \"sources\": {\"a\": [1, 2]}}"}}]}}]})
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
            {"index": 1, "id": "call_b", "type": "function", "function": {"name": "search_notes", "arguments": "{\"query\": \"statin\"}"}}]}}]})
        ev({**base, "choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]})
        ev({**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                              "prompt_tokens_details": {"cached_tokens": 64}},
            "timings": {"prompt_n": 36, "prompt_ms": 12.5, "predicted_n": 20, "predicted_ms": 200.0}})
        d = b"data: [DONE]\n\n"
        self.wfile.write(b"%x\r\n" % len(d) + d + b"\r\n0\r\n\r\n"); self.wfile.flush()


def wait_port(port: int, seconds: float = 10) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError("relay did not start")


def run() -> str:
    SEEN.clear()
    Fake.close_idle = False
    fp, rp = free_port(), free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", fp), Fake); srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="relay_selftest_"))
    tok = tmp / "token"; tok.write_text("tok123\n")
    logp = tmp / "relay.log"
    pr = subprocess.Popen([sys.executable, "-I", str(KIT / "relay.py"), "--listen", f"127.0.0.1:{rp}",
                           "--upstream", f"http://127.0.0.1:{fp}", "--token-file", str(tok), "--log", str(logp)])
    try:
        wait_port(rp)
        cfg = {"backend": "openai_compat", "base_url": f"http://127.0.0.1:{rp}/v1", "model": "m",
               "temperature": 0, "seed": 1, "max_tokens": 4096, "timeout": 60,
               "preflight": "llamacpp", "context_window": 1000, "min_reply_tokens": 10,
               "extra": {"chat_template_kwargs": {"enable_thinking": False}, "parallel_tool_calls": True}}
        b = backends.make_backend(cfg)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        tools = [{"name": "prevent_risk", "description": "d", "parameters": {"type": "object"}}]
        for i in range(3):  # also exercises pooled connection reuse
            r = b.chat(msgs, tools)
        assert r.text == "Hello", r.text
        assert [t.name for t in r.tool_calls] == ["prevent_risk", "search_notes"]
        assert r.tool_calls[0].arguments == {"age": 61, "sources": {"a": [1, 2]}}, r.tool_calls[0].arguments
        assert r.tool_calls[0].id == "call_a" and r.tool_calls[1].id == "call_b"
        assert r.stop_reason == "tool_calls"
        assert r.usage["input_tokens"] == 100 and r.usage["output_tokens"] == 20
        assert r.usage["cached_tokens"] == 64 and r.usage["server_prompt_n"] == 36
        assert r.usage["preflight_prompt_tokens"] == 50 and r.usage["max_tokens_sent"] == 950
        chat_bodies = [s for s in SEEN if s["path"].endswith("/chat/completions")]
        assert all(s["auth"] == "Bearer tok123" for s in SEEN)
        assert chat_bodies[-1]["body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert chat_bodies[-1]["body"]["parallel_tool_calls"] is True
        assert chat_bodies[-1]["body"]["stream_options"] == {"include_usage": True}
        # stale pooled connections: the fake server drops every idle keep-alive connection
        Fake.close_idle = True
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(4) as ex:  # leaves 4 dead connections in the relay pool
            assert all(x.text == "Hello" for x in ex.map(lambda _: b.chat(msgs, tools), range(4)))
        time.sleep(0.2)
        for i in range(4):
            r = b.chat(msgs, tools)
            assert r.text == "Hello"
        Fake.close_idle = False
        cfg2 = dict(cfg, model="too-long", preflight=None)
        try:
            backends.make_backend(cfg2).chat(msgs, tools)
            raise AssertionError("expected BackendError")
        except backends.BackendError as e:
            assert e.status == 400 and "exceed" in str(e)
        lines = [json.loads(l) for l in logp.read_text().splitlines()]
        assert any(l.get("convert") and l.get("cached") == 64 for l in lines)
        return f"relay test OK: {len(SEEN)} upstream requests; {len(lines)} log lines"
    finally:
        pr.terminate()
        pr.wait()
        srv.shutdown()
        srv.server_close()
        tok.unlink(missing_ok=True)
        logp.unlink(missing_ok=True)
        tmp.rmdir()


def main():
    print(run())


if __name__ == "__main__":
    main()
