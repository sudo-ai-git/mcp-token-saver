#!/usr/bin/env python3
"""
mcp-token-saver — deterministic token-usage profiler + redundancy advisor.

Shows an agent team WHERE their tokens actually go and how much they can save,
using ONLY public generic techniques. No proprietary semantic method here — the
intent is that this is shippable as a standalone, auditable tool.

Methods (all public, deterministic, no-LLM):
- exact duplicate detection via SHA-256 of the raw bytes (public hash)
- near-duplicate detection via n-gram overlap (generic cosine-over-sets)
- per-message / per-role token approximation (chars/4 fallback or supplied counts)
- repeated-system-prompt and repeated-payload bloat reporting
- cache-key suggestion using only the SHA-256 exact hash (NOT a semantic
  fingerprint — a client that wants semantic memo adds its own layer)

Deterministic: same input -> same output, line by line auditable.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Message:
    """A single message in a conversation/tool log."""
    role: str                      # system | user | assistant | tool
    content: str
    tool_name: Optional[str] = None
    provided_tokens: Optional[int] = None   # if caller supplies real token count

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TokenProfiler:
    """Deterministic token bloat + redundancy analyzer."""

    def __init__(self, ngram: int = 3, near_threshold: float = 0.75,
                 approx_tokens_per_char: float = 0.25) -> None:
        self.ngram = ngram
        self.near_threshold = near_threshold
        self.approx_tokens_per_char = approx_tokens_per_char

    # ---------------------------------------------------------------- tokens
    def _tokens(self, m: Message) -> int:
        if m.provided_tokens is not None:
            return int(m.provided_tokens)
        content = m.content or ""
        # neutral, model-agnostic approximation; exact when caller supplies counts
        return max(1, int(len(content) * self.approx_tokens_per_char))

    def _chars(self, c) -> int:
        return len(c or "")

    # ---------------------------------------------------------------- hashing
    @staticmethod
    def _sha(c) -> str:
        return hashlib.sha256((c or "").encode("utf-8", errors="replace")).hexdigest()[:16]

    def _ngrams(self, c) -> set:
        # generic word n-grams; punctuation-split tokens
        words = [w for w in re.findall(r"[A-Za-z0-9']+", (c or "").lower())]
        if len(words) < self.ngram:
            return {w for w in words}
        return set(" ".join(words[i:i+self.ngram]) for i in range(len(words)-self.ngram+1))

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    # ---------------------------------------------------------------- analyze
    def analyze(self, messages: List[Message]) -> Dict[str, Any]:
        exact_seen: Dict[str, List[int]] = {}  # sha -> indexes
        groups: List[Dict[str, Any]] = []
        by_role: Dict[str, int] = {}
        total_tokens = 0

        # pass 1: counts + exact dupes
        for i, m in enumerate(messages):
            tok = self._tokens(m)
            total_tokens += tok
            by_role[m.role] = by_role.get(m.role, 0) + tok
            h = self._sha(m.content)
            exact_seen.setdefault(h, []).append(i)

        # pass 2: exact-duplicate groups (>=2 identical -> wasting tokens)
        for h, idxs in exact_seen.items():
            if len(idxs) < 2:
                continue
            rep = messages[idxs[0]]
            waste = (len(idxs) - 1) * self._tokens(rep)
            groups.append({
                "kind": "exact",
                "count": len(idxs),
                "sha256_short": h,
                "role": rep.role,
                "tokens_wasted_if_not_cached": waste,
                "example": rep.content[:120],
                "cache_key": h,   # public exact-hash cache key
            })

        # pass 3: near-duplicate detection across ~unique messages (bounded)
        # compare only non-identical messages to avoid O(n^2) blowups on big logs
        uniq = {}
        idx_to_m = []
        for i, m in enumerate(messages):
            h = self._sha(m.content)
            if h not in exact_seen or len(exact_seen[h]) == 1:
                uniq[i] = m
                idx_to_m.append(i)
        near_pairs = []
        seen_pairs = set()
        li = idx_to_m
        for a in range(len(li)):
            for b in range(a+1, len(li)):
                ma, mb = messages[li[a]], messages[li[b]]
                if self._chars(ma.content) < 40 or self._chars(mb.content) < 40:
                    continue
                sim = self._jaccard(self._ngrams(ma.content), self._ngrams(mb.content))
                if sim >= self.near_threshold:
                    key = tuple(sorted((li[a], li[b])))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    near_pairs.append({
                        "kind": "near",
                        "similarity": round(sim, 3),
                        "indexes": [li[a], li[b]],
                        "roles": [ma.role, mb.role],
                        "waste_potential": 0,  # computed below (2nd is partial dup)
                    })

        # system-prompt bloat: if system role dominates, flag
        sys_share = (by_role.get("system", 0) / total_tokens) if total_tokens else 0.0

        # recompute waste: each near-dup's shorter tail is arguably redundant
        for np in near_pairs:
            a, b = np["indexes"]
            shorter = min(self._tokens(messages[a]), self._tokens(messages[b]))
            np["waste_potential"] = shorter  # at most the shorter one is duplicate-ish

        total_redundant = (sum(g["tokens_wasted_if_not_cached"] for g in groups)
                           + sum(np["waste_potential"] for np in near_pairs))
        savings_pct = (total_redundant / total_tokens * 100) if total_tokens else 0.0

        return {
            "message_count": len(messages),
            "total_tokens_approx": total_tokens,
            "tokens_by_role": by_role,
            "exact_duplicate_groups": groups,
            "near_duplicate_pairs": len(near_pairs),
            "near_duplicates": near_pairs[:50],  # cap output
            "redundant_tokens_est": total_redundant,
            "savings_potential_pct": round(savings_pct, 2),
            "system_prompt_share_pct": round(sys_share * 100, 2),
            "recommendations": self._recommendations(groups, near_pairs, sys_share, total_tokens),
            "_method_note": "public methods only: SHA-256 exact dedupe + n-gram "
                            "near-dupe + role token counts. No proprietary semantic fingerprint.",
        }

    def _recommendations(self, groups, near_pairs, sys_share, total_tokens):
        rec = []
        if groups:
            n_exact = sum(g["count"] for g in groups)
            rec.append({"action": "cache_exact", "severity": "HIGH",
                        "msg": f"{n_exact} exact-duplicate payload(s) re-sent; cache by SHA-256 to avoid resend."})
        if near_pairs:
            rec.append({"action": "review_near_duplicates", "severity": "MEDIUM",
                        "msg": f"{len(near_pairs)} near-duplicate pair(s) — likely repeated tool results or templated logs."})
        if sys_share > 0.5:
            rec.append({"action": "compress_system_prompt", "severity": "MEDIUM",
                        "msg": f"System prompt is {round(sys_share*100)}% of tokens — consider static caching / summarization."})
        return rec


# ------------------------------------------------------------------ MCP surface
def register_token_saver_tools(mcp) -> None:
    profiler = TokenProfiler()

    @mcp.tool()
    def profile_tokens(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze a conversation/tool log for token usage + redundancy.

        messages: [{role, content, tool_name?, provided_tokens?}]
        Reports per-role token spend, exact-duplicate payloads (SHA-256),
        near-duplicates (n-gram), estimated savings, and cache-key suggestions
        (public exact hash). Deterministic, no-LLM.
        """
        msgs = [Message(m.get("role","user"), m.get("content",""),
                        m.get("tool_name"), m.get("provided_tokens")) for m in messages]
        return profiler.analyze(msgs)

    @mcp.tool()
    def duplicate_payload_report(payloads: List[str]) -> Dict[str, Any]:
        """Given a list of payload strings, report which are exact or near
        duplicates (good for spotting repeated tool results / log tails)."""
        msgs = [Message("tool", p) for p in payloads]
        return profiler.analyze(msgs)


def demo_messages() -> List[Message]:
    """A realistic agent log with heavy repetition."""
    sys_prompt = "You are a careful agent. Verify each step. Write evidence-graded claims."
    tool_result = '{"status":"ok","records":[{"id":1,"name":"alpha"},{"id":2,"name":"beta"}]}'
    return [
        Message("system", sys_prompt),
        Message("user", "Check the status of records."),
        Message("assistant", "I'll check the records."),
        Message("tool", tool_result, tool_name="list_records"),
        Message("assistant", "The records are: alpha, beta."),
        Message("user", "Check the status of records."),          # exact dup user msg
        Message("assistant", "I'll check the records."),           # exact dup
        Message("tool", tool_result, tool_name="list_records"),   # exact dup tool result
        Message("assistant", "The records are: alpha, beta."),    # exact dup
    ]


if __name__ == "__main__":
    res = TokenProfiler().analyze(demo_messages())
    print(json.dumps(res, indent=2)[:1500])
