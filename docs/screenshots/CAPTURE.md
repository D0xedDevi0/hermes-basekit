# Screenshot provenance

Every image in `docs/screenshots/` and the `docs/banner.png` card is rendered from **captured
command output** by `scripts/render_screenshots.py`. Nothing is mocked, no value is hand-typed
into an image, and the renderer reads the same text files the commands wrote.

Captured **2026-11-05 (UTC)** on the machine that built the plugin, against live Base mainnet
(chain 8453).

| image | produced by | what it shows |
| --- | --- | --- |
| `banner.png` | drawn by the renderer | the 2:1 catalog card |
| `screenshots/status.png` | `hermes basekit status-full` | block 53,578,649 · gas 0.006067 gwei · RPC `base-rpc.publicnode.com` 292 ms |
| `screenshots/wallet.png` | `hermes basekit wallet 0x4200000000000000000000000000000000000006` | 8 positions, $\u2009637,308,650.21 total |
| `screenshots/token.png` | `hermes basekit token 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | USDC: proxy detected, owner, supply, source-verification state |
| `screenshots/gate.png` | `_pre_llm_call` called directly | six messages through the gate, from `docs/evidence/hook-gate.json` |

## The addresses are not personal

`wallet.png` uses `0x4200000000000000000000000000000000000006` (the canonical WETH contract on
Base) and `token.png` uses USDC. Both are public contracts. No personal wallet, key or API
credential appears in any image; `token.png` shows `source_verification.status: not_configured`
precisely because no Basescan key was set on the capture machine.

## They age, and they say how they age

The prices, gas and block numbers in these pictures were live when captured and are stale now —
that is what "every answer carries its block" means. The block number printed inside each image
is the timestamp. Nothing here is asserted to be current.

## Regenerating

```bash
# 1. capture (writes the text the renderer consumes)
mkdir -p /opt/data/cache/scratch/shots && cd /opt/data/cache/scratch/shots
hermes basekit status-full   > status.txt
hermes basekit wallet 0x4200000000000000000000000000000000000006 > wallet.txt
hermes basekit token  0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913  > token.txt

# 2. render (reads /opt/data/cache/scratch/shots by default, override with CAPTURES)
python3 scripts/render_screenshots.py
```

The catalog entry pins each raw image URL to the entry's commit, so a picture is as immutable as
the code it documents: to change one, re-capture, re-render, and re-pin.
