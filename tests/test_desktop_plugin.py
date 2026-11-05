"""Loads desktop/plugin.js in Node against a stub SDK and *runs* it.

Why this exists: the desktop half is plain ESM loaded uncompiled by the app. A bad
import, a name that only exists in the docs, an i18n key with no string, or a component
that throws when it renders all look identical from outside — "nothing appeared". None
of that requires Electron to catch: the SDK is stubbed here, register() is called with a
recording context, and every component is invoked and walked.

If Node isn't installed the class skips rather than fails, so the Python suite still
runs on a bare box.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_JS = PLUGIN_DIR / "desktop" / "plugin.js"

SDK_PACKAGE = {"name": "@hermes/plugin-sdk", "version": "0.0.0", "type": "module",
               "main": "index.mjs", "exports": {".": "./index.mjs"}}
REACT_PACKAGE = {"name": "react", "version": "0.0.0", "type": "module", "main": "index.mjs",
                 "exports": {".": "./index.mjs", "./jsx-runtime": "./jsx-runtime.mjs"}}

# The stub deliberately mirrors the *documented* contributions and hooks, so a plugin
# written against docs the app doesn't implement fails here first.
SDK_INDEX = textwrap.dedent("""
    const state = (globalThis.__stub ||= {
      bundles: {}, requestedKeys: [], navigations: [], queryState: {},
      socketPath: null, cacheWrites: [], invalidations: [], haptics: []
    })

    export const PANES_AREA = 'panes'
    export const STATUSBAR_AREAS = { left: 'statusBar.left', center: 'statusBar.center', right: 'statusBar.right' }
    export const PALETTE_AREA = 'palette'
    export const KEYBINDS_AREA = 'keybinds'
    export const ROUTES_AREA = 'routes'
    export const SIDEBAR_NAV_AREA = 'sidebarNav'

    export const cn = (...parts) => parts.filter(Boolean).join(' ')
    export const haptic = (kind) => { state.haptics.push(kind) }
    export const host = {
      navigate: (path) => { state.navigations.push(path) },
      notify: (message) => { state.navigations.push(['notify', message]) }
    }
    export const queryClient = {
      setQueryData: (key, data) => { state.cacheWrites.push([key, data]) },
      invalidateQueries: (opts) => { state.invalidations.push(opts) }
    }

    const passthrough = (name) => {
      const component = (props) => ({ $$element: true, type: name, props: props || {} })
      component.displayName = name
      return component
    }
    export const Badge = passthrough('Badge')
    export const ErrorState = passthrough('ErrorState')
    export const GlyphSpinner = passthrough('GlyphSpinner')
    export const ScrollArea = passthrough('ScrollArea')
    export const Separator = passthrough('Separator')
    export const StatusDot = passthrough('StatusDot')
    export const Tip = passthrough('Tip')

    export function useQuery(options) {
      state.lastQueryKey = options && options.queryKey
      return Object.assign({
        refetch: () => { state.refetches = (state.refetches || 0) + 1 },
        isFetching: false,
        error: null,
        isPending: false,
        isError: false,
        data: null
      }, state.queryState)
    }

    export function translateNow(key, ...args) { return key }
    export function useValue(value) { return typeof value === 'function' ? value() : value }

    export function usePluginI18n(pluginId) {
      return (key, ...args) => {
        state.requestedKeys.push(key)
        const bundle = state.bundles.en || state.bundles[pluginId] || {}
        const entry = bundle[key]
        if (typeof entry === 'function') return entry(...args)
        if (typeof entry === 'string') return entry
        return key
      }
    }
""").lstrip()

REACT_JSX = textwrap.dedent("""
    export const jsx = (type, props) => ({ $$element: true, type, props: props || {} })
    export const jsxs = jsx
    export const Fragment = 'Fragment'
""").lstrip()

# register() is called with a recording ctx; every component is then rendered with three
# query states (loaded / loading / error) so all branches execute.
HARNESS = textwrap.dedent("""
    import plugin from './plugin.js'

    const state = globalThis.__stub
    const registered = []

    const ctx = {
      rest: async (path) => ({ path }),
      socket: (path, handler) => { state.socketPath = path; state.socketHandler = typeof handler },
      i18n: {
        register: (bundles) => { state.bundles = bundles },
        t: (key, ...args) => {
          state.requestedKeys.push(key)
          const entry = (state.bundles.en || {})[key]
          if (typeof entry === 'function') return entry(...args)
          if (typeof entry === 'string') return entry
          return key
        }
      },
      register: (contribution) => registered.push(contribution)
    }

    const problems = []
    const walk = (node, depth = 0) => {
      if (node == null || depth > 20) return
      if (Array.isArray(node)) { node.forEach((child) => walk(child, depth + 1)); return }
      if (typeof node !== 'object' || !node.$$element) return
      const { type, props } = node
      if (typeof type === 'function') {
        let produced
        try {
          produced = type(props || {})
        } catch (error) {
          problems.push(`component threw: ${error && error.message}`)
          return
        }
        walk(produced, depth + 1)
      }
      if (props && 'children' in props) walk(props.children, depth + 1)
    }

    plugin.register(ctx)

    const LOADED = {
      network: {
        chain: 'base', chain_id: 8453, head_block: 53522884, gas_price_gwei: 0.006,
        base_fee_gwei: 0.005, priority_fee_gwei: 0.001, eth_price_usd: 2420.92,
        block_time_utc: '2026-11-04T12:00:00Z', txs_in_head_block: 42,
        cost_usd: { eth_transfer_21000: 0.000305, erc20_transfer_65000: 0.000944, uniswap_swap_150000: 0.002179 },
        rpc: { endpoint: 'base-rpc.publicnode.com', latency_ms: 90 }, read_only: true
      },
      wallet: {
        address: '0x23129c0472172D75bEd1e6dd061301796760Ecd9', total_usd: 8.61,
        holdings: [
          { symbol: 'ETH', balance: 0.001, value_usd: 2.42, address: null },
          { symbol: 'USDC', balance: 6.19, value_usd: 6.19, address: '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' }
        ]
      },
      ledger: [{ tool: 'base_network', head_block: 53522884, ts: '2026-11-04T12:00:00Z' }],
      as_of: '2026-11-04T12:00:01Z'
    }

    const rendered = []
    for (const queryState of [
      { data: LOADED, isPending: false, isError: false },
      { data: null, isPending: true, isError: false },
      { data: null, isPending: false, isError: true, error: new Error('backend down') }
    ]) {
      state.queryState = queryState
      for (const contribution of registered) {
        if (typeof contribution.render !== 'function') continue
        try {
          walk(contribution.render())
          rendered.push(`${contribution.area}:${contribution.id}`)
        } catch (error) {
          problems.push(`render() threw for ${contribution.id}: ${error && error.message}`)
        }
      }
    }

    // Exercise the imperative paths too (palette command, keybind) — those run inside
    // handlers that a render walk never reaches.
    for (const contribution of registered) {
      const data = contribution.data
      if (data && typeof data.run === 'function') {
        try { data.run() } catch (error) { problems.push(`run() threw for ${contribution.id}: ${error.message}`) }
      }
    }

    const bundleKeys = Object.keys(state.bundles.en || {})
    const missingKeys = [...new Set(state.requestedKeys)].filter((key) => !bundleKeys.includes(key))

    const result = {
      id: plugin.id,
      defaultEnabled: plugin.defaultEnabled,
      areas: registered.map((c) => c.area),
      ids: registered.map((c) => c.id),
      rendered,
      navigations: state.navigations,
      socketPath: state.socketPath,
      runnableContributions: registered.filter((c) => c.data && typeof c.data.run === 'function').length,
      missingKeys,
      bundleKeys,
      problems
    }
    process.stdout.write(JSON.stringify(result))
""").lstrip()


class TestDesktopPlugin(unittest.TestCase):
    """Loads and executes the desktop half in Node with a stubbed SDK."""

    node = shutil.which("node")

    @classmethod
    def setUpClass(cls):
        if not cls.node:
            raise unittest.SkipTest("node not installed")
        if not PLUGIN_JS.is_file():
            raise unittest.SkipTest("desktop/plugin.js not written")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        sdk = root / "node_modules" / "@hermes" / "plugin-sdk"
        sdk.mkdir(parents=True)
        (sdk / "package.json").write_text(json.dumps(SDK_PACKAGE))
        (sdk / "index.mjs").write_text(SDK_INDEX)
        react = root / "node_modules" / "react"
        react.mkdir(parents=True)
        (react / "package.json").write_text(json.dumps(REACT_PACKAGE))
        (react / "index.mjs").write_text("export default {}\n")
        (react / "jsx-runtime.mjs").write_text(REACT_JSX)
        shutil.copy(PLUGIN_JS, root / "plugin.js")
        (root / "harness.mjs").write_text(HARNESS)

        proc = subprocess.run([cls.node, "harness.mjs"], cwd=root, capture_output=True, text=True, timeout=60)
        cls.stderr = proc.stderr
        cls.returncode = proc.returncode
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stdout}\n{proc.stderr}")
        cls.result = json.loads(proc.stdout)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def test_no_problems_and_clean_exit(self):
        self.assertEqual(self.returncode, 0, self.stderr)
        self.assertEqual(self.result["problems"], [])

    def test_identity_and_opt_in_default(self):
        self.assertEqual(self.result["id"], "basekit")
        self.assertIs(self.result["defaultEnabled"], False)

    def test_registers_every_documented_area(self):
        self.assertEqual(
            sorted(self.result["areas"]),
            sorted(["panes", "statusBar.right", "routes", "sidebarNav", "palette", "keybinds"]),
        )

    def test_every_contribution_renders_without_throwing(self):
        rendered = set(self.result["rendered"])
        for expected in ("panes:pane", "statusBar.right:chip", "routes:page"):
            self.assertIn(expected, rendered)

    def test_render_walk_covered_all_three_query_states(self):
        # 3 contributions with render x 3 query states
        self.assertEqual(len(self.result["rendered"]), 9)

    def test_i18n_bundle_covers_every_key_used(self):
        self.assertEqual(self.result["missingKeys"], [], "a key is used but never registered")
        self.assertGreater(len(self.result["bundleKeys"]), 20)

    def test_palette_and_keybind_are_runnable_and_navigate(self):
        self.assertEqual(self.result["runnableContributions"], 2)
        self.assertIn("/basekit", self.result["navigations"])

    def test_socket_is_wired_to_the_backend_route(self):
        self.assertEqual(self.result["socketPath"], "/events")

    def test_static_import_discipline(self):
        """Every identifier passed to jsx() must be imported or declared in the file —
        the documented trap for uncompiled ESM UI halves."""
        source = PLUGIN_JS.read_text()
        imported = set()
        for match in __import__("re").finditer(r"import\s*\{([^}]*)\}\s*from", source):
            for name in match.group(1).split(","):
                name = name.strip()
                if name:
                    imported.add(name)
        declared = set(__import__("re").findall(r"(?:function|const|let)\s+([A-Za-z_$][\w$]*)", source))
        used = set(__import__("re").findall(r"\bjsxs?\(\s*([A-Za-z_$][\w$]*)", source))
        unknown = {name for name in used if name not in imported and name not in declared}
        self.assertEqual(unknown, set(), f"jsx() used with unimported identifiers: {sorted(unknown)}")

    def test_no_bare_npm_imports(self):
        """Only the SDK and react are resolvable in the desktop runtime."""
        source = PLUGIN_JS.read_text()
        specifiers = __import__("re").findall(r"from\s+'([^']+)'", source)
        allowed = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
        for specifier in specifiers:
            self.assertIn(specifier, allowed, f"{specifier} would not resolve at runtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
