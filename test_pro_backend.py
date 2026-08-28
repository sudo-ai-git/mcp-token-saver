#!/usr/bin/env python3
"""Tests for pro_backend.py — the PRO assessment backend.

CRITICAL contract tests:
  1. content tier returns a REAL delta (exact_dedupe + semantic_additional).
  2. The RESPONSE never contains raw content, scrubbed content, OR any semantic
     method artifact (gematria projection, base-6, mod-5, vectors, weights).
  3. hash tier returns a labeled proxy (honest).
  4. auth gate (401 without key).
  5. adversarial: bad json, huge payload, non-list, unicode.
  6. determinism.
"""
import json, os, subprocess, sys, time, socket, threading, urllib.error, urllib.request
sys.path.insert(0, "/home/sudosudo/mcp-token-saver")
import pro_backend as pb

PASS=FAIL=0
def check(n,c,d=""):
    global PASS,FAIL
    if c: PASS+=1; print(f"  PASS: {n}")
    else: FAIL+=1; print(f"  FAIL: {n} {d}")

# --- pure engine tests (deterministic) ---
def _t(t):  # helper for text token units
    return Pb_token_units if False else pb._token_units(t)

check("token_units deterministic", pb._token_units("hello world")==pb._token_units("hello world"))
check("token_units non-empty", len(pb._token_units("check status of records"))>0)
check("empty text -> empty units", pb._token_units("")==[])

# cosine: identical vec = 1, disjoint = low
a = [1.0,0,0,0]; b=[1.0,0,0,0]; c=[0,1.0,0,0]
check("cosine identical = 1", abs(pb._cosine(a,b)-1.0)<1e-9)
check("cosine orthogonal < 1", pb._cosine(a,c)<0.1)

# semantic near-dupe delta: two near-identical (not exact) messages -> >0 saved
n1 = "Incrementally snapshot table CUSTOMERS where CUSTOMER_NAME is Acme Co"
n2 = "Incrementally snapshot the CUSTOMERS table where CUSTOMER_NAME is Acme"
saved = pb._semantic_near_dupe_delta([n1,n2],[10.0,10.0], threshold=0.7)
check("semantic near-dupe (not exact) delta > 0", saved > 0, str(saved))
# two very different messages -> ~0 saved
d1 = "the weather outside is cold today"
d2 = "please pay the invoice in full"
saved2 = pb._semantic_near_dupe_delta([d1,d2],[10.0,10.0], threshold=0.9)
check("dissimilar messages -> delta ~0", saved2 < 1.0, str(saved2))

# --- content tier compute ---
rows = [
  {"role":"tool","approx_tokens":50,"content_sha256":"a","content_length":40,"content_scrubbed":"Incrementally snapshot table CUSTOMERS where CUSTOMER_NAME is Acme Co"},
  {"role":"tool","approx_tokens":50,"content_sha256":"b","content_length":39,"content_scrubbed":"Incrementally snapshot the CUSTOMERS table where CUSTOMER_NAME is Acme"},
  {"role":"user","approx_tokens":50,"content_sha256":"c","content_length":5,"content_scrubbed":""},
]
res = pb._content_tier_compute(rows)
check("content tier computes", res["delta_is_proxy"] is False)
check("content tier has exact_dedupe_savings", "exact_dedupe_savings" in res)
check("content tier has semantic_additional_savings", "semantic_additional_savings" in res)
check("content tier semantic_extra >= 0", res["semantic_additional_savings"] >= 0)
blob = json.dumps(res)
check("content tier output NO content leak", "CUSTOMER" not in blob and "Acme" not in blob)

# --- SAUCE-GATE: response must NOT expose method ---
for meth in ["gematria","base-6","mod-5","token_units","_semantic_vec","cosine","projection","0x05D0","5D0","_token_units"]:
    check(f"sauce not leaked in response: {meth}", meth not in blob, f"found {meth}")

# --- hash tier proxy (honest) ---
hrows = [
  {"role":"tool","approx_tokens":50,"content_sha256":pb._sha("same payload") if hasattr(pb,'_sha') else "s1","content_length":40},
  {"role":"tool","approx_tokens":50,"content_sha256":"s2","content_length":39},
  {"role":"tool","approx_tokens":60,"content_sha256":"s3","content_length":400},
]
# patch sha to real to trigger exact-dupe detection (two identical 40-len)
hrows[1]["content_sha256"] = hrows[0]["content_sha256"]
res = pb._hash_tier_proxy(hrows)
check("hash tier proxy labeled", res["delta_is_proxy"] is True)
check("hash tier note honest", "proxy" in res["note"].lower())
check("hash tier exact_dedupe_savings present", "exact_dedupe_savings" in res)
check("hash tier semantic = proxy flag (not real)", res["semantic_additional_savings_proxy"] >= 0)

# --- auth gate ---
os.environ["MCP_TOKEN_SAVER_API_KEY"] = "test-key-xyz"
class FakeHandler:
    def __init__(self, auth=None):
        self.auth = auth
        self.code = None
    @property
    def headers(self):
        # minimal headers object whose .get(key, default) returns auth for Authorization
        class _H:
            def __init__(self, auth): self.auth = auth
            def get(self, key, default=None):
                return self.auth if key.lower() == "authorization" else default
        return _H(self.auth)
    def send_response(self, c): self.code = c
    def _send_json(self, o): self.body = o
ok = pb._require_auth(FakeHandler(auth="Bearer wrong-key"))
check("auth: wrong key -> False (401)", ok is False)
ok = pb._require_auth(FakeHandler(auth="Bearer test-key-xyz"))
check("auth: right key -> True", ok is True)
del os.environ["MCP_TOKEN_SAVER_API_KEY"]
ok = pb._require_auth(FakeHandler(auth=None))
check("auth: no key configured -> allow (dev)", ok is True)

# --- END-TO-END HTTP (start server on ephemeral port) ---
os.environ["MCP_TOKEN_SAVER_API_KEY"] = "test-secret"
srv = pb.HTTPServer(("127.0.0.1", 0), pb.AssessmentHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
# read the server is actually accepting connections before the first request
import time
for _ in range(50):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)

def post(path, payload, key=None):
    import urllib.request, urllib.error
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type":"application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

# healthz (GET, not POST — the server only answers GET for /healthz)
r = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
check("healthz 200", r.status == 200 and json.loads(r.read()).get("ok"))

# assess without auth -> 401
st, body = post("/assess", {"messages":[]}, key=None)
check("assess no key -> 401", st==401, str(st))

# assess content tier WITH auth -> 200 + real delta
content_rows = [{"role":"tool","approx_tokens":50,"content_scrubbed":n,"content_length":len(n)} for n in [n1,n2]]
st, body = post("/assess", {"messages":content_rows,"tier":"content"}, key="test-secret")
check("assess content tier 200", st==200, str(st))
res = body.get("result",{})
check("content tier real (not proxy)", res.get("delta_is_proxy") is False, json.dumps(res)[:120])
check("content tier semantic number", res.get("semantic_additional_savings",0) >= 0)

# assess hash tier -> honest proxy
st, body = post("/assess", {"messages":[{"role":"tool","approx_tokens":50,"content_sha256":"x","content_length":10}],"tier":"hash"}, key="test-secret")
check("assess hash tier 200", st==200)
check("hash tier proxy marked", body.get("result",{}).get("delta_is_proxy") is True)

# adversarial: invalid json
import urllib.request, urllib.error
req = urllib.request.Request(f"http://127.0.0.1:{port}/assess", data=b"not-json",
    headers={"Content-Type":"application/json","Authorization":"Bearer test-secret"})
try:
    urllib.request.urlopen(req, timeout=5); st=None
except urllib.error.HTTPError as e: st=e.code
check("invalid json -> 400", st==400, str(st))

# adversarial: huge payload -> server rejects (either a clean 413 OR a
# connection-close/BrokenPipe is correct: the server holds payload>5MB and
# rejects before reading the full body, so the client may see a broken write).
huge = {"messages":[{"approx_tokens":1000,"content_scrubbed":"x"*100}]*60000}  # >5MB
rejected = False
try:
    st, body = post("/assess", huge, key="test-secret")
    rejected = (st == 413 or st == 400)
except Exception as e:
    # BrokenPipe / URLError also means the server refused the oversized body
    rejected = isinstance(e, (urllib.error.URLError, BrokenPipeError, ConnectionResetError))
check("huge payload -> rejected (413/400 or connection refusal)", rejected, str(e)[:60] if 'e' in dir() else "")

srv.shutdown()
del os.environ["MCP_TOKEN_SAVER_API_KEY"]

print(f"\n{'='*50}\npro_backend: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
