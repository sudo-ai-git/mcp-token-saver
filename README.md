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
| `prepare_assessment` | (PRO) de-identify a log for the paid assessment — two tiers, no raw content by default |
| `assess_upgrade_value` | (PRO) call the paid endpoint for the full-optimizer delta |

## The PRO / paid upsell: "what would YOUR setup really save?"

The free core reports **generic** savings (SHA exact-dedupe + n-gram near-dupe).
The **paid assessment** answers the harder question: *"how much more would a
non-exact / semantic optimizer save on top of that?"* — using a proprietary
memo engine that we run **only server-side**.

**Sauce protection (the deal, hard):**
- The **method and weights are never shipped, sent, or returned.** They run on
  our assessment server, opaque; the client receives only a savings number.
- `prepare_assessment` de-identifies to one of two tiers:
  - **tier `hash`** — sends ONLY `role` + token count + `content_sha256` +
    length. No raw content ever leaves the machine. Returns a labeled *proxy*
    delta.
  - **tier `content`** — additionally sends **secrets-scrubbed** text (a public
    pattern redactor blanks `sk-`, `AKIA…`, `password …`, bearer `auth`/`secret`
    keys, card-run digits). Accurate delta; method still server-side/opaque.
- **Honest limit:** the scrubber is a *pattern* matcher, not semantic security.
  For a hard privacy guarantee use tier `hash` (sends no content at all).
- **This repo ships BOTH the client AND the deployable PRO backend**
  (`pro_backend.py` → `mcp-token-saver-pro`). The backend runs the semantic
  engine server-side, opaque, and returns only the savings number. The free
  client talks to it over the documented contract. You deploy the backend on
  infrastructure you control; it is the paid service.

## Run

```bash
# free client — stdio (default)
python3 server.py

# free client — Streamable HTTP
python3 server.py --http --port 9400

# PRO backend (paid service — the semantic delta engine, server-side)
MCP_TOKEN_SAVER_API_KEY="<your-key>" python3 pro_backend.py --port 9500
```

The backend serves:
- `GET /healthz` — liveness
- `POST /assess` — accepts the de-identified rows (tier hash/content), Bearer-auth
  gated (`MCP_TOKEN_SAVER_API_KEY`), returns the real/exposed delta + numbers only.

> The `content` tier computes the semantic near-duplicate delta with the
> internal engine **server-side**; the response contains only `exact_dedupe_savings`,
> `semantic_additional_savings`, totals — never raw content and never any method
> artifact (verified by a test that greps the response for gematria/base-6/vector
> method markers and asserts none leak).

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
