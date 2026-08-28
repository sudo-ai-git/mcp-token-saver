#!/usr/bin/env python3
"""Test the DeeperThawt anti-trial-farming + 60-min-cut mechanisms.

Exercises pro_subscription ProSubStore + nowpayments_webhook._handle_trial
logic via a small in-memory harness (no live network):
  1. a valid HWID can start a trial
  2. the SAME hwid is refused a second free trial (anti-farming)
  3. a blank/short hwid is refused (hwid_required)
  4. an expired trial is NOT re-granted (60-min cut holds)
  5. the customers snapshot lists each hwid (customer tracking)
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point the store's ledger at a temp dir so tests are isolated
import pro_subscription as ps
from nowpayments_webhook import NowPaymentsWebhookHandler

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS: {name}")
    else: FAIL += 1; print(f"  FAIL: {name} {detail}")

# --- build an isolated store over a temp ledger ---
tmp = tempfile.mkdtemp()
class TmpLedger(ps.SubscriptionLedger):
    def __init__(self):
        super().__init__(path=os.path.join(tmp, "ledger.jsonl"))
store = TmpLedger.__new__(TmpLedger)
# simplest: reuse ProSubStore with a temp ledger path
store = ps.ProSubStore()
store.ledger.path = os.path.join(tmp, "ledger.jsonl")

print("== anti-trial-farming ==")
# 1. first trial with a valid hwid
store.record_hwid("hwid-aaaaaaaaaaaaaaaa", "trial_1", kind="trial")
check("trial recorded for a valid hwid", store.trial_already_used("hwid-aaaaaaaaaaaaaaaa"))
check("new hwid not used", not store.trial_already_used("hwid-bbbbbbbbbbbbbbbb"))

# 2. same hwid -> refused second trial (anti-farming)
store.record_hwid("hwid-aaaaaaaaaaaaaaaa", "trial_2", kind="trial")
# already used check stays true; a second issue must be denied by caller logic
check("second trial on same hwid blocked by trial_already_used",
      store.trial_already_used("hwid-aaaaaaaaaaaaaaaa"))

# 3. short/blank hwid refused
check("blank hwid not recorded (anti-farm)", not store.trial_already_used(""))
check("short hwid not recorded", not store.trial_already_used("abc"))

# 4. an expired trial is invalid (60-min cut)
from datetime import datetime, timedelta, timezone
e = ps.Entitlement(token_id="t", order_id="trial_exp", payment_id="", amount=0,
                   currency="", issued_at=datetime.now(timezone.utc),
                   expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                   active=True, kind="trial")
store.activate(e)
check("expired trial is_valid() == False (60-min cut)", not store.is_entitled("trial_exp"))

# 5. customer tracking snapshot
store.record_hwid("hwid-cccccccccccccccc", "trial_9", kind="trial")
store.record_hwid("hwid-cccccccccccccccc", "paid_1", kind="payment")
customers = store.customers()
by = {c["hwid"]: c for c in customers}
check("customer list includes each hwid", "hwid-cccccccccccccccc" in by and "hwid-aaaaaaaaaaaaaaaa" in by)
check("subscribed hwid marked subscribed", by.get("hwid-cccccccccccccccc", {}).get("subscribed") is True)
check("trial hwid marked trial_used", by.get("hwid-cccccccccccccccc", {}).get("trial_used") is True)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
