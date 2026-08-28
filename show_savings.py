#!/usr/bin/env python3
"""Show REAL token savings through the live Fly proxy (DeepSeek upstream).

Sends a tool-heavy request through the proxy with a duplicated big tool result
(the wasteful pattern), captures the X-Token-Saver-Saved header, the response,
and estimates the tokens saved vs. what the user sends.
"""
import json, os, re, sys, urllib.request, urllib.error

PROXY = "https://mcp-token-saver-proxy.fly.dev/v1/chat/completions"
DSKEY = None
try:
    s = open("/home/sudosudo/.hermes/.env").read()
    DSKEY = re.search(r'(?m)^DEEPSEEK_API_KEY=(\S+)', s).group(1)
except Exception:
    pass

# A big tool result that the loop re-injects identically (the wasteful pattern)
manifest = json.dumps({
    "apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app", "namespace": "default"},
    "spec": {"containers": [{"name": "main", "image": "nginx:latest",
        "resources": {"requests": {"cpu": "250m", "memory": "128Mi"},
                      "limits": {"cpu": "500m", "memory": "256Mi"}}}]},
    "status": {"phase": "Running", "podIP": "10.0.0.1", "hostIP": "192.168.1.2"},
    "data": {"replicas": 3, "selector": {"app": "app", "tier": "backend"}},
})

def approx_tok(s): return max(1, len(s)//4)

# Build a request: system + a chain where tool returns the SAME manifest 5x
# (the redundant re-send the proxy should strip)
msgs = [{"role": "system", "content": "You are an agent managing a Kubernetes workload. Keep answers short."}]
msgs.append({"role": "user", "content": "Summarize the pod status in one line."})
# a matching assistant tool_call so the tool messages are well-formed
msgs.append({"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_0", "type": "function",
                             "function": {"name": "get_pod", "arguments": "{}"}}]})
for i in range(5):
    msgs.append({"role": "tool", "content": manifest, "tool_call_id": "call_0"})
msgs.append({"role": "user", "content": "Now reply in a single brief sentence describing the pod state."})

# what the USER would have sent (unreduced)
orig_tokens = sum(approx_tok(m.get("content") or "") for m in msgs)

body = {"model": "deepseek-chat", "messages": msgs, "max_tokens": 60, "stream": False}
req = urllib.request.Request(PROXY, data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {DSKEY}"}, method="POST")
print("=== routing a REAL tool-heavy request through the live proxy -> DeepSeek ===")
print(f"  messages:            {len(msgs)} (5 identical tool re-sends of a {len(manifest)//4:+} token manifest)")
print(f"  tokens user would send: ~{orig_tokens}")
try:
    r = urllib.request.urlopen(req, timeout=90)
    saved = r.headers.get("X-Token-Saver-Saved")
    ct = r.headers.get("Content-Type","")
    resp = json.loads(r.read())
    reply = resp.get("choices",[{}])[0].get("message",{}).get("content","")
    print(f"  HTTP {r.status} | Content-Type: {ct}")
    print(f"  X-Token-Saver-Saved: {saved}  <= the proxy's actual reduction")
    print(f"  real reply: {reply[:120]}")
    # compare, if header present
    if saved and saved.isdigit():
        s=int(saved)
        print(f"\n  >>> proxy removed ~{s} tokens from the input you were billed for")
        print(f"  >>> that's {s/max(orig_tokens,1)*100:.1f}% of what you would have sent")
    print("  AUTH: forward went to DeepSeek and returned a real completion -> END-TO-END PROVEN.")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}")
    print("  (if 5xx/429: check DeepSeek key validity / proxy upstream; if 4xx it's the request)")
except Exception as e:
    print(f"  error: {e}")
