#!/usr/bin/env python3
"""Tests + edge cases for mcp_token_saver.py — must be bulletproof before push."""
import json, sys
sys.path.insert(0, "/home/sudosudo/mcp-token-saver")
from mcp_token_saver import TokenProfiler, Message, demo_messages

PASS=FAIL=0
def check(n,c,d=""):
    global PASS,FAIL
    if c: PASS+=1; print(f"  PASS: {n}")
    else: FAIL+=1; print(f"  FAIL: {n} {d}")

tp = TokenProfiler()

# --- baseline: demo detects exact dupes ---
res = tp.analyze(demo_messages())
check("demo has exact dup groups", len(res["exact_duplicate_groups"])>=3, json.dumps([g["count"] for g in res["exact_duplicate_groups"]]))
check("demo savings_pct > 0", res["savings_potential_pct"] > 0)
check("demo message_count = 9", res["message_count"]==9)
check("demo total_tokens > 0", res["total_tokens_approx"] > 0)

# --- no dupes -> zero savings, pass clean ---
clean = [Message("system","init"), Message("user","do A"), Message("assistant","done A"),
         Message("user","do B"), Message("assistant","done B")]
res = tp.analyze(clean)
check("no-dupe log -> savings 0", res["savings_potential_pct"]==0.0)
check("no-dupe log -> zero exact groups", res["exact_duplicate_groups"]==[])

# --- provided_tokens honored ---
m = Message("user","x",provided_tokens=500)
check("provided_tokens honored", tp._tokens(m)==500)

# --- exact hash cache key is SHA-256 short (public, no semantic) ---
h = TokenProfiler._sha("hello world")
check("cache key is sha256 family", len(h)==16 and all(c in '0123456789abcdef' for c in h))

# --- near-dup detection ---
a = Message("tool","record A contains status ONLINE and detail 123")
b = Message("tool","record A contains status ONLINE and detail 123 extra appended")
res = tp.analyze([a,b])
check("near-dupe pair detected", any(np["kind"]=="near" for np in res["near_duplicates"]), str(res["near_duplicates"])[:80])

# --- role token split ---
res = tp.analyze([Message("system","s"*100), Message("user","u"*100)])
check("role tokens split", res["tokens_by_role"]["system"]==25 and res["tokens_by_role"]["user"]==25, str(res["tokens_by_role"]))

# --- system prompt share ---
res = tp.analyze([Message("system","x"*1000), Message("user","y")])
check("system share flagged", res["system_prompt_share_pct"] > 90)
check("sys-prompt recommendation present", any(r["action"]=="compress_system_prompt" for r in res["recommendations"]))

# --- EDGE: empty list ---
res = tp.analyze([])
check("empty list -> clean", res["message_count"]==0 and res["savings_potential_pct"]==0.0)

# --- EDGE: None content / missing fields ---
res = tp.analyze([Message("user", None)])
check("None content (no crash)", res["message_count"]==1)

# --- EDGE: unicode + emoji ---
res = tp.analyze([Message("user","héllo wörld ☕ 你好"), Message("tool","☕ 你好 same emoji line")])
check("unicode/emoji no crash", res["message_count"]==2)

# --- EDGE: huge single message (10k) ---
res = tp.analyze([Message("user","x"*10000)])
check("10k single message no crash", res["total_tokens_approx"]>=2500)

# --- EDGE: 200-message log with REAL exact dupes (performance + no crash) ---
big = [Message("user", f"message {i%5} same text here") for i in range(200)]  # 40 each of 5 texts
res = tp.analyze(big)
check("200-msg log no crash", res["message_count"]==200)
check("200-msg detects exact dupes (40 each of 5 texts -> 5 groups)", len(res["exact_duplicate_groups"])>=5, str(len(res["exact_duplicate_groups"])))

# --- DETERMINISM ---
r1 = json.dumps(tp.analyze(demo_messages()), sort_keys=True)
r2 = json.dumps(tp.analyze(demo_messages()), sort_keys=True)
check("deterministic identical", r1==r2)

# --- sauce-guard: output must DISCLOSE generic method + NOT claim/use a
# proprietary semantic analyzer. 'semantic fingerprint' appears only in the
# honest disclosure "No proprietary semantic fingerprint" — allowed. Check we
# don't advertise a gematria/fingerprint capability or embed tuned weights. ---
blob = json.dumps(res, sort_keys=True).lower()
check("does not install/claim gematria or base-6 method", not any(x in blob for x in ["gematria", "base-6", "mod-5", "attractor"]))
check("does not ship tuned semantic weights/vectors", "verification_weight" not in blob and "attention" not in blob)

print(f"\n{'='*50}\ntoken-saver: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
