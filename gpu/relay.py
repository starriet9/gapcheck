"""Local relay between the (unchanged) harness and llama-server behind the RunPod HTTPS proxy.

Why: the RunPod proxy (Cloudflare) closes a request with 524 when no response arrives within
100 s. A non-streaming chat completion with a long prompt or a long answer can exceed that.
The relay sends each non-streaming /v1/chat/completions request upstream as a streaming request
(stream=true, stream_options.include_usage, return_progress so bytes flow during prefill),
rebuilds the ordinary chat.completion JSON (content, reasoning_content, tool_calls,
finish_reason, usage incl. prompt_tokens_details.cached_tokens, timings) and returns it.
Every other request (/apply-template, /tokenize, /props, /health, /slots, /metrics) passes
through unchanged. The relay adds the llama-server API key; the harness sends none.

Usage: relay.py --listen 127.0.0.1:18080 --upstream https://<pod>-8080.proxy.runpod.net \
         --token-file secret/llama_api_key --log logs/relay.log [--no-stream]
"""
import argparse
import http.client
import json
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

A = None
TOKEN = ""
POOL: "queue.LifoQueue" = queue.LifoQueue()
LOG_LOCK = threading.Lock()
LOGF = None
SEQ = [0]


def log(d: dict):
    with LOG_LOCK:
        LOGF.write(json.dumps(d) + "\n")
        LOGF.flush()


def new_conn():
    u = urllib.parse.urlsplit(A.upstream)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=A.timeout)


def upstream_request(method, path, body, headers):
    """Send one request; returns (conn, response). Retries once on a stale pooled connection."""
    for attempt in (1, 2):
        conn, reused = None, False
        if attempt == 1:
            try:
                conn, reused = POOL.get_nowait(), True
            except queue.Empty:
                pass
        if conn is None:
            conn = new_conn()  # the retry always uses a fresh connection
        try:
            conn.request(method, path, body=body, headers=headers)
            return conn, conn.getresponse()
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                http.client.CannotSendRequest, http.client.BadStatusLine) as e:
            conn.close()
            if not reused or attempt == 2:
                raise
    raise RuntimeError("unreachable")


def rebuild(resp, t0, rid):
    """Read an SSE chat stream and rebuild a non-streaming chat.completion."""
    content, reasoning, tcs = [], [], {}
    finish = usage = timings = None
    meta = {}
    n_events = 0
    t_first = None
    err = None
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        n_events += 1
        if t_first is None:
            t_first = time.time()
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            err = {"error": {"message": "relay: bad SSE json", "data": data[:500]}}
            break
        if "error" in obj and not obj.get("choices"):
            err = obj
            break
        for k in ("id", "model", "created", "system_fingerprint"):
            if obj.get(k) is not None:
                meta[k] = obj[k]
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                i = tc.get("index", 0)
                t = tcs.setdefault(i, {"id": None, "type": "function",
                                       "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    t["id"] = tc["id"]
                f = tc.get("function") or {}
                if f.get("name"):
                    t["function"]["name"] += f["name"]
                if f.get("arguments"):
                    t["function"]["arguments"] += f["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
        if obj.get("usage"):
            usage = obj["usage"]
        if obj.get("timings"):
            timings = obj["timings"]
    if err is not None:
        return None, err, n_events, t_first
    msg = {"role": "assistant", "content": "".join(content) if content else None}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tcs:
        msg["tool_calls"] = [tcs[i] for i in sorted(tcs)]
    out = {"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
           "object": "chat.completion", "usage": usage or {}, **meta}
    if timings is not None:
        out["timings"] = timings
    out["relay"] = {"events": n_events, "first_event_s": None if t_first is None else round(t_first - t0, 3),
                    "rid": rid}
    return out, None, n_events, t_first


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        SEQ[0] += 1
        rid = SEQ[0]
        t0 = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        headers = {"Authorization": "Bearer " + TOKEN, "Accept-Encoding": "identity"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        path = self.path
        convert = False
        if method == "POST" and path.rstrip("/").endswith("/chat/completions") and not A.no_stream:
            try:
                req = json.loads(body)
            except Exception:
                req = None
            if isinstance(req, dict) and not req.get("stream"):
                req["stream"] = True
                req["stream_options"] = {"include_usage": True}
                req["return_progress"] = True
                body = json.dumps(req).encode()
                convert = True
                headers["Accept"] = "text/event-stream"
        try:
            conn, resp = upstream_request(method, path, body, headers)
        except Exception as e:
            log({"rid": rid, "t": t0, "path": path, "error": f"connect: {type(e).__name__}: {e}"})
            return self._send(502, json.dumps({"error": {"message": f"relay upstream: {e}"}}).encode())
        t_hdr = time.time()
        status = resp.status
        rec = {"rid": rid, "t": round(t0, 3), "path": path, "status": status, "convert": convert,
               "hdr_s": round(t_hdr - t0, 3)}
        try:
            if convert and status == 200:
                out, err, n_ev, t_first = rebuild(resp, t0, rid)
                resp.read()
                POOL.put(conn)
                rec.update(events=n_ev, total_s=round(time.time() - t0, 3))
                if err is not None:
                    rec["stream_error"] = json.dumps(err)[:500]
                    log(rec)
                    return self._send(500, json.dumps(err).encode())
                tm = out.get("timings") or {}
                rec.update(prompt_n=tm.get("prompt_n"), predicted_n=tm.get("predicted_n"),
                           cached=(out.get("usage") or {}).get("prompt_tokens_details", {}).get("cached_tokens"),
                           finish=out["choices"][0]["finish_reason"],
                           n_tool_calls=len(out["choices"][0]["message"].get("tool_calls") or []))
                log(rec)
                return self._send(200, json.dumps(out).encode())
            data = resp.read()
            POOL.put(conn)
            rec.update(total_s=round(time.time() - t0, 3), bytes=len(data))
            if status >= 400:
                rec["body"] = data[:300].decode(errors="replace")
            log(rec)
            return self._send(status, data, resp.getheader("Content-Type") or "application/json")
        except Exception as e:
            conn.close()
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["total_s"] = round(time.time() - t0, 3)
            log(rec)
            return self._send(502, json.dumps({"error": {"message": f"relay: {e}"}}).encode())

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")


def main():
    global A, TOKEN, LOGF
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:18080")
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--token-file")
    ap.add_argument("--log", required=True)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--no-stream", action="store_true")
    A = ap.parse_args()
    TOKEN = open(A.token_file).read().strip() if A.token_file else ""
    LOGF = open(A.log, "a")
    host, port = A.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), H)
    srv.daemon_threads = True
    log({"start": time.time(), "upstream_host": urllib.parse.urlsplit(A.upstream).hostname,
         "no_stream": A.no_stream})
    srv.serve_forever()


if __name__ == "__main__":
    main()
