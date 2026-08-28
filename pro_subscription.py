"""mcp-token-saver PRO — crypto subscription / entitlement layer (NowPayments-backed).

Deterministic, no-LLM. Ties the paid Pro tier to a NowPayments-hosted crypto
subscription ("we accept all crypto"). Flow:

    1. create_subscription_request(?) -> calls NowPayments create_payment
       for a 30-day Pro token at a fixed price (USD, paid in the buyer's coin).
    2. Buyer pays from their own wallet via the returned payment URL / QR.
    3. NowPayments confirms on-chain and POSTs a webhook (HMAC-SHA512 of the
       raw body, keyed by the merchant IPN secret).
    4. verify_and_activate(webhook_json, raw_body, signature) -> HMAC-verifies,
       mints a 30-day entitlement token, appends to a private (0600) append-only
       ledger (hash-chained), returns the entitlement.
    5. is_entitled(?) / require_pro(...) -> the gate pro_backend consults.

CROWN-JEWEL-SAFE: no gematria / semantic method lives here. This is pure
billing/entitlement plumbing. The Pro *semantic* delta stays out of scope of
what a buyer can extract — the backend only ever returns numbers, never the
method.

KEY MATERIAL: this module NEVER holds or requests a wallet private key. It only
uses the merchant's public API key + IPN secret (both are non-custodial of the
user's funds; disbursement goes to the merchant's wallet address they configure
in NowPayments). No seed phrases ever.

Runtime deps: stdlib only (+ `requests` for the live payment call). The webhook
path and ledger are fully testable offline with a stub.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

PRO_TOKEN_LIFETIME_DAYS = 30
WIN_PRICE_USD = 250.00          # e.g. 30-day Pro tier (sane mid-tier price; overridable)
LEDGER_PATH = os.environ.get(
    "MCP_TOKEN_SAVER_LEDGER", "~/.mcp-token-saver/pro_ledger.jsonl"
).replace("~", os.path.expanduser("~"))

# NowPayments API endpoints (live; sandbox is same host with a test key)
API_BASE = "https://api.nowpayments.io/v1"
CREATE_PAYMENT_PATH = "/payment"
PAYMENT_STATUS_PATH = "/payment/{payment_id}"

# Secure credential store (0600, merchant-owned, never committed/logged).
# Path mirrors the established .nowpayments_key layout in the revenue workdir,
# overridable via env for tests.
CRED_FILE = os.environ.get(
    "NOWPAYMENTS_CRED_FILE", "~/.hermes/.nowpayments_key"
).replace("~", os.path.expanduser("~"))


def _load_nowpayments_creds(path: Optional[str] = None) -> Dict[str, str]:
    """Read NowPayments API key + IPN secret from a 0600 env-style file.

    Never logs or echoes the values. Returns {} if the file is missing.
    """
    p = path or CRED_FILE
    out: Dict[str, str] = {}
    if not os.path.exists(p):
        return out
    for line in open(p, "r", encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().upper()
        if k in ("NOWPAYMENTS_API_KEY", "NOWPAYMENTS_IPN_SECRET"):
            out[k] = v.strip()
    return out

# Payment statuses NowPayments reports (subset we act on)
STATUS_WAITING = "waiting"
STATUS_CONFIRMING = "confirming"
STATUS_CONFIRMED = "confirmed"
STATUS_SENDING = "sending"
STATUS_PARTIALLY_PAID = "partially_paid"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_REFUNDED = "refunded"
STATUS_EXPIRED = "expired"

ACTIVATION_STATUSES = {STATUS_CONFIRMED, STATUS_FINISHED}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PaymentError(Exception):
    pass


class WebhookSignatureError(PaymentError):
    """HMAC mismatch — reject the webhook, do NOT activate."""


class DuplicateWebhookError(PaymentError):
    """Already processed this payment id/order id — idempotent no-op."""


# ---------------------------------------------------------------------------
# Ledger (append-only, private, hash-chained)
# ---------------------------------------------------------------------------

class SubscriptionLedger:
    """Private (0600) append-only JSONL with a SHA-256 chain for tamper evidence.

    Each record links to the prior via prev_hash. Rotating the file's mode to
    0600 on every append; created 0600 on first write. No secrets stored — only
    public identifiers (payment id, order id, address, amount, token id).
    """

    def __init__(self, path: str = LEDGER_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        d = os.path.dirname(self.path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True, mode=0o700)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        else:
            os.chmod(self.path, 0o600)

    def _read_chain(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        rows: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    def _record_hash(self, record: Dict[str, Any]) -> str:
        blob = json.dumps(record, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _tail(self) -> Optional[Dict[str, Any]]:
        rows = self._read_chain()
        return rows[-1] if rows else None

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._ensure()
            tail = self._tail()
            prev = tail["hash"] if tail else None
            rec = dict(record)
            rec["prev_hash"] = prev
            rec["ts"] = datetime.now(timezone.utc).isoformat()
            rec["hash"] = self._record_hash({**rec, "prev_hash": prev})
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            return rec

    def ledger_valid(self) -> bool:
        """Verify the hash chain (first record may have prev_hash=None)."""
        rows = self._read_chain()
        prev = None
        for row in rows:
            if row.get("prev_hash") != prev:
                return False
            # recompute: hash over the record WITHOUT its own hash field + prev
            check = {k: v for k, v in row.items() if k != "hash"}
            if self._record_hash(check) != row.get("hash"):
                return False
            prev = row.get("hash")
        return True

    def read(self) -> List[Dict[str, Any]]:
        return self._read_chain()


# ---------------------------------------------------------------------------
# Entitlement tokens
# ---------------------------------------------------------------------------

@dataclass
class Entitlement:
    token_id: str
    order_id: str
    payment_id: str
    amount: float
    currency: str
    issued_at: datetime
    expires_at: datetime
    active: bool = True

    def is_valid(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if not self.active:
            return False
        return now < self.expires_at

    def to_json(self) -> Dict[str, Any]:
        return {
            "token_id": self.token_id,
            "order_id": self.order_id,
            "payment_id": self.payment_id,
            "amount": self.amount,
            "currency": self.currency,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "active": self.active,
        }


@dataclass
class ProSubStore:
    """In-memory + ledger-backed mapping of order_id -> Entitlement."""

    ledger: SubscriptionLedger = field(default_factory=SubscriptionLedger)
    _entitlements: Dict[str, Entitlement] = field(default_factory=dict)

    def _reload(self) -> None:
        for rec in self.ledger.read():
            if rec.get("kind") == "activation" and rec.get("order_id"):
                exp = rec.get("expires_at")
                try:
                    self._entitlements[rec["order_id"]] = Entitlement(
                        token_id=rec.get("token_id", ""),
                        order_id=rec["order_id"],
                        payment_id=rec.get("payment_id", ""),
                        amount=float(rec.get("amount_paid", 0.0)),
                        currency=rec.get("currency", ""),
                        issued_at=datetime.fromisoformat(rec.get("issued_at", "")),
                        expires_at=datetime.fromisoformat(exp) if exp else datetime.now(timezone.utc),
                        active=bool(rec.get("active", True)),
                    )
                except Exception:
                    continue

    def activate(self, ent: Entitlement) -> Entitlement:
        self.ledger.append({
            "kind": "activation",
            "token_id": ent.token_id,
            "order_id": ent.order_id,
            "payment_id": ent.payment_id,
            "amount_paid": ent.amount,
            "currency": ent.currency,
            "issued_at": ent.issued_at.isoformat(),
            "expires_at": ent.expires_at.isoformat(),
            "active": True,
        })
        self._entitlements[ent.order_id] = ent
        return ent

    def get(self, order_id: str) -> Optional[Entitlement]:
        if order_id not in self._entitlements:
            return None
        return self._entitlements[order_id]

    def is_entitled(self, order_id: str, now: Optional[datetime] = None) -> bool:
        e = self.get(order_id)
        return bool(e and e.is_valid(now))


# ---------------------------------------------------------------------------
# NowPayments client (HMAC webhook verify + payment creation)
# ---------------------------------------------------------------------------

class NowPayments:
    """Minimal host-gated client. Pass a `post`-able callable for tests."""

    def __init__(self, api_key: str, ipn_secret: str, *, post: Optional[Any] = None,
                 base: str = API_BASE) -> None:
        if not api_key:
            raise PaymentError("NowPayments API key required (get from your merchant account)")
        if not ipn_secret:
            raise PaymentError("NowPayments IPN secret required (set in merchant settings)")
        self.api_key = api_key
        self.ipn_secret = ipn_secret
        self.base = base
        self._post = post  # injectable (urllib/requests wrapper) for sandbox

    def create_payment(self, price_usd: float = WIN_PRICE_USD,
                       coins: Optional[List[str]] = None,
                       order_id: str = "") -> Dict[str, Any]:
        """Create a NowPayments invoice. Buyer pays any supported coin.

        Returns the NowPayments payment object (payment_id, payment_status,
        pay_address, pay_url...). `coins` optionally restricts the accepted
        set (e.g. ["btc","eth","xrp","usdttrc20","usdtsol","sol"]).
        """
        payload: Dict[str, Any] = {
            "price_amount": float(price_usd),
            "price_currency": "usd",
            "order_id": order_id or f"pro_{int(time.time()*1000)}",
            "ipn_callback_url": "https://yourhost.example/webhook/nowpayments",
        }
        if coins:
            payload["pay_currency"] = coins[0]  # preferred; NowPayments fills invoice
            if len(coins) > 1:
                payload["currencies"] = coins  # accepted set when multi
        if self._post is not None:
            return self._post(payload)
        # live path: requests (lazy import so stdlib-only offline tests pass)
        import requests
        r = requests.post(
            self.base + CREATE_PAYMENT_PATH,
            json=payload,
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code != 201:
            raise PaymentError(f"NowPayments create_payment failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def verify_webhook(self, raw_body: str, signature: str) -> Dict[str, Any]:
        """HMAC-SHA512 verify the NowPayments IPN body with our secret.

        Mismatch -> WebhookSignatureError (do NOT activate). Returns parsed JSON
        on valid signature. Caller then checks amount/status.
        """
        if not signature:
            raise WebhookSignatureError("missing IPN signature header")
        expected = hmac.new(self.ipn_secret.encode(), raw_body.encode(), hashlib.sha512).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise WebhookSignatureError("HMAC mismatch — reject")
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as e:
            raise PaymentError(f"invalid IPN JSON: {e}")

    @staticmethod
    def extract_pro_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Pull the fields we act on; returns {} if not a Pro order."""
        status = payload.get("payment_status")
        order_id = payload.get("order_id", "")
        payment_id = str(payload.get("payment_id", ""))
        amount = payload.get("actually_paid", payload.get("price_amount", 0))
        currency = payload.get("pay_currency", payload.get("price_currency", ""))
        # Only care if it's one of our Pro orders
        if not (order_id or payment_id):
            return {}
        return {
            "payment_id": payment_id,
            "order_id": order_id,
            "status": status,
            "amount_paid": float(amount or 0),
            "currency": currency,
            "pay_address": payload.get("pay_address", ""),
        }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_webhook(pay: NowPayments, store: ProSubStore, raw_body: str,
                    signature: str,
                    price_usd: float = WIN_PRICE_USD) -> Optional[Entitlement]:
    """Full pipeline: HMAC-verify -> check status/amount -> activate -> entropy.

    Returns an Entitlement on first activation; raises on reject; returns None
    for non-activation statuses (waiting/expired/failed) that aren't errors.
    """
    payload = pay.verify_webhook(raw_body, signature)
    fields = NowPayments.extract_pro_fields(payload)
    if not fields:
        return None  # not a Pro order — ignore, not an error

    # Idempotency: already processed?
    if store.get(fields["order_id"]) is not None:
        raise DuplicateWebhookError(f"order {fields['order_id']} already activated")

    status = fields["status"]
    if status in ACTIVATION_STATUSES:
        # Amount gate (P2): don't activate an underpayment. Allow small float tol.
        if fields["amount_paid"] < price_usd - 0.01:
            store.ledger.append({"kind": "reject_underpaid", **fields, "reason": "amount_paid_below_price"})
            raise PaymentError(
                f"underpaid: got {fields['amount_paid']} {fields['currency']}, need {price_usd} USD"
            )
        now = datetime.now(timezone.utc)
        ent = Entitlement(
            token_id=hashlib.sha256(f"{fields['payment_id']}:{fields['order_id']}".encode()).hexdigest()[:16],
            order_id=fields["order_id"],
            payment_id=fields["payment_id"],
            amount=fields["amount_paid"],
            currency=fields["currency"],
            issued_at=now,
            expires_at=now + timedelta(days=PRO_TOKEN_LIFETIME_DAYS),
        )
        return store.activate(ent)
    # not an activation status (waiting/expired/failed/refunded) — not an error
    store.ledger.append({"kind": "webhook_status", "status": status, **fields,
                         "note": "waiting/other status logged, no activation"})
    return None


# ---------------------------------------------------------------------------
# Gate helper for pro_backend
# ---------------------------------------------------------------------------

def require_pro(order_id: str, store: Optional[ProSubStore] = None,
                now: Optional[datetime] = None) -> bool:
    """The gate pro_backend calls before running a paid assessment.

    True only if order_id has an unexpired, active entitlement.
    """
    store = store or ProSubStore()
    store._reload()
    return store.is_entitled(order_id, now)


def __main___guard() -> None:
    # minimal CLI: show ledger health + entitlement for a given order id
    import sys
    if len(sys.argv) == 2 and sys.argv[1] in ("--validate-ledger",):
        store = ProSubStore()
        print(f"ledger path: {store.ledger.path}")
        print(f"chain valid: {store.ledger.ledger_valid()}")
        print(f"entries:     {len(store.ledger.read())}")
    elif len(sys.argv) == 2:
        store = ProSubStore(); store._reload()
        print(f"entitled(order={sys.argv[1]}): {store.is_entitled(sys.argv[1])}")
    else:
        print("usage: python3 pro_subscription.py --validate-ledger")
        print("       python3 pro_subscription.py <order_id>   # entitlement check")


if __name__ == "__main__":
    __main___guard()
