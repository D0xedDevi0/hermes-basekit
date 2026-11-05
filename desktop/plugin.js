/**
 * basekit — the desktop half.
 *
 * A status-bar chip, a draggable pane, a full page, a ⌘K command and a keybind, all
 * reading /api/plugins/basekit/ (the Python half in dashboard/, which uses the same
 * engine as the agent tools — so the pane and the model can't disagree).
 *
 * Plain ESM, loaded uncompiled: UI is jsx() calls, not JSX syntax, and the only
 * resolvable imports are @hermes/plugin-sdk, react and react/jsx-runtime.
 */

import {
  Badge,
  cn,
  ErrorState,
  GlyphSpinner,
  haptic,
  host,
  KEYBINDS_AREA,
  PALETTE_AREA,
  PANES_AREA,
  queryClient,
  ROUTES_AREA,
  ScrollArea,
  Separator,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  usePluginI18n,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'basekit'
const PAGE_PATH = '/basekit'
const QUERY_KEY = ['basekit', 'status']
const REFRESH_MS = 15000

// ctx isn't passed to components, so register() parks it here for them (standard practice).
let appCtx = null

// ── formatting ────────────────────────────────────────────────────────────────
const fmtInt = v => (typeof v === 'number' ? v.toLocaleString('en-US') : '—')
const fmtUsd = v => {
  if (typeof v !== 'number') return '—'
  if (v === 0) return '$0'
  if (v < 0.01) return `$${v.toFixed(4)}`
  return `$${v.toFixed(v >= 100 ? 0 : 2)}`
}
const fmtGas = v => (typeof v === 'number' ? `${v} gwei` : '—')
const gasTone = v => (typeof v !== 'number' ? 'muted' : v < 0.05 ? 'good' : v < 1 ? 'warn' : 'bad')

function useStatus() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => appCtx.rest('/status'),
    refetchInterval: REFRESH_MS,
    staleTime: 5000,
    retry: 1
  })
}

// ── small pieces ──────────────────────────────────────────────────────────────
function Metric({ label, value, tone }) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5',
    children: [
      jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: label }),
      jsxs('div', {
        className: 'flex items-center gap-1.5 text-sm font-medium text-(--ui-text-secondary)',
        children: [tone ? jsx(StatusDot, { tone }) : null, jsx('span', { children: value })]
      })
    ]
  })
}

function Row({ label, value }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-2 text-xs',
    children: [
      jsx('span', { className: 'text-(--ui-text-quaternary)', children: label }),
      jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: value })
    ]
  })
}

function NetworkBlock({ net, t }) {
  if (!net) return null
  const costs = net.cost_usd || {}
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'grid grid-cols-2 gap-2',
        children: [
          jsx(Metric, { label: t('block'), value: fmtInt(net.head_block) }),
          jsx(Metric, { label: t('gas'), value: fmtGas(net.gas_price_gwei), tone: gasTone(net.gas_price_gwei) }),
          jsx(Metric, { label: t('eth'), value: fmtUsd(net.eth_price_usd) }),
          jsx(Metric, { label: t('baseFee'), value: fmtGas(net.base_fee_gwei) })
        ]
      }),
      jsx(Separator, {}),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx(Row, { label: t('costTransfer'), value: fmtUsd(costs.eth_transfer_21000) }),
          jsx(Row, { label: t('costErc20'), value: fmtUsd(costs.erc20_transfer_65000) }),
          jsx(Row, { label: t('costSwap'), value: fmtUsd(costs.uniswap_swap_150000) })
        ]
      }),
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
        children: t('asOf', net.block_time_utc || '—')
      })
    ]
  })
}

function WalletBlock({ wallet, t }) {
  if (!wallet) return null
  if (wallet.error) {
    return jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: t('walletError', wallet.error) })
  }
  const holdings = wallet.holdings || []
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx(Row, { label: t('wallet'), value: `${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` }),
      jsx(Row, { label: t('total'), value: fmtUsd(wallet.total_usd) }),
      jsx('div', {
        className: 'flex flex-col gap-1',
        children: holdings.map(h =>
          jsx(Row, {
            key: h.address || h.symbol,
            label: h.symbol || '—',
            value: `${h.balance}${h.value_usd != null ? ` · ${fmtUsd(h.value_usd)}` : ''}`
          })
        )
      })
    ]
  })
}

function LedgerBlock({ entries, t, limit = 5 }) {
  const rows = (entries || []).slice(0, limit)
  if (!rows.length) {
    return jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: t('noLedger') })
  }
  return jsx('div', {
    className: 'flex flex-col gap-1',
    children: rows.map((entry, index) =>
      jsxs(
        'div',
        {
          className: 'flex items-baseline justify-between gap-2 text-[0.6875rem]',
          children: [
            jsx('span', { className: 'text-(--ui-text-secondary)', children: entry.tool }),
            jsx('span', {
              className: 'truncate text-(--ui-text-quaternary)',
              children: entry.error
                ? t('errorTag')
                : (entry.head_block || entry.block ? `#${fmtInt(entry.head_block || entry.block)}` : '—')
            })
          ]
        },
        index
      )
    )
  })
}

function DataState({ query, t, children }) {
  if (query.isPending) {
    return jsx('div', {
      className: 'flex flex-1 items-center justify-center gap-2 text-xs text-(--ui-text-quaternary)',
      children: jsxs('div', { className: 'flex items-center gap-2', children: [jsx(GlyphSpinner, {}), t('loading')] })
    })
  }
  if (query.isError && !query.data) {
    return jsx(ErrorState, {
      title: t('backendDown'),
      description: String(query.error && query.error.message ? query.error.message : query.error || '')
    })
  }
  return children
}

// ── surfaces ──────────────────────────────────────────────────────────────────
function BasePane() {
  const t = usePluginI18n(ID)
  const query = useStatus()
  const snapshot = query.data
  const net = snapshot && snapshot.network

  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsxs('div', {
      className: 'flex flex-col gap-3 p-3 text-sm',
      children: [
        jsxs('div', {
          className: 'flex items-center justify-between gap-2',
          children: [
            jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                jsx(StatusDot, { tone: net ? gasTone(net.gas_price_gwei) : 'muted' }),
                jsx('span', { className: 'font-medium', children: t('paneTitle') })
              ]
            }),
            jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                net ? jsx(Badge, { children: `#${fmtInt(net.head_block)}` }) : null,
                jsx('button', {
                  type: 'button',
                  className: cn(
                    'text-[0.6875rem] text-(--ui-text-tertiary) transition-colors',
                    'hover:bg-(--chrome-action-hover) hover:text-foreground'
                  ),
                  onClick: () => {
                    haptic('tap')
                    query.refetch()
                  },
                  children: query.isFetching ? t('refreshing') : t('refresh')
                })
              ]
            })
          ]
        }),
        jsx(DataState, {
          query,
          t,
          children: jsxs('div', {
            className: 'flex flex-col gap-3',
            children: [
              jsx(NetworkBlock, { net, t }),
              snapshot && snapshot.wallet
                ? jsxs('div', {
                    className: 'flex flex-col gap-2',
                    children: [jsx(Separator, {}), jsx(WalletBlock, { wallet: snapshot.wallet, t })]
                  })
                : null,
              jsxs('div', {
                className: 'flex flex-col gap-2',
                children: [
                  jsx(Separator, {}),
                  jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: t('ledger') }),
                  jsx(LedgerBlock, { entries: snapshot && snapshot.ledger, t })
                ]
              }),
              jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('readOnly') })
            ]
          })
        })
      ]
    })
  })
}

function BasePage() {
  const t = usePluginI18n(ID)
  const query = useStatus()
  const snapshot = query.data
  const net = snapshot && snapshot.network

  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsxs('div', {
      className: 'mx-auto flex w-full max-w-3xl flex-col gap-4 p-4',
      children: [
        jsxs('div', {
          className: 'flex flex-col gap-1',
          children: [
            jsx('div', { className: 'text-lg font-medium', children: t('pageTitle') }),
            jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t('pageSubtitle') })
          ]
        }),
        jsx(DataState, {
          query,
          t,
          children: jsxs('div', {
            className: 'flex flex-col gap-4',
            children: [
              jsxs('div', {
                className: 'flex flex-col gap-3 border-(--ui-stroke-secondary) rounded-md border p-3',
                children: [jsx(NetworkBlock, { net, t })]
              }),
              snapshot && snapshot.wallet
                ? jsxs('div', {
                    className: 'flex flex-col gap-3 border-(--ui-stroke-secondary) rounded-md border p-3',
                    children: [jsx(WalletBlock, { wallet: snapshot.wallet, t })]
                  })
                : null,
              jsxs('div', {
                className: 'flex flex-col gap-3 border-(--ui-stroke-secondary) rounded-md border p-3',
                children: [
                  jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: t('ledger') }),
                  jsx(LedgerBlock, { entries: snapshot && snapshot.ledger, t, limit: 8 })
                ]
              }),
              jsxs('div', {
                className: 'flex flex-col gap-2',
                children: [
                  jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: t('tools') }),
                  jsx('div', {
                    className: 'flex flex-wrap gap-1.5',
                    children: ['base_network', 'base_portfolio', 'base_tx', 'base_token', 'base_activity', 'base_allowances'].map(name =>
                      jsx(Badge, { key: name, children: name })
                    )
                  }),
                  jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('toolsHint') })
                ]
              })
            ]
          })
        })
      ]
    })
  })
}

function BaseChip() {
  const t = usePluginI18n(ID)
  const query = useStatus()
  const net = query.data && query.data.network
  const gas = net ? net.gas_price_gwei : null

  return jsx(Tip, {
    label: net
      ? t('chipTip', fmtInt(net.head_block), String(gas), fmtUsd(net.eth_price_usd))
      : t('chipTipWaiting'),
    children: jsx('button', {
      type: 'button',
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      onClick: () => {
        haptic('tap')
        host.navigate(PAGE_PATH)
      },
      children: jsxs('span', {
        className: 'inline-flex items-center gap-1',
        children: [
          jsx(StatusDot, { tone: gasTone(gas) }),
          jsx('span', { children: `BASE ${fmtGas(gas)}` })
        ]
      })
    })
  })
}

// ── registration ──────────────────────────────────────────────────────────────
export default {
  id: ID,
  name: 'basekit',
  // Opt-in, matching the unified-package default: nothing on screen until the user
  // enables it in Settings → Plugins.
  defaultEnabled: false,
  register(ctx) {
    appCtx = ctx

    ctx.i18n.register({
      en: {
        paneTitle: 'basekit',
        pageTitle: 'basekit — live Base',
        pageSubtitle: 'Read-only Base (chain 8453) state, wallet and ledger. No key, no signing.',
        block: 'block',
        gas: 'gas',
        eth: 'ETH',
        baseFee: 'base fee',
        costTransfer: 'ETH transfer',
        costErc20: 'ERC-20 transfer',
        costSwap: 'swap (150k gas)',
        asOf: time => `block time ${time}`,
        wallet: 'wallet',
        total: 'total',
        walletError: message => `wallet read failed: ${message}`,
        ledger: 'recent agent reads',
        noLedger: 'Nothing looked up yet.',
        errorTag: 'error',
        loading: 'reading Base…',
        refreshing: 'reading…',
        refresh: 'refresh',
        readOnly: 'Read-only: basekit cannot sign or send transactions.',
        tools: 'tools',
        toolsHint: 'These six tools are what the agent calls; the counts above come from the same engine.',
        backendDown: 'basekit backend unreachable',
        chipTip: (block, gas, eth) => `Base block ${block} · gas ${gas} gwei · ETH ${eth}`,
        chipTipWaiting: 'basekit — waiting for the backend',
        palette: 'Base: live network state',
        keybind: 'Base: open basekit page',
        navLabel: 'Base'
      }
    })

    // Registration-time labels can't use a hook; `ctx.i18n.t` is the module-level
    // translator for exactly this case. Falls back to English if it ever isn't there.
    const label = (key, fallback) => {
      try {
        const value = ctx.i18n && typeof ctx.i18n.t === 'function' ? ctx.i18n.t(key) : null
        return value || fallback
      } catch {
        return fallback
      }
    }

    // Live push when the backend socket is available; the query keeps polling either way
    // (ctx.socket is a documented no-op on OAuth remotes, so it can never be the only path).
    if (typeof ctx.socket === 'function') {
      try {
        ctx.socket('/events', message => {
          if (message && message.snapshot) queryClient.setQueryData(QUERY_KEY, message.snapshot)
          else queryClient.invalidateQueries({ queryKey: QUERY_KEY })
        })
      } catch {
        // polling still covers it
      }
    }

    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'basekit',
      data: { placement: 'right', width: '260px' },
      render: () => jsx(BasePane, {})
    })

    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 120,
      render: () => jsx(BaseChip, {})
    })

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: PAGE_PATH },
      render: () => jsx(BasePage, {})
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: PAGE_PATH, label: label('navLabel', 'Base'), codicon: 'globe' }
    })

    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'basekit.open',
        label: label('palette', 'Base: live network state'),
        keywords: ['base', 'gas', 'gwei', 'eth', 'onchain', 'network'],
        run: () => {
          haptic('tap')
          host.navigate(PAGE_PATH)
        }
      }
    })

    ctx.register({
      id: 'keybind',
      area: KEYBINDS_AREA,
      data: {
        id: 'basekit.openPage',
        label: label('keybind', 'Base: open basekit page'),
        defaults: ['mod+alt+b'],
        run: () => host.navigate(PAGE_PATH)
      }
    })
  }
}
