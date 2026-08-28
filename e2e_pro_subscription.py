"""E2E: boot the real pro_backend HTTP server, assert the subscription gate 402/200."""

import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request

# isolate ledger to a temp dir
tmp = tempfile.mkdtemp()
os.environ["MCP_TOKEN_SAVER_LEDGER"] = os.path.join(tmp, "pro_ledger.jsonl")
os.environ["MCP_TOKEN_SAVER_SUB"] = "true"  # enable subscription gate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pro_subscription as ps

# find a free port
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()

# boot the real server
proc = subprocess.Popen([sys.executable, "pro_backend.py", "--host", "127.0.0.1", "--port", str(port)],
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
base = f"http://127.0.0.1:{port}"
FAIL = []
def check(c, m):
    print(("  PASS: " if c else "  FAIL: ") + m); (FAIL if not c else None).append(m) if not c else None

try:
    # 1. healthz
    try:
        r = urllib.request.urlopen(base + "/healthz", timeout=5)
        check(r.status == 200, "healthz 200")
    except Exception as e:
        check(False, f"healthz: {e}")

    PAYLOAD = json.dumps({"tier": "hash", "messages": [
        {"role":"user","approx_tokens":10,"text":"a"*40,"sha":"abc123",
         "content_sha256":"abc123","content_length":40},
    ]}).encode()

    # 2. unentitled order -> 402 payment_required
    req = urllib.request.Request(base + "/assess", data=PAYLOAD,
        headers={"Content-Type":"application/json", "X-Order-Id":"pro_nobody"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        check(False, "unentitled order was NOT blocked (expected 402)")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        check(e.code == 402 and body.get("error") == "payment_required",
              f"unentitled -> 402 payment_required (got {e.code})")

    # 3. no subscription enabled? (default path still fine) — covered by unit tests
    # 4. entitled order -> 200 with result
    store = ps.ProSubStore()
    # mint via the real webhook-ish path
    ent = ps.Entitlement(order_id="pro_paid", payment_id="9", token_id="t", amount=250.0,
                         currency="usd", issued_at=ps.datetime.now(ps.timezone.utc),
                         expires_at=ps.datetime.now(ps.timezone.utc) + ps.timedelta(days=30))
    store.activate(ent)
    req = urllib.request.Request(base + "/assess", data=PAYLOAD,
        headers={"Content-Type":"application/json", "X-Order-Id":"pro_paid"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=5)
        body = json.loads(r.read())
        check(r.status == 200 and body.get("result", {}).get("tier") == "hash",
              f"entitled order -> 200 + result (status {r.status})")
    except urllib.error.HTTPError as e:
        check(False, f"entitled order blocked: {e.code} {e.read()[:100]}")
finally:
    proc.terminate(); proc.wait(timeout=5)

print("\n==========================================")
print(f"pro_backend subscription E2E: {'ALL PASS' if not FAIL else str(len(FAIL))+' FAILED'}")
sys.exit(1 if FAIL else 0)
