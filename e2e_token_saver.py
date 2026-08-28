#!/usr/bin/env python3
"""Real MCP stdio E2E + adversarial transport for mcp-token-saver."""
import json, os, subprocess, sys
S = os.path.join("/home/sudosudo/mcp-token-saver", "server.py")
PASS=FAIL=0
def check(n,c,d=""):
    global PASS,FAIL
    if c: PASS+=1; print(f"  PASS: {n}")
    else: FAIL+=1; print(f"  FAIL: {n} {d}")

def raw_call(tool, args):
    proc = subprocess.Popen([sys.executable, S], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    def send(o): proc.stdin.write(json.dumps(o)+"\n"); proc.stdin.flush()
    def recv():
        while True:
            line = proc.stdout.readline()
            if not line: return None
            try: return json.loads(line)
            except Exception: continue
    send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"1"}}})
    recv(); send({"jsonrpc":"2.0","method":"notifications/initialized","params":{}})
    send({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":tool,"arguments":args}})
    res = recv()
    # survival check
    send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"profile_tokens","arguments":{"messages":[]}}})
    alive = recv()
    proc.stdin.close()
    try: proc.wait(timeout=6)
    except Exception: proc.kill()
    return res, alive is not None

print("=== tools register ===")
res, alive = raw_call("profile_tokens", {"messages":[]})
txt = res["result"]["content"][0]["text"] if res and "result" in res else None
check("profile_tokens executes (empty log)", alive and txt is not None)
check("empty profile clean", txt and '"message_count": 0' in txt, (txt or "")[:80])

# duplicate_payload_report
res, alive = raw_call("duplicate_payload_report", {"payloads":["same payload here","same payload here","different stuff now","same payload here"]})
txt = res["result"]["content"][0]["text"] if res and "result" in res else None
check("duplicate_payload_report registers+executes", alive and txt is not None)
check("detects the triple dupe", txt and "tokens_wasted_if_not_cached" in txt, (txt or "")[:80])

print("=== adversarial (server survives) ===")
for tool, args in [
    ("profile_tokens", {"messages": "not-a-list"}),
    ("profile_tokens", {}),
    ("profile_tokens", {"messages": [{"role":"user"}]}),   # missing content
    ("duplicate_payload_report", {"payloads": 42}),
    ("nonexistent_tool", {}),
]:
    res, alive = raw_call(tool, args)
    check(f"{tool} bad/missing args -> survives", alive)

print(f"\n--- RESULT: {PASS} passed, {FAIL} failed ---")
sys.exit(1 if FAIL else 0)
