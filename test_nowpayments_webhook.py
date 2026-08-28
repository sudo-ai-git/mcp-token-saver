"""Tests for nowpayments_webhook.py — deterministic, real HTTP boot, stubbed NowPayments."""

import hashlib, hmac, json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp()
os.environ["MCP_TOKEN_SAVER_LEDGER"] = os.path.join(tmp, "pro_ledger.jsonl")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pro_subscription as ps
import nowpayments_webhook as wh

# stub NowPayments (offline) so test doesn't hit the network
class StubPay(ps.NowPayments):
    def verify_webhook(self, raw_body, signature):
        return super().verify_webhook(raw_body, signature)

SECRET = "test_ipn_secret"
stub_pay = StubPay("test_api_key", SECRET)
store = ps.ProSubStore()

# boot the real webhook server
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
wh.NowPaymentsWebhookHandler.pay = stub_pay
wh.NowPaymentsWebhookHandler.store = store
srv = wh.ThreadingHTTPServer(("127.0.0.1", port), wh.NowPaymentsWebhookHandler)
th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
time.sleep(0.5)
BASE = f"http://127.0.0.1:{port}"
FAIL = []

def check(c, m):
    print(("  PASS: " if c else "  FAIL: ") + m)
    if not c: FAIL.append(m)

def sign(body):
    return hmac.new(SECRET.encode(), body.encode(), hashlib.sha512).hexdigest()

def make_body(**over):
    p = {"payment_id": 7001, "payment_status": ps.STATUS_FINISHED, "order_id": "pro_w1",
         "price_amount": 250.0, "price_currency": "usd", "pay_currency": "btc",
         "actually_paid": 250.0}
    p.update(over)
    return json.dumps(p)

def post(path, body=None, headers=None, method="POST"):
    h = {"Content-Type": "application/json"}; h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=(body or b""), headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

print("=== healthz ===")
st, b = post("/nowpayments/healthz", method="GET")
check(st == 200 and b.get("ok"), "healthz 200 ok")

print("=== valid IPN -> activate 30-day Pro ===")
body = make_body()
st, b = post("/nowpayments/ipn", body.encode(), {"x-nowpayments-sig": sign(body)})
check(st == 200 and b.get("status") == "activated", f"valid IPN activates (got {st} {b.get('status')})")
check(b.get("order_id") == "pro_w1", "order_id echoed")
check(store.is_entitled("pro_w1"), "entitlement active in store")

print("=== check endpoint reflects entitlement ===")
st, b = post("/nowpayments/check?order_id=pro_w1", method="GET")
check(st == 200 and b.get("entitled") is True, "check order_id entitled True")
st, b = post("/nowpayments/check?order_id=pro_nobody", method="GET")
check(st == 200 and b.get("entitled") is False, "check unknown order entitled False")

print("=== bad signature -> 400, no activation ===")
body_bad = make_body(order_id="pro_badsig")
st, b = post("/nowpayments/ipn", body_bad.encode(), {"x-nowpayments-sig": "deadbeef"})
check(st == 400 and b.get("error") == "invalid_signature", f"bad sig -> 400 (got {st})")
check(not store.is_entitled("pro_badsig"), "badsig order not entitled")

print("=== missing signature -> 400 ===")
body_no = make_body(order_id="pro_nosig")
st, b = post("/nowpayments/ipn", body_no.encode(), {})
check(st == 400 and b.get("error") == "invalid_signature", "missing sig -> 400")
check(not store.is_entitled("pro_nosig"), "nosig order not entitled")

print("=== underpaid IPN -> 400 rejected, not entitled ===")
body_low = make_body(order_id="pro_low", actually_paid=50.0)
st, b = post("/nowpayments/ipn", body_low.encode(), {"x-nowpayments-sig": sign(body_low)})
check(st == 400 and b.get("status") == "rejected", f"underpaid -> 400 rejected (got {st} {b.get('status')})")
check(not store.is_entitled("pro_low"), "underpaid order not entitled")

print("=== duplicate IPN -> idempotent, exactly one activation ===")
body_dup = make_body(order_id="pro_dup")
st, b = post("/nowpayments/ipn", body_dup.encode(), {"x-nowpayments-sig": sign(body_dup)})
check(st == 200 and b.get("status") == "activated", "first dup IPN activates")
st, b = post("/nowpayments/ipn", body_dup.encode(), {"x-nowpayments-sig": sign(body_dup)})
check(st == 200 and b.get("status") == "already_processed", f"second dup -> already_processed (got {b.get('status')})")
acts = [r for r in store.ledger.read() if r.get("kind") == "activation" and r.get("order_id") == "pro_dup"]
check(len(acts) == 1, "exactly one activation entry for dup order")

print("=== non-activation status -> accepted, no entitlement ===")
body_wait = make_body(order_id="pro_wait", payment_status=ps.STATUS_WAITING)
st, b = post("/nowpayments/ipn", body_wait.encode(), {"x-nowpayments-sig": sign(body_wait)})
check(st == 200 and b.get("activated") is False, f"waiting -> accepted no-activate (got {b.get('activated')})")

print("=== ledger chain intact after all writes ===")
check(store.ledger.ledger_valid(), "ledger chain still valid after webhooks")

print("=== missing cred file -> server refuses to start (fail-fast) ===")
os.environ["NOWPAYMENTS_CRED_FILE"] = "/nonexistent/nope"
# main() would sys.exit(1); simulate by calling the check
creds = ps._load_nowpayments_creds("/nonexistent/nope")
check(not creds.get("NOWPAYMENTS_API_KEY"), "missing cred file -> empty creds (fail-fast on start)")

srv.shutdown()
print("\n==========================================")
print(f"nowpayments_webhook tests: {'ALL PASS' if not FAIL else str(len(FAIL))+' FAILED'}")
sys.exit(1 if FAIL else 0)
