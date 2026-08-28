"""Tests for pro_subscription.py — deterministic, no real payments, no network."""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pro_subscription as ps

FAIL = 0

def check(cond, msg):
    global FAIL
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAIL += 1

def hmac_sign(secret, body):
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()

def make_payload(**over):
    p = {
        "payment_id": 123456,
        "payment_status": ps.STATUS_FINISHED,
        "order_id": "pro_1001",
        "price_amount": 250.0,
        "price_currency": "usd",
        "pay_currency": "btc",
        "actually_paid": 250.0,
        "pay_address": "bc1q...",
    }
    p.update(over)
    return json.dumps(p)  # deterministic body text

# --- isolation: temp ledger per run ---
def fresh():
    tmp = tempfile.mkdtemp()
    ledger_path = os.path.join(tmp, "pro_ledger.jsonl")
    return ps.ProSubStore(ledger=ps.SubscriptionLedger(path=ledger_path)), tmp

# ===================================================
print("=== create_payment stub path (injected post) ===")
store, tmp = fresh()
captured = {}
def fake_post(payload):
    captured["payload"] = payload
    return {"payment_id": 999, "payment_status": ps.STATUS_WAITING,
            "pay_address": "DA2iLS...", "pay_url": "https://nowpayments.io/payment/999"}
pay_obj = ps.NowPayments("test_key", "test_secret", post=fake_post)
res = pay_obj.create_payment(price_usd=250.0, coins=["btc", "eth", "xrp"])
check(res["payment_status"] == ps.STATUS_WAITING, "create_payment returns payment object")
check(captured["payload"]["price_amount"] == 250.0, "price forwarded")
check(captured["payload"]["price_currency"] == "usd", "currency usd")
check(captured["payload"]["pay_currency"] == "btc", "preferred coin btc")

print("=== HMAC webhook verification ===")
body_good = make_payload()
sig_good = hmac_sign("test_secret", body_good)
ent = ps.process_webhook(pay_obj, store, body_good, sig_good)
check(ent is not None and ent.active, "valid HMAC + finished -> activates entitlement")
check(store.is_entitled("pro_1001"), "order_id now entitled")

# bad signature
body_bad = make_payload()
sig_bad = hmac_sign("wrong_secret", body_bad)
try:
    ps.process_webhook(pay_obj, fresh()[0], body_bad, sig_bad)
    check(False, "bad HMAC must reject")
except ps.WebhookSignatureError:
    check(True, "bad HMAC rejected (no activation)")

# missing signature
try:
    ps.process_webhook(pay_obj, fresh()[0], body_good, "")
    check(False, "missing signature must reject")
except ps.WebhookSignatureError:
    check(True, "missing signature rejected")

# tampered body, original sig
sig_for_orig = hmac_sign("test_secret", make_payload())
tampered = make_payload(actually_paid=9999.0)  # attacker inflates amount
try:
    ps.process_webhook(pay_obj, fresh()[0], tampered, sig_for_orig)
    check(False, "tampered body with stale sig must reject")
except ps.WebhookSignatureError:
    check(True, "tampered body rejected (HMAC binds body)")

print("=== underpayment rejection (P2 amount gate) ===")
store2, _ = fresh()
body_low = make_payload(actually_paid=100.0, order_id="pro_2001", payment_id=222)
sig_low = hmac_sign("test_secret", body_low)
try:
    ps.process_webhook(pay_obj, store2, body_low, sig_low)
    check(False, "underpaid must reject")
except ps.PaymentError as e:
    check("underpaid" in str(e), f"underpaid rejected: {str(e)[:60]}")
check(not store2.is_entitled("pro_2001"), "underpaid order NOT entitled")

print("=== idempotency (duplicate webhook) ===")
body_d = make_payload(order_id="pro_3001", payment_id=333)
sig_d = hmac_sign("test_secret", body_d)
e1 = ps.process_webhook(pay_obj, store, body_d, sig_d)
try:
    ps.process_webhook(pay_obj, store, body_d, sig_d)
    check(False, "duplicate activation must not double-mint")
except ps.DuplicateWebhookError:
    check(True, "duplicate webhook -> DuplicateWebhookError (idempotent)")
# ledger has exactly ONE activation for that order
acts = [r for r in store.ledger.read() if r.get("kind") == "activation" and r.get("order_id") == "pro_3001"]
check(len(acts) == 1, "exactly one activation ledger entry")

print("=== non-activation statuses are not errors, no activation ===")
store3, _ = fresh()
body_waiting = make_payload(payment_status=ps.STATUS_WAITING, order_id="pro_4001", payment_id=444)
r = ps.process_webhook(pay_obj, store3, body_waiting, hmac_sign("test_secret", body_waiting))
check(r is None, "waiting status -> None, no activation")
check(not store3.is_entitled("pro_4001"), "waiting order not entitled")

print("=== ledger chain integrity + tamper detection ===")
store4, _ = fresh()
for i in range(3):
    body = make_payload(order_id=f"pro_500{i}", payment_id=5000+i, actually_paid=250.0)
    ps.process_webhook(pay_obj, store4, body, hmac_sign("test_secret", body))
check(store4.ledger.ledger_valid(), "3-entry ledger chain valid")
# tamper: flip an amount in place
rows = store4.ledger.read()
tampered_row = json.loads(json.dumps(rows[1]))
tampered_row["amount_paid"] = 0.01
# rewrite the whole ledger with the tampered row
with open(store4.ledger.path, "w") as f:
    for r in rows:
        if r.get("order_id") == tampered_row["order_id"]:
            f.write(json.dumps(tampered_row) + "\n")
        else:
            f.write(json.dumps(r) + "\n")
check(not store4.ledger.ledger_valid(), "tampered ledger detected (chain valid=False)")

print("=== file permission hygiene ===")
store5, tmp = fresh()
body = make_payload(order_id="pro_perm", payment_id=1)
ps.process_webhook(pay_obj, store5, body, hmac_sign("test_secret", body))
mode = oct(os.stat(store5.ledger.path).st_mode & 0o777)
check(mode == "0o600", f"ledger written 0600 (got {mode})")
# dir
dirmode = oct(os.stat(os.path.dirname(store5.ledger.path)).st_mode & 0o777)
check(dirmode <= "0o700", f"ledger dir at most 0700 (got {dirmode})")

print("=== entitlement expiry + gate ===")
store6, _ = fresh()
body = make_payload(order_id="pro_exp", payment_id=2)
ent = ps.process_webhook(pay_obj, store6, body, hmac_sign("test_secret", body))
from datetime import timedelta
check(ent.is_valid(), "fresh entitlement valid")
expired = ps.Entitlement(order_id="x", payment_id="p", token_id="t", amount=250.0,
                         currency="usd", issued_at=ent.issued_at - timedelta(days=31),
                         expires_at=ent.issued_at - timedelta(days=1))
check(not expired.is_valid(), "expired entitlement invalid")
# gate via require_pro with an explicitly-unwired store:
gstore, _ = fresh()
body = make_payload(order_id="pro_gate", payment_id=3)
ps.process_webhook(pay_obj, gstore, body, hmac_sign("test_secret", body))
from datetime import datetime, timezone
check(ps.require_pro("pro_gate", store=gstore, now=datetime.now(timezone.utc)),
      "require_pro True for entitled order")
check(not ps.require_pro("pro_none", store=gstore), "require_pro False for unknown order")

print("=== missing key/secret guard ===")
try:
    ps.NowPayments("", "s")
    check(False, "empty api_key must raise")
except ps.PaymentError:
    check(True, "empty api_key rejected")
try:
    ps.NowPayments("k", "")
    check(False, "empty ipn_secret must raise")
except ps.PaymentError:
    check(True, "empty ipn_secret rejected")

print("\n==================================================")
print(f"pro_subscription tests: {'ALL PASS' if FAIL==0 else str(FAIL)+' FAILED'}")
sys.exit(1 if FAIL else 0)
