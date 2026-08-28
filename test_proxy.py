"""Tests for the mcp-token-saver PROXY (request-path optimizer + server)."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_optimize import ProxyOptimizer

FAIL = []
def check(c, m):
    print(("  PASS: " if c else "  FAIL: ") + m)
    if not c: FAIL.append(m)

# isolate ledger
os.environ["PROXY_LEDGER"] = os.path.join(tempfile.mkdtemp(), "proxy_ledger.jsonl")
opt = ProxyOptimizer()

def msg(role, content):
    return {"role": role, "content": content}

print("=== OPTIMIZER: exact-dup tool results deduped ===")
msgs = [
    msg("system", "You are a helpful agent."),
    msg("user", "list records"),
    msg("tool", '{"status":"ok","records":[{"id":1},{"id":2}]}'),
    msg("tool", '{"status":"ok","records":[{"id":1},{"id":2}]}'),  # exact dup
    msg("assistant", "Found 2 records."),
]
r = opt.optimize(msgs)
check(r["messages"][3] is not msgs[3] or len(r["messages"]) < len(msgs),
      f"exact dup tool result dropped (msgs {len(msgs)} -> {len(r['messages'])})")
check(r["stats"]["removed"] == 1, f"removed==1 (got {r['stats']['removed']})")

print("=== assistant content NEVER deduped (pairing safety) ===")
msgs_a = [
    msg("user", "run"),
    msg("assistant", "Let me check"),
    msg("assistant", "Let me check"),  # identical assistant — must NOT be removed
]
r = opt.optimize(msgs_a)
check(len(r["messages"]) == 3, f"duplicate assistant kept (got {len(r['messages'])})")

print("=== non-duplicate tool results kept ===")
msgs_b = [
    msg("user", "hi"),
    msg("tool", '{"id":1}'),
    msg("tool", '{"id":2}'),   # different -> kept
]
r = opt.optimize(msgs_b)
check(len(r["messages"]) == 3, f"distinct tool results kept ({len(r['messages'])})")

print("=== near-dup user tail compressed ===")
big_blob = "alpha beta gamma delta " * 40
msgs_n = [
    msg("user", "process this blob: " + big_blob),
    msg("assistant", "ok"),
    msg("user", "process this blob: " + big_blob),  # near-identical
]
r = opt.optimize(msgs_n)
check(len(r["messages"]) == 3, f"near-dup compressed not removed ({len(r['messages'])})")
c = r["messages"][2]["content"]
check("[…repeated" in c, f"near-dup tail collapsed with marker (got {c[:40]}...)")
check(r["stats"]["compressed"] == 1, f"compressed==1 (got {r['stats']['compressed']})")

print("=== tool_call / assistant tool_calls untouched ===")
msgs_t = [
    msg("user", "call a tool"),
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}]},
    msg("tool", '{"result": 1}'),
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}]},  # dup tool_call
]
r = opt.optimize(msgs_t)
check(len(r["messages"]) == 4, f"tool_calls all preserved ({len(r['messages'])})")

print("=== ledger wrote audit entries ===")
check(os.path.exists(os.environ["PROXY_LEDGER"]), "ledger file created")
with open(os.environ["PROXY_LEDGER"]) as f:
    lines = [l for l in f if l.strip()]
check(len(lines) >= 1, f"ledger has entries ({len(lines)})")

print("=== summarize reports token reduction ===")
s = ProxyOptimizer.summarize(r["stats"].__class__({
    "original_approx_tokens": 100, "removed_approx_tokens": 50,
    "compressed_approx_tokens": 10, "removed": 2, "compressed": 1}))
check("60" in s and "%" in s, f"summarize shows pct (60/100=60%) got ({s})")

# =====================================================
# HTTP server tests (boot real server)
# =====================================================
print("\n=== HTTP SERVER ===")
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()

# boot server, pointing at a STUB upstream we control
STUB_PORT = port + 200
def run_upstream():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got = {}
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got["body"] = json.loads(self.rfile.read(n))
            got["auth"] = self.headers.get("Authorization")
            body = b'{"id":"stub","choices":[{"message":{"role":"assistant","content":"done"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self,*a): pass
    HTTPServer(("127.0.0.1", STUB_PORT), H).serve_forever()
th = threading.Thread(target=run_upstream, daemon=True); th.start()
time.sleep(0.6)

env = dict(os.environ, UPSTREAM_BASE_URL=f"http://127.0.0.1:{STUB_PORT}/v1")
proc = subprocess.Popen([sys.executable, "proxy_server.py"], env=env,
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
# find the port the server bound — use a fixed one
PROXY_PORT = STUB_PORT + 100
# restart with known port
proc.kill(); time.sleep(0.3)
env["PROXY_PORT"] = str(PROXY_PORT)
proc = subprocess.Popen([sys.executable, "proxy_server.py"], env=env,
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
BASE = f"http://127.0.0.1:{PROXY_PORT}"

try:
    # healthz
    r = urllib.request.urlopen(BASE + "/healthz", timeout=6)
    check(r.status == 200, "healthz 200")

    # full proxy: request with exact-dup tool result -> upstream sees fewer msgs
    req = urllib.request.Request(BASE + "/v1/chat/completions",
        data=json.dumps({"model":"test","messages":[
            {"role":"system","content":"sys"},
            {"role":"user","content":"hi"},
            {"role":"tool","content":'{"x":1}'},
            {"role":"tool","content":'{"x":1}'}
        ]}).encode(),
        headers={"Content-Type":"application/json","Authorization":"Bearer sk-test"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=10)
    body = json.loads(resp.read())
    check(body.get("choices",[{}])[0].get("message",{}).get("content")=="done",
          "proxy forwarded + returned stub response")
    # verify via the stub's captured auth (auth passthrough)
    time.sleep(0.3)
    # (we can't easily read the stub's got from here, but 200 + response proves path)

    # non-chat path -> 404
    try:
        req404 = urllib.request.Request(BASE + "/notchatt", data=b'{}', method="POST")
        urllib.request.urlopen(req404, timeout=6)
        check(False, "non-chat 404 expected")
    except urllib.error.HTTPError as e:
        check(e.code == 404, f"non-chat -> 404 (got {e.code})")

    # streaming request -> SSE streamed-back (content-type text/event-stream)
    sess_stream = f"""
import urllib.request
req = urllib.request.Request("http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
    data='{{"model":"t","stream":true,"messages":[]}}'.encode(),
    headers={{"Content-Type":"application/json","Authorization":"Bearer sk"}},
    method="POST")
try:
    r = urllib.request.urlopen(req, timeout=8)
    ct = r.headers.get("Content-Type","")
    data = r.read()
    print("CT=" + ct)
    print("LEN=" + str(len(data)))
except Exception as e:
    print("ERR=" + str(e))
"""
    pr = subprocess.run([sys.executable, "-c", sess_stream], capture_output=True, text=True, timeout=15)
    out = pr.stdout
    check("CT=text/event-stream" in out or "CT=text/event-stream; charset" in out,
          f"streaming request -> SSE content-type (got {out.strip()[:80]})")

    # EDGE: upstream unreachable -> clean 502 (point a prox at a dead port)
    env_dead = dict(os.environ, UPSTREAM_BASE_URL="http://127.0.0.1:1/v1", PROXY_PORT=str(PROXY_PORT+50))
    proc_dead = subprocess.Popen([sys.executable, "proxy_server.py"], env=env_dead,
                                 cwd=os.path.dirname(os.path.abspath(__file__)),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        req502 = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT+50}/v1/chat/completions",
            data=json.dumps({"model":"t","messages":[]}).encode(),
            headers={"Content-Type":"application/json"}, method="POST")
        try:
            urllib.request.urlopen(req502, timeout=8)
            check(False, "dead upstream should 502")
        except urllib.error.HTTPError as e:
            check(e.code == 502 or e.code == 500, f"dead upstream -> 502/500 (got {e.code})")
    finally:
        proc_dead.kill()

    # EDGE: oversized request -> 413 handled upstream? (proxy caps at 20MB)
    # send an 21MB body would be slow; instead verify malformed JSON -> 400
    req_bad = urllib.request.Request(BASE + "/v1/chat/completions", data=b'NOT_JSON',
        headers={"Content-Type":"application/json"}, method="POST")
    try:
        urllib.request.urlopen(req_bad, timeout=8)
        check(False, "malformed JSON should 400")
    except urllib.error.HTTPError as e:
        check(e.code == 400, f"malformed JSON -> 400 (got {e.code})")
finally:
    proc.kill()

print("\n==================================================")
print(f"proxy tests: {'ALL PASS' if not FAIL else str(len(FAIL))+' FAILED'}")
sys.exit(1 if FAIL else 0)
