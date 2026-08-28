#!/usr/bin/env python3
"""Tests for pro_client.py — two-tier de-identification + PRO upsell client.
MUST prove: hash tier leaks nothing; content tier scrubs secrets; edge cases."""
import json, sys
sys.path.insert(0, "/home/sudosudo/mcp-token-saver")
from pro_client import deidentify, ProAssessor, scrub_content, AssessmentRow

PASS=FAIL=0
def check(n,c,d=""):
    global PASS,FAIL
    if c: PASS+=1; print(f"  PASS: {n}")
    else: FAIL+=1; print(f"  FAIL: {n} {d}")

SECRET_MSGS = [
    {"role":"user","content":"my password is hunter2 and key sk-abc123456789"},
    {"role":"tool","content":'{"secret":"s3cr3t","api_key":"AKIA1234567890ABCDEF","auth":"bearer-token-here"}',"provided_tokens":50},
    {"role":"user","content":'api_key: abcdef123456 secret token 1234567890123456'},
]

# ---- TIER hash: NO content, NO secrets -----------------------------
rows = deidentify(SECRET_MSGS, "hash")
blob = json.dumps([r.__dict__ for r in rows])
for s in ["hunter2","sk-abc123456789","s3cr3t","AKIA1234567890ABCDEF","bearer-token-here","password","abcdef123456"]:
    check(f"hash tier: secret '{s}' absent", s not in blob)
check("hash tier: content_scrubbed is None on all", all(r.content_scrubbed is None for r in rows))
check("hash tier: sha256 64 hex for all", all(len(r.content_sha256)==64 for r in rows))
check("hash tier: safe field set", all(set(r.__dict__.keys())=={"role","approx_tokens","content_sha256","content_length","content_scrubbed"} for r in rows))
check("provided_tokens honored (50)", rows[1].approx_tokens==50)

# ---- TIER content: scrubbed text present, secret-SHAPES removed ----------
crows = deidentify(SECRET_MSGS, "content")
cblob = json.dumps([r.__dict__ for r in crows])
# each message's secret-shapes must be gone (verify the ACUTAL leaked substrings)
for s in ["sk-abc123456789","s3cr3t","AKIA1234567890ABCDEF","bearer-token-here","1234567890123456","hunter2"]:
    check(f"content tier: secret-shape '{s}' absent (scrubbed)", s not in cblob)
check("content tier: some rows carry scrubbed text", any(r.content_scrubbed for r in crows))
check("content tier: scrub replaced with [REDACTED]", all(r.content_scrubbed is None or "[REDACTED]" in r.content_scrubbed for r in crows))

# ---- scrub_content direct -------------------------------------------
check("scrub blanks api key pattern", "REDACTED" in scrub_content('api_key: AKIA1234ABCD5678EF90 xyz'))
check("scrub blanks bearer header", "REDACTED" in scrub_content('Authorization: Bearer tok1234567890'))
check("scrub blanks sk- key of 15 chars", "REDACTED" in scrub_content("key sk-abc123456789"))  # was len15, {10,} now catches
check("scrub leaves normal text", "REDACTED" not in scrub_content("Check the current status of the pipeline please."))

# HONEST LIMIT note: a BARE secret with no key-context in prose is not reliably
# caught by a pattern scrubber. The hard privacy guarantee = hash tier (no
# content). Test the disclosed boundary: prose with NO key-shape stays as-is.
check("honest boundary: prose without key-shape untouched (scrubber is pattern, not semantic)",
      scrub_content("I think the vendor used a strange key format now now now") == "I think the vendor used a strange key format now now now")

# ---- EDGE: None content / missing -----------------------------------
r0 = deidentify([{"role":"user"}], "hash")[0]
check("None content -> len 0 + 64-hex hash, no crash", r0.content_length==0 and len(r0.content_sha256)==64)
rc = deidentify([{"role":"user"}], "content")[0]
check("None content content-tier -> scrubbed '' (no crash)", rc.content_scrubbed=="")

# ---- EDGE: unicode deterministic hash -------------------------------
h1=deidentify([{"role":"user","content":"héllo ☕ 你好"}],"hash")[0].content_sha256
h2=deidentify([{"role":"user","content":"héllo ☕ 你好"}],"hash")[0].content_sha256
check("unicode hash deterministic", h1==h2)

# ---- EDGE: bad tier ----------------------------------------------
try:
    deidentify([{"role":"user","content":"x"}], "banana")
    check("bad tier raises ValueError", False)
except ValueError:
    check("bad tier raises ValueError", True)

# ---- EDGE: huge 10k content --------------------------------------
big = deidentify([{"role":"user","content":"x"*10000,"provided_tokens":2000}],"hash")[0]
check("10k content token count honored", big.approx_tokens==2000)
bigc = deidentify([{"role":"user","content":"x"*10000}],"content")[0]
check("10k scrubbed no crash", bigc.content_scrubbed is not None)

# ---- assessment payload safety (hash tier) ------------------------
out = [r.__dict__ for r in deidentify(SECRET_MSGS,"hash")]
pl = json.dumps(out)
check("assessment hash payload secret-free", all(s not in pl for s in ["hunter2","AKIA1234567890ABCDEF"]))

print(f"\n{'='*50}\npro_client (2-tier): {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
