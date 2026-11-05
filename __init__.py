"""basekit — live Base (chain 8453) intelligence for Hermes. Agent half.

Registers:
  * the ``base_*`` toolset (six read-only tools),
  * a ``pre_llm_call`` hook that injects one line of live Base state, but only on turns
    that are actually about Base and only from a cached snapshot — unrelated turns pay
    nothing,
  * an in-session ``/basekit`` slash command and a ``hermes basekit`` CLI command,
  * the bundled ``basekit`` skill.

Nothing here signs, sends, or stores a key. basekit answers questions about Base; it
cannot move funds.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_HERE = Path(__file__).resolve().parent


def _load(module_name: str, filename: str):
    """Load a sibling module by path (the loader gives us no import package)."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


schemas = _load("basekit_schemas", "schemas.py")
tools = _load("basekit_tools", "tools.py")
core = tools.core

_TOOLS = (
    ("base_network", schemas.NETWORK_SCHEMA, tools.handle_network, "⛓️"),
    ("base_portfolio", schemas.PORTFOLIO_SCHEMA, tools.handle_portfolio, "👛"),
    ("base_tx", schemas.TX_SCHEMA, tools.handle_tx, "🧾"),
    ("base_token", schemas.TOKEN_SCHEMA, tools.handle_token, "🔎"),
    ("base_activity", schemas.ACTIVITY_SCHEMA, tools.handle_activity, "🔄"),
    ("base_allowances", schemas.ALLOWANCES_SCHEMA, tools.handle_allowances, "🛡️"),
)

# --- pre_llm_call keyword gate ------------------------------------------------- #
# PRIMARY terms are Base/crypto-specific enough to inject on their own. SECONDARY terms
# are common in unrelated agent talk ("token budget", "block", "contract"), so they only
# count in pairs. A bare 0x address or hash always counts.
_PRIMARY = (
    r"\bbase\b", r"\bon-?chain\b", r"\bblockchain\b", r"\bevm\b", r"\bwallet\b", r"\bgwei\b",
    r"\bwei\b", r"\busdc\b", r"\busdbc\b", r"\baerodrome\b", r"\baero\b", r"\bdex\b", r"\bamm\b",
    r"\bmempool\b", r"\bmainnet\b", r"\brollup\b", r"\bl2\b", r"\berc-?20\b", r"\berc-?721\b",
    r"\ballowance\b", r"\bapprovals?\b", r"\bportfolio\b", r"\bdefi\b", r"\bcbbtc\b", r"\bcbeth\b",
    r"\bwsteth\b", r"\bdegen\b", r"\bbrett\b", r"\btoshi\b", r"\bgas (?:fee|price)\b",
    r"\beth price\b", r"\bswap\b", r"\btransaction hash\b",
)
_SECONDARY = (r"\btoken\b", r"\bblock\b", r"\bcontract\b", r"\bbalance\b", r"\btx\b",
              r"\btransaction\b", r"\baddress\b", r"\bgas\b", r"\beth\b", r"\bnft\b")
_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b|\b0x[0-9a-fA-F]{64}\b")
_PRIMARY_RE = re.compile("|".join(_PRIMARY), re.IGNORECASE)
_SECONDARY_RE = re.compile("|".join(_SECONDARY), re.IGNORECASE)

_SNAPSHOT: dict[str, Any] = {"at": 0.0, "line": None}
_SNAPSHOT_TTL = 60.0


def _is_base_relevant(text: str) -> bool:
    if not text:
        return False
    if _ADDRESS_RE.search(text):
        return True
    if _PRIMARY_RE.search(text):
        return True
    return len(_SECONDARY_RE.findall(text)) >= 2


def _context_line() -> str | None:
    """One cached line of live Base state, or None when the RPC is unreachable."""
    now = time.time()
    if _SNAPSHOT["line"] and now - _SNAPSHOT["at"] < _SNAPSHOT_TTL:
        return _SNAPSHOT["line"]
    try:
        # Short timeout, no retries: a hook must never delay a turn.
        status = core.network_status(core.Rpc(tools.settings().get("rpc_url") or core.DEFAULT_RPC,
                                             timeout=4.0, retries=0))
        cost = (status.get("cost_usd") or {}).get("eth_transfer_21000")
        line = (
            f"basekit live Base read (chain 8453, read-only, block {status.get('head_block')}): "
            f"gas {status.get('gas_price_gwei')} gwei · ETH ${status.get('eth_price_usd')}"
            + (f" · a plain transfer costs ~${cost}" if cost is not None else "")
            + ". Use the base_* tools for wallet, receipt, token, transfer and approval facts; "
            "every basekit answer states the block it read."
        )
        _SNAPSHOT.update({"at": now, "line": line})
        return line
    except Exception as exc:
        logger.debug("basekit context line unavailable: %s", exc)
        return None


def _pre_llm_call(**kwargs) -> str | None:
    try:
        if not tools.settings().get("inject_chain_context", True):
            return None
        message = kwargs.get("user_message")
        text = message if isinstance(message, str) else json.dumps(message, default=str)
        if not _is_base_relevant(text):
            return None
        return _context_line()
    except Exception as exc:  # pragma: no cover - a hook must fail open
        logger.debug("basekit pre_llm_call failed open: %s", exc)
        return None


# --- commands ------------------------------------------------------------------ #
def _summary_text(detail: str = "summary") -> str:
    result = json.loads(tools.handle_network({"detail": detail}))
    if "error" in result:
        return f"basekit: {result['error']}"
    costs = result.get("cost_usd") or {}
    lines = [
        f"Base (chain {result.get('chain_id')}) — block {result.get('head_block')}",
        f"gas {result.get('gas_price_gwei')} gwei (base fee {result.get('base_fee_gwei')}), ETH ${result.get('eth_price_usd')}",
        f"cost: transfer ~${costs.get('eth_transfer_21000')} · ERC-20 ~${costs.get('erc20_transfer_65000')} · swap ~${costs.get('uniswap_swap_150000')}",
    ]
    if detail == "full":
        rpc = result.get("rpc") or {}
        lines.append(f"rpc {rpc.get('endpoint')} ({rpc.get('latency_ms')} ms)")
    return "\n".join(lines)


def _wallet_text(address: str) -> str:
    if not address:
        address = (tools.settings().get("watch_address") or "").strip()
    if not address:
        return "basekit: no address given and no watch_address set in Basekit settings."
    result = json.loads(tools.handle_portfolio({"address": address}))
    if "error" in result:
        return f"basekit: {result['error']}"
    lines = [f"{result['address']} — ${result['total_usd']} across {result['token_count']} positions"]
    for holding in result.get("holdings", [])[:8]:
        value = holding.get("value_usd")
        lines.append(
            f"  {holding.get('symbol') or holding.get('address')}: {holding.get('balance')}"
            + (f" (${value})" if value is not None else " (no price)")
        )
    return "\n".join(lines)


def _ledger_text(limit: int = 5) -> str:
    entries = tools.read_ledger(limit=limit)
    if not entries:
        return "basekit: nothing looked up yet."
    lines = ["basekit — recent reads (newest first)"]
    for entry in entries:
        target = entry.get("address") or entry.get("owner") or ""
        lines.append(
            f"  {entry.get('ts')} {entry.get('tool')} {target[:10]}"
            + (f" block {entry.get('head_block') or entry.get('block')}" if entry.get("head_block") or entry.get("block") else "")
            + (f" ERROR {entry['error'][:60]}" if entry.get("error") else "")
        )
    return "\n".join(lines)


def _slash(raw_args: str = "") -> str:
    """`/basekit [status|full|wallet <addr>|ledger]`"""
    parts = (raw_args or "").split()
    command = (parts[0].lower() if parts else "status")
    if command in {"status", "summary"}:
        return _summary_text()
    if command == "full":
        return _summary_text("full")
    if command == "wallet":
        return _wallet_text(parts[1] if len(parts) > 1 else "")
    if command == "ledger":
        return _ledger_text()
    if command in {"token", "activity"} and len(parts) > 1:
        handler = tools.handle_token if command == "token" else None
        if command == "token":
            result = json.loads(handler({"address": parts[1]}))
            return json.dumps(result, indent=1)[:1800]
        result = json.loads(tools.handle_activity({"address": parts[1], "token": parts[2]}) if len(parts) > 2
                            else {"error": "usage: /basekit activity <address> <token>"})
        return json.dumps(result, indent=1)[:1800]
    return (
        "basekit — /basekit status | full | wallet <address> | token <address> | "
        "activity <address> <token> | ledger"
    )


def _cli_setup(parser) -> None:
    """`hermes basekit ...` (setup_fn receives the argparse subparser)."""
    sub = parser.add_subparsers(dest="basekit_command")
    sub.add_parser("status", help="Live Base network state (block, gas, ETH, costs)")
    full = sub.add_parser("status-full", help="Status plus the RPC endpoint and latency")
    full.set_defaults(basekit_command="status", detail="full")
    wallet = sub.add_parser("wallet", help="Portfolio for an address")
    wallet.add_argument("address", nargs="?", default="")
    token = sub.add_parser("token", help="Contract facts for one token")
    token.add_argument("address")
    ledger = sub.add_parser("ledger", help="Recent basekit lookups")
    ledger.add_argument("--limit", type=int, default=20)


def _cli(args) -> int:
    command = getattr(args, "basekit_command", None) or "status"
    if command == "wallet":
        print(_wallet_text(getattr(args, "address", "") or ""))
    elif command == "token":
        print(json.dumps(json.loads(tools.handle_token({"address": args.address})), indent=2, ensure_ascii=False))
    elif command == "ledger":
        for entry in tools.read_ledger(limit=max(1, getattr(args, "limit", 20))):
            print(json.dumps(entry, ensure_ascii=False))
    else:
        print(_summary_text(getattr(args, "detail", "summary")))
    return 0


def register(ctx) -> None:
    """Called once by the plugin loader."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset="basekit", schema=schema, handler=handler, emoji=emoji)

    ctx.register_hook("pre_llm_call", _pre_llm_call)

    ctx.register_command(
        "basekit",
        _slash,
        description="Live Base (chain 8453) state, wallet, token and ledger views",
        args_hint="[status|full|wallet <addr>|token <addr>|activity <addr> <token>|ledger]",
    )
    ctx.register_cli_command(
        name="basekit",
        help="Live Base (chain 8453) read-only intelligence",
        setup_fn=_cli_setup,
        handler_fn=_cli,
        description=(
            "Read-only Base tools: network state, wallet portfolios in USD, decoded transaction "
            "receipts, token facts with proxy detection, windowed transfers and approvals. "
            "No key, no signing, no transactions."
        ),
    )

    skill_path = _HERE / "skills" / "basekit" / "SKILL.md"
    if skill_path.exists():
        try:
            ctx.register_skill(
                "basekit",
                skill_path,
                description="Read-only Base (chain 8453) facts: network, wallet, tx, token, transfers, approvals.",
            )
        except Exception as exc:  # pragma: no cover - never block load on the skill
            logger.warning("basekit: skill registration failed: %s", exc)
