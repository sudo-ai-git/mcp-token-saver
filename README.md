# mcp-token-saver

> **Request-path token proxy + deterministic analyzer for agent conversations.**
> Stop paying for redundant tokens — proven live.

**⚡ FLAGSHIP — the request-path proxy (live):**
`mcp-token-saver-proxy` sits between your agent and any OpenAI-compatible
provider, deduping redundant tool results **before** they're billed. Streaming,
deterministic, no-LLM. Point any agent at it via `base_url`:

```
https://mcp-token-saver-proxy.fly.dev
```

**Live proof (not a benchmark):** a tool-heavy request routed through the proxy
to DeepSeek removed **416 of 562 input tokens — 74%** of what you'd be billed
for (measured via the `X-Token-Saver-Saved` header on a real, completed
inference). See `show_savings.py` to reproduce.

**🪙 Try Pro (1-hour trial) / Go Pro (crypto):** [product page](https://sudo-ai-git.github.io/mcp-token-saver/)

## Route your agent through the proxy (10 seconds)

```bash
# OpenAI-compatible — just point your client's base_url at the proxy:
export OPENAI_BASE_URL="https://mcp-token-saver-proxy.fly.dev"
export OPENAI_API_KEY="<your-provider-key>"   # forwarded unchanged

# your existing client calls work unchanged; the proxy strips redundant
# tool results before they reach your provider, and returns an
# X-Token-Saver-Saved header so you can see the savings per request.
```

Self-host instead: `UPSTREAM_BASE_URL=https://api.openai.com/v1 python3 proxy_server.py`

mcp-name: io.github.sudo-ai-git/mcp-token-saver

---

## The analyzer — the number you can't see is the one that's costing you

A single agent day on one model — measured from our own stack:

- **962 requests**
- **~289,000 input tokens per request** (a growing context you can't see)
- **278,112,413 input tokens in 24h**
- **$5.61 / day** on one model

Long agent loops append every tool result to context and re-send it on each turn.
Native prompt caching catches most of the re-send — but leaves a **0.78% cache-miss
tail** (2.17M fresh tokens/day, ~$28/mo on one model) and can't help at all across
**13 separate provider caches** once you span more than one model. All of it is
invisible until you export a usage CSV and stare at it.

`mcp-token-saver` surfaces that waste in seconds — per role, per payload, as a
single redundant-token number. Same-agent behavior, but you finally see the bill.

## What it reports

- **Token spend by role** (system / user / assistant / tool) — where the cost is
- **Exact-duplicate payloads** — identical messages/tool-results re-sent (the
  classic bloat: the same big tool JSON sent 30 turns apart), with their SHA-256
  cache key
- **Near-duplicate pairs** — repeated templated results or log tails (~same via
  n-gram overlap)
- **System-prompt bloat** — `system` role share; if it dominates, a cache /
  summarization flag
- **Savings potential** — one number: redundant tokens ÷ total

## Install & quick start

```bash
pip install mcp-token-saver-pro   # our clean PyPI name (publishing 1.0.0)
# or, from source (works today):
uv tool install git+https://github.com/sudo-ai-git/mcp-token-saver
```

```python
from mcp_token_saver import profile_log
result = profile_log(messages)   # your conversation / tool log
print(result["baseline_total_tokens"], result["exact_dedupe_savings"])
```

Or add it as an MCP server to any MCP-compatible assistant (Claude, VS Code,
Cursor, OpenHands, gateway/CLI sessions).

## The "no sauce" contract (deliberate)

Only **public generic methods**:
- exact dedupe via **SHA-256** of the raw bytes
- near-dupe via **word n-gram Jaccard overlap**
- model-agnostic token approximation (chars/4) unless you pass real counts

No embeddings, no tuned weights, no custom fingerprint, no internal reduction
method. Independently auditable, MIT-usable, crown-jewel-clean. The Pro
assessment adds a calibrated server-side semantic-delta proxy (de-identified)
on the same principled baseline.

## Free vs Pro

| | Free (MIT) | Pro (30-day) |
|---|---|---|
| Per-role token profiling | ✅ | ✅ |
| Exact + near-dupe detection | ✅ | ✅ |
| De-identified semantic-delta assessment | — | ✅ (server-side, calibrated) |
| Price | $0 | **$250 / 30 days** |
| Payment | — | **Crypto** (BTC/ETH/XRP/SOL/USDT), no KYC |
| Trial | — | **1 hour free** |

## Tools (MCP)

| tool | purpose |
|---|---|
| `profile_tokens` | analyze a message log → per-role tokens, exact/near dupes, savings %, cache keys |
| `duplicate_payload_report` | given a list of payload strings, report exact/near duplicates |
| `prepare_assessment` | (PRO) de-identify a log for the paid assessment — tiers, no raw content by default |

## Why you can trust the numbers

Run it, diff it, audit it — the core is open MIT. The Pro path is crypto-native
and end-to-end de-identified, with no recurring charge unless you choose it.

---

**License:** MIT · **Product:** [sudo-ai-git.github.io/mcp-token-saver](https://sudo-ai-git.github.io/mcp-token-saver/) · **Issues:** this repo
