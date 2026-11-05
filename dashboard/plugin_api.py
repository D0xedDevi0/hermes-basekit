"""basekit dashboard backend — routes for the desktop pane, mounted at /api/plugins/basekit/.

Every handler is a thin wrapper over ``basekit_core``, the same engine the agent tools
use, so the pane and the model can never disagree about a number.

Read-only: no route mutates chain state, none accepts a key, and the optional Basescan
key is reported as presence-only (``basescan_key_present``) and never echoed back.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status as http_status

log = logging.getLogger(__name__)
router = APIRouter()

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_EVENT_POLL_SECONDS = 15.0


def _load(module_name: str, filename: str):
    """Load a sibling module by path; the loader gives plugin_api.py no import package."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


core = _load("basekit_core", "basekit_core.py")
tools = _load("basekit_tools", "tools.py")


def _settings() -> dict:
    return tools.settings()


def _rpc():
    return tools.get_rpc()


def _guard(fn, *args, **kwargs):
    """Run a blocking engine call off the event loop and map failures to honest HTTP codes."""
    try:
        return fn(*args, **kwargs)
    except core.BasekitError as exc:
        raise HTTPException(status_code=http_status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


async def _guard_async(fn, *args, **kwargs):
    return await asyncio.to_thread(_guard, fn, *args, **kwargs)


def _snapshot_payload() -> dict:
    settings = _settings()
    return core.snapshot(
        _rpc(),
        watch_address=(settings.get("watch_address") or "").strip() or None,
        ledger=tools.read_ledger(limit=8),
        plugin_dir=_PLUGIN_DIR,
    )


@router.get("/status")
async def status() -> dict[str, Any]:
    """Chain state (+ the watch wallet's total and the recent ledger) in one payload."""
    return await _guard_async(_snapshot_payload)


@router.get("/portfolio")
async def portfolio(
    address: str = Query(..., description="Wallet address to read"),
    include_zero: bool = Query(False, description="Include zero balances"),
) -> dict[str, Any]:
    if not core.is_address(address):
        raise HTTPException(status_code=400, detail=f"{address!r} is not a valid EVM address")
    return await _guard_async(
        core.portfolio, _rpc(), address, None, include_zero, _PLUGIN_DIR
    )


@router.get("/token")
async def token(address: str = Query(..., description="Token contract address")) -> dict[str, Any]:
    if not core.is_address(address):
        raise HTTPException(status_code=400, detail=f"{address!r} is not a valid EVM address")
    return await _guard_async(
        core.token_facts, _rpc(), address,
        None, core.verify_source(address, core.basescan_key()),
    )


@router.get("/activity")
async def activity(
    address: str = Query(..., description="Wallet address"),
    token: str = Query(..., description="Token contract to scan"),
    blocks: int = Query(20000, ge=500, le=50000),
    limit: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:
    for value in (address, token):
        if not core.is_address(value):
            raise HTTPException(status_code=400, detail=f"{value!r} is not a valid EVM address")
    return await _guard_async(core.token_activity, _rpc(), address, token, blocks, limit)


@router.get("/ledger")
async def ledger(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """What the agent actually looked up, and at which block."""
    return {"entries": tools.read_ledger(limit=limit)}


@router.get("/settings")
async def settings() -> dict[str, Any]:
    """Effective settings, with the optional secret reduced to a boolean."""
    current = dict(_settings())
    current["basescan_key_present"] = bool(current.pop("basescan_api_key", ""))
    return current


def _ws_authorized(ws: WebSocket) -> bool:
    """Unused: kept as documentation of where authorisation actually happens.

    The host applies its core Host/Origin/peer gate (``PluginApiWebSocketGate`` →
    ``_ws_request_is_allowed``) to *every* plugin socket, before this route's own code
    is reached, and refuses disabled/unknown plugins there too. Re-checking by importing
    ``hermes_cli.web_server_chat`` would duplicate that logic against a private core
    module (a catalog rule-9 violation) and could drift from core auth — so the route
    below simply trusts the host gate.
    """
    return True


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    """Push a fresh snapshot every ~15s. The pane also polls, since this is a no-op on
    OAuth remotes (documented SDK behaviour) and must never be the only data path.

    Authentication is the host's job: ``PluginApiWebSocketGate`` runs the core
    Host/Origin/peer check for every plugin socket before this handler is entered.
    """
    await ws.accept()
    try:
        while True:
            try:
                payload = await _guard_async(_snapshot_payload)
                await ws.send_json({"snapshot": payload})
            except HTTPException as exc:
                # Keep the socket alive on an RPC hiccup; the client shows the error text.
                await ws.send_json({"error": exc.detail})
            try:
                # Race receive() against the interval so a disconnect is noticed while idle.
                message = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                if message.get("type") == "websocket.disconnect":
                    return
            except TimeoutError:
                continue
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return  # normal shutdown
    except Exception as exc:  # never take the dashboard worker down
        log.warning("basekit event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
