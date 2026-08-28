# mcp-token-saver

> Deterministic, no-LLM **token-usage profiler + redundancy advisor** for agent
> conversations. Shows where your tokens actually go and what you can save —
> using only public, auditable techniques.

## What it does

For a conversation/tool log, it reports:

- **Token spend by role** (system / user / assistant / tool) — where the cost is.
- **Exact-duplicate payloads** — identical messages/tool-results re-sent (the
  classic bloat: the same big tool JSON sent 30 turns apart). Cache by the
  returned SHA-256 key to avoid the resend.
- **Near-duplicate pairs** — repeated templated results or log tails that are
  ~mostly the same (n-gram overlap).
- **System-prompt bloat** — `system` role share; if it dominates, a static-cache
  / summarization flag.
- **Savings potential** — a single % number: redundant tokens / total.

## The "no sauce" contract (deliberate)

This tool uses **only public generic methods**:
- exact dedupe via **SHA-256** of the raw bytes
- near-dupe via **word n-gram Jaccard overlap**
- model-agnostic token approximation (chars/4) unless you pass real counts

It ships **no proprietary semantic layer** — no embedding model, no tuned
weights, no custom fingerprint, no internal reduction method. That keeps it
independently auditable, MIT-usable, and crown-jewel-clean. A client that wants
semantic (not exact) memoiszation adds that layer on their side; this tool is
the transparent, verifiable baseline.

## Tools (MCP)

| tool | purpose |
|---|---|
| `profile_tokens` | analyze a message log → per-role tokens, exact/near dupes, savings %, cache keys |
| `duplicate_payload_report` | given a list of payload strings, report exact/near duplicates |

## Run

```bash
# stdio (default)
python3 server.py

# Streamable HTTP
python3 server.py --http --port 9400
```

## Test evidence

- `test_token_saver.py` — 21 assertions: dup detection, near-dupe, role split,
  system-share, provided-token honor, edge (empty/None/unicode/10k/200-msg),
  determinism, AND a **sauce-guard** asserting no gematria/base-6/tuned-weights
  leak into output.
- `e2e_token_saver.py` — 9 assertions: both tools register + execute over real
  MCP stdio, triple-dupe detected, server survives adversarial/missing args.
- Combined **30 passing** across unit + real-MCP-transport.

**Scope honest note:** this gives *approximate savings potential* via neutral
token/char approximation and public redundancy detection — it's a profiler and
advisor, not a token-accurate billing engine. Feed real `provided_tokens` for
exact numbers. It reports *where* redundancy is; it doesn't rewrite messages.
