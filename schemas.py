"""Tool schemas for the basekit toolset.

Kept separate from the handlers so the shapes can be unit-tested (and reviewed by a
model) without importing the agent-facing surface. Every description states the
read-only guarantee, and the two log-reading tools name their window honestly instead
of implying they scan the whole chain.
"""

from __future__ import annotations

from typing import Any

TOOLSET = "basekit"

_ADDRESS = "0x-prefixed 20-byte EVM address"
_TX = "0x-prefixed 32-byte transaction hash"


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _int(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    parameters["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": parameters}


NETWORK_SCHEMA = _schema(
    "base_network",
    "Live Base (chain 8453) network state: head block, gas price and base fee in gwei, "
    "ETH/USD, and the USD cost of a plain ETH transfer, an ERC-20 transfer and a swap. "
    "Call this before telling the user anything about current Base conditions or fees. "
    "Read-only: no key, no transaction.",
    {
        "detail": {
            "type": "string",
            "enum": ["summary", "full"],
            "description": "summary (default) returns the state and cost table; full also includes the raw RPC endpoint and latency.",
        }
    },
)

PORTFOLIO_SCHEMA = _schema(
    "base_portfolio",
    "Native ETH plus ERC-20 balances with USD values for one Base address, resolved from "
    "a curated token set and priced with DefiLlama. Balances come straight from the chain; "
    "a token with no price reports value_usd: null rather than a guess. Read-only.",
    {
        "address": _str(f"Wallet address to read. Required. {_ADDRESS}."),
        "extra_tokens": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"Additional token addresses to include beyond the built-in set (max 25). {_ADDRESS} each.",
        },
        "include_zero": {
            "type": "boolean",
            "description": "Include positions with a zero balance (default false) - useful to prove a wallet sold everything.",
        },
    },
    ["address"],
)

TX_SCHEMA = _schema(
    "base_tx",
    "Decode one Base transaction into a human-readable receipt: success or reverted, block and "
    "UTC time, from/to, ETH value, gas used, the fee in ETH and USD, whether it deployed a "
    "contract, and every ERC-20 / ERC-721 token movement and approval it emitted. Use this "
    "instead of guessing what a hash did. Read-only.",
    {"hash": _str(f"Transaction hash to decode. Required. {_TX}.")},
    ["hash"],
)

TOKEN_SCHEMA = _schema(
    "base_token",
    "Contract-level facts for one Base token: name, symbol, decimals, total supply, owner() if "
    "exposed, proxy detection (EIP-1967 slot, EIP-1167 clone, or a custom implementation() "
    "proxy, with the implementation address), and capability hints from 4-byte selector "
    "presence in the deployed code. These are heuristics and are labelled as such - this tool "
    "does not claim honeypot or rug detection. Read-only.",
    {"address": _str(f"Token or contract address to inspect. Required. {_ADDRESS}.")},
    ["address"],
)

ACTIVITY_SCHEMA = _schema(
    "base_activity",
    "Recent ERC-20 transfers in and out of one address for ONE token, newest first, with USD "
    "values. Public Base RPCs reject chain-wide log scans, so this takes a token address and a "
    "block window (default 20000 blocks, about 11 hours; max 50000) and reports the window it "
    "actually read. Raise it by pointing basekit at your own RPC endpoint in settings. Read-only.",
    {
        "address": _str(f"Wallet address whose transfers you want. Required. {_ADDRESS}."),
        "token": _str(f"Token contract to scan. Required - this is what makes the query legal on a public RPC. {_ADDRESS}."),
        "blocks": _int("How many blocks back to scan (default 20000, max 50000; Base is ~2s per block)."),
        "limit": _int("Maximum transfers to return (default 25, max 200)."),
    },
    ["address", "token"],
)

ALLOWANCES_SCHEMA = _schema(
    "base_allowances",
    "Approvals an owner granted on ONE token: which spenders they approved inside the scan "
    "window, the CURRENT on-chain allowance for each (re-read live; already-revoked ones are "
    "dropped), whether it is unlimited, and the USD value at risk. Answers 'what can still "
    "drain this wallet'. Window-bounded like base_activity, and it never revokes anything. Read-only.",
    {
        "owner": _str(f"Wallet that granted the approvals. Required. {_ADDRESS}."),
        "token": _str(f"Token contract whose approvals you want. Required. {_ADDRESS}."),
        "blocks": _int("How many blocks back to scan for Approval events (default 20000, max 50000)."),
    },
    ["owner", "token"],
)

ALL_SCHEMAS = (NETWORK_SCHEMA, PORTFOLIO_SCHEMA, TX_SCHEMA, TOKEN_SCHEMA, ACTIVITY_SCHEMA, ALLOWANCES_SCHEMA)
