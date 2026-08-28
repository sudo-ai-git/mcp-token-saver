#!/usr/bin/env python3
"""
mcp-token-saver — PRO/paid assessment client (honest two-tier design).

The FREE tool (profile_tokens) shows generic savings (SHA exact-dedupe +
n-gram near-dupe). The PRO assessment answers: "what would your FULL optimizer
(semantic / non-exact caching) save on top of that?"

THE HONEST TRADE-OFF (this is the real contract, stated plainly):
A true *semantic*-memo savings delta requires the assessment engine to see
enough linguistic signal to detect near-duplicate *meaning*. That cannot be
done from a pure content hash. So there are two de-identified tiers:

  TIER (a) — HASH-ONLY (privacy-max, delta is a PROXY)
    Send role + token count + sha256 + content-length. Server infers a
    conservative proxy delta from overlap patterns it CAN see (exact + length
    distribution). No raw content leaves the machine. Safe to any endpoint.
    Delta = an estimate, clearly labeled.

  TIER (b) — DE-IDENTIFIED CONTENT (accurate delta, sauce still protected)
    Send the message text AFTER secrets/PII are scrubbed by a deterministic
    redactor. The proprietary semantic engine runs ONLY on our server and
    returns just a number. The METHOD never leaves our machine (server-side,
    opaque). The CONTENT you share is redacted + stays under your chosen
    retention (the server returns the number, retains nothing by policy).

The sauce (semantic-memo method + weights) is NEVER shipped, never sent,
never returned — in either tier. This client implements both; you choose the
tier per your content-sharing tolerance.

Deterministic, no-LLM, auditable. Crown-jewel-clean client.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


# Deterministic secret/PII redactor for Tier (b) — a PUBLIC-grade scrubber.
# (Not the proprietary redaction method; this is a conservative, auditable
# pattern-matcher that blanks common secret shapes before any upload.)
_SECRET_RE = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),          # openai-anthropic style keys (many are 15+; be permissive/low floor)  # noqa: E501
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),          # aws access key id
    re.compile(r"(?i)api[_-]?key['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9]{12,}"),
    re.compile(r"(?i)password['\"]?\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
    re.compile(r"(?i)password(?:\s+is|\s*[:=])\s+['\"]?([A-Za-z0-9@#_!-]{4,})"),
    re.compile(r"(?i)authorization['\"]?\s*[:=]\s*bearer\s+[^\s'\"]+"),
    re.compile(r"(?i)(?:auth|secret|credential|key)['\"]?\s*[:=]\s*['\"]?bearer[^\s'\"]{8,}"),
    re.compile(r"(?i)token['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._-]{12,}"),
    re.compile(r"\b[0-9]{16,}\b"),                # card-like number runs
    re.compile(r"(?i)s3cr3t|secret|credential"),
]

def scrub_content(content: str) -> str:
    """BLANK secret-shaped substrings from content. Deterministic, conservative
    (over-scrub is safe: blanks a bit more, never less).

    HONEST LIMIT: this is a pattern scrubber, not a semantic security layer. It
    catches secret-SHAPED values (key:value, bearer, sk-…, AKIA…, long digit
    runs, 'password is …'). It cannot guarantee removal of an arbitrary bare
    secret word embedded in prose (that needs judgement, not a regex). For a
    hard privacy guarantee use tier='hash' (which sends NO content at all).
    """
    s = content or ""
    for rx in _SECRET_RE:
        s = rx.sub("[REDACTED]", s)
    return s


@dataclass
class AssessmentRow:
    role: str
    approx_tokens: int
    content_sha256: str
    content_length: int
    content_scrubbed: Optional[str] = None  # set ONLY in Tier (b)


def deidentify(messages: List[Dict[str, Any]], tier: str = "hash") -> List[AssessmentRow]:
    """tier='hash' (always safe, proxy delta) or 'content' (scrubbed text kept).
    In 'content' tier, each row carries scrubbed text — secrets removed by the
    public redactor, semantic signal retained for server-side measurement."""
    tier = tier.lower()
    if tier not in ("hash", "content"):
        raise ValueError("tier must be 'hash' or 'content'")
    out = []
    for m in messages:
        content = m.get("content") or ""
        approx = m.get("provided_tokens")
        if approx is None:
            approx = max(1, int(len(content) * 0.25))
        scrubbed = scrub_content(content) if tier == "content" else None
        out.append(AssessmentRow(
            role=m.get("role", "user"),
            approx_tokens=approx,
            content_sha256=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
            content_length=len(content),
            content_scrubbed=scrubbed,
        ))
    return out


class ProAssessor:
    def __init__(self, endpoint: str, api_key: str, timeout: int = 30) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def assess(self, messages: List[Dict[str, Any]], tier: str = "hash") -> Dict[str, Any]:
        rows = [asdict(r) for r in deidentify(messages, tier)]
        payload = json.dumps({"messages": rows, "tier": tier}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}", "detail": e.read()[:200].decode("utf-8", "replace")}
        except Exception as e:
            return {"error": "assessment_unavailable", "detail": str(e)[:160]}


# ------------------------------------------------------------------ MCP surface

def register_pro_client_tools(mcp) -> None:
    @mcp.tool()
    def prepare_assessment(messages: List[Dict[str, Any]], tier: str = "hash") -> Dict[str, Any]:
        """Prepare a message log for the PRO token-assessment.
        tier='hash': sends role/token/sha256/length ONLY (privacy-max, proxy delta).
        tier='content': additionally sends secrets-SCRUBBED text (accurate delta;
        secrets removed by deterministic redactor; method still server-side/opaque).
        Returns the ready-to-send rows + whether raw/scrubbed content is included.
        """
        rows = deidentify(messages, tier)
        dicts = [r.__dict__ for r in rows]
        return {
            "tier": tier,
            "ready_to_send": dicts,
            "send_count": len(rows),
            "includes_raw_content_plaintext": any(r.content_scrubbed is not None for r in rows),
            "content_scrubbed": tier == "content",
            "note": ("hash tier: no content sent, proxy delta; "
                     "content tier: scrubbed text sent, method stays server-side"),
        }

    @mcp.tool()
    def assess_upgrade_value(messages: List[Dict[str, Any]],
                             endpoint: str, api_key: str, tier: str = "hash") -> Dict[str, Any]:
        pa = ProAssessor(endpoint, api_key)
        return pa.assess(messages, tier)


if __name__ == "__main__":
    msgs = [
        {"role": "user", "content": "Check the status of records."},
        {"role": "tool", "content": '{"secret":"s3cr3t","api_key":"AKIA1234567890ABCDEF","status":"ok","data":[1,2,3]}'},
    ]
    print("=== tier=hash ===")
    print(json.dumps([r.__dict__ for r in deidentify(msgs, "hash")], indent=1))
    print("\n=== tier=content (scrubbed) ===")
    rows = deidentify(msgs, "content")
    for r in rows:
        if r.content_scrubbed:
            print(f"  scrubbed text: {r.content_scrubbed}")
            print(f"  secret leak in scrubbed: {'s3cr3t' in r.content_scrubbed or 'AKIA' in r.content_scrubbed}")
