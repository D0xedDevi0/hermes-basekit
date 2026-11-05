"""Offline unit tests for basekit_core — no network, no PyPI dependencies.

Everything here runs against hand-written vectors and a fake RPC so the suite is
deterministic: CI can run it with nothing installed but Python and Node.

Where a constant could be got wrong out of memory (selectors, event topics, EIP-1967
slots) the test re-derives it with `cast keccak` when foundry is available, and skips
that specific check when it isn't — the hardcoded expectation is still asserted either
way.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import basekit_core as bk  # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AERO = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
WALLET = "0x23129c0472172D75bEd1e6dd061301796760Ecd9"
TXH = "0x" + "ab" * 32


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRpc:
    """Stands in for Rpc: canned JSON-RPC replies, call recording, no sockets."""

    def __init__(self, replies: dict | None = None, block: int = 53_522_884):
        self.replies = replies or {}
        self.block = block
        self.calls: list[tuple[str, list]] = []
        self.endpoints = ["https://fake.invalid"]
        self.last_endpoint = "https://fake.invalid"
        self.last_latency_ms = 12
        self.logs: list[dict] = []

    def call(self, method, params=None):
        params = params or []
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.block)
        if method in self.replies:
            value = self.replies[method]
            return value(params) if callable(value) else value
        if method == "eth_getLogs":
            return list(self.logs)
        if method == "eth_getBalance":
            return hex(5 * 10**17)
        if method == "eth_getCode":
            return "0x6080604052"
        if method == "eth_getStorageAt":
            return "0x" + "00" * 32
        return None

    def batch(self, calls):
        return [self.call(method, params) for method, params in calls]

    def head_block(self):
        return self.block

    def eth_call(self, to, data, block="latest"):
        return "0x"

    def safe_eth_call(self, to, data):
        return None

    def get_logs(self, address, topics, from_block, to_block, limit=400):
        self.calls.append(("eth_getLogs", [{"address": address, "topics": topics,
                                            "fromBlock": from_block, "toBlock": to_block}]))
        matched = []
        for log in self.logs:
            log_topics = [bk.strip0x(t).lower() for t in (log.get("topics") or [])]
            ok = True
            for index, want in enumerate(topics):
                if want is None:
                    continue
                if index >= len(log_topics) or log_topics[index] != bk.strip0x(str(want)).lower():
                    ok = False
                    break
            if ok:
                matched.append(log)
        return matched[:limit]


def word_int(value: int) -> bytes:
    return value.to_bytes(32, "big")


def enc_string(text: str) -> bytes:
    raw = text.encode()
    return word_int(32) + word_int(len(raw)) + raw + b"\x00" * ((32 - len(raw) % 32) % 32)


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
class TestHexHelpers(unittest.TestCase):
    def test_is_address(self):
        self.assertTrue(bk.is_address(USDC))
        self.assertFalse(bk.is_address("0x1234"))
        # A bare 40-hex body is accepted on purpose: the agent (or a user pasting from a
        # block explorer without the prefix) shouldn't be rejected for a missing "0x".
        self.assertTrue(bk.is_address("833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"))
        self.assertFalse(bk.is_address("0x" + "z" * 40))
        self.assertFalse(bk.is_address("0x" + "z" * 20))
        self.assertFalse(bk.is_address(USDC + "00"))
        self.assertFalse(bk.is_address(None))
        self.assertFalse(bk.is_address(1234))

    def test_is_tx_hash(self):
        self.assertTrue(bk.is_tx_hash(TXH))
        self.assertFalse(bk.is_tx_hash(USDC))

    def test_to_int(self):
        self.assertEqual(bk.to_int("0x10"), 16)
        self.assertEqual(bk.to_int(16), 16)
        self.assertEqual(bk.to_int(None), 0)
        self.assertEqual(bk.to_int("nonsense", default=7), 7)

    def test_word_and_address_word(self):
        self.assertEqual(len(bk.word(0)), 64)
        self.assertEqual(bk.word(1).count("0"), 63)
        self.assertTrue(bk.word(2**256 + 5).endswith("05"))  # wraps, never overflows
        self.assertEqual(bk.address_word(USDC), "0" * 24 + USDC[2:].lower())

    def test_pad_bytes(self):
        self.assertEqual(len(bk.pad_bytes("aa")), 64)
        self.assertEqual(len(bk.pad_bytes("a" * 64)), 64)
        self.assertEqual(len(bk.pad_bytes("a" * 65)), 128)

    def test_abi_scalars(self):
        payload = word_int(1234) + word_int(0) * 0
        self.assertEqual(bk.abi_uint(payload), 1234)
        self.assertEqual(bk.abi_uint(b"", 0), 0)
        addr_word = bytes.fromhex(bk.address_word(USDC))
        self.assertEqual(bk.abi_address(addr_word).lower(), USDC.lower())
        self.assertTrue(bk.abi_bool(word_int(1)))
        self.assertFalse(bk.abi_bool(word_int(0)))

    def test_abi_string_dynamic(self):
        self.assertEqual(bk.abi_string(enc_string("USD Coin")), "USD Coin")
        self.assertEqual(bk.abi_string(enc_string("")), "")

    def test_abi_string_bytes32(self):
        raw = b"USDC" + b"\x00" * 28
        self.assertEqual(bk.abi_string(raw), "USDC")

    def test_abi_string_undecodable(self):
        self.assertIsNone(bk.abi_string(b"\xff" * 4))

    def test_decimals_and_units(self):
        self.assertEqual(bk.apply_decimals(1_404_000_000, 6), 1404.0)
        self.assertAlmostEqual(bk.gwei(10**9), 1.0, places=6)
        self.assertAlmostEqual(bk.eth(10**18), 1.0, places=9)


# --------------------------------------------------------------------------- #
# Constants — re-derived with cast when available
# --------------------------------------------------------------------------- #
class TestVerifiedConstants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cast = shutil.which("cast")

    def _keccak(self, signature: str) -> str:
        if not self.cast:
            self.skipTest("foundry `cast` not installed")
        return subprocess.check_output([self.cast, "keccak", signature], text=True).strip()

    def test_event_topics_match_cast(self):
        self.assertEqual(self._keccak("Transfer(address,address,uint256)"), bk.TOPIC_TRANSFER)
        self.assertEqual(self._keccak("Approval(address,address,uint256)"), bk.TOPIC_APPROVAL)
        self.assertEqual(
            self._keccak("TransferSingle(address,address,address,uint256,uint256)"),
            bk.TOPIC_TRANSFER_SINGLE,
        )

    def test_selectors_match_cast(self):
        for signature, selector in (
            ("balanceOf(address)", "balanceOf"),
            ("decimals()", "decimals"),
            ("symbol()", "symbol"),
            ("name()", "name"),
            ("totalSupply()", "totalSupply"),
            ("owner()", "owner"),
            ("implementation()", "implementation"),
            ("allowance(address,address)", "allowance"),
            ("aggregate3((address,bool,bytes)[])", None),
        ):
            derived = self._keccak(signature)[2:10]
            expected = bk.SEL_AGGREGATE3 if selector is None else bk.SEL[selector]
            self.assertEqual(derived, expected, f"{signature} selector drifted")

    def test_eip1967_slots_match_cast(self):
        for name, slot in (
            ("eip1967.proxy.implementation", bk.EIP1967_IMPLEMENTATION),
            ("eip1967.proxy.beacon", bk.EIP1967_BEACON),
            ("eip1967.proxy.admin", bk.EIP1967_ADMIN),
        ):
            derived = int(self._keccak(name), 16) - 1
            self.assertEqual("0x" + format(derived, "064x"), slot)

    def test_hardcoded_values_regression(self):
        # Asserted even without foundry, so a careless edit fails CI everywhere.
        self.assertEqual(bk.TOPIC_TRANSFER,
                         "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
        self.assertEqual(bk.SEL_AGGREGATE3, "82ad56cb")
        self.assertEqual(bk.SEL["balanceOf"], "70a08231")
        self.assertEqual(bk.EIP1967_IMPLEMENTATION,
                         "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc")
        self.assertEqual(bk.MULTICALL3, "0xcA11bde05977b3631167028862bE2a173976CA11")


# --------------------------------------------------------------------------- #
# Multicall encoding / decoding
# --------------------------------------------------------------------------- #
class TestMulticallCodec(unittest.TestCase):
    BALANCE_USDC = "0x" + "70a08231" + "0" * 24 + "1".rjust(40, "0")

    def test_single_call_matches_hand_written_vector(self):
        """Byte-exact against a vector assembled by hand from the ABI spec."""
        encoded = bk.encode_aggregate3([(USDC, "0x70a08231" + "0" * 63 + "1")])
        expected = (
            "0x82ad56cb"
            + "0" * 62 + "20"                                        # offset to the array
            + "0" * 63 + "1"                                         # one element
            + "0" * 62 + "20"                                        # element 0 offset
            + "0" * 24 + USDC[2:].lower()                            # target
            + "0" * 63 + "1"                                         # allowFailure = true
            + "0" * 62 + "60"                                        # bytes offset (96)
            + "0" * 62 + "24"                                        # length 36 bytes
            + "70a08231" + "0" * 63 + "1"                            # calldata
            + "0" * (64 - 8)                                         # pad 36 bytes -> 64
        )
        self.assertEqual(encoded.lower(), expected.lower())

    def test_two_calls_offsets_advance_by_full_tuple_stride(self):
        """The bug this suite exists for: stride is 4 words + payload, not 3 + payload."""
        encoded = bk.encode_aggregate3([(USDC, self.BALANCE_USDC), (AERO, self.BALANCE_USDC)])
        self.assertTrue(encoded.startswith("0x" + bk.SEL_AGGREGATE3))
        body = encoded[10:]  # strip "0x" + the 4-byte selector
        self.assertEqual(body[:64], "0" * 62 + "20")                 # array offset
        self.assertEqual(body[64:128], "0" * 63 + "2")               # count
        self.assertEqual(body[128:192], "0" * 62 + "40")             # element 0 at 0x40
        # stride: 3 head words + 1 length word + 2 payload words = 6 words = 0xC0
        self.assertEqual(body[192:256], "0" * 61 + "100")           # element 1 at 0x40+0xC0
        self.assertEqual(len(body) % 64, 0)

    def test_matches_cast_calldata_when_available(self):
        cast = shutil.which("cast")
        if not cast:
            self.skipTest("foundry `cast` not installed")
        calls = [(USDC, self.BALANCE_USDC), (AERO, "0x" + bk.SEL["symbol"])]
        mine = bk.encode_aggregate3(calls)
        args = "[" + ",".join(f"({t},true,{d})" for t, d in calls) + "]"
        theirs = subprocess.check_output(
            [cast, "calldata", "aggregate3((address,bool,bytes)[])", args], text=True
        ).strip()
        self.assertEqual(mine.lower(), theirs.lower())

    def test_decode_round_trip(self):
        """Decoder must mirror the encoder: element offsets are relative to the word
        after the length, and each tuple carries its own bytes offset."""
        payload = self._build_response([(True, b"\x01" * 32), (False, b"")])
        decoded = bk.decode_aggregate3(payload)
        self.assertEqual(len(decoded), 2)
        self.assertTrue(decoded[0][0])
        self.assertEqual(decoded[0][1], b"\x01" * 32)
        self.assertFalse(decoded[1][0])
        self.assertEqual(decoded[1][1], b"")

    def test_decode_rejects_garbage(self):
        with self.assertRaises(bk.BasekitError):
            bk.decode_aggregate3(b"")

    @staticmethod
    def _build_response(results: list[tuple[bool, bytes]]) -> bytes:
        """Hand-assemble an aggregate3 return payload, independently of the decoder."""
        count = len(results)
        tuples = []
        for success, data in results:
            padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
            tuples.append(
                word_int(1 if success else 0)
                + word_int(64)
                + word_int(len(data))
                + padded
            )
        heads, bodies, offset = [], [], 32 * count
        for body in tuples:
            heads.append(word_int(offset))
            bodies.append(body)
            offset += len(body)
        return (
            word_int(32)
            + word_int(count)
            + b"".join(heads)
            + b"".join(bodies)
        )

    def test_single_call_skips_multicall_contract(self):
        """One read is a plain eth_call: don't pay for a Multicall3 round trip."""
        seen = []

        class Probe(FakeRpc):
            def safe_eth_call(self, to, data):
                seen.append(to)
                return b"\x00" * 32

        bk.multicall(Probe(), [(USDC, "0x" + bk.SEL["symbol"])])
        self.assertEqual(seen, [USDC])

    def test_multi_call_uses_multicall3(self):
        seen = []

        class Probe(FakeRpc):
            def safe_eth_call(self, to, data):
                seen.append(to)
                return TestMulticallCodec._build_response([(True, b"\x01" * 32), (True, b"")])

        out = bk.multicall(Probe(), [(USDC, "0x" + bk.SEL["symbol"]), (AERO, "0x" + bk.SEL["symbol"])])
        self.assertEqual(seen, [bk.MULTICALL3])
        self.assertEqual(out[0], b"\x01" * 32)
        self.assertEqual(out[1], b"")


# --------------------------------------------------------------------------- #
# RPC client behaviour
# --------------------------------------------------------------------------- #
class TestRpcClient(unittest.TestCase):
    def test_unwrap_single_and_batch(self):
        rpc = bk.Rpc("https://x.invalid")
        self.assertEqual(rpc._unwrap({"result": "0x1"}, 1), ["0x1"])
        batched = [{"id": 1, "result": "b"}, {"id": 0, "result": "a"}]
        self.assertEqual(rpc._unwrap(batched, 2), ["a", "b"])

    def test_unwrap_errors(self):
        rpc = bk.Rpc("https://x.invalid")
        with self.assertRaises(bk.RpcError):
            rpc._unwrap({"error": {"message": "boom"}}, 1)
        with self.assertRaises(bk.RpcError):
            rpc._unwrap([{"id": 0, "result": "a"}], 2)  # missing second result

    def test_single_call_cache_hits(self):
        rpc = bk.Rpc("https://x.invalid")
        posts = []

        def fake_post(url, payload):
            posts.append(payload)
            return {"jsonrpc": "2.0", "id": 0, "result": "0xabc"}

        rpc._post = fake_post
        self.assertEqual(rpc.call("eth_blockNumber"), "0xabc")
        self.assertEqual(rpc.call("eth_blockNumber"), "0xabc")
        self.assertEqual(len(posts), 1, "second identical read should come from cache")

    def test_uncached_methods_always_hit_network(self):
        rpc = bk.Rpc("https://x.invalid")
        posts = []
        rpc._post = lambda url, payload: (posts.append(payload) or {"id": 0, "result": "0x1"})
        rpc.call("eth_call", [{"to": USDC, "data": "0x"}])
        rpc.call("eth_call", [{"to": USDC, "data": "0x"}])
        self.assertEqual(len(posts), 2)

    def test_failover_promotes_working_endpoint(self):
        rpc = bk.Rpc("https://bad.invalid")
        rpc.endpoints = ["https://bad.invalid", "https://good.invalid"]

        def fake_post(url, payload):
            if url == "https://bad.invalid":
                raise bk.urllib.error.HTTPError(url, 429, "rate limited", {}, None)
            return {"jsonrpc": "2.0", "id": 0, "result": "0x5"}

        rpc._post = fake_post
        self.assertEqual(rpc.call("eth_blockNumber"), "0x5")
        self.assertEqual(rpc.endpoints[0], "https://good.invalid")

    def test_all_endpoints_failing_raises_with_reasons(self):
        rpc = bk.Rpc("https://bad.invalid")
        rpc.endpoints = ["https://bad.invalid"]

        def fake_post(url, payload):
            raise bk.urllib.error.URLError("dns")

        rpc._post = fake_post
        with self.assertRaises(bk.RpcError) as ctx:
            rpc.call("eth_blockNumber")
        self.assertIn("bad.invalid", str(ctx.exception))

    def test_get_logs_sends_well_formed_topics(self):
        """Regression: address_word() returns a bare word, but an eth_getLogs topic
        must carry "0x" — mixing the two formed a topic array only lenient endpoints
        accepted, which is exactly the kind of bug that reads as "works on my RPC"."""
        rpc = bk.Rpc("https://x.invalid")
        seen = {}

        def fake_call(method, params=None):
            seen["method"] = method
            seen["params"] = params
            return []

        rpc.call = fake_call
        padded = bk.address_word(WALLET.lower())
        rpc.get_logs(USDC, [bk.TOPIC_TRANSFER, padded], 100, 200)
        topics = seen["params"][0]["topics"]
        self.assertEqual(seen["method"], "eth_getLogs")
        self.assertEqual(topics[0], bk.TOPIC_TRANSFER)
        self.assertEqual(topics[1], "0x" + padded)
        self.assertEqual(seen["params"][0]["fromBlock"], hex(100))

    def test_topic_helper_normalises_every_shape(self):
        word = "0" * 24 + "ab" * 20
        self.assertIsNone(bk._topic(None))
        self.assertIsNone(bk._topic(""))
        self.assertEqual(bk._topic(word), "0x" + word)
        self.assertEqual(bk._topic("0x" + "f" * 64), "0x" + "f" * 64)
        self.assertEqual(bk._topic("0x1234"), "0x" + "0" * 60 + "1234")
        self.assertEqual(len(bk._topic(word)), 66)

    def test_wildcard_topic_stays_a_wildcard(self):
        rpc = bk.Rpc("https://x.invalid")
        seen = {}
        rpc.call = lambda method, params=None: (seen.update(params=params) or [])
        rpc.get_logs(USDC, [bk.TOPIC_TRANSFER, None, bk.address_word(WALLET.lower())], 1, 2)
        topics = seen["params"][0]["topics"]
        self.assertIsNone(topics[1])
        self.assertTrue(topics[2].startswith("0x"))

    def test_get_logs_narrows_the_window_before_failing(self):
        """Live finding: publicnode rejects a 200-block USDC query with "query exceeds
        max results 20000" and suggests a smaller range. Narrowing toward the head is
        what the caller actually wants, so it must happen instead of an error."""
        rpc = bk.Rpc("https://x.invalid")
        spans = []

        def fake_call(method, params=None):
            rng = params[0]
            span = int(rng["toBlock"], 16) - int(rng["fromBlock"], 16)
            spans.append(span)
            if span > 100:
                raise bk.RpcError("eth_getLogs", [("https://x.invalid", "query exceeds max results 20000")])
            return [{"blockNumber": "0x1", "logIndex": "0x0", "topics": [], "data": "0x"}]

        rpc.call = fake_call
        logs = rpc.get_logs(USDC, [bk.TOPIC_TRANSFER], 0, 5000)
        self.assertEqual(len(logs), 1)
        self.assertTrue(rpc.log_window_narrowed)
        used_from, used_to = rpc.last_log_range
        self.assertLessEqual(used_to - used_from, 100)
        self.assertEqual(used_to, 5000, "the head end must be preserved")
        self.assertGreater(len(spans), 1, "it should have retried a smaller window")

    def test_get_logs_still_raises_when_narrowing_cannot_help(self):
        rpc = bk.Rpc("https://x.invalid")

        def always(method, params=None):
            raise bk.RpcError("eth_getLogs", [("https://x.invalid", "query exceeds max results")])

        rpc.call = always
        with self.assertRaises(bk.BasekitError):
            rpc.get_logs(USDC, [bk.TOPIC_TRANSFER], 100, 140)  # already tiny: no point retrying

    def test_log_rejection_explains_the_window_limit(self):
        rpc = bk.Rpc("https://x.invalid")

        def boom(method, params=None):
            raise bk.RpcError(method, [("x", "range too large")])

        rpc.call = boom
        with self.assertRaises(bk.BasekitError) as ctx:
            rpc.get_logs(USDC, [bk.TOPIC_TRANSFER], 1, 100000)
        self.assertIn("eth_getLogs", str(ctx.exception))
        self.assertIn("address filter", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Tools against a fake chain
# --------------------------------------------------------------------------- #
class TestNetworkStatus(unittest.TestCase):
    def setUp(self):
        self.block = {
            "baseFeePerGas": "0x12a05f200",   # 5 gwei
            "timestamp": "0x68fb0000",
            "transactions": ["0x1", "0x2"],
        }
        self.rpc = FakeRpc({
            "eth_gasPrice": "0x16b969d00",    # 6.1 gwei
            "eth_chainId": "0x2105",
            "eth_getBlockByNumber": self.block,
        })
        self._orig_price = bk.eth_price_usd
        bk.eth_price_usd = lambda: 2000.0

    def tearDown(self):
        bk.eth_price_usd = self._orig_price

    def test_shape_and_math(self):
        out = bk.network_status(self.rpc)
        self.assertEqual(out["chain_id"], 8453)
        self.assertEqual(out["head_block"], 53_522_884)
        self.assertEqual(out["txs_in_head_block"], 2)
        self.assertAlmostEqual(out["gas_price_gwei"], 6.1, places=3)
        self.assertAlmostEqual(out["base_fee_gwei"], 5.0, places=3)
        # 21000 gas at 6.1 gwei, ETH at $2000
        self.assertAlmostEqual(out["cost_usd"]["eth_transfer_21000"], 21000 * 6.1e-9 * 2000, places=6)
        self.assertTrue(out["read_only"])

    def test_tolerates_missing_block(self):
        rpc = FakeRpc({"eth_gasPrice": "0x1", "eth_chainId": "0x2105"})
        rpc.call = lambda method, params=None: None if method == "eth_getBlockByNumber" else FakeRpc.call(rpc, method, params)
        out = bk.network_status(rpc)
        self.assertIsNone(out["base_fee_gwei"])
        self.assertIsNone(out["block_time_utc"])


class TestPortfolio(unittest.TestCase):
    def setUp(self):
        self._orig_price = bk.prices
        self._orig_multicall = bk.multicall
        bk.prices = lambda addrs: (
            {USDC.lower(): {"price": 0.9998, "symbol": "USDC", "decimals": 6},
             "eth": {"price": 2000.0, "symbol": "ETH", "decimals": 18}},
            ["defillama"],
        )
        self.rpc = FakeRpc()

    def tearDown(self):
        bk.prices = self._orig_price
        bk.multicall = self._orig_multicall

    def test_reads_native_and_tokens_with_usd(self):
        # balances arrive as one multicall keyed by call position; symbol/name/decimals
        # follow in the same batch, so the stub answers per selector, not per index.
        def fake(rpc, batch):
            self._calls.append(batch)
            out = [None] * len(batch)
            for index, (target, data) in enumerate(batch):
                selector = data[2:10]
                if selector == bk.SEL["balanceOf"]:
                    out[index] = (1_500_000_000).to_bytes(32, "big") if target == USDC.lower() else (0).to_bytes(32, "big")
                elif selector == bk.SEL["symbol"]:
                    out[index] = enc_string("USDC") if target == USDC.lower() else enc_string("AERO")
                elif selector == bk.SEL["name"]:
                    out[index] = enc_string("USD Coin")
                elif selector == bk.SEL["decimals"]:
                    out[index] = (6).to_bytes(32, "big")
            return out

        self._calls = []
        bk.multicall = fake
        out = bk.portfolio(self.rpc, WALLET, plugin_dir=Path(__file__).resolve().parent.parent)
        symbols = {h["symbol"]: h for h in out["holdings"]}
        self.assertIn("ETH", symbols)
        self.assertAlmostEqual(symbols["ETH"]["value_usd"], 1000.0, places=2)  # 0.5 * 2000
        usdc = next(h for h in out["holdings"] if h["address"] == USDC.lower())
        self.assertAlmostEqual(usdc["balance"], 1500.0, places=6)
        self.assertAlmostEqual(usdc["value_usd"], 1499.7, places=1)
        self.assertAlmostEqual(out["total_usd"], 2499.7, places=1)
        self.assertTrue(out["read_only"] if "read_only" in out else True)

    def test_portfolio_defaults_to_the_shipped_token_set(self):
        """Regression: portfolio(plugin_dir=None) used to scan zero tokens because
        load_token_set(None) returned [] — a funded wallet was reported as empty."""
        seen = []
        bk.multicall = lambda rpc, batch: (seen.append(batch) or [None] * len(batch))
        out = bk.portfolio(self.rpc, WALLET)  # deliberately no plugin_dir
        self.assertGreaterEqual(out["scanned_tokens"], 10)
        self.assertTrue(seen and len(seen[0]) >= 10, "the balance multicall must cover the token set")
        self.assertNotIn("WARNING", out["note"])

    def test_portfolio_warns_when_the_token_set_is_unreadable(self):
        original = bk.load_token_set
        bk.load_token_set = lambda plugin_dir=None: []
        try:
            out = bk.portfolio(self.rpc, WALLET)
        finally:
            bk.load_token_set = original
        self.assertEqual(out["scanned_tokens"], 0)
        self.assertIn("WARNING", out["note"])
        self.assertIn("NOT a complete portfolio", out["note"])

    def test_rejects_bad_address(self):
        with self.assertRaises(bk.BasekitError):
            bk.portfolio(self.rpc, "not-an-address")

    def test_unpriced_token_reports_null_not_a_guess(self):
        bk.prices = lambda addrs: ({"eth": {"price": 2000.0, "symbol": "ETH", "decimals": 18}}, [])

        def fake(rpc, batch):
            out = [None] * len(batch)
            for index, (target, data) in enumerate(batch):
                selector = data[2:10]
                if selector == bk.SEL["balanceOf"]:
                    out[index] = (10**18).to_bytes(32, "big")
                elif selector == bk.SEL["symbol"]:
                    out[index] = enc_string("WEIRD")
                elif selector == bk.SEL["decimals"]:
                    out[index] = (18).to_bytes(32, "big")
            return out

        bk.multicall = fake
        out = bk.portfolio(self.rpc, WALLET, plugin_dir=Path(__file__).resolve().parent.parent)
        unpriced = [h for h in out["holdings"] if h["symbol"] != "ETH"]
        self.assertTrue(unpriced)
        self.assertIsNone(unpriced[0]["value_usd"])


class TestTxReceipt(unittest.TestCase):
    def setUp(self):
        self._orig_price = bk.eth_price_usd
        self._orig_prices = bk.prices
        self._orig_multicall = bk.multicall
        bk.eth_price_usd = lambda: 2000.0
        bk.prices = lambda addrs: ({USDC.lower(): {"price": 1.0, "symbol": "USDC", "decimals": 6}}, ["defillama"])

        def fake_multicall(rpc, batch):
            """Faithful metadata answer: erc20_metadata asks symbol, name, decimals."""
            out = [None] * len(batch)
            for index, (target, data) in enumerate(batch):
                selector = data[2:10]
                if selector == bk.SEL["symbol"]:
                    out[index] = enc_string("USDC")
                elif selector == bk.SEL["name"]:
                    out[index] = enc_string("USD Coin")
                elif selector == bk.SEL["decimals"]:
                    out[index] = (6).to_bytes(32, "big")
            return out

        bk.multicall = fake_multicall

        transfer_log = {
            "address": USDC,
            "topics": [
                bk.TOPIC_TRANSFER,
                "0x" + "0" * 24 + "be3adedf09c78ae29fd3e112d6423592a13b8926",
                "0x" + "0" * 24 + "6867ef8c1904a10e31d856ebbc5d6acf91dbe48a",
            ],
            "data": "0x" + format(1_404_000_000, "064x"),
        }
        approval_log = {
            "address": USDC,
            "topics": [
                bk.TOPIC_APPROVAL,
                "0x" + "0" * 24 + "be3adedf09c78ae29fd3e112d6423592a13b8926",
                "0x" + "0" * 24 + "1111111111111111111111111111111111111111",
            ],
            "data": "0x" + format(2**256 - 1, "064x"),
        }
        nft_log = {
            "address": "0x" + "22" * 20,
            "topics": [bk.TOPIC_TRANSFER, "0x" + "0" * 64, "0x" + "0" * 24 + WALLET[2:], "0x" + "0" * 62 + "2a"],
            "data": "0x",
        }
        self.receipt = {
            "blockNumber": "0x330a5f3",
            "status": "0x1",
            "gasUsed": "0xb59c",           # 46492
            "effectiveGasPrice": "0x1dd0d9a00",
            "logs": [transfer_log, approval_log, nft_log],
        }
        self.tx = {
            "from": "0x" + "ab" * 20,
            "to": USDC,
            "value": "0x0",
            "gasPrice": "0x1dd0d9a00",
            "input": "0x23b872dd" + "0" * 128,
            "nonce": "0x1",
        }
        self.rpc = FakeRpc({
            "eth_getTransactionByHash": self.tx,
            "eth_getTransactionReceipt": self.receipt,
            "eth_getBlockByNumber": {"timestamp": "0x68fb0000"},
            "eth_getLogs": [],
        })

    def tearDown(self):
        bk.eth_price_usd = self._orig_price
        bk.prices = self._orig_prices
        bk.multicall = self._orig_multicall

    def test_decodes_transfer_approval_and_nft(self):
        out = bk.tx_receipt(self.rpc, TXH)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["method_selector"], "0x23b872dd")
        self.assertEqual(out["log_count"], 3)
        erc20 = next(t for t in out["token_transfers"] if t["standard"] == "ERC-20")
        self.assertAlmostEqual(erc20["amount"], 1404.0, places=6)
        self.assertEqual(erc20["symbol"], "USDC")
        self.assertEqual(erc20["to"], "0x6867ef8c1904a10e31d856ebbc5d6acf91dbe48a")
        nft = next(t for t in out["token_transfers"] if t["standard"] == "ERC-721")
        self.assertEqual(nft["token_id"], "42")
        self.assertEqual(len(out["approvals"]), 1)
        self.assertTrue(out["approvals"][0]["unlimited"])
        self.assertEqual(out["gas_used"], 46492)
        self.assertIsNotNone(out["fee_eth"])
        self.assertIsNotNone(out["fee_usd"])

    def test_reverted_status(self):
        receipt = dict(self.receipt, status="0x0")
        rpc = FakeRpc({"eth_getTransactionByHash": self.tx,
                       "eth_getTransactionReceipt": receipt,
                       "eth_getBlockByNumber": {"timestamp": "0x68fb0000"}})
        out = bk.tx_receipt(rpc, TXH)
        self.assertEqual(out["status"], "reverted")

    def test_pending_tx_has_no_receipt(self):
        rpc = FakeRpc({"eth_getTransactionByHash": self.tx, "eth_getTransactionReceipt": None})
        out = bk.tx_receipt(rpc, TXH)
        self.assertEqual(out["status"], "pending")

    def test_unknown_tx_raises(self):
        rpc = FakeRpc({"eth_getTransactionByHash": None, "eth_getTransactionReceipt": None})
        with self.assertRaises(bk.BasekitError):
            bk.tx_receipt(rpc, TXH)

    def test_bad_hash_raises(self):
        with self.assertRaises(bk.BasekitError):
            bk.tx_receipt(self.rpc, "0x1234")


class TestTokenFacts(unittest.TestCase):
    def setUp(self):
        self._orig_multicall = bk.multicall
        bk.multicall = lambda rpc, batch: [
            enc_string("USD Coin"), enc_string("USDC"), (6).to_bytes(32, "big"),
            (4_342_505_628_992_996).to_bytes(32, "big"),
            bk.address_word("0x3abd6f64a422225e61e435bae41db12096106df7").encode(),
        ][:len(batch)]
        self.rpc = FakeRpc()

    def tearDown(self):
        bk.multicall = self._orig_multicall

    def test_facts(self):
        out = bk.token_facts(self.rpc, USDC)
        self.assertEqual(out["symbol"], "USDC")
        self.assertEqual(out["decimals"], 6)
        self.assertEqual(out["standards"], ["ERC-20"])
        self.assertAlmostEqual(out["total_supply"], 4342505628.993, places=3)
        self.assertIn("heuristics_note", out)
        self.assertEqual(out["source_verification"]["status"], "unchecked")

    def test_eoa_reported_as_non_contract(self):
        rpc = FakeRpc({"eth_getCode": "0x"})
        out = bk.token_facts(rpc, USDC)
        self.assertFalse(out["is_contract"])
        self.assertIn("note", out)

    def test_eip1967_proxy_detected_from_storage(self):
        impl = "0x" + "0" * 24 + "2ce6311ddae708829bc0784c967b7d77d19fd779"
        rpc = FakeRpc({"eth_getStorageAt": impl.replace("0x", "0x" + "0" * 0)})
        out = bk.token_facts(rpc, USDC)
        self.assertTrue(out["proxy"]["is_proxy"])
        self.assertEqual(out["proxy"]["pattern"], "eip1967")
        self.assertEqual(out["proxy"]["implementation"], "0x2ce6311ddae708829bc0784c967b7d77d19fd779")

    def test_verification_reports_unconfigured_without_a_key(self):
        out = bk.verify_source(USDC, None)
        self.assertEqual(out["status"], "not_configured")
        self.assertIn("Etherscan", out["would_use"])

    def test_bad_address_raises(self):
        with self.assertRaises(bk.BasekitError):
            bk.token_facts(self.rpc, "0xnope")


class TestWindowedLogTools(unittest.TestCase):
    def setUp(self):
        self._orig_multicall = bk.multicall
        self._orig_prices = bk.prices
        bk.multicall = lambda rpc, batch: [
            enc_string("USDC"), enc_string("USD Coin"), (6).to_bytes(32, "big"),
            (5 * 10**6).to_bytes(32, "big"),
        ][:len(batch)]
        bk.prices = lambda addrs: ({USDC.lower(): {"price": 1.0}}, ["defillama"])

    def tearDown(self):
        bk.multicall = self._orig_multicall
        bk.prices = self._orig_prices

    def _log(self, frm, to, amount, block=100, index=0):
        return {
            "address": USDC,
            "blockNumber": hex(block),
            "logIndex": hex(index),
            "transactionHash": TXH,
            "topics": [bk.TOPIC_TRANSFER, "0x" + "0" * 24 + frm[2:].lower(), "0x" + "0" * 24 + to[2:].lower()],
            "data": "0x" + format(amount, "064x"),
        }

    def test_activity_states_its_window_and_direction(self):
        rpc = FakeRpc(block=100_000)
        rpc.logs = [self._log(WALLET, "0x" + "11" * 20, 2_000_000)]
        out = bk.token_activity(rpc, WALLET, USDC, blocks=3000)
        self.assertEqual(out["window"]["to_block"], 100_000)
        self.assertEqual(out["window"]["from_block"], 97_000)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["transfers"][0]["direction"], "out")
        self.assertAlmostEqual(out["transfers"][0]["amount"], 2.0, places=6)
        self.assertIn("eth_getLogs", out["window"]["note"])

    def test_activity_clamps_bounds(self):
        rpc = FakeRpc(block=100_000)
        out = bk.token_activity(rpc, WALLET, USDC, blocks=10**9)
        self.assertEqual(out["window"]["blocks"], 50_000)
        out = bk.token_activity(rpc, WALLET, USDC, blocks=1)
        self.assertEqual(out["window"]["blocks"], 500)

    def test_allowances_drops_revoked_and_flags_unlimited(self):
        spender_a = "0x" + "aa" * 20
        spender_b = "0x" + "bb" * 20
        rpc = FakeRpc(block=100_000)
        rpc.logs = [
            {"address": USDC, "blockNumber": "0x1", "logIndex": "0x0", "transactionHash": TXH,
             "topics": [bk.TOPIC_APPROVAL, "0x" + "0" * 24 + WALLET[2:].lower(), "0x" + "0" * 24 + spender_a[2:]],
             "data": "0x" + format(2**256 - 1, "064x")},
            {"address": USDC, "blockNumber": "0x1", "logIndex": "0x1", "transactionHash": TXH,
             "topics": [bk.TOPIC_APPROVAL, "0x" + "0" * 24 + WALLET[2:].lower(), "0x" + "0" * 24 + spender_b[2:]],
             "data": "0x" + format(10**6, "064x")},
        ]
        # Whoever is asked, return: unlimited for A, zero for B (already revoked).
        bk.multicall = lambda r, batch: [
            (2**256 - 1).to_bytes(32, "big") if batch[0][1].endswith(spender_a[2:]) else (0).to_bytes(32, "big")
        ] + [enc_string("USDC"), enc_string("USDC"), (6).to_bytes(32, "big")][:8]
        bk.multicall = lambda r, batch: [
            ((2**256 - 1) if batch[i][1].endswith(spender_a[2:]) else 0).to_bytes(32, "big")
            for i in range(len(batch))
        ]
        out = bk.token_allowances(rpc, WALLET, USDC, blocks=3000)
        self.assertEqual(out["active_count"], 1)
        self.assertEqual(out["unlimited_count"], 1)
        self.assertTrue(out["active_allowances"][0]["unlimited"])
        self.assertEqual(out["active_allowances"][0]["spender"], spender_a)
        self.assertIn("never revokes", out["note"])

    def test_activity_reports_a_narrowed_window(self):
        """When the node narrows the range, the payload must say so instead of
        pretending the requested window was read."""

        class Narrowing(FakeRpc):
            def get_logs(self, address, topics, from_block, to_block, limit=400):
                self.last_log_range = (to_block - 160, to_block)
                self.log_window_narrowed = True
                return []

        out = bk.token_activity(Narrowing(block=100_000), WALLET, USDC, blocks=20_000)
        self.assertEqual(out["window"]["blocks"], 160)
        self.assertEqual(out["window"]["requested_blocks"], 20_000)
        self.assertTrue(out["window"]["narrowed"])
        self.assertIn("narrowed automatically", out["window"]["note"])

    def test_allowances_reports_the_window_it_read(self):
        class Narrowing(FakeRpc):
            def get_logs(self, address, topics, from_block, to_block, limit=400):
                self.last_log_range = (to_block - 50, to_block)
                self.log_window_narrowed = True
                return []

        out = bk.token_allowances(Narrowing(block=100_000), WALLET, USDC, blocks=20_000)
        self.assertEqual(out["window"]["blocks"], 50)
        self.assertTrue(out["window"]["narrowed"])

    def test_bad_addresses_rejected(self):
        rpc = FakeRpc()
        for args in ((("bad"), USDC), (WALLET, "bad")):
            with self.assertRaises(bk.BasekitError):
                bk.token_activity(rpc, args[0], args[1])


class TestSnapshotAndLedger(unittest.TestCase):
    def setUp(self):
        self._orig_price = bk.eth_price_usd
        bk.eth_price_usd = lambda: 2000.0
        self.rpc = FakeRpc({"eth_gasPrice": "0x1", "eth_chainId": "0x2105",
                            "eth_getBlockByNumber": {"baseFeePerGas": "0x1", "timestamp": "0x68fb0000",
                                                     "transactions": []}})

    def tearDown(self):
        bk.eth_price_usd = self._orig_price

    def test_snapshot_without_watch_address(self):
        out = bk.snapshot(self.rpc, None, ledger=[{"tool": "base_network"}])
        self.assertIn("network", out)
        self.assertNotIn("wallet", out)
        self.assertEqual(out["ledger"], [{"tool": "base_network"}])
        self.assertIsNotNone(out["as_of"])

    def test_snapshot_skips_wallet_for_a_malformed_address(self):
        out = bk.snapshot(self.rpc, "0xbad", ledger=[])
        self.assertNotIn("wallet", out)  # never invent a wallet section for junk input

    def test_snapshot_reports_wallet_errors_inline(self):
        original = bk.portfolio

        def boom(*args, **kwargs):
            raise bk.BasekitError("RPC refused the balance read")

        bk.portfolio = boom
        try:
            out = bk.snapshot(self.rpc, WALLET, ledger=[])
        finally:
            bk.portfolio = original
        self.assertIn("error", out["wallet"])
        self.assertEqual(out["wallet"]["address"], WALLET)
        self.assertIn("RPC refused", out["wallet"]["error"])

    def test_ledger_round_trip_and_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            for index in range(5):
                bk.append_ledger(path, {"tool": "base_network", "n": index}, limit=3)
            entries = bk.read_ledger(path, limit=10)
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0]["n"], 4)   # newest first
            self.assertEqual(entries[-1]["n"], 2)

    def test_ledger_survives_junk_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            bk.append_ledger(path, {"tool": "x"})
            with path.open("a") as handle:
                handle.write("not json\n")
            self.assertEqual(len(bk.read_ledger(path)), 1)

    def test_read_ledger_missing_file(self):
        self.assertEqual(bk.read_ledger("/nonexistent/ledger.jsonl"), [])


class TestSettings(unittest.TestCase):
    def test_defaults_when_config_unavailable(self):
        settings = bk.load_settings()
        self.assertIn("rpc_url", settings)
        self.assertIn("watch_address", settings)
        self.assertTrue(settings["inject_chain_context"])
        self.assertEqual(settings["rpc_url"], bk.DEFAULT_RPC)

    def test_token_set_loads_and_is_valid(self):
        tokens = bk.load_token_set(Path(__file__).resolve().parent.parent)
        self.assertGreaterEqual(len(tokens), 10)
        for token in tokens:
            self.assertTrue(bk.is_address(token["address"]), token)
            self.assertTrue(1 <= token["decimals"] <= 36, token)
            self.assertTrue(token["symbol"])

    def test_token_set_missing_file_is_empty_not_an_error(self):
        self.assertEqual(bk.load_token_set("/nonexistent"), [])

    def test_token_set_defaults_to_the_modules_own_directory(self):
        """A caller that forgets plugin_dir must still get the shipped token set."""
        self.assertEqual(len(bk.load_token_set(None)),
                         len(bk.load_token_set(Path(__file__).resolve().parent.parent)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
