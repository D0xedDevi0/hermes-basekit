"""basekit core — a dependency-free Base (chain 8453) read engine for Hermes.

Everything here is stdlib-only on purpose: a plugin that brings no PyPI packages
resolves inside Hermes's core constraints with nothing to review, and the whole
surface stays auditable in one file.

Design notes that matter:

* **Read-only by construction.** basekit never signs, sends, or holds a key. It
  answers questions about Base; it cannot move funds. That is a deliberate scope
  decision, not an unfinished feature.
* **Verified constants.** Every selector, topic hash and storage slot below was
  re-derived with `cast keccak` rather than copied from memory; tests/test_core.py
  re-checks them offline. Do not "fix" one from memory — re-derive it.
* **Bounded, address-filtered log reads.** Public Base RPCs (publicnode, mainnet.base.org)
  reject `eth_getLogs` without an `address` filter and cap ranges (~50k blocks). Every
  log-based tool here therefore takes an explicit token address and a block window, and
  says so in its output instead of pretending to scan the whole chain.
* **Failover, not a single point of failure.** Requests try the configured endpoint
  first, then a small list of public ones, remembering whichever answered last.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION = "1.0.0"
PLUGIN_ID = "basekit"
CHAIN_ID = 8453
CHAIN_NAME = "Base"
NATIVE_SYMBOL = "ETH"
WEI = 10**18
GWEI = 10**9

#: Endpoint the user gets unless they set one. PublicNode serves both plain calls and
#: `eth_getLogs` (1000+ block windows) without a key; mainnet.base.org 429s on logs.
DEFAULT_RPC = "https://base-rpc.publicnode.com"
#: Tried in order when the preferred endpoint fails or rate-limits.
RPC_FALLBACKS = (
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://base.drpc.org",
    "https://base.meowrpc.com",
)

#: Canonical Multicall3 deployment (same address on every chain that has it).
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_AGGREGATE3 = "82ad56cb"  # cast keccak 'aggregate3((address,bool,bytes)[])'

#: Event topics (cast keccak of the canonical signatures).
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
TOPIC_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"  # Approval(address,address,uint256)
TOPIC_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"  # ERC-1155

#: Function selectors used by basekit (cast keccak, first 4 bytes).
SEL = {
    "balanceOf": "70a08231",
    "decimals": "313ce567",
    "symbol": "95d89b41",
    "name": "06fdde03",
    "totalSupply": "18160ddd",
    "owner": "8da5cb5b",
    "implementation": "5c60da1b",
    "allowance": "dd62ed3e",
}

#: Selectors whose *presence in bytecode* we report as a capability hint. Presence is a
#: heuristic (a selector can appear in unreachable code or data); basekit labels it as such.
BYTECODE_PROBES = {
    "40c10f19": "mint(address,uint256)",
    "8456cb59": "pause()",
    "5c975abb": "paused()",
    "f9f92be4": "blacklist(address)",
    "69fe0e2d": "setFee(uint256)",
    "715018a6": "renounceOwnership()",
    "f2fde38b": "transferOwnership(address)",
    "8da5cb5b": "owner()",
}

#: EIP-1967 storage slots = keccak256(name) - 1 (re-derived with cast, see tests).
EIP1967_IMPLEMENTATION = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_BEACON = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
EIP1967_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

#: Default token set for portfolio reads. Addresses are canonical Base deployments;
#: `scripts/verify_tokens.py` re-checks each one live (symbol/decimals) against the chain.
TOKENS_FILENAME = "tokens.json"

#: Cache lifetimes (seconds) — tuned so a busy agent does not hammer a public endpoint.
TTL_BLOCK = 4.0
TTL_GAS = 6.0
TTL_PRICE = 90.0
TTL_TOKEN_FACTS = 600.0

OWNER_ADDRESS_RE = None  # compiled lazily in `is_address`


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BasekitError(Exception):
    """A failure basekit can explain to the model instead of leaking a traceback."""


class RpcError(BasekitError):
    """Every candidate endpoint failed; carries the per-endpoint reasons."""

    def __init__(self, method: str, attempts: list[tuple[str, str]]):
        self.method = method
        self.attempts = attempts
        detail = "; ".join(f"{url.split('//')[-1]}: {err}" for url, err in attempts)
        super().__init__(f"{method} failed on all endpoints ({detail})")


# --------------------------------------------------------------------------- #
# Hex / ABI helpers
# --------------------------------------------------------------------------- #
def strip0x(value: str) -> str:
    return value[2:] if value.startswith(("0x", "0X")) else value


def is_address(value: object) -> bool:
    if not isinstance(value, str):
        return False
    body = strip0x(value)
    if len(body) != 40:
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


def is_tx_hash(value: object) -> bool:
    if not isinstance(value, str):
        return False
    body = strip0x(value)
    if len(body) != 64:
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


def to_int(value: object, default: int = 0) -> int:
    """Decode a JSON-RPC hex quantity; tolerate already-decoded ints and None."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith(("0x", "0X")) else int(value)
        except ValueError:
            return default
    return default


def word(value: int) -> str:
    """Left-pad an integer to one 32-byte ABI word (no 0x)."""
    return format(value % (1 << 256), "064x")


def address_word(address: str) -> str:
    """ABI-encode an address as one 32-byte word (no 0x)."""
    return strip0x(address).lower().rjust(64, "0")


def pad_bytes(hex_body: str) -> str:
    """Right-pad a hex payload to a whole number of 32-byte words."""
    body = strip0x(hex_body)
    remainder = len(body) % 64
    return body if remainder == 0 else body + "0" * (64 - remainder)


def _topic(value: object) -> str | None:
    """Normalise an eth_getLogs topic: None (wildcard) or "0x" + 64 hex."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    body = strip0x(text)
    if len(body) > 64:
        body = body[-64:]
    return "0x" + body.rjust(64, "0")


def abi_uint(data: bytes, offset: int = 0) -> int:
    chunk = data[offset : offset + 32]
    return int.from_bytes(chunk, "big") if chunk else 0


def abi_address(data: bytes, offset: int = 0) -> str:
    chunk = data[offset : offset + 32]
    if len(chunk) < 32:
        return ""
    return "0x" + chunk[12:32].hex()


def abi_bool(data: bytes, offset: int = 0) -> bool:
    return abi_uint(data, offset) != 0


def abi_string(data: bytes) -> str | None:
    """Decode a `string` return value: dynamic (offset+len) or bytes32 (MKR-style).

    Returns None when the payload is not decodable as either shape, so callers can
    distinguish "not a string" from "empty string".
    """
    if len(data) < 32:
        return None
    first = abi_uint(data, 0)
    # Dynamic string: word 0 is an offset inside the payload, word 1 is the length.
    if 0 < first <= len(data) - 32 and len(data) >= first + 32:
        length = abi_uint(data, first)
        if 0 <= length <= len(data) - first - 32:
            raw = data[first + 32 : first + 32 + length]
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    # bytes32 string: NUL-terminated, right-padded (tokens like older MKR/DSTOKEN).
    raw = data[:32].split(b"\x00", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def apply_decimals(raw: int, decimals: int) -> float:
    return raw / (10**decimals) if decimals else float(raw)


def gwei(wei_value: int) -> float:
    return round(wei_value / GWEI, 6)


def eth(value_wei: int) -> float:
    return round(value_wei / WEI, 9)


# --------------------------------------------------------------------------- #
# RPC client
# --------------------------------------------------------------------------- #
class Rpc:
    """Minimal JSON-RPC client with endpoint failover and a tiny TTL cache.

    Thread-safe: agent tool calls can run on worker threads and the web backend
    serves requests concurrently, so the cache and the "preferred endpoint"
    pointer are guarded by a lock.
    """

    def __init__(self, url: str | None = None, timeout: float = 20.0, retries: int = 1):
        preferred = (url or DEFAULT_RPC).strip()
        endpoints = [preferred] + [u for u in RPC_FALLBACKS if u != preferred]
        self.endpoints = endpoints
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}
        self.last_endpoint: str | None = None
        self.last_latency_ms: int | None = None
        # Set by get_logs so callers can report the window they actually read.
        self.last_log_range: tuple[int, int] | None = None
        self.log_window_narrowed = False

    # -- plumbing ---------------------------------------------------------- #
    def _post(self, url: str, payload: object) -> object:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            body,
            {"Content-Type": "application/json", "User-Agent": f"basekit/{VERSION} (+hermes-plugin)"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def _rpc_error(self, message: str) -> str:
        return message or "rpc error"

    def call(self, method: str, params: list | None = None) -> object:
        return self.batch([(method, params or [])])[0]

    def batch(self, calls: list[tuple[str, list]]) -> list:
        """Run JSON-RPC calls in one batch, honouring the cache for single calls."""
        if not calls:
            return []
        # Serve single-call batches from cache when fresh.
        if len(calls) == 1:
            method, params = calls[0]
            key = self._cache_key(method, params)
            hit = self._cache_get(key)
            if hit is not None:
                return [hit]

        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls)
        ]
        attempts: list[tuple[str, str]] = []
        for url in self.endpoints:
            for attempt in range(self.retries + 1):
                started = time.time()
                try:
                    raw = self._post(url, payload if len(payload) > 1 else payload[0])
                except urllib.error.HTTPError as exc:
                    attempts.append((url, f"HTTP {exc.code}"))
                    # 429/5xx are worth retrying; 4xx (bad request) is not.
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                        break
                    time.sleep(0.4 * (attempt + 1))
                    continue
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    attempts.append((url, type(exc).__name__))
                    if attempt == self.retries:
                        break
                    time.sleep(0.4 * (attempt + 1))
                    continue
                except json.JSONDecodeError:
                    attempts.append((url, "malformed JSON"))
                    break

                elapsed = int((time.time() - started) * 1000)
                try:
                    results = self._unwrap(raw, len(calls))
                except RpcError as exc:
                    attempts.append((url, str(exc)))
                    break
                with self._lock:
                    self.last_endpoint = url
                    self.last_latency_ms = elapsed
                # Cache writes take the lock themselves - never nest them inside it.
                if len(calls) == 1:
                    self._cache_put(self._cache_key(calls[0][0], calls[0][1]), results[0])
                self._promote(url)
                return results
        raise RpcError(calls[0][0] if len(calls) == 1 else f"batch({len(calls)})", attempts)

    # -- internals --------------------------------------------------------- #
    def _promote(self, url: str) -> None:
        """Move a working endpoint to the front so the next call starts there."""
        with self._lock:
            if self.endpoints and self.endpoints[0] != url:
                self.endpoints.remove(url)
                self.endpoints.insert(0, url)

    def _unwrap(self, raw: object, expected: int) -> list:
        """Normalise a single or batched JSON-RPC response into positional results."""
        if isinstance(raw, dict):
            if "error" in raw:
                raise RpcError("rpc", [("response", self._rpc_error(str(raw["error"])))])
            return [raw.get("result")]
        if not isinstance(raw, list):
            raise RpcError("rpc", [("response", "unexpected response shape")])
        by_id = {item.get("id"): item for item in raw if isinstance(item, dict)}
        out = []
        for index in range(expected):
            item = by_id.get(index)
            if item is None:
                raise RpcError("rpc", [("response", f"missing result for call {index}")])
            if "error" in item:
                raise RpcError("rpc", [("response", self._rpc_error(str(item["error"])))])
            out.append(item.get("result"))
        return out

    def _cache_key(self, method: str, params: list) -> str:
        return f"{method}:{json.dumps(params, sort_keys=True)}"

    def _cache_get(self, key: str) -> object | None:
        with self._lock:
            entry = self._cache.get(key)
        if not entry:
            return None
        expires, value = entry
        return value if time.time() < expires else None

    def _cache_put(self, key: str, value: object, ttl: float | None = None) -> None:
        ttl = ttl if ttl is not None else self._ttl_for(key)
        if ttl <= 0:
            return
        with self._lock:
            self._cache[key] = (time.time() + ttl, value)
            if len(self._cache) > 512:  # keep the cache from growing without bound
                now = time.time()
                for stale in [k for k, (exp, _) in self._cache.items() if exp < now]:
                    self._cache.pop(stale, None)

    @staticmethod
    def _ttl_for(key: str) -> float:
        if key.startswith("eth_blockNumber"):
            return TTL_BLOCK
        if key.startswith(("eth_gasPrice", "eth_getBlockByNumber")):
            return TTL_GAS
        return 0.0

    # -- convenience ------------------------------------------------------- #
    def head_block(self) -> int:
        return to_int(self.call("eth_blockNumber"))

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def safe_eth_call(self, to: str, data: str) -> bytes | None:
        """eth_call that returns None instead of raising (reverts are expected here)."""
        try:
            raw = self.eth_call(to, data)
        except BasekitError:
            return None
        if not raw or raw == "0x":
            return None
        return bytes.fromhex(strip0x(raw))

    def get_logs(
        self,
        address: str,
        topics: list,
        from_block: int,
        to_block: int,
        limit: int = 400,
    ) -> list[dict]:
        """Address-filtered log query.

        Public endpoints require the `address` filter and reject wide ranges, so this
        is never used for chain-wide scans; callers pass a window and report it.

        Topics are normalised to `0x` + 64 hex here: callers build indexed-address
        topics with ``address_word()``, which returns a *bare* word, and a topic array
        that mixes prefixed and unprefixed entries is malformed JSON-RPC that only
        lenient endpoints accept.

        A busy contract can blow the endpoint's result cap even inside a small window
        ("query exceeds max results 20000"). Rather than failing, the window is halved
        toward the head and retried, because the *recent* blocks are what a caller
        asking "what moved lately" actually wants. The range finally used is exposed as
        ``last_log_range`` / ``log_window_narrowed`` so callers can state it honestly.
        """
        self.last_log_range = (max(0, from_block), max(0, to_block))
        self.log_window_narrowed = False
        start, end = max(0, from_block), max(0, to_block)
        last_error: RpcError | None = None
        logs: list | None = None

        for _ in range(7):
            params = [
                {
                    "address": address,
                    "topics": [_topic(item) for item in topics],
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                }
            ]
            try:
                logs = self.call("eth_getLogs", params) or []
                break
            except RpcError as exc:
                last_error = exc
                text = str(exc).lower()
                too_wide = any(marker in text for marker in
                               ("max results", "exceeds", "too large", "range", "limit"))
                span = end - start
                if too_wide and span > 64:
                    start = max(0, end - max(64, span // 2))  # keep the head end
                    self.last_log_range = (start, end)
                    self.log_window_narrowed = True
                    continue
                break

        if logs is None:
            # Never return "no transfers" for a query that was actually rejected: an
            # empty list is a claim about the chain, and it would be false.
            raise BasekitError(
                f"log query rejected for {address} over {end - start} blocks — public RPCs "
                f"cap eth_getLogs ranges and require an address filter ({last_error})"
            ) from last_error

        if not isinstance(logs, list):
            return []
        logs = sorted(logs, key=lambda item: (to_int(item.get("blockNumber")), to_int(item.get("logIndex"))))
        return logs[:limit]


# --------------------------------------------------------------------------- #
# Multicall
# --------------------------------------------------------------------------- #
def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """Encode `aggregate3((address,bool,bytes)[])` calldata.

    Layout (verified byte-for-byte against `cast calldata` in tests/test_core.py):
    offset-to-array, length, one offset per element, then each (address, bool, bytes)
    tuple whose `bytes` offset is relative to the tuple start.
    """
    count = len(calls)
    heads: list[str] = []
    tails: list[str] = []
    offset = 32 * count  # first tuple starts after the count + N offsets
    for target, call_data in calls:
        body = strip0x(call_data)
        body_words = (len(body) + 63) // 64
        heads.append(word(offset))
        tails.append(
            address_word(target) + word(1) + word(96) + word(len(body) // 2) + pad_bytes(body)
        )
        # (address, bool, bytes) encodes as 3 head words + a length word + the payload.
        offset += 32 * (4 + body_words)
    return (
        "0x"
        + SEL_AGGREGATE3
        + word(32)
        + word(count)
        + "".join(heads)
        + "".join(tails)
    )


def decode_aggregate3(payload: bytes) -> list[tuple[bool, bytes]]:
    """Decode the `(bool success, bytes returnData)[]` return of aggregate3."""
    if len(payload) < 32:
        raise BasekitError("multicall returned an empty payload")
    array_offset = abi_uint(payload, 0)
    if array_offset + 32 > len(payload):
        raise BasekitError("multicall payload is malformed")
    count = abi_uint(payload, array_offset)
    results: list[tuple[bool, bytes]] = []
    for index in range(count):
        # Element offsets are relative to the word *after* the length (matches the
        # encoder above and `cast calldata`, which tests re-check byte-for-byte).
        element_offset = array_offset + 32 + abi_uint(payload, array_offset + 32 + 32 * index)
        success = abi_bool(payload, element_offset)
        bytes_offset = element_offset + abi_uint(payload, element_offset + 32)
        length = abi_uint(payload, bytes_offset)
        results.append((success, payload[bytes_offset + 32 : bytes_offset + 32 + length]))
    return results


def multicall(rpc: Rpc, calls: list[tuple[str, str]]) -> list[bytes | None]:
    """Batch read-only contract calls into one eth_call. None marks a reverted call."""
    if not calls:
        return []
    # A single call is cheaper as a plain eth_call than a Multicall3 round trip.
    if len(calls) == 1:
        target, data = calls[0]
        return [rpc.safe_eth_call(target, data)]
    payload = rpc.safe_eth_call(MULTICALL3, encode_aggregate3(calls))
    if payload is None:
        raise BasekitError("multicall3 call reverted at the contract level")
    return [data if success else None for success, data in decode_aggregate3(payload)]


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
_PRICE_CACHE: dict[str, tuple[float, dict]] = {}
_PRICE_LOCK = threading.Lock()
DEFILLAMA_URL = "https://coins.llama.fi/prices/current/"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/ETH-USD/spot"
WETH_BASE = "0x4200000000000000000000000000000000000006"


def _http_json(url: str, timeout: float = 12.0) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": f"basekit/{VERSION}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def prices(addresses: list[str]) -> tuple[dict[str, dict], list[str]]:
    """USD prices for Base token addresses via DefiLlama (keyless).

    Returns ({address_lower: {"price", "symbol", "decimals", "confidence"}}, sources).
    """
    wanted = [a.lower() for a in addresses if is_address(a)]
    if not wanted:
        return {}, []
    now = time.time()
    with _PRICE_LOCK:
        fresh = {a: _PRICE_CACHE[a][1] for a in wanted if a in _PRICE_CACHE and _PRICE_CACHE[a][0] > now}
    missing = [a for a in wanted if a not in fresh]
    sources: list[str] = ["defillama"] if fresh else []

    if missing:
        query = ",".join(f"base:{a}" for a in missing)
        try:
            payload = _http_json(DEFILLAMA_URL + query)
            coins = (payload or {}).get("coins", {}) if isinstance(payload, dict) else {}
            for key, info in coins.items():
                address = key.split(":")[-1].lower()
                price = info.get("price")
                if price is None:
                    continue
                entry = {
                    "price": float(price),
                    "symbol": info.get("symbol"),
                    "decimals": info.get("decimals"),
                    "confidence": info.get("confidence"),
                }
                fresh[address] = entry
                with _PRICE_LOCK:
                    _PRICE_CACHE[address] = (now + TTL_PRICE, entry)
            if "defillama" not in sources:
                sources.append("defillama")
        except Exception as exc:  # pragma: no cover - network failure path
            logger.debug("basekit: price lookup failed: %s", exc)

    # Native ETH price: WETH on Base (DefiLlama) with a Coinbase spot fallback.
    if "eth" not in fresh:
        weth = fresh.get(WETH_BASE.lower())
        if weth:
            fresh["eth"] = {**weth, "symbol": NATIVE_SYMBOL}
            with _PRICE_LOCK:
                _PRICE_CACHE["eth"] = (now + TTL_PRICE, fresh["eth"])
        else:
            try:
                payload = _http_json(COINBASE_SPOT_URL)
                amount = float(payload["data"]["amount"])
                fresh["eth"] = {"price": amount, "symbol": NATIVE_SYMBOL, "decimals": 18, "confidence": None}
                with _PRICE_LOCK:
                    _PRICE_CACHE["eth"] = (now + TTL_PRICE, fresh["eth"])
                if "coinbase" not in sources:
                    sources.append("coinbase")
            except Exception as exc:  # pragma: no cover - network failure path
                logger.debug("basekit: ETH spot lookup failed: %s", exc)

    return fresh, sources


def eth_price_usd() -> float | None:
    fresh, _ = prices([WETH_BASE])
    entry = fresh.get("eth") or fresh.get(WETH_BASE.lower())
    return round(entry["price"], 2) if entry else None


# --------------------------------------------------------------------------- #
# Token set (shipped data file)
# --------------------------------------------------------------------------- #
def load_token_set(plugin_dir: Path | str | None) -> list[dict]:
    """The curated Base token list shipped with the plugin (empty list if unreadable).

    Defaults to this file's own directory when the caller doesn't pass one. The
    fallback matters: a caller that forgot the argument used to get an empty list and
    therefore an empty portfolio — a wallet holding thousands of dollars reported as
    holding nothing, which is the worst kind of wrong.
    """
    base = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent
    path = base / TOKENS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    tokens = data.get("tokens", data) if isinstance(data, dict) else data
    out = []
    for item in tokens or []:
        if isinstance(item, dict) and is_address(item.get("address")):
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Read primitives
# --------------------------------------------------------------------------- #
def erc20_metadata(rpc: Rpc, addresses: list[str]) -> dict[str, dict]:
    """symbol/name/decimals for many tokens in one multicall."""
    unique = []
    for address in addresses:
        lowered = address.lower()
        if lowered not in unique:
            unique.append(lowered)
    if not unique:
        return {}
    calls: list[tuple[str, str]] = []
    for address in unique:
        calls.append((address, "0x" + SEL["symbol"]))
        calls.append((address, "0x" + SEL["name"]))
        calls.append((address, "0x" + SEL["decimals"]))
    results = multicall(rpc, calls)
    meta: dict[str, dict] = {}
    for index, address in enumerate(unique):
        symbol_raw, name_raw, decimals_raw = results[index * 3 : index * 3 + 3]
        entry: dict = {}
        if symbol_raw:
            symbol = abi_string(symbol_raw)
            if symbol:
                entry["symbol"] = symbol.strip()
        if name_raw:
            name = abi_string(name_raw)
            if name:
                entry["name"] = name.strip()
        if decimals_raw:
            decimals = abi_uint(decimals_raw)
            if 0 < decimals <= 36:
                entry["decimals"] = decimals
        if entry:
            meta[address] = entry
    return meta


def erc20_balances(rpc: Rpc, owner: str, addresses: list[str]) -> dict[str, int]:
    """balanceOf(owner) for many tokens in one multicall; absent key = call reverted."""
    calls = [(a.lower(), "0x" + SEL["balanceOf"] + address_word(owner)) for a in addresses]
    results = multicall(rpc, calls)
    out: dict[str, int] = {}
    for (address, _), raw in zip(calls, results):
        if raw:
            out[address] = abi_uint(raw)
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def network_status(rpc: Rpc) -> dict:
    """Chain state a model needs before promising anything on-chain."""
    block_hex, gas_hex, chain_hex = rpc.batch(
        [("eth_blockNumber", []), ("eth_gasPrice", []), ("eth_chainId", [])]
    )
    head = to_int(block_hex)
    gas_price = to_int(gas_hex)
    chain_id = to_int(chain_hex)

    base_fee = None
    block_time = None
    tx_count = None
    if head:
        block = rpc.call("eth_getBlockByNumber", [hex(head), False]) or {}
        # Only report what was actually read: a failed block fetch must not surface as
        # "0 gwei base fee", which would be a fabricated number the model would repeat.
        if block:
            base_fee = to_int(block.get("baseFeePerGas")) if "baseFeePerGas" in block else None
            block_time = to_int(block.get("timestamp"))
            tx_count = len(block.get("transactions") or [])

    price = eth_price_usd()
    transfer_wei = 21000 * (gas_price or base_fee or 0)
    swap_wei = 150000 * (base_fee or gas_price or 0)

    out = {
        "chain": CHAIN_NAME,
        "chain_id": chain_id or CHAIN_ID,
        "head_block": head,
        "block_time_utc": _iso(block_time),
        "txs_in_head_block": tx_count,
        "gas_price_gwei": gwei(gas_price),
        "base_fee_gwei": gwei(base_fee) if base_fee is not None else None,
        "priority_fee_gwei": gwei(max(0, gas_price - (base_fee or 0))),
        "eth_price_usd": price,
        "cost_usd": {
            "eth_transfer_21000": _usd_value(transfer_wei, price),
            "erc20_transfer_65000": _usd_value(65000 * (gas_price or 0), price),
            "uniswap_swap_150000": _usd_value(swap_wei, price),
        },
        "rpc": {"endpoint": (rpc.last_endpoint or rpc.endpoints[0]).split("//")[-1], "latency_ms": rpc.last_latency_ms},
        "read_only": True,
    }
    return out


def portfolio(rpc: Rpc, address: str, extra_tokens: list[str] | None = None,
              include_zero: bool = False, plugin_dir: Path | str | None = None) -> dict:
    """Native + ERC-20 holdings with USD values for one address."""
    if not is_address(address):
        raise BasekitError(f"{address!r} is not a valid EVM address")
    owner = address.lower()

    token_set = load_token_set(plugin_dir)
    addresses: list[str] = [t["address"] for t in token_set]
    for extra in extra_tokens or []:
        if is_address(extra) and extra.lower() not in [a.lower() for a in addresses]:
            addresses.append(extra)

    native_wei = to_int(rpc.call("eth_getBalance", [owner, "latest"]))
    balances = erc20_balances(rpc, owner, addresses) if addresses else {}
    meta = erc20_metadata(rpc, list(balances.keys())) if balances else {}
    price_map, sources = prices(list(balances.keys()) + [WETH_BASE])
    native_price = price_map.get("eth") or price_map.get(WETH_BASE.lower())

    holdings = []
    total = 0.0
    if native_wei or include_zero:
        value = apply_decimals(native_wei, 18) * (native_price["price"] if native_price else 0.0)
        total += value
        holdings.append({
            "symbol": NATIVE_SYMBOL,
            "name": "Ether (native)",
            "address": None,
            "balance": eth(native_wei),
            "price_usd": round(native_price["price"], 4) if native_price else None,
            "value_usd": round(value, 2),
        })

    for address_lower, raw in balances.items():
        entry = meta.get(address_lower, {})
        decimals = entry.get("decimals") or next(
            (t.get("decimals") for t in token_set if t["address"].lower() == address_lower), 18
        )
        price_entry = price_map.get(address_lower)
        amount = apply_decimals(raw, decimals)
        value = amount * (price_entry["price"] if price_entry else 0.0)
        if raw == 0 and not include_zero:
            continue
        total += value
        holdings.append({
            "symbol": entry.get("symbol") or _token_symbol_from_set(token_set, address_lower),
            "name": entry.get("name"),
            "address": address_lower,
            "balance": round(amount, 6),
            "decimals": decimals,
            "price_usd": round(price_entry["price"], 6) if price_entry else None,
            "value_usd": round(value, 2) if price_entry else None,
        })

    holdings.sort(key=lambda item: (item.get("value_usd") or 0), reverse=True)
    return {
        "address": owner,
        "chain": CHAIN_NAME,
        "holdings": holdings,
        "token_count": len(holdings),
        "total_usd": round(total, 2),
        "scanned_tokens": len(addresses),
        "price_sources": sources,
        "as_of": _iso(int(time.time())),
        "note": (
            "Balances are on-chain truth; USD values use DefiLlama (keyless). A token with no "
            "price shows value_usd: null rather than a guess. The list covers the curated "
            f"{len(addresses)}-token set plus any addresses you pass."
            + ("" if addresses else " WARNING: the curated token set could not be read, so only "
                                   "the native ETH balance was checked — this is NOT a complete "
                                   "portfolio.")
        ),
    }


def _token_symbol_from_set(token_set: list[dict], address_lower: str) -> str | None:
    for token in token_set:
        if token["address"].lower() == address_lower:
            return token.get("symbol")
    return None


def tx_receipt(rpc: Rpc, tx_hash: str) -> dict:
    """Decode one Base transaction into a human-readable receipt (incl. token movements)."""
    if not is_tx_hash(tx_hash):
        raise BasekitError(f"{tx_hash!r} is not a 32-byte transaction hash")
    tx_hash = "0x" + strip0x(tx_hash).lower()
    tx, receipt = rpc.batch(
        [("eth_getTransactionByHash", [tx_hash]), ("eth_getTransactionReceipt", [tx_hash])]
    )
    if not tx:
        raise BasekitError(f"no transaction found for {tx_hash}")
    if not receipt:
        return {
            "hash": tx_hash,
            "status": "pending",
            "note": "the transaction is known but not yet mined — no receipt to decode",
            "from": tx.get("from"),
            "to": tx.get("to"),
            "nonce": to_int(tx.get("nonce")),
        }

    block_number = to_int(receipt.get("blockNumber"))
    block = rpc.call("eth_getBlockByNumber", [hex(block_number), False]) or {}
    gas_used = to_int(receipt.get("gasUsed"))
    gas_price = to_int(receipt.get("effectiveGasPrice") or tx.get("gasPrice"))
    fee_wei = gas_used * gas_price
    price = eth_price_usd()

    logs = receipt.get("logs") or []
    transfers: list[dict] = []
    approvals: list[dict] = []
    token_addresses: list[str] = []
    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        token = (log.get("address") or "").lower()
        topic0 = topics[0].lower()
        if topic0 == TOPIC_TRANSFER and len(topics) >= 3:
            token_addresses.append(token)
            entry = {
                "token": token,
                "from": "0x" + topics[1][-40:],
                "to": "0x" + topics[2][-40:],
            }
            if len(topics) >= 4:  # ERC-721: 4th topic is the tokenId
                entry["standard"] = "ERC-721"
                entry["token_id"] = str(to_int(topics[3]))
            else:
                entry["standard"] = "ERC-20"
                entry["raw_amount"] = str(abi_uint(bytes.fromhex(strip0x(log.get("data") or "0x"))))
            transfers.append(entry)
        elif topic0 == TOPIC_APPROVAL and len(topics) >= 3:
            token_addresses.append(token)
            approvals.append({
                "token": token,
                "owner": "0x" + topics[1][-40:],
                "spender": "0x" + topics[2][-40:],
                "raw_amount": str(abi_uint(bytes.fromhex(strip0x(log.get("data") or "0x")))),
            })

    meta = erc20_metadata(rpc, token_addresses) if token_addresses else {}
    price_map, _ = prices(sorted({t.lower() for t in token_addresses}))
    for entry in transfers:
        info = meta.get(entry["token"], {})
        entry["symbol"] = info.get("symbol")
        if entry.get("standard") == "ERC-20":
            decimals = info.get("decimals", 18)
            amount = apply_decimals(int(entry["raw_amount"]), decimals)
            entry["amount"] = round(amount, 6)
            price_entry = price_map.get(entry["token"])
            entry["value_usd"] = round(amount * price_entry["price"], 2) if price_entry else None
    for entry in approvals:
        info = meta.get(entry["token"], {})
        decimals = info.get("decimals", 18)
        amount = apply_decimals(int(entry["raw_amount"]), decimals)
        entry["symbol"] = info.get("symbol")
        entry["amount"] = round(amount, 6)
        entry["unlimited"] = int(entry["raw_amount"]) >= 2**255

    status_ok = to_int(receipt.get("status")) == 1
    return {
        "hash": tx_hash,
        "status": "success" if status_ok else "reverted",
        "block": block_number,
        "timestamp_utc": _iso(to_int(block.get("timestamp"))),
        "from": tx.get("from"),
        "to": tx.get("to"),
        "contract_created": receipt.get("contractAddress"),
        "value_eth": eth(to_int(tx.get("value"))),
        "gas_used": gas_used,
        "gas_price_gwei": gwei(gas_price),
        "fee_eth": eth(fee_wei),
        "fee_usd": _usd_value(fee_wei, price),
        "eth_price_usd": price,
        "log_count": len(logs),
        "token_transfers": transfers,
        "approvals": approvals,
        "method_selector": (tx.get("input") or "0x")[:10],
        "read_only": True,
    }


def token_facts(rpc: Rpc, address: str, implementation_source: str | None = None,
                verification: dict | None = None) -> dict:
    """Contract-level facts and honest bytecode heuristics for one Base token."""
    if not is_address(address):
        raise BasekitError(f"{address!r} is not a valid EVM address")
    address = address.lower()
    code = rpc.call("eth_getCode", [address, "latest"]) or "0x"
    code_body = strip0x(code)
    is_contract = len(code_body) > 0

    out: dict = {
        "address": address,
        "is_contract": is_contract,
        "code_size_bytes": len(code_body) // 2,
        "read_only": True,
    }
    if not is_contract:
        out["note"] = "this address has no bytecode on Base — it may be an EOA or an undeployed address"
        return out

    results = multicall(rpc, [
        (address, "0x" + SEL["name"]),
        (address, "0x" + SEL["symbol"]),
        (address, "0x" + SEL["decimals"]),
        (address, "0x" + SEL["totalSupply"]),
        (address, "0x" + SEL["owner"]),
    ])
    name_raw, symbol_raw, decimals_raw, supply_raw, owner_raw = results

    if name_raw:
        out["name"] = abi_string(name_raw)
    if symbol_raw:
        out["symbol"] = abi_string(symbol_raw)
    if decimals_raw:
        decimals = abi_uint(decimals_raw)
        out["decimals"] = decimals if 0 < decimals <= 36 else None
    if supply_raw:
        raw_supply = abi_uint(supply_raw)
        out["total_supply_raw"] = str(raw_supply)
        if out.get("decimals") is not None:
            out["total_supply"] = round(apply_decimals(raw_supply, out["decimals"]), 4)
    if owner_raw:
        owner = abi_address(owner_raw)
        out["owner"] = owner if int(owner, 16) else None
    has_decimals = decimals_raw is not None
    has_symbol = symbol_raw is not None
    if has_symbol and has_decimals:
        out["standards"] = ["ERC-20"]
    elif has_symbol:
        out["standards"] = ["non-fungible or non-standard (symbol() responds, decimals() does not)"]
    else:
        out["standards"] = ["unknown"]

    # --- proxy detection: storage slots first (authoritative), bytecode hints second ---
    proxy: dict = {"is_proxy": False, "pattern": None, "implementation": None}
    for label, slot in (("eip1967", EIP1967_IMPLEMENTATION), ("eip1967-beacon", EIP1967_BEACON)):
        raw = rpc.call("eth_getStorageAt", [address, slot, "latest"]) or "0x"
        value = strip0x(raw).rjust(64, "0")
        if int(value, 16):
            proxy = {
                "is_proxy": True,
                "pattern": label,
                "implementation": "0x" + value[-40:],
                "admin_slot": (
                    "0x" + strip0x(rpc.call("eth_getStorageAt", [address, EIP1967_ADMIN, "latest"]) or "0x").rjust(64, "0")[-40:]
                ),
            }
            break
    if not proxy["is_proxy"]:
        if code_body.startswith("363d3d373d3d3d363d73") and "5af43d82803e903d91602b57fd5bf3" in code_body:
            proxy = {"is_proxy": True, "pattern": "eip1167-minimal-clone", "implementation": "0x" + code_body[20:60]}
        elif SEL["implementation"] in code_body:
            proxy = {
                "is_proxy": True,
                "pattern": "custom (exposes implementation())",
                "implementation": None,
            }
    out["proxy"] = proxy

    if proxy.get("is_proxy") and proxy.get("implementation") is None:
        impl_raw = rpc.safe_eth_call(address, "0x" + SEL["implementation"])
        if impl_raw:
            proxy["implementation"] = abi_address(impl_raw) or None

    out["bytecode_flags"] = sorted(
        {sig for selector, sig in BYTECODE_PROBES.items() if selector in code_body}
    )
    out["heuristics_note"] = (
        "bytecode_flags come from 4-byte selector presence in the deployed code — a hint about "
        "what the contract *can* do, not proof of behaviour. basekit does not claim honeypot or "
        "rug detection: those need transaction simulation, which this plugin deliberately does not do."
    )
    out["source_verification"] = verification or {
        "status": "unchecked",
        "note": "set a Basescan key in Basekit settings to verify source code",
    }
    if implementation_source:
        out["implementation_source"] = implementation_source
    return out


def token_activity(rpc: Rpc, owner: str, token: str, blocks: int = 20000, limit: int = 25) -> dict:
    """Recent ERC-20 transfers in and out of an address for one token.

    Address-filtered by construction: public RPCs refuse chain-wide log scans, so the
    caller names the token and the window, and the output states that window back.
    """
    if not is_address(owner):
        raise BasekitError(f"{owner!r} is not a valid EVM address")
    if not is_address(token):
        raise BasekitError(f"{token!r} is not a valid EVM address")
    owner = owner.lower()
    token = token.lower()
    blocks = max(500, min(int(blocks), 50000))

    head = rpc.head_block()
    from_block = max(0, head - blocks)
    padded = address_word(owner)

    outgoing = rpc.get_logs(token, [TOPIC_TRANSFER, padded], from_block, head, limit=limit)
    incoming = rpc.get_logs(token, [TOPIC_TRANSFER, None, padded], from_block, head, limit=limit)
    used_from, used_to = getattr(rpc, "last_log_range", None) or (from_block, head)
    used_blocks = max(0, used_to - used_from)
    narrowed = bool(getattr(rpc, "log_window_narrowed", False))

    meta = erc20_metadata(rpc, [token]).get(token, {})
    decimals = meta.get("decimals", 18)
    price_map, _ = prices([token])
    price_entry = price_map.get(token)

    movements = []
    for direction, logs in (("out", outgoing), ("in", incoming)):
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            raw = abi_uint(bytes.fromhex(strip0x(log.get("data") or "0x")))
            amount = apply_decimals(raw, decimals)
            movements.append({
                "direction": direction,
                "amount": round(amount, 6),
                "value_usd": round(amount * price_entry["price"], 2) if price_entry else None,
                "counterparty": "0x" + (topics[2] if direction == "out" else topics[1])[-40:],
                "block": to_int(log.get("blockNumber")),
                "tx": log.get("transactionHash"),
            })
    movements.sort(key=lambda item: item["block"], reverse=True)
    movements = movements[:limit]
    return {
        "address": owner,
        "token": {"address": token, "symbol": meta.get("symbol"), "decimals": decimals},
        "window": {
            "from_block": used_from,
            "to_block": used_to,
            "blocks": used_blocks,
            "requested_blocks": blocks,
            "approx_hours": round(used_blocks * 2 / 3600, 2),
            "narrowed": narrowed,
            "note": (
                "public Base RPCs reject wider eth_getLogs ranges and require an address filter; "
                "point Basekit at your own RPC endpoint in settings to scan further back"
                + (
                    f"; the window was narrowed automatically from {blocks} blocks because this "
                    "token has more activity than the endpoint's result cap allows"
                    if narrowed else ""
                )
            ),
        },
        "transfers": movements,
        "count": len(movements),
        "read_only": True,
    }


def token_allowances(rpc: Rpc, owner: str, token: str, blocks: int = 20000,
                     max_spenders: int = 25) -> dict:
    """Approvals this owner granted on one token, with the *current* allowance and USD at risk."""
    if not is_address(owner):
        raise BasekitError(f"{owner!r} is not a valid EVM address")
    if not is_address(token):
        raise BasekitError(f"{token!r} is not a valid EVM address")
    owner = owner.lower()
    token = token.lower()
    blocks = max(500, min(int(blocks), 50000))

    head = rpc.head_block()
    from_block = max(0, head - blocks)
    logs = rpc.get_logs(token, [TOPIC_APPROVAL, address_word(owner)], from_block, head, limit=400)
    used_from, used_to = getattr(rpc, "last_log_range", None) or (from_block, head)
    used_blocks = max(0, used_to - used_from)

    spenders: list[str] = []
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) >= 3:
            spender = "0x" + topics[2][-40:]
            if spender not in spenders:
                spenders.append(spender)
    spenders = spenders[:max_spenders]

    meta = erc20_metadata(rpc, [token]).get(token, {})
    decimals = meta.get("decimals", 18)
    price_map, _ = prices([token])
    price_entry = price_map.get(token)

    rows = []
    if spenders:
        calls = [(token, "0x" + SEL["allowance"] + address_word(owner) + address_word(s)) for s in spenders]
        for spender, raw in zip(spenders, multicall(rpc, calls)):
            if raw is None:
                continue
            amount_raw = abi_uint(raw)
            amount = apply_decimals(amount_raw, decimals)
            if amount_raw == 0:
                continue  # already revoked
            rows.append({
                "spender": spender,
                "allowance": round(amount, 6),
                "unlimited": amount_raw >= 2**255,
                "value_at_risk_usd": round(amount * price_entry["price"], 2) if price_entry else None,
            })
    rows.sort(key=lambda item: (item["value_at_risk_usd"] or 0, item["unlimited"]), reverse=True)

    return {
        "owner": owner,
        "token": {"address": token, "symbol": meta.get("symbol"), "decimals": decimals},
        "window": {"from_block": used_from, "to_block": used_to, "blocks": used_blocks,
                   "requested_blocks": blocks, "approx_hours": round(used_blocks * 2 / 3600, 2),
                   "narrowed": bool(getattr(rpc, "log_window_narrowed", False))},
        "active_allowances": rows,
        "active_count": len(rows),
        "unlimited_count": sum(1 for row in rows if row["unlimited"]),
        "note": (
            "current allowances are re-read on-chain; the window only bounds which spenders we "
            "learned about from Approval logs. Older approvals still active outside the window "
            "are not listed. basekit never revokes — it reports; the user decides."
        ),
        "read_only": True,
    }


# --------------------------------------------------------------------------- #
# Snapshot used by the desktop pane / status bar (batched, cache-friendly)
# --------------------------------------------------------------------------- #
def snapshot(rpc: Rpc, watch_address: str | None = None, ledger: list[dict] | None = None,
             plugin_dir: Path | str | None = None) -> dict:
    """One round trip the UI can poll: chain state + optional watch-wallet total."""
    status = network_status(rpc)
    payload: dict = {"network": status, "as_of": _iso(int(time.time()))}
    if watch_address and is_address(watch_address):
        try:
            wallet = portfolio(rpc, watch_address, plugin_dir=plugin_dir)
            payload["wallet"] = {
                "address": wallet["address"],
                "total_usd": wallet["total_usd"],
                "holdings": [
                    {k: h.get(k) for k in ("symbol", "balance", "value_usd", "address")}
                    for h in wallet["holdings"][:8]
                ],
            }
        except BasekitError as exc:
            payload["wallet"] = {"address": watch_address, "error": str(exc)}
    if ledger is not None:
        payload["ledger"] = ledger
    return payload


# --------------------------------------------------------------------------- #
# Ledger — what the agent actually looked up, and at which block
# --------------------------------------------------------------------------- #
def append_ledger(path: Path | str, entry: dict, limit: int = 200) -> None:
    """Append one JSONL ledger line, trimmed to `limit` recent entries."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, separators=(",", ":"))
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        existing.append(line)
        if len(existing) > limit:
            existing = existing[-limit:]
        path.write_text("\n".join(existing) + "\n", encoding="utf-8")
    except Exception as exc:  # pragma: no cover - ledger must never break a tool call
        logger.debug("basekit: ledger write failed: %s", exc)


def read_ledger(path: Path | str, limit: int = 30) -> list[dict]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


# --------------------------------------------------------------------------- #
# Settings (shared by the agent half and the web/desktop backend)
# --------------------------------------------------------------------------- #
DEFAULT_SETTINGS = {
    "rpc_url": DEFAULT_RPC,
    "watch_address": "",
    "inject_chain_context": True,
    "basescan_api_key": "",
}


def load_settings() -> dict:
    """Read `plugins.entries.basekit.settings` with defaults, tolerating any import failure.

    Uses Hermes's own config reader when available (the documented route for backend
    code); never reads .env or any credential file itself.
    """
    settings = dict(DEFAULT_SETTINGS)
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        entry = ((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
        stored = entry.get("settings") or entry.get("config") or {}
        for key in settings:
            if key in stored and stored[key] is not None:
                settings[key] = stored[key]
    except Exception as exc:  # pragma: no cover - absent config is normal in tests
        logger.debug("basekit: falling back to default settings: %s", exc)
    return settings


def basescan_key() -> str | None:
    """The optional Basescan key, read through Hermes's secret scope (never a file)."""
    try:
        from agent.secret_scope import get_secret

        return get_secret("BASESCAN_API_KEY") or None
    except Exception:
        return None


def verify_source(address: str, api_key: str | None = None) -> dict:
    """Optional Etherscan-V2 source check for one contract. Reports honestly when unkeyed."""
    if not api_key:
        return {
            "status": "not_configured",
            "would_use": "Etherscan V2 (chainid=8453) GET /v2/api?module=contract&action=getsourcecode",
            "note": "add a Basescan API key in Basekit settings to verify source code",
        }
    url = (
        "https://api.etherscan.io/v2/api?chainid=8453&module=contract&action=getsourcecode"
        f"&address={address}&apikey={api_key}"
    )
    try:
        payload = _http_json(url)
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__, "source": "etherscan-v2"}
    result = (payload or {}).get("result")
    first = result[0] if isinstance(result, list) and result else {}
    source = (first or {}).get("SourceCode") or ""
    return {
        "status": "verified" if source else "unverified",
        "source": "etherscan-v2",
        "contract_name": (first or {}).get("ContractName") or None,
        "compiler": (first or {}).get("CompilerVersion") or None,
        "proxy_flag": (first or {}).get("Proxy") or "0",
        "verified_at": (first or {}).get("VerifiedAt") or None,
    }


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def _iso(timestamp: int | None) -> str | None:
    if not timestamp:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _usd_value(wei_amount: int, price: float | None) -> float | None:
    if not price:
        return None
    return round((wei_amount / WEI) * price, 6)
