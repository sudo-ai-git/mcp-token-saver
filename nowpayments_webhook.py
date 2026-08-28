"""mcp-token-saver PRO — NowPayments IPN webhook receiver.

Deterministic, no-LLM. The public endpoint you set as ``ipn_callback_url`` in
NowPayments. When a buyer pays the Pro invoice, NowPayments POSTs the IPN here:

    POST /nowpayments/ipn
    header x-nowpayments-sig: HMAC-SHA512(body, IPN_SECRET) hex
    body: raw JSON (payment_status, order_id, payment_id, actually_paid, ...)

We HMAC-verify with the merchant IPN secret (from the 0600 store), then hand the
body to pro_subscription.process_webhook, which:
  - rejects on bad/missing signature (WebhookSignatureError)
  - rejects underpayment (P2 amount gate)
  - is idempotent (duplicate IPNs don't double-mint)
  - mints a 30-day entitlement + appends the hash-chained 0600 ledger
On success we return 200 so NowPayments stops retrying. Non-200 => it retries.

Also exposes:
  GET /nowpayments/healthz        -> liveness
  GET /nowpayments/check?order_id= -> entitlement status for the given order

Usage:
  python3 nowpayments_webhook.py --host 0.0.0.0 --port 9501
Requires creds at ~/.hermes/.nowpayments_key (see pro_subscription.CRED_FILE).

CROWN-JEWEL-SAFE: loads the semantic method nowhere; only billing plumbing.
No wallet keys ever handled — only merchant API key/IPN secret.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pro_subscription as ps

# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

class NowPaymentsWebhookHandler(BaseHTTPRequestHandler):
    server_version = "mcp-token-saver-nowpayments/1.0"

    # Injected singletons (set by the server main) so tests can substitute.
    pay: "ps.NowPayments | None" = None
    store: "ps.ProSubStore | None" = None

    def log_message(self, *a, **k):
        # quiet unless LOG=1
        if os.environ.get("NOWPAYMENTS_WEBHOOK_LOG") in ("1", "true"):
            super().log_message(*a, **k)

    # -- helpers ----------------------------------------------------------
    def _send(self, obj: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_store(self) -> "ps.ProSubStore":
        if NowPaymentsWebhookHandler.store is not None:
            return NowPaymentsWebhookHandler.store
        s = ps.ProSubStore()
        s._reload()
        return s

    def _get_pay(self) -> "ps.NowPayments":
        if NowPaymentsWebhookHandler.pay is not None:
            return NowPaymentsWebhookHandler.pay
        creds = ps._load_nowpayments_creds(ps.CRED_FILE)
        return ps.NowPayments(
            api_key=creds.get("NOWPAYMENTS_API_KEY", ""),
            ipn_secret=creds.get("NOWPAYMENTS_IPN_SECRET", ""),
        )

    # -- GET --------------------------------------------------------------
    def do_GET(self) -> None:
        if self.path.startswith("/nowpayments/healthz"):
            self._send({"ok": True, "service": "nowpayments-webhook",
                        "ledger_valid": self._get_store().ledger.ledger_valid()})
            return
        if self.path.startswith("/nowpayments/check"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            order_id = (q.get("order_id") or [""])[0]
            if not order_id:
                self._send({"error": "order_id required"}, 400); return
            store = self._get_store()
            ent = store.get(order_id)
            if ent is None:
                self._send({"order_id": order_id, "entitled": False}); return
            self._send({"order_id": order_id, "entitled": ent.is_valid(),
                        "expires_at": ent.expires_at.isoformat()})
            return
        self._send({"error": "not_found"}, 404)

    # -- POST (IPN) -------------------------------------------------------
    def do_POST(self) -> None:
        if self.path != "/nowpayments/ipn":
            self._send({"error": "not_found"}, 404); return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 1 * 1024 * 1024:
            self._send({"error": "payload_too_large"}, 413); return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        sig = self.headers.get("x-nowpayments-sig", "") or self.headers.get("X-Nowpayments-Sig", "")
        ent: Optional[ps.Entitlement] = None

        try:
            pay = self._get_pay()
            ent = ps.process_webhook(pay, self._get_store(), raw, sig)
        except ps.WebhookSignatureError as e:
            # Invalid signature: do NOT activate; 200 to stop retries? NO —
            # return 401 so NowPayments flags it, but do NOT retry-loop on a
            # bad sig (it won't fix itself). Log + 400 to stop retries.
            self._send({"error": "invalid_signature"}, 400)
            return
        except ps.DuplicateWebhookError:
            # Already processed — idempotent OK, 200 stops retries.
            self._send({"status": "already_processed", "activated": True}); return
        except ps.PaymentError as e:
            # underpaid / malformed — do not activate; 400 stops NowPayments retry spam
            self._send({"status": "rejected", "reason": str(e)[:120]}, 400)
            return
        except Exception as e:  # defensive
            self._send({"status": "error", "detail": str(e)[:120]}, 500)

        if ent is not None:
            self._send({"status": "activated", "order_id": ent.order_id,
                        "token_id": ent.token_id, "expires_at": ent.expires_at.isoformat()})
        else:
            # waiting/expired/failed status — not an activation, not an error
            self._send({"status": "accepted", "activated": False,
                        "note": "status not an activation (waiting/expired/etc)"})


def main() -> None:
    parser = argparse.ArgumentParser(description="mcp-token-saver PRO NowPayments IPN webhook")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9501)
    args = parser.parse_args()

    # load creds to fail-fast if missing (don't start a broken server)
    creds = ps._load_nowpayments_creds(ps.CRED_FILE)
    if not creds.get("NOWPAYMENTS_API_KEY"):
        print("ERROR: NOWPAYMENTS_API_KEY not found in cred store — webhook can't verify without it", file=sys.stderr)
        sys.exit(1)

    server = ThreadingHTTPServer((args.host, args.port), NowPaymentsWebhookHandler)
    print(f"[nowpayments-webhook] listening on {args.host}:{args.port} "
          f"(set this as NowPayments ipn_callback_url + add /nowpayments/ipn)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
