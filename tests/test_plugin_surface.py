"""Offline tests for the plugin surface: schemas, handlers, registration, manifest.

The point of these is that a schema the model can't satisfy, or a handler that doesn't
return JSON, is a broken plugin — so they're asserted rather than reviewed by eye.

No network: the RPC client is replaced with a fake, and the ledger is redirected to a
temp dir so a test run never writes into the real plugin data directory.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FakeRpc, enc_string  # noqa: E402

import basekit_core as bk  # noqa: E402
import schemas as schema_module  # noqa: E402
import tools as tools_module  # noqa: E402


def load_plugin_init():
    """Import __init__.py by path — the directory name isn't a valid module name."""
    spec = importlib.util.spec_from_file_location("basekit_plugin_init", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["basekit_plugin_init"] = module
    spec.loader.exec_module(module)
    return module


class RecordingCtx:
    """Stands in for the plugin context, recording how the plugin registers itself."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return recorder

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class TestSchemas(unittest.TestCase):
    EXPECTED = {
        "base_network", "base_portfolio", "base_tx", "base_token", "base_activity", "base_allowances"
    }

    def test_all_six_present_and_named(self):
        names = {schema["name"] for schema in schema_module.ALL_SCHEMAS}
        self.assertEqual(names, self.EXPECTED)

    def test_shape(self):
        for schema in schema_module.ALL_SCHEMAS:
            with self.subTest(schema=schema["name"]):
                self.assertTrue(schema["description"].strip())
                params = schema["parameters"]
                self.assertEqual(params["type"], "object")
                self.assertFalse(params["additionalProperties"])
                self.assertIn("properties", params)
                for field in params.get("required", []):
                    self.assertIn(field, params["properties"], f"{field} required but not described")

    def test_read_only_promised_in_every_description(self):
        """The model reads these; a schema that doesn't say 'read-only' invites a
        request to send a transaction and then a confusing refusal."""
        for schema in schema_module.ALL_SCHEMAS:
            with self.subTest(schema=schema["name"]):
                self.assertIn("read-only", schema["description"].lower())

    def test_activity_is_explicit_about_its_window(self):
        params = schema_module.ACTIVITY_SCHEMA["parameters"]
        self.assertIn("blocks", params["properties"])
        self.assertIn("token", params["required"])
        self.assertIn("address", params["required"])


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
class TestHandlers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_rpc = tools_module.get_rpc
        self.orig_data_dir = tools_module.data_dir
        tools_module.data_dir = lambda: Path(self.tmp.name)
        self.rpc = FakeRpc({
            "eth_gasPrice": "0x16b969d00",
            "eth_chainId": "0x2105",
            "eth_getBlockByNumber": {"baseFeePerGas": "0x12a05f200", "timestamp": "0x68fb0000",
                                     "transactions": []},
            "eth_getTransactionByHash": None,
            "eth_getTransactionReceipt": None,
        })
        tools_module.get_rpc = lambda url=None: self.rpc
        self.orig_multicall = bk.multicall
        bk.multicall = lambda rpc, batch: [None] * len(batch)
        self.orig_prices = bk.prices
        self.orig_eth_price = bk.eth_price_usd
        bk.prices = lambda addrs: ({"eth": {"price": 2000.0}}, ["defillama"])
        bk.eth_price_usd = lambda: 2000.0

    def tearDown(self):
        tools_module.get_rpc = self.orig_rpc
        tools_module.data_dir = self.orig_data_dir
        bk.multicall = self.orig_multicall
        bk.prices = self.orig_prices
        bk.eth_price_usd = self.orig_eth_price
        self.tmp.cleanup()

    def test_handlers_cover_the_schemas(self):
        self.assertEqual(set(tools_module.HANDLERS), {s["name"] for s in schema_module.ALL_SCHEMAS})

    def test_every_handler_returns_parseable_json(self):
        from test_core import USDC, WALLET, TXH

        calls = {
            "base_network": {},
            "base_portfolio": {"address": WALLET},
            "base_tx": {"hash": TXH},
            "base_token": {"address": USDC},
            "base_activity": {"address": WALLET, "token": USDC, "blocks": 1000},
            "base_allowances": {"address": WALLET, "token": USDC, "blocks": 1000},
        }
        for name, args in calls.items():
            with self.subTest(tool=name):
                handler = tools_module.HANDLERS[name]
                raw = handler(args)
                self.assertIsInstance(raw, str)
                payload = json.loads(raw)
                self.assertIsInstance(payload, dict)

    def test_errors_are_actionable_not_tracebacks(self):
        out = json.loads(tools_module.HANDLERS["base_tx"]({"hash": "0xnotahash"}))
        self.assertIn("error", out)
        self.assertNotIn("Traceback", json.dumps(out))

    def test_handlers_accept_extra_kwargs(self):
        """The registry calls handlers with keyword context (hermes_home, session id...)."""
        handler = tools_module.HANDLERS["base_network"]
        self.assertIsInstance(handler({}, hermes_home="/tmp", agent_identity="x"), str)

    def test_ledger_records_the_tool_and_the_block(self):
        tools_module.HANDLERS["base_network"]({})
        entries = tools_module.read_ledger(limit=5)
        self.assertTrue(entries)
        self.assertEqual(entries[0]["tool"], "base_network")
        self.assertIn("head_block", entries[0])

    def test_network_payload_states_read_only(self):
        payload = json.loads(tools_module.HANDLERS["base_network"]({}))
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["chain_id"], 8453)

    def test_missing_required_argument_is_reported(self):
        out = json.loads(tools_module.HANDLERS["base_portfolio"]({}))
        self.assertIn("error", out)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
class TestRegistration(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin_init()
        self.ctx = RecordingCtx()
        self.module.register(self.ctx)

    def test_registers_all_six_tools(self):
        calls = self.ctx.named("register_tool")
        self.assertEqual(len(calls), 6)
        names = {kwargs.get("name") or args[0] for _, args, kwargs in calls}
        self.assertEqual(names, {s["name"] for s in schema_module.ALL_SCHEMAS})

    def test_tools_are_tagged_with_a_toolset(self):
        for _, args, kwargs in self.ctx.named("register_tool"):
            self.assertEqual(kwargs.get("toolset", args[1] if len(args) > 1 else None), "basekit")

    def test_each_tool_gets_a_callable_handler_and_a_schema(self):
        by_name = {}
        for _, args, kwargs in self.ctx.named("register_tool"):
            name = kwargs.get("name") or args[0]
            by_name[name] = kwargs
        for schema in schema_module.ALL_SCHEMAS:
            entry = by_name[schema["name"]]
            self.assertTrue(callable(entry["handler"]))
            self.assertIsInstance(entry["schema"], dict)
            self.assertEqual(entry["schema"]["name"], schema["name"])

    def test_registers_the_pre_llm_hook(self):
        hooks = self.ctx.named("register_hook")
        self.assertEqual(len(hooks), 1)
        _, args, kwargs = hooks[0]
        self.assertEqual(args[0] if args else kwargs.get("event"), "pre_llm_call")
        self.assertTrue(callable(args[1] if len(args) > 1 else kwargs["handler"]))

    def test_registers_slash_command_and_cli(self):
        self.assertEqual(len(self.ctx.named("register_command")), 1)
        self.assertEqual(len(self.ctx.named("register_cli_command")), 1)

    def test_registers_the_bundled_skill(self):
        skills = self.ctx.named("register_skill")
        self.assertEqual(len(skills), 1)
        blob = json.dumps([str(call) for call in skills])
        self.assertIn("SKILL.md", blob)

    def test_bundled_skill_file_exists(self):
        self.assertTrue((PLUGIN_DIR / "skills" / "basekit" / "SKILL.md").is_file())

    def test_registration_is_idempotent(self):
        """Re-registering must not explode (Hermes can rebuild the plugin registry)."""
        self.module.register(RecordingCtx())


class TestContextInjection(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin_init()

    def test_base_terms_trigger_injection(self):
        for text in (
            "what's gas on base right now?",
            "check this address 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "how much is my wallet worth",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.module._is_base_relevant(text))

    def test_unrelated_turns_do_not_trigger_injection(self):
        for text in ("write me a haiku about rain", "summarise this contract clause", "hello there"):
            with self.subTest(text=text):
                self.assertFalse(self.module._is_base_relevant(text))

    def test_unrelated_turn_returns_none_without_touching_the_rpc(self):
        calls = []
        original = self.module._context_line
        self.module._context_line = lambda *a, **k: calls.append(1)
        try:
            result = self.module._pre_llm_call(prompt="write me a haiku about rain")
        finally:
            self.module._context_line = original
        self.assertIsNone(result)
        self.assertEqual(calls, [], "unrelated turns must not cost an RPC call")

    def test_hook_never_raises_on_junk_input(self):
        for kwargs in ({}, {"prompt": None}, {"prompt": 12345}, {"prompt": ""}):
            with self.subTest(kwargs=kwargs):
                self.module._pre_llm_call(**kwargs)

    def test_ledger_text_is_plain_text(self):
        # __init__ loads tools.py by path under the name "basekit_tools", so its
        # reference is a different module object than a plain `import tools` here.
        original = self.module.tools.read_ledger
        self.module.tools.read_ledger = lambda limit=5: [{"tool": "base_network", "head_block": 53_522_884}]
        try:
            text = self.module._ledger_text(3)
        finally:
            self.module.tools.read_ledger = original
        self.assertIn("base_network", text)
        self.assertIn("53522884", text)   # the block is reported verbatim

    def test_ledger_text_when_empty(self):
        original = self.module.tools.read_ledger
        self.module.tools.read_ledger = lambda limit=5: []
        try:
            self.assertIn("nothing looked up", self.module._ledger_text())
        finally:
            self.module.tools.read_ledger = original


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")

    def test_required_fields(self):
        for pattern in (r"^name: basekit$", r"^manifest_version: 2$", r"^api_version: 1$",
                        r"^version: 1\.0\.0$", r"^license: MIT$"):
            with self.subTest(pattern=pattern):
                self.assertRegex(self.text, re.compile(pattern, re.MULTILINE))

    def test_no_python_dependencies(self):
        """The whole selling point is stdlib-only: an empty list is load-bearing."""
        self.assertRegex(self.text, re.compile(r"^python_dependencies: \[\]$", re.MULTILINE))

    def test_no_required_env(self):
        self.assertRegex(self.text, re.compile(r"^requires_env: \[\]$", re.MULTILINE))

    def test_declares_the_hooks_and_tools_it_registers(self):
        """The manifest must agree with what register() actually adds. This is the exact
        rule the catalog validator enforces ('undeclared tools registered'), so a drift
        between plugin.yaml and __init__.py fails here instead of at review."""
        self.assertRegex(self.text, re.compile(r"^provides_hooks: \[pre_llm_call\]$", re.MULTILINE))
        self.assertRegex(self.text, re.compile(r"^provides_tools:$", re.MULTILINE))
        for tool in ("base_network", "base_portfolio", "base_tx",
                     "base_token", "base_activity", "base_allowances"):
            self.assertIn(f"  - {tool}\n", self.text)

    def test_secret_is_env_backed_and_optional(self):
        self.assertIn("basescan_api_key", self.text)
        self.assertIn("env: BASESCAN_API_KEY", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
