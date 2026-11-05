---
name: basekit
description: Read-only Base (chain 8453) on-chain facts — network/gas state, wallet portfolios, decoded transaction receipts, token facts, transfers, approvals. Use when a question touches Base, a 0x address, gas, ETH price, a tx hash, or a token's contract.
version: 1.0.0
metadata:
  hermes:
    tags: [base, onchain, evm, read-only, defi]
    category: blockchain
    related_skills: [base-l2-free-data-stack, honest-contract-audit]
---

# basekit — read-only Base intelligence

basekit answers questions about Base (chain 8453) with **live chain reads**: what the
network costs right now, what a wallet holds, what a transaction actually did, what a
token contract is, and what approvals can still move funds. It ships six tools, a
`pre_llm_call` hook, a `/basekit` slash command and a desktop pane.

**It cannot sign, send, or hold a key.** Every tool is a read. If the user asks you to
send, swap, approve or revoke, say plainly that basekit is read-only and use whatever
other tooling the user has configured — never imply basekit can move funds.

## Rules that keep basekit honest

1. **State the block you read.** Every payload carries `head_block` (or `block` for a
   receipt). On-chain state is a moving target; a number without a block is a rumour.
   Quote the block when the answer matters.
2. **Never guess a missing value.** A token with no price returns `value_usd: null`.
   Report it as unknown; do not estimate. `eth_price_usd` comes from DefiLlama (with a
   Coinbase spot fallback) and is labelled as a market price, not an on-chain one.
3. **Log-window answers are windowed.** `base_activity` and `base_allowances` are
   bounded by an explicit block range (default 20,000 blocks ≈ 11h; max 50,000) because
   public RPCs reject chain-wide `eth_getLogs` without an address filter. The payload
   echoes `window.from_block` / `window.to_block` / `approx_hours`. **Repeat the window
   when you report results** — "no transfers in the last 11 hours" is true and useful;
   "this wallet never received USDC" is a lie the window does not support.
4. **Heuristics are labelled as heuristics.** `base_token`'s `bytecode_flags` come from
   4-byte selector presence in deployed code — a hint about capability, not proof of
   behaviour. basekit does **not** do honeypot, rug or scam verdicts; those need
   simulation it deliberately does not do. Say so instead of extrapolating.
5. **An `error` field is the answer.** RPC failures come back as
   `{"error": ..., "hint": ...}`. Relay the reason; don't paper over it or invent data.

## Tools

| Tool | Use it for |
| --- | --- |
| `base_network` | Head block, gas price / base fee / priority fee in gwei, ETH/USD, and USD cost of an ETH transfer, an ERC-20 transfer and a 150k-gas swap. Call this before making any claim about current Base fees. |
| `base_portfolio` | Native ETH + ERC-20 balances with USD values for one address, across a curated 12-token set plus any extra addresses passed in. |
| `base_tx` | Decode a tx hash: success/reverted, block, UTC time, from/to, ETH value, gas used, fee in ETH **and** USD, contract creation, every ERC-20/ERC-721 transfer, every approval. |
| `base_token` | name, symbol, decimals, total supply, `owner()`, proxy detection (EIP-1967 slot / EIP-1167 clone / custom `implementation()`), capability hints, optional source verification. |
| `base_activity` | Transfers in and out for one address **and one token** inside a block window, newest first, priced where a price exists. |
| `base_allowances` | Approvals an owner granted on one token: spenders seen in the window, their **current** allowance (re-read live, revoked ones dropped), `unlimited` flag, and USD at risk. |

## Workflows

**"Is now a good time to transact?"** → `base_network`, then quote gas in gwei *and* the
dollar cost of the specific operation. Base gas is cheap enough that the honest answer is
usually "fees are irrelevant here" — say that rather than performing drama about it.

**"What happened in this tx?"** → `base_tx` and lead with the token movements and the fee
in USD, not the raw gas numbers. Read `status` first: a reverted tx still costs the fee,
and that distinction is usually what the user actually cares about.

**"Is this token safe?"** → `base_token` for facts (supply, proxy, implementation, owner)
and then be explicit that basekit does not score risk. A proxy with an upgradeable
implementation and a live `owner()` is a **statement of fact**, not a verdict. Pair with
a real scanner if the user needs a risk call.

**"What can drain this wallet?"** → `base_allowances` per token, then report
`unlimited_count` and the top `value_at_risk_usd` entries with the window caveat.
basekit never revokes; the user decides.

## Configuration

Settings live under `plugins.entries.basekit.settings` (Desktop → Settings → Plugins):

- `rpc_url` — default `https://base-rpc.publicnode.com`. Point this at your own RPC for
  wider log windows and higher rate limits; the tools automatically report the window
  they could read.
- `watch_address` — optional wallet shown in the pane and status-bar chip.
- `inject_chain_context` — default on: one line of live Base state is injected via
  `pre_llm_call` **only** on turns that mention Base, a chain term, or a 0x address
  (cached 60s). Turn it off for zero prompt cost.
- `basescan_api_key` — optional, used only for `base_token` source verification. Without
  it that field reports `not_configured` rather than guessing.

## Pitfalls

- `eth_getLogs` on public Base RPCs **requires** an `address` filter and rejects wide
  ranges. That is why `base_activity` and `base_allowances` take a token address: it is
  a protocol constraint, not a design preference.
- A single call is cheaper than a Multicall3 round trip — basekit switches
  automatically, so don't "optimise" by forcing multicall for one read.
- Token metadata can be missing on odd contracts; a `null` symbol means the contract
  didn't answer, not that it's hostile.
- Don't re-price a position with a stale `eth_price_usd` from an old block; re-run the
  tool.
