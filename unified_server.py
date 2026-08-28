#!/usr/bin/env python3
"""Unified mcp-token-saver-pro server — ONE port, ALL routes.

Serves on PORT (default 8080) and routes:
  /nowpayments/*  -> NowPayments webhook + payment create + trial (nowpayments_webhook)
  /assess, /healthz (Pro) -> the Pro assessment backend with the fail-closed
                            subscription gate (pro_backend)

Single-service on one port avoids Fly's dual-service host-routing ambiguity
(which made /assess vs /nowpayments 404 inconsistently). stdlib-only.
"""
import json, os, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Reuse the pro backend's assessment + gate logic
import pro_backend
# Reuse the webhook handler's routes (subclass merges both)
from nowpayments_webhook import NowPaymentsWebhookHandler


class UnifiedHandler(NowPaymentsWebhookHandler):
    server_version = "mcp-token-saver-pro-unified/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz" or path == "/pro/healthz":
            # Pro backend health
            self._send_json({"ok": True, "service": "mcp-token-saver-pro"})
            return
        # everything else goes to the webhook GET handler (nowpayments/healthz etc.)
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/assess":
            self._handle_assess()
            return
        # everything else goes to the webhook POST handler (nowpayments/...)
        super().do_POST()

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_assess(self) -> None:
        """Pro assessment route — fail-closed gate via pro_backend._require_auth."""
        if not pro_backend._require_auth(self):
            return  # _require_auth already sent the 402/401
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 5 * 1024 * 1024:
            self._send_json({"error": "payload_too_large"}, 413)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
        except Exception:
            self._send_json({"error": "invalid_json"}, 400)
            return
        rows = data.get("messages", [])
        tier = data.get("tier", "hash")
        if not isinstance(rows, list):
            self._send_json({"error": "bad_messages"}, 400)
            return
        try:
            if tier == "content":
                result = pro_backend._content_tier_compute(rows)
            else:
                result = pro_backend._hash_tier_proxy(rows)
            result["received_rows"] = len(rows)
            result["tier"] = tier
            self._send_json({"result": result})
        except Exception as e:
            self._send_json({"error": "compute_failed", "detail": str(e)[:120]}, 500)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), UnifiedHandler)
    print(f"[unified] mcp-token-saver-pro on 0.0.0.0:{port} "
          f"(/nowpayments/* + /assess + /healthz)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
