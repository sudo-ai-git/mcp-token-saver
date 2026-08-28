#!/usr/bin/env python3
"""Measure the proxy's real token reduction (increment C).

Replays a faithful replicate of the agent traffic pattern behind our measured
usage (962 req / ~289K tokens-req / 278M tokens day, DeepSeek v4-flash) through
ProxyOptimizer and reports pre-provider token reduction.

We can't send the raw 278M tokens (we only have the aggregate CSV), so we
construct a REPRESENTATIVE conversation matching the measured pattern:
a long tool-heavy agent loop that re-injects the same large tool results
multiple times (the exact waste the proxy targets), at realistic sizes.

Honest: this is a structured replicate grounded in the measured profile, not a
replay of the literal 278M-token payload (we don't have the raw messages). The
savings% is what the proxy would achieve on the redundant/re-sent portion.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_optimize import ProxyOptimizer, _approx_tokens

def build_tool_heavy_session(n_turns=30, manifest_tokens=1500):
    """A tool-heavy agent session matching the 'tool JSON re-sent 30x' pattern."""
    messages = []
    messages.append({"role": "system", "content": "You are an autonomous agent managing a cloud workload. Use tools to inspect state, then report."})
    # a big tool result (K8s manifest, ~1500 tokens) that the loop re-injects
    manifest = ("apiVersion: v1 -- kind: Pod -- metadata name app namespace default "
                "spec containers image nginx resources requests cpu 250m memory 128Mi "
                "limits cpu 500m memory 256Mi "
                + "volumeMounts mountPath /data name data readOnly false " * 20)
    # pad to ~target tokens with realistic field repetition
    while _approx_tokens(manifest) < manifest_tokens:
        manifest += "status phase Running podIP 10.0.0.1 hostIP 192.168.1.2 startTime 2026-08-28 conditions ready True " 
    for t in range(n_turns):
        messages.append({"role": "user", "content": f"turn {t}: what is the current state of the workload?"})
        # the tool re-returns the SAME big manifest each turn (the wasteful resend)
        messages.append({"role": "tool", "content": manifest})
        messages.append({"role": "assistant", "content": f"Replying for turn {t}: result observed."})
    # plus repeated log tails near the end
    log = "INFO 2026-08-28T10:00:00Z request completed status=200 method=GET path=/api/v1/health latency=42ms"
    for _ in range(10):
        messages.append({"role": "tool", "content": log})
    return messages

def main():
    msgs = build_tool_heavy_session()
    orig_tok = sum(_approx_tokens(m.get("content") or "") for m in msgs)
    opt = ProxyOptimizer()  # fresh instance so dedupe is session-scoped
    r = opt.optimize(msgs)
    saved = r["stats"]["removed_approx_tokens"] + r["stats"]["compressed_approx_tokens"]
    print("=== proxy real-data measurement (structured replicate of our profile) ===")
    print(f"  messages:      {len(msgs)} -> {len(r['messages'])}")
    print(f"  input tokens:  ~{orig_tok:,} -> ~{r['stats']['optimized_approx_tokens']:,}")
    print(f"  removed:       ~{saved:,} tokens ({saved/orig_tok*100:.1f}%)")
    print(f"  exact dupes dropped:  {r['stats']['removed']}")
    print(f"  near-dupes compressed: {r['stats']['compressed']}")
    print(f"  summary: {ProxyOptimizer.summarize(r['stats'])}")
    print()
    print("HONEST: this is a representative tool-heavy profile, not the literal 278M")
    print("token payload (we only hold aggregate CSVs). The % is what the proxy would")
    print("recover on the redundant re-sent portion — the slice native caching")
    print("does NOT already absorb. Real single-provider-with-good-caching savings")
    print("will be lower; the CROSS-PROVIDER + no-native-cache case is where the big")
    print("number lives.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
