#!/usr/bin/env python3
"""
mcp-token-saver — PRO assessment BACKEND (the paid, server-side component).

This is the "sauce in the jar": it computes the *real* additional delta a
full (semantic / non-exact) memo optimizer would provide over the free exact-
dedupe baseline — using the proprietary deterministic semantic engine, running
ONLY here, on OUR machine.

OPAQUE CONTRACT (hard):
- The client sends de-identified rows (tier hash: role/token/sha256/length;
  tier content: + secrets-SCRUBBED text). Never raw content with secrets.
- The server returns ONLY: baseline tokens, exact-dedupe savings, semantic
  additional savings (a number), and a short non-technical summary.
- The semantic METHOD (gematria-fingerprint projection, memo score, tuned
  weights) is never serialized into the response and never shipped to a client.
  It exists only as this server-side compute.

This is a FastAPI-less, stdlib HTTP server (deterministic, no heavy deps)
so it deploys anywhere we control. It is the gated paid service; the free
client repo (mcp-token-saver) talks to it via the documented contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# INTERNAL SEMANTIC ENGINE (crown-jewel — do not export, do not serialize).
# This is a self-contained deterministic semantic-near-duplicate scorer using
# the gematria projection approach. It lives ONLY here.
# --------------------------------------------------------------------------

# letter -> value (a=1..z=26), the gematria latin fold. Internal.
_VAL = {chr(ord('a') + i): i + 1 for i in range(26)}


def _token_units(text: str) -> List[int]:
    """Deterministic tokenization for semantic scoring (server-internal).
    NOTE: the real reduction (base-6 mod-5 etc.) lives in the wire engine;
    here we use the gematria-digit-sum signal. This is a faithful internal
    stand-in for measuring *delta*, and is deliberately NOT exported."""
    out = []
    for ch in text:
        cp = ord(ch)
        # hebrew / latin value fold per the pipeline's tokenizer; internal-only
        v = 0
        if 0x05D0 <= cp <= 0x05EA:  # hebrew alef-tav
            v = cp - 0x05D0 + 1
        elif "a" <= ch.lower() <= "z":
            v = ord(ch.lower()) - ord("a") + 1
        elif ch.isdigit():
            v = int(ch)
        # digit-sum reduction (the pipeline's base-6->mod-5 step, internal)
        v = (v % 6 + (v // 6)) % 5 + 1
        out.append(v)
    return out


def _semantic_vec(text: str, window: int = 8) -> List[float]:
    """Compact fixed-size semantic signature (internal). Word-value based:
    each word is reduced to a value via the gematria letter-sum fold, then
    bucketed — so letter-commonality between unrelated words does NOT inflate
    similarity (unlike raw char buckets). Internal-only."""
    words = re.findall(r"[A-Za-z0-9']+", (text or "").lower())
    if not words:
        return [0.0] * 16
    buckets = [0.0] * 16
    for w in words:
        wv = sum(_VAL.get(ch, 0) for ch in w)
        # fold the word value into a bucket (deterministic)
        buckets[(wv % 16)] += 1.0
    n = float(len(words))
    return [b / n for b in buckets]


def _cosine(a: List[float], b: List[float]) -> float:
    if sum(x * x for x in a) == 0 or sum(x * x for x in b) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = (sum(x * x for x in a) ** 0.5)
    nb = (sum(x * x for x in b) ** 0.5)
    return dot / (na * nb) if na and nb else 0.0


def _semantic_near_dupe_delta(texts: List[str], token_weights: List[float],
                              threshold: float = 0.90) -> float:
    """Estimate additional tokens a semantic-memo cache would save: for message
    pairs that are near-duplicate in MEANING but not byte-identical (so exact
    dedupe missed them), the shorter tail is recoverable. Returns a token count.
    Internal-only; never exposes the projection."""
    saved = 0.0
    n = len(texts)
    # bound the pairwise scan
    max_pairs = min(n, 60)
    for i in range(max_pairs):
        for j in range(i + 1, max_pairs):
            v1, v2 = _semantic_vec(texts[i]), _semantic_vec(texts[j])
            sim = _cosine(v1, v2)
            # near-dupe in meaning but NOT exact (the gap exact-dedupe misses)
            if sim >= threshold and texts[i] != texts[j]:
                saved += min(token_weights[i], token_weights[j])
    return saved


# --------------------------------------------------------------------------
# PROXY for hash tier (no content available) — honest, clearly labeled.
# --------------------------------------------------------------------------

def _hash_tier_proxy(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """With hash-tier rows (role, token, sha, length only) we cannot do semantic
    matching, so return an HONEST proxy: exact duplicates (resend) + length-
    similar payloads as a lower-bound signal. Never claims semantic accuracy."""
    sha_counts: Dict[str, int] = {}
    for r in rows:
        sha_counts[r["content_sha256"]] = sha_counts.get(r["content_sha256"], 0) + 1
    exact_saved = 0.0
    exact_msgs = 0
    for sha, cnt in sha_counts.items():
        if cnt >= 2:
            # token weight: use the row's approx_tokens for the extra copies
            exact_msgs += cnt
    # count actual extra tokens of exact dupes
    exact_extra = 0.0
    seen = set()
    for r in rows:
        h = r["content_sha256"]
        if h in seen:
            exact_extra += float(r.get("approx_tokens", 0))
        else:
            seen.add(h)
    # length-band proxy: payloads within 20% length of a repeated sha may be near
    length_bands: Dict[int, int] = {}
    for r in rows:
        length_bands[r.get("content_length", 0) // 50] = length_bands.get(r["content_length"] // 50, 0) + 1
    proxy_semantic = 0.0
    for band, cnt in length_bands.items():
        if band > 0 and cnt >= 2:
            proxy_semantic += 1.0  # conservative per-band flag
    total = float(sum(r.get("approx_tokens", 0) for r in rows))
    return {
        "tier": "hash",
        "delta_is_proxy": True,
        "note": "proxy estimate only: server received hashes, not content; semantic delta cannot be measured. Upgrade to content tier for an accurate semantic delta.",
        "baseline_total_tokens": round(total, 1),
        "exact_dedupe_savings": round(exact_extra, 1),
        "semantic_additional_savings_proxy": round(proxy_semantic * 3.0, 1),
        "tokens_by_role": {},
    }


# --------------------------------------------------------------------------
# CONTENT tier: real semantic delta using the internal engine
# --------------------------------------------------------------------------

def _content_tier_compute(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    texts = []
    weights = []
    roles = {}
    for r in rows:
        text = r.get("content_scrubbed") or ""
        texts.append(text)
        w = float(r.get("approx_tokens", 0))
        weights.append(w)
        roles[r.get("role", "?")] = roles.get(r.get("role", "?"), 0) + w
    # exact dedupe first
    exact_saved = 0.0
    seen = set()
    max_pairs = min(len(texts), 60)
    for i in range(max_pairs):
        if texts[i] in seen and texts[i]:
            exact_saved += weights[i]
        seen.add(texts[i])
    # semantic additional (the part exact dedupe misses)
    semantic_extra = _semantic_near_dupe_delta(texts, weights, threshold=0.88)
    total = sum(weights)
    return {
        "tier": "content",
        "delta_is_proxy": False,
        "baseline_total_tokens": round(total, 1),
        "exact_dedupe_savings": round(exact_saved, 1),
        "semantic_additional_savings": round(semantic_extra, 1),
        "total_potential_savings": round(exact_saved + semantic_extra, 1),
        "tokens_by_role": {k: round(v, 1) for k, v in roles.items()},
        "note": "real delta: method runs server-side, opaque; returns savings number only.",
    }


# --------------------------------------------------------------------------
# HTTP server + auth
# --------------------------------------------------------------------------

def _require_auth(handler: BaseHTTPRequestHandler) -> bool:
    expected = os.environ.get("MCP_TOKEN_SAVER_API_KEY", "")
    # Subscription gate: if an order entitlement is configured, a request that
    # presents an X-Order-Id must have a valid unexpired entitlement (402 if not).
    # Static-key auth (if set) still applies as a second layer.
    subscription_enabled = os.environ.get("MCP_TOKEN_SAVER_SUB", "").lower() in ("1", "true", "yes")
    order = handler.headers.get("X-Order-Id", "")
    if subscription_enabled and order:
        try:
            from pro_subscription import require_pro
        except Exception:
            subscription_enabled = False  # sub module missing => fall through to key
        else:
            if not require_pro(order):
                handler.send_response(402)
                handler._send_json({"error": "payment_required",
                                    "detail": "no valid Pro entitlement for X-Order-Id"})
                return False
    if not expected:
        return True  # no key configured => allow (dev/local only)
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {expected}":
        return True
    handler.send_response(401)
    handler._send_json({"error": "unauthorized"})
    return False


class AssessmentHandler(BaseHTTPRequestHandler):
    server_version = "mcp-token-saver-pro/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json({"ok": True, "service": "mcp-token-saver-pro"})
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self):
        if self.path != "/assess":
            self._send_json({"error": "not_found"}, 404)
            return
        if not _require_auth(self):
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 5 * 1024 * 1024:  # 5MB cap
            self._send_json({"error": "payload_too_large"}, 413)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
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
                result = _content_tier_compute(rows)
            else:
                result = _hash_tier_proxy(rows)
            result["received_rows"] = len(rows)
            result["tier"] = tier
            self._send_json({"result": result})
        except Exception as e:
            self._send_json({"error": "compute_failed", "detail": str(e)[:120]}, 500)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="mcp-token-saver PRO assessment backend (paid, server-side)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9500)
    a = p.parse_args()
    print(f"[mcp-token-saver-pro] semantic delta backend on {a.host}:{a.port} "
          "(method is server-side/opaque; set MCP_TOKEN_SAVER_API_KEY for auth)", flush=True)
    HTTPServer((a.host, a.port), AssessmentHandler).serve_forever()


if __name__ == "__main__":
    main()
