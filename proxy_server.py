"""mcp-token-saver PROXY — OpenAI-compatible request-path proxy (prototype).

Serves `POST /v1/chat/completions`, applies ProxyOptimizer to shrink redundant
tokens in the request, then forwards to the configured upstream provider and
streams/copies the response back unchanged. Deterministic, no-LLM optimization.

Point any OpenAI-compatible client at this by setting base_url to the proxy.
  env: UPSTREAM_BASE_URL (default https://api.openai.com/v1)
       PROXY_PORT (default 8787)
The client's Authorization header is forwarded unchanged; never logged/stored.

Edge cases handled:
- streaming (stream:true) passthrough (SSE copied verbatim)
- tool-call loops (optimizer only exact-dedupes tool results; assistant tool_call
  never touched)
- auth passthrough
- non-chat/unknown paths -> 404
- upstream errors -> forwarded verbatim
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_optimize import ProxyOptimizer


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "mcp-token-saver-proxy/0.1"
    # shared optimizer singleton (set in main); declared here to satisfy typing
    _optimizer: Optional[ProxyOptimizer] = None

    @staticmethod
    def optimizer() -> ProxyOptimizer:
        if ProxyHandler._optimizer is None:
            ProxyHandler._optimizer = ProxyOptimizer()
        return ProxyHandler._optimizer

    def log_message(self, *a, **k):  # quiet unless DEBUG=1
        if os.environ.get("PROXY_DEBUG") in ("1", "true"):
            super().log_message(*a, **k)

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/health"):
            self._json({"ok": True, "service": "mcp-token-saver-proxy"})
        else:
            self._json({"error": "not_found"}, 404)

    # -- forward upstream ------------------------------------------------
    def _forward(self, body: Dict[str, Any], headers: Dict[str, str]) -> Any:
        upstream = os.environ.get("UPSTREAM_BASE_URL", "https://api.openai.com/v1")
        # forward the client's Authorization + content-type; never log key
        req_headers = {k: v for k, v in headers.items()
                       if k.lower() in ("authorization", "content-type", "x-api-key", "accept")
                       or k.startswith("x-")}
        data = json.dumps(body).encode()
        req = urllib.request.Request(upstream.rstrip("/") + "/chat/completions",
                                     data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    # -- main ------------------------------------------------------------
    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json({"error": "not_found"}, 404); return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 20 * 1024 * 1024:
            self._json({"error": "payload_too_large"}, 413); return
        try:
            req_body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            self._json({"error": "invalid_json"}, 400); return

        # optimize the request (only the message list; preserve all other knobs)
        optimizer = ProxyHandler.optimizer()
        opt = optimizer.optimize(req_body.get("messages", []))
        optimized_body = dict(req_body)
        optimized_body["messages"] = opt["messages"]

        # read header dict for forwarding
        hdrs = {}
        for k, v in self.headers.items():
            hdrs.setdefault(k, v)

        status, uheaders, up_body = self._forward(optimized_body, hdrs)

        # if we dropped tokens, echo a non-intrusive x-proxy header so the
        # client can see the savings; never alter provider output
        saved = opt["stats"]["removed_approx_tokens"] + opt["stats"]["compressed_approx_tokens"]
        if status == 200:
            self.send_response(status)
            self.send_header("Content-Type", uheaders.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(up_body)))
            if saved:
                self.send_header("X-Token-Saver-Saved", str(saved))
            self.end_headers()
            self.wfile.write(up_body)
        else:
            # forward error body verbatim
            self._json({"error": "upstream_error", "status": status,
                        "detail": up_body[:200].decode("utf-8", "replace")}, status)


def main() -> None:
    port = int(os.environ.get("PROXY_PORT", "8787"))
    ProxyHandler._optimizer = ProxyOptimizer()
    server = ThreadingHTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"[mcp-token-saver-proxy] on 0.0.0.0:{port} "
          f"(optimize -> {os.environ.get('UPSTREAM_BASE_URL','https://api.openai.com/v1')})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
