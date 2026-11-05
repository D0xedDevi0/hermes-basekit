"""Live smoke test against real Base — opt-in, so CI stays hermetic.

Run with:  BASEKIT_LIVE=1 python3 tests/test_live.py

It asserts *structure and honesty*, not specific market values: block numbers and prices
change every few seconds, so a test that pinned them would be a lie generator. What it
checks is that real reads come back, that the window is stated, that a real transaction
decodes, and that nothing claims to be signed or sent.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import basekit_core as bk  # noqa: E402

LIVE = os.environ.get("BASEKIT_LIVE") == "1"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"


@unittest.skipUnless(LIVE, "set BASEKIT_LIVE=1 to hit the real chain")
class TestLiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rpc = bk.Rpc()
        cls.started = time.time()
        cls._subject: str | None = None

    @classmethod
    def _usdc_holder(cls) -> str:
        """Discover a wallet that *verifiably* holds USDC: take recent transfer
        recipients and confirm a live balance, because the recipient of a transfer is
        usually a pool or router, not a holder."""
        if cls._subject:
            return cls._subject
        head = cls.rpc.head_block()
        logs = cls.rpc.get_logs(USDC, [bk.TOPIC_TRANSFER], head - 200, head, limit=25)
        candidates: list[str] = []
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) >= 3:
                address = "0x" + topics[2][-40:]
                if address.lower() != USDC.lower() and address not in candidates:
                    candidates.append(address)
        if not candidates:
            raise unittest.SkipTest("no recent USDC recipients found")
        shortlist = candidates[:8]
        results = bk.multicall(
            cls.rpc,
            [(USDC, "0x" + bk.SEL["balanceOf"] + bk.address_word(a)) for a in shortlist],
        )
        for address, raw in zip(shortlist, results):
            if raw and bk.abi_uint(raw) > 0:
                cls._subject = address
                print(f"  subject {address[:10]}… verified holding USDC on-chain")
                return address
        raise unittest.SkipTest("none of the recent recipients still hold USDC")

    def test_network_state(self):
        status = bk.network_status(self.rpc)
        self.assertEqual(status["chain_id"], 8453)
        self.assertGreater(status["head_block"], 1_000_000)
        self.assertIsNotNone(status["gas_price_gwei"])
        self.assertTrue(status["read_only"])
        print(f"  block {status['head_block']} · gas {status['gas_price_gwei']} gwei · "
              f"ETH ${status['eth_price_usd']} · transfer ${status['cost_usd']['eth_transfer_21000']}")
        print(f"  rpc {status['rpc']['endpoint']} · {status['rpc']['latency_ms']} ms")

    def test_token_facts_are_real(self):
        facts = bk.token_facts(self.rpc, USDC)
        self.assertEqual(facts["symbol"], "USDC")
        self.assertEqual(facts["decimals"], 6)
        self.assertGreater(facts["total_supply"], 1_000_000_000)
        print(f"  USDC supply {facts['total_supply']:,.0f} · proxy={facts['proxy']['is_proxy']}")

    def test_weth_has_no_proxy_owner_theatre(self):
        facts = bk.token_facts(self.rpc, WETH)
        self.assertEqual(facts["decimals"], 18)
        self.assertIsInstance(facts["proxy"]["is_proxy"], bool)

    def test_portfolio_reads_a_real_wallet(self):
        subject = self._usdc_holder()
        started = time.time()
        result = bk.portfolio(self.rpc, subject)
        elapsed = time.time() - started
        self.assertIn("total_usd", result)
        self.assertTrue(result["holdings"], "a wallet verified to hold USDC must show a holding")
        symbols = {holding["symbol"] for holding in result["holdings"]}
        self.assertIn("USDC", symbols)
        for holding in result["holdings"]:
            self.assertIsInstance(holding["balance"], float)
            if holding["value_usd"] is not None:
                self.assertGreaterEqual(holding["value_usd"], 0)
        print(f"  wallet {subject[:10]}… total ${result['total_usd']} "
              f"across {len(result['holdings'])} positions in {elapsed:.2f}s")

    def test_windowed_activity_states_its_window(self):
        subject = self._usdc_holder()
        result = bk.token_activity(self.rpc, subject, USDC, blocks=2000)
        window = result["window"]
        self.assertGreaterEqual(window["blocks"], 64)
        self.assertLessEqual(window["blocks"], 2000)
        self.assertIn("eth_getLogs", window["note"])
        self.assertLessEqual(result["count"], 25)
        print(f"  USDC transfers over {window['blocks']} blocks "
              f"({window['approx_hours']}h, narrowed={window['narrowed']}): {result['count']}")
        self.assertIn("transfers", result)

    def test_a_real_receipt_decodes(self):
        """Take a real USDC transfer and decode its receipt end to end."""
        head = self.rpc.head_block()
        logs = self.rpc.get_logs(USDC, [bk.TOPIC_TRANSFER], head - 200, head, limit=1)
        if not logs:
            self.skipTest("no USDC transfer in the last 200 blocks to decode")
        tx_hash = logs[0]["transactionHash"]
        decoded = bk.tx_receipt(self.rpc, tx_hash)
        self.assertIn(decoded["status"], ("success", "reverted"))
        # Both the exact integer and the human amount must be present: one for precision,
        # one so a model can't misread the units (the classic 1.404e9 USDC bug).
        for transfer in decoded["token_transfers"]:
            if transfer["standard"] == "ERC-20":
                self.assertIsNotNone(transfer["amount"])
                self.assertTrue(str(transfer["raw_amount"]).isdigit())
        print(f"  tx {tx_hash[:14]}… {decoded['status']} · {decoded['log_count']} logs · "
              f"fee ${decoded['fee_usd']} · transfers {len(decoded['token_transfers'])}")

    def test_snapshot_payload_shape(self):
        payload = bk.snapshot(self.rpc, None, ledger=[])
        self.assertIn("network", payload)
        self.assertIn("as_of", payload)

    def test_no_tool_can_write(self):
        """Guard against a future edit quietly adding a write path."""
        source = (Path(__file__).resolve().parent.parent / "basekit_core.py").read_text()
        for forbidden in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign",
                          "privateKey", "private_key", "mnemonic", "personal_sign"):
            self.assertNotIn(forbidden, source, f"{forbidden} has no place in basekit")

    @classmethod
    def tearDownClass(cls):
        print(f"  live suite took {time.time() - cls.started:.2f}s "
              f"against {cls.rpc.last_endpoint}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
