"""Agent-facing handlers for basekit.

Each handler takes the parsed tool arguments, calls the shared read engine and returns a
JSON string. Failures come back as {"error": ...} with an actionable hint rather than a
traceback, because a public RPC rate-limiting us is a normal Tuesday, not a bug.

The engine is loaded from this directory by path so the same file backs the agent half
and the web/desktop backend without assuming either one's import layout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def load_core():
    """Import basekit_core once, under a stable module name shared by every entry point."""
    if "basekit_core" in sys.modules:
        return sys.modules["basekit_core"]
    spec = importlib.util.spec_from_file_location("basekit_core", _HERE / "basekit_core.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["basekit_core"] = module
    spec.loader.exec_module(module)
    return module


core = load_core()

_RPC_LOCK = threading.Lock()
_RPC_CACHE: dict[str, Any] = {}
_MAX_EXTRA_TOKENS = 25


def settings() -> dict:
    return core.load_settings()


def data_dir() -> Path:
    """Where basekit keeps its ledger. Mirrors <HERMES_HOME>/plugin-data/<id>/ by default."""
    override = os.environ.get("HERMES_BASEKIT_DATA_DIR")
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "plugin-data" / core.PLUGIN_ID


def ledger_path() -> Path:
    return data_dir() / "ledger.jsonl"


def get_rpc(url: str | None = None):
    """One cached client per endpoint, so the cache and failover state are actually reused."""
    target = (url or settings().get("rpc_url") or core.DEFAULT_RPC).strip() or core.DEFAULT_RPC
    with _RPC_LOCK:
        rpc = _RPC_CACHE.get(target)
        if rpc is None:
            rpc = core.Rpc(target)
            _RPC_CACHE[target] = rpc
    return rpc


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=1, ensure_ascii=False, default=str)


def _fail(exc: Exception) -> str:
    if isinstance(exc, core.BasekitError):
        return _json({
            "error": str(exc),
            "hint": "Base RPC endpoints are public and rate-limited; retry, or set an rpc_url in Basekit settings.",
            "read_only": True,
        })
    return _json({
        "error": f"{type(exc).__name__}: {exc}",
        "hint": "Unexpected failure inside basekit; the traceback is in ~/.hermes/logs/errors.log.",
        "read_only": True,
    })


def _note(tool: str, args: dict, result: dict | None = None, error: str | None = None) -> None:
    """Append a ledger line: which question was asked, at which block, and how it went."""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "args": {k: v for k, v in (args or {}).items() if v not in (None, "", [])},
        }
        if error:
            entry["error"] = error[:200]
        if isinstance(result, dict):
            for key in ("head_block", "block", "count", "active_count", "total_usd", "address", "owner", "symbol"):
                if result.get(key) is not None:
                    entry[key] = result[key]
            if isinstance(result.get("network"), dict):  # snapshot-shaped payloads
                entry["head_block"] = result["network"].get("head_block")
        core.append_ledger(ledger_path(), entry)
    except Exception:  # pragma: no cover - the ledger must never break a tool call
        pass


def read_ledger(limit: int = 30) -> list[dict]:
    return core.read_ledger(ledger_path(), limit=limit)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def handle_network(args: dict, **_kw) -> str:
    try:
        out = core.network_status(get_rpc())
        if (args or {}).get("detail") != "full":
            out.pop("rpc", None)
        _note("base_network", args, out)
        return _json(out)
    except Exception as exc:
        _note("base_network", args, error=str(exc))
        return _fail(exc)


def handle_portfolio(args: dict, **_kw) -> str:
    args = args or {}
    try:
        extra = [a for a in (args.get("extra_tokens") or []) if isinstance(a, str)][:_MAX_EXTRA_TOKENS]
        out = core.portfolio(
            get_rpc(),
            args.get("address") or "",
            extra_tokens=extra,
            include_zero=bool(args.get("include_zero")),
            plugin_dir=_HERE,
        )
        _note("base_portfolio", args, out)
        return _json(out)
    except Exception as exc:
        _note("base_portfolio", args, error=str(exc))
        return _fail(exc)


def handle_tx(args: dict, **_kw) -> str:
    args = args or {}
    try:
        out = core.tx_receipt(get_rpc(), args.get("hash") or "")
        _note("base_tx", args, out)
        return _json(out)
    except Exception as exc:
        _note("base_tx", args, error=str(exc))
        return _fail(exc)


def handle_token(args: dict, **_kw) -> str:
    args = args or {}
    try:
        address = args.get("address") or ""
        out = core.token_facts(get_rpc(), address, verification=core.verify_source(address, core.basescan_key()))
        _note("base_token", args, out)
        return _json(out)
    except Exception as exc:
        _note("base_token", args, error=str(exc))
        return _fail(exc)


def handle_activity(args: dict, **_kw) -> str:
    args = args or {}
    try:
        out = core.token_activity(
            get_rpc(),
            args.get("address") or "",
            args.get("token") or "",
            blocks=int(args.get("blocks") or 20000),
            limit=max(1, min(int(args.get("limit") or 25), 200)),
        )
        _note("base_activity", args, out)
        return _json(out)
    except Exception as exc:
        _note("base_activity", args, error=str(exc))
        return _fail(exc)


def handle_allowances(args: dict, **_kw) -> str:
    args = args or {}
    try:
        out = core.token_allowances(
            get_rpc(),
            args.get("owner") or "",
            args.get("token") or "",
            blocks=int(args.get("blocks") or 20000),
        )
        _note("base_allowances", args, out)
        return _json(out)
    except Exception as exc:
        _note("base_allowances", args, error=str(exc))
        return _fail(exc)


HANDLERS = {
    "base_network": handle_network,
    "base_portfolio": handle_portfolio,
    "base_tx": handle_tx,
    "base_token": handle_token,
    "base_activity": handle_activity,
    "base_allowances": handle_allowances,
}
