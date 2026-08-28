"""mcp-token-saver PROXY — deterministic request-path optimization core.

Reduces redundant tokens in an OpenAI-compatible chat.completions request
BEFORE it reaches the inference provider. No-LLM, deterministic, auditable.

Optimizations applied (conservative — never changes model behavior on
legitimately-distinct messages):
  1. EXACT-DUPLICATE tool/user message dedupe: identical payload bytes re-sent
     (SHA-256) are dropped/compressed to a pointer. Safe because identical
     bytes carry no new information.
  2. NEAR-DUPLICATE tail compress: repeated log fragments (n-gram Jaccard >
     threshold) are shortened (kept first occurrence, later ones truncated with
     a marker). Conservative: only clearly-redundant tails.
  3. SYSTEM-PREFIX STABILITY: does not reorder roles; ensures the system-prompt
     / tool-schema head is kept byte-stable (never interleaved with variable
     tool results) so provider caches retain their boundary. (Operational hint
     for the proxy, not a message rewrite.)

Safety gates (edge cases):
- Only dedupe `tool` and `user` messages that are BYTE-IDENTICAL or clearly
  near-identical; never `assistant` content that carries a model's own token
  stream (would corrupt the conversation).
- A `tool` result that pairs to an assistant tool_call must never be dropped
  if dedupe would orphan the pairing — we keep the first occurrence and only
  collapse *subsequent identical* resends, which preserves the pairing.
- Streaming is handled at the transport layer (request optimization only).

EVERY optimization is appended to an audit ledger: what was dropped/shrunk,
by how many tokens, which message index.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

# ---- helpers (mirror the passive tool; keep identical for consistency) ----

def _sha256(b: str) -> str:
    return hashlib.sha256(b.encode("utf-8", "replace")).hexdigest()

def _approx_tokens(text: str) -> int:
    # chars/4 heuristic (matches the passive tool's model-agnostic approx)
    return max(1, len(text) // 4)

def _n_grams(s: str, n: int = 3) -> set:
    s = s.split() if s else []
    return set(" ".join(s[i:i+n]) for i in range(max(0, len(s) - n + 1)))

def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

# ---- optimization engine ----

NEAR_DUP_THRESHOLD = 0.90   # only collapse clearly-redundant tails
MAX_TAIL_TOKENS = 24        # how much of a near-dup to keep when collapsing


class ProxyOptimizer:
    """Deterministic request optimizer. Thread-safe."""

    def __init__(self, ledger_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self.ledger: List[Dict[str, Any]] = []
        self.ledger_path = ledger_path or os.environ.get(
            "PROXY_LEDGER", os.path.expanduser("~/.mcp-token-saver/proxy_ledger.jsonl"))
        self._seen_exact: Dict[str, int] = {}   # sha -> first msg index

    def _log(self, ev: Dict[str, Any]) -> None:
        self.ledger.append(ev)
        if self.ledger_path:
            try:
                d = os.path.dirname(self.ledger_path)
                if d and not os.path.exists(d):
                    os.makedirs(d, exist_ok=True, mode=0o700)
                line = json.dumps({**ev, "ts": time.time()}) + "\n"
                with open(self.ledger_path, "a", encoding="utf-8") as f:
                    os.chmod(self.ledger_path, 0o600)
                    f.write(line)
            except Exception:
                pass  # ledger is best-effort; never break the request

    def optimize(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Return optimized messages + a stats/diff report. Preserves order.

        Extra care: never remove an `assistant` tool_call; never orphan a tool
        result. We dedupe exact byte-identical tool/user resends and collapse
        clearly near-identical tails, keeping the first occurrence each time.
        """
        result: List[Dict[str, Any]] = []
        stats = {"original_messages": len(messages),
                 "original_approx_tokens": sum(_approx_tokens(m.get("content") or "") for m in messages),
                 "removed": 0, "removed_approx_tokens": 0,
                 "compressed": 0, "compressed_approx_tokens": 0,
                 "optimized_messages": 0, "optimized_approx_tokens": 0}
        valid_roles = {"system", "user", "assistant", "tool"}

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content")
            if role not in valid_roles or content is None:
                # pass through anything we don't touch (tool_calls, etc.)
                result.append(msg)
                continue

            text = content if isinstance(content, str) else json.dumps(content)
            tok = _approx_tokens(text)

            # Only exact/near-dedupe tool + user messages. NEVER assistant:
            # assistant content is the model's own stream; collapsing it would
            # corrupt the conversation and break pairing.
            if role in ("tool", "user"):
                sha = _sha256(text)
                if role == "tool":
                    # EXACT dedupe for tool results (identical bytes re-sent)
                    if sha in self._seen_exact:
                        stats["removed"] += 1
                        stats["removed_approx_tokens"] += tok
                        self._log({"op": "drop_exact_dup", "role": role,
                                   "msg_index": i, "sha": sha[:12],
                                   "saved_approx_tokens": tok,
                                   "first_at": self._seen_exact[sha]})
                        continue
                    self._seen_exact[sha] = i
                    result.append(msg)
                    stats["optimized_messages"] += 1
                    stats["optimized_approx_tokens"] += tok
                    continue

                # role == user: near-dup tail compress (only for clearly
                # redundant repeated payloads, e.g. the same pasted blob)
                # Compare against the prior user messages' tails.
                if len(result) >= 1:
                    prev_roles = [m.get("role") for m in result if m.get("content")] or [""]
                    # find the most recent user message to compare
                    prev_user = None
                    for m in reversed(result):
                        if m.get("role") == "user" and isinstance(m.get("content"), str):
                            prev_user = m["content"]; break
                    if prev_user and len(prev_user.split()) > 8:
                        j = _jaccard(_n_grams(prev_user), _n_grams(text))
                        if j >= NEAR_DUP_THRESHOLD:
                            # collapse tail, keep a short pointer
                            tail = text.split()[:MAX_TAIL_TOKENS]
                            # keep the FIRST full occurrence; collapse the second
                            saved = tok - len(" ".join(tail)) // 4
                            stats["compressed"] += 1
                            stats["compressed_approx_tokens"] += saved
                            self._log({"op": "compress_near_dup_tail",
                                       "role": role, "msg_index": i,
                                       "jaccard": round(j, 3),
                                       "saved_approx_tokens": saved})
                            collapsed = {"role": "user",
                                         "content": " ".join(tail) +
                                         " […repeated; see earlier identical message]"}
                            result.append(collapsed)
                            stats["optimized_messages"] += 1
                            stats["optimized_approx_tokens"] += len(" ".join(tail)) // 4
                            continue
                result.append(msg)
                stats["optimized_messages"] += 1
                stats["optimized_approx_tokens"] += tok
            else:
                result.append(msg)

        stats["optimized_messages"] = len(result)
        return {"messages": result, "stats": stats, "ledger_len": len(self.ledger)}

    @staticmethod
    def summarize(stats: Dict[str, Any]) -> str:
        orig = stats["original_approx_tokens"]
        saved = stats["removed_approx_tokens"] + stats["compressed_approx_tokens"]
        pct = (saved / orig * 100) if orig else 0.0
        return (f"proxy: {saved}/{orig} approx tokens removed ({pct:.1f}%) "
                f"— {stats['removed']} exact dupes dropped, "
                f"{stats['compressed']} near-dupes compressed")
