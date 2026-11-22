"""Fake llama-server for dry runs of gpu/session.py (no model, no GPU, no network).

Endpoints as llama-server answers them: /health, /props, /slots, /metrics, /apply-template,
/tokenize, /v1/chat/completions (SSE when stream=true, else one JSON). Bearer token checked
when --token-file is given (the relay must add it).

Scripted model, the same for every case (it never knows the case):
  - L1 (only prevent_risk offered): answers CANNOT_DETERMINE with no evidence; about one
    conversation in five first replies with text that is not JSON (one re-ask follows).
  - Agent: round 1 demographics, conditions by code, medications; round 2 six observation
    codes; round 3 a note search; from round 4 the same note search again (a repeated
    search), until the rounds-left notice or the limit prompt, then it answers. About one
    conversation in four first cites a row that does not exist (verifier check 1, one repair).
So a run with max_steps 20 and no notice hits the limit in every case, and a run with the
notice at 2 answers right after it.

--log-requests F: one JSON line per chat request with its sampling fields (model, temperature,
top_p, top_k, seed, max_tokens, stream), for tests of what the client sends.
--fail-503-text T: every chat request with T in a message answers HTTP 503 (a case whose
requests always fail, for the exhausted-attempts rule of gpu/queue_run.py).

Usage: fake_server.py --port P [--token-file F] [--np 8] [--ctx-per-slot 49152]
          [--delay 0.05] [--ready-after 0] [--template FILE] [--chars-per-token 2.3]
          [--fail-503-text T] [--log-requests F]
"""
import argparse
import hashlib
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

A = None
T0 = time.time()
LOG_LOCK = threading.Lock()
TOKEN = None
TEMPLATE = ""
ABSTAIN = {"status": "CANNOT_DETERMINE", "benefit_group": None, "risk_10y": None, "missing": [],
           "evidence": [], "note_for_clinician": "Dry run: scripted answer from the fake server."}
BAD_ROW = dict(ABSTAIN, evidence=[{"claim": "sbp", "support": "DOCUMENTED", "source": ["obs_doesnotexist"],
                                   "value": 120}])
CODES_RECORDS = ["22298006", "53741008", "44054006", "431855005", "433144002"]
OBS = ["2093-3", "2085-9", "18262-6", "8480-6", "72166-2", "33914-3"]


def render(body: dict) -> str:
    parts = []
    for m in body.get("messages") or []:
        parts.append(f"<|{m.get('role')}|>{m.get('content') or ''}")
        for c in m.get("tool_calls") or []:
            parts.append(json.dumps(c))
    parts.append(json.dumps(body.get("tools") or []))
    return "\n".join(parts)


def ntok(s: str) -> int:
    return math.ceil(len(s) / A.chars_per_token)


def h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def decide(body: dict) -> tuple[str, list]:
    """(content, tool_calls as OpenAI dicts) for one request."""
    msgs = body.get("messages") or []
    tools = {t["function"]["name"] for t in body.get("tools") or []}
    users = [m for m in msgs if m.get("role") == "user"]
    first_user = users[0]["content"] if users else ""
    last = msgs[-1] if msgs else {}
    answered_before = sum(1 for m in msgs if m.get("role") == "assistant" and not m.get("tool_calls"))
    if tools <= {"prevent_risk"}:  # L1
        if answered_before == 0 and h(first_user) % 5 == 0:
            return "I think this patient has a care gap.", []
        return json.dumps(ABSTAIN), []
    rounds = sum(1 for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    said = last.get("content") or "" if last.get("role") == "user" else ""
    demo = next((m.get("content") or "" for m in msgs if m.get("role") == "tool"), "")
    if said.startswith("The evidence checks failed"):
        return json.dumps(ABSTAIN), []
    if said.startswith(("Rounds of tool calls left", "The tool limit is reached", "Your reply is not")):
        return json.dumps(BAD_ROW if (answered_before == 0 and h(demo) % 4 == 0) else ABSTAIN), []
    if rounds == 0:
        calls = [("get_demographics", {}), ("search_conditions", {"codes": CODES_RECORDS}),
                 ("get_medications", {"active_only": True})]
    elif rounds == 1:
        calls = [("get_observations", {"code": c}) for c in OBS]
    else:
        calls = [("search_notes", {"query": "statin"})]
    return "", [{"id": f"call_{rounds}_{i}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj, ctype="application/json"):
        b = (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self) -> bool:
        if TOKEN and self.headers.get("Authorization") != "Bearer " + TOKEN:
            self._json(401, {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            if time.time() - T0 < A.ready_after:
                return self._json(503, {"error": {"code": 503, "message": "Loading model"}})
            return self._json(200, {"status": "ok"})
        if not self._auth():
            return
        if self.path == "/props":
            return self._json(200, {"chat_template": TEMPLATE, "total_slots": A.np,
                                    "build_info": "dry-fake-server", "model_path": "/models/fake.gguf",
                                    "default_generation_settings": {"params": {"temperature": 1.0, "seed": 1}}})
        if self.path == "/slots":
            return self._json(200, [{"id": i, "n_ctx": A.ctx_per_slot} for i in range(A.np)])
        if self.path == "/metrics":
            return self._json(200, b"# dry\n", "text/plain")
        return self._json(404, {"error": {"code": 404, "message": "not found"}})

    def do_POST(self):
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/apply-template":
            return self._json(200, {"prompt": render(body)})
        if self.path == "/tokenize":
            return self._json(200, {"tokens": [0] * ntok(body.get("content") or "")})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"code": 404, "message": "not found"}})
        if A.log_requests:
            with LOG_LOCK, open(A.log_requests, "a") as f:
                f.write(json.dumps({k: body.get(k) for k in ("model", "temperature", "top_p", "top_k", "seed",
                                                            "max_tokens", "stream")}) + "\n")
        if A.fail_503_text and any(A.fail_503_text in (m.get("content") or "") for m in body.get("messages") or []):
            return self._json(503, {"error": {"code": 503, "message": "dry: scripted failure", "type": "unavailable_error"}})
        p = ntok(render(body))
        if p + int(body.get("max_tokens") or 0) > A.ctx_per_slot:
            return self._json(400, {"error": {"code": 400, "message": "request exceeds the available context size",
                                              "type": "exceed_context_size_error"}})
        time.sleep(A.delay)
        content, calls = decide(body)
        last = (body.get("messages") or [{}])[-1]
        new = min(p, ntok(json.dumps(last)) + 8)
        gen = max(ntok(content) + 20 * len(calls), 1)
        usage = {"prompt_tokens": p, "completion_tokens": gen, "total_tokens": p + gen,
                 "prompt_tokens_details": {"cached_tokens": p - new}}
        timings = {"prompt_n": new, "prompt_ms": new / 5.0, "predicted_n": gen, "predicted_ms": gen * 10.0}
        finish = "tool_calls" if calls else "stop"
        base = {"id": "chatcmpl-dry", "model": body.get("model"), "object": "chat.completion.chunk",
                "created": int(time.time())}
        if not body.get("stream"):
            msg = {"role": "assistant", "content": content or None}
            if calls:
                msg["tool_calls"] = calls
            return self._json(200, {**base, "object": "chat.completion", "usage": usage, "timings": timings,
                                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def ev(o):
            d = ("data: " + (o if isinstance(o, str) else json.dumps(o)) + "\n\n").encode()
            self.wfile.write(b"%x\r\n" % len(d) + d + b"\r\n")
            self.wfile.flush()
        ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "content": None}}],
            "prompt_progress": {"total": p, "cache": p - new, "processed": p, "time_ms": 1}})
        for i in range(0, len(content), 40):
            ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"content": content[i:i + 40]}}]})
        for k, c in enumerate(calls):
            a = c["function"]["arguments"]
            ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                {"index": k, "id": c["id"], "type": "function", "function": {"name": c["function"]["name"],
                                                                             "arguments": a[:7]}}]}}]})
            ev({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                {"index": k, "function": {"arguments": a[7:]}}]}}]})
        ev({**base, "choices": [{"index": 0, "finish_reason": finish, "delta": {}}]})
        ev({**base, "choices": [], "usage": usage, "timings": timings})
        ev("[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main():
    global A, TOKEN, TEMPLATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token-file")
    ap.add_argument("--np", type=int, default=8)
    ap.add_argument("--ctx-per-slot", type=int, default=49152)
    ap.add_argument("--delay", type=float, default=0.05)
    ap.add_argument("--ready-after", type=float, default=0)
    ap.add_argument("--template")
    ap.add_argument("--chars-per-token", type=float, default=2.3)
    ap.add_argument("--fail-503-text")
    ap.add_argument("--log-requests")
    A = ap.parse_args()
    TOKEN = open(A.token_file).read().strip() if A.token_file else None
    TEMPLATE = open(A.template, encoding="utf-8").read() if A.template else ""
    srv = ThreadingHTTPServer(("127.0.0.1", A.port), H)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
