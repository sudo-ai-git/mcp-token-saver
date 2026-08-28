#!/usr/bin/env python3
"""Activate Pro entitlements by POLLING NowPayments (GitHub Actions host).

Replaces the IPN webhook when a reachable public URL isn't available. A
scheduled GitHub Actions workflow runs this periodically; it:

  1. Lists NowPayments payments via the API (the same key that already works).
  2. For each payment whose `payment_status` is in activation set (confirmed/
     finished), verifies the amount, and mints a 30-day entitlement in the
     hash-chained 0600 ledger.
  3. Writes the updated ledger + a small state JSON that can be persisted back
     to the repo (actions/upload-artifact or git commit) so activation survives
     across runs.

Deterministic, no-LLM. Reuses pro_subscription's amount-gate + ledger. No
public URL required. Idempotent (a payment not in the ledger is activated once).

Env needed (GitHub Actions secrets):
  NOWPAYMENTS_API_KEY    (merchant API key — already have it)
  NOWPAYMENTS_PUBLIC_KEY (merchant public key)
  GITHUB_TOKEN           (auto-provided in Actions for committing the ledger)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pro_subscription as ps

API = ps.API_BASE + "/payment"


def api_get(key: str, path: str):
    req = urllib.request.Request(path, headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    api_key = os.environ.get("NOWPAYMENTS_API_KEY", "")
    if not api_key:
        print("NOWPAYMENTS_API_KEY missing"); return 1

    store = ps.ProSubStore()
    store._reload()  # load existing ledger (persisted across runs)

    # Paginate over payments. NowPayments /payment returns a list.
    # We only care about confirmed/finished payments for our Pro orders.
    page = 1
    activated = 0
    checked = 0
    while page <= 10:
        try:
            data = api_get(api_key, f"{API}?page={page}")
        except Exception as e:
            print(f"list error page {page}: {e}"); break
        payments = data.get("data") or []
        if not payments:
            break
        for p in payments:
            order_id = (p.get("order_id") or "").strip()
            status = p.get("payment_status") or ""
            # Only activation-status payments
            if status not in ps.ACTIVATION_STATUSES:
                continue
            amount = float(p.get("actually_paid") or p.get("price_amount") or 0)
            currency = p.get("pay_currency") or "usd"
            payment_id = str(p.get("payment_id") or "")
            # Skip if already activated
            if store.get(order_id) is not None:
                checked += 1
                continue
            # amount gate
            if amount < ps.WIN_PRICE_USD - 0.01:
                print(f"  underpaid order {order_id}: {amount} {currency} < {ps.WIN_PRICE_USD} USD")
                continue
            now = datetime.now(timezone.utc)
            ent = ps.Entitlement(
                token_id=__import__("hashlib").sha256(f"{payment_id}:{order_id}".encode()).hexdigest()[:16],
                order_id=order_id, payment_id=payment_id, amount=amount,
                currency=currency, issued_at=now,
                expires_at=now + timedelta(days=ps.PRO_TOKEN_LIFETIME_DAYS))
            store.activate(ent)
            activated += 1
            print(f"  ACTIVATED order={order_id} status={status} paid={amount} {currency}")
        page += 1
        if len(payments) < 1:
            break

    # Persist ledger for the next run: write it to the workspace so the
    # workflow can commit or upload it.
    ledger_path = store.ledger.path
    out_dir = os.environ.get("LEDGER_OUT", ".")
    import shutil
    shutil.copy(ledger_path, os.path.join(out_dir, "pro_ledger.jsonl")) if os.path.exists(ledger_path) else None

    print(f"done: {activated} activated, {checked} already-valid, ledger at {store.ledger.path} -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
