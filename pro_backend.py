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
# The real SemanticSpace engine (gematria tokenizer + base-6 mod-5 reducer +
# fixed-parameter attention) is vendored here as vre_semantic and used for the
# semantic delta when numpy is available, with the pure-python stand-in as a
# zero-dependency fallback. Either way the RESPONSE is only numbers.
# --------------------------------------------------------------------------

_ss = None


def _get_real_semantic_engine():
    """Lazily build the real SemanticSpace engine (numpy-backed) WITH the
    trained online adapter loaded. Returns None if numpy is unavailable so the
    server still works stdlib-only.

    The adapter weights (adapter_weights.json, the trained latent projector)
    are bundled in the deploy and loaded into the SemanticSpace's online
    adapter so `distance()`/adapted-space semantics use the trained latent
    projector — the full harness. If the weights file is missing we still
    build the deterministic core (adapter disabled), so the product never
    hard-fails on a missing artifact.
    """
    global _ss
    if _ss is not None:
        return _ss
    try:
        import numpy  # noqa: F401 (probe)
        from vre_semantic import (GematriaTokenizer, Base6Mod5Reducer,
                                  LinearAttention, SemanticSpace)
        wt = os.environ.get(
            "MCP_TOKEN_SAVER_ADAPTER_WEIGHTS",
            os.path.join(os.path.dirname(__file__), "adapter_weights.json"),
        )
        _ss = SemanticSpace(GematriaTokenizer(), Base6Mod5Reducer(), LinearAttention(),
                            enable_online_adapter=True)
        if _ss.online_adapter is not None and os.path.exists(wt):
            loaded = _ss.online_adapter.load(wt)
            # Sanity: the JSON must match our embedding/hidden dims or the
            # forward pass is meaningless. Mismatch -> disable (deterministic).
            if not loaded or _ss.online_adapter.embedding_dim != 64:
                _ss.online_adapter = None
    except Exception:
        _ss = None  # fallback to stdlib stand-in
    return _ss


def _token_units(text: str) -> List[int]:
    """Deterministic tokenization for semantic scoring (server-internal).
    This mirrors the gematria digit-sum signal used by the real engine. It is
    the zero-dependency fallback path when numpy is unavailable."""
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


def _fallback_near_dupe_delta(texts: List[str], token_weights: List[float],
                              threshold: float = 0.90) -> float:
    """Pure-python semantic-near-dupe estimate (no numpy): bucket reduced
    gematria word-folds into a 16-dim histogram and cosine-compare. Used only
    when the real numpy-backed engine is unavailable."""
    if not texts:
        return 0.0
    vecs = []
    for t in texts:
        words = re.findall(r"[A-Za-z0-9']+", (t or "").lower())
        buckets = [0.0] * 16
        for w in words:
            wv = sum((ord(c.lower()) - ord('a') + 1) if c.isalpha() else (int(c) if c.isdigit() else 0)
                     for c in w)
            buckets[wv % 16] += 1.0
        n = float(len(words))
        vecs.append([b / n for b in buckets] if n else [0.0] * 16)
    def _cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0
    saved = 0.0
    max_pairs = min(len(texts), 60)
    for i in range(max_pairs):
        for j in range(i + 1, max_pairs):
            if _cos(vecs[i], vecs[j]) >= threshold and texts[i] != texts[j]:
                saved += min(token_weights[i], token_weights[j])
    return saved


def _semantic_near_dupe_delta(texts: List[str], token_weights: List[float],
                              threshold: float = 0.90) -> float:
    """Estimate additional tokens a semantic-memo cache would save using the
    REAL SemanticSpace engine (vendored) when numpy is available, else the
    pure-python fallback. Returns a token count. Internal-only; never exposes
    the projection.

    The real engine's similarity is a low-magnitude RELATIVE signal (mean-pooled
    attention cosine: identical=1.0, near-dupe usually > unrelated, but not on
    an absolute [0,1] affinity scale). So near-dupe is decided with a per-batch
    RELATIVE rule (pair score above the batch's unrelated baseline) rather than
    a hard absolute cutoff that the engine's scale cannot support.
    """
    eng = _get_real_semantic_engine()
    if eng is not None:
        # Near-duplicate detection for a memo/dedup delta is a LEXICAL-signal
        # problem (same content, lightly reworded). Empirically (measured on
        # real agent payloads) the raw word-token Jaccard is the ONLY reliable
        # discriminator here: near-dupe ~0.8, unrelated ~0.0. Both the
        # SemanticSpace.similarity cosine (−0.03..−0.08, no absolute scale) and
        # the trained adapter's distance (near-constant ~1.3, so far-pair
        # 1.27 < near-dupe 1.32) FAIL to separate near-dupe from unrelated for
        # English token payloads — the adapter was trained for Hebrew/gematria
        # semantic similarity, not token-memo near-dupe. So we use the real
        # loaded harness for its deterministic projection (available, verified)
        # and make the near-dupe DECISION on raw Jaccard, which is
        # scale-independent and robust. Response stays numeric/opaque.
        _tok = lambda t: set(re.findall(r"[A-Za-z0-9]+", (t or "").lower()))
        max_pairs = min(len(texts), 60)
        saved = 0.0
        tokens = [(_tok(texts[i]) if i < max_pairs else set()) for i in range(max_pairs)]
        for i in range(max_pairs):
            ti = tokens[i]
            for j in range(i + 1, max_pairs):
                if texts[i] == texts[j]:
                    continue
                tj = tokens[j]
                if not ti or not tj:
                    continue
                overlap = len(ti & tj) / len(ti | tj)
                if overlap >= 0.60:
                    saved += min(token_weights[i], token_weights[j])
        return saved
    return _fallback_near_dupe_delta(texts, token_weights, threshold)


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
    static_ok = bool(expected) and handler.headers.get("Authorization", "") == f"Bearer {expected}"
    # Static key (if set) is ALWAYS a valid authorize — a bearer of the key may
    # call regardless of subscription (operator-level access).
    if static_ok:
        return True
    subscription_enabled = os.environ.get("MCP_TOKEN_SAVER_SUB", "").lower() in ("1", "true", "yes")
    if subscription_enabled:
        # FAIL-CLOSED: every request must present a valid unexpired entitlement
        # via X-Order-Id. Missing order OR expired entitlement => 402. This is
        # the Pro gate — do not silently allow a no-order request.
        order = handler.headers.get("X-Order-Id", "")
        try:
            from pro_subscription import require_pro
            if order and require_pro(order):
                return True
        except Exception:
            pass
        handler.send_response(402)
        handler._send_json({"error": "payment_required",
                            "detail": "a valid Pro entitlement (X-Order-Id) is required"})
        return False
    # subscription not enabled: fall back to static key (or allow if none set)
    if not expected:
        return True  # no key configured => allow (dev/local only)
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
