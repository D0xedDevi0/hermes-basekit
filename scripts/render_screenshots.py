#!/usr/bin/env python3
"""Render the catalog images from captured text. No value is hand-typed.

Every terminal image is drawn from a file captured by running the real command
(see docs/screenshots/CAPTURE.md for the exact invocations and dates), so the
pictures cannot drift from the product: re-capture, re-run this, re-pin.

  python3 scripts/render_screenshots.py   ->  docs/banner.png + docs/screenshots/*.png
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs"
SHOTS = OUT / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
CAPTURES = Path(os.environ.get("BASEKIT_CAPTURES", "/opt/data/cache/scratch/shots"))

NAVY = (0, 18, 64)
CYAN = (0, 212, 255)
INK = (10, 16, 32)
TEXT = (214, 227, 255)
DIM = (122, 145, 190)
WHITE = (255, 255, 255)


def _font(candidates: list[str], size: int):
    roots = ["/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts")]
    found: list[Path] = []
    for root in roots:
        p = Path(root)
        if not p.exists():
            continue
        for name in candidates:
            found += sorted(p.rglob(name))
    if found:
        return ImageFont.truetype(str(found[0]), size)
    any_ttf = sorted(Path("/usr/share/fonts").rglob("*.ttf"))
    return ImageFont.truetype(str(any_ttf[0]), size) if any_ttf else ImageFont.load_default()


MONO = lambda s: _font(["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "JetBrainsMono-Regular.ttf"], s)
MONO_B = lambda s: _font(["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"], s)
SANS_B = lambda s: _font(["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Inter-Bold.ttf"], s)
SANS = lambda s: _font(["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Inter-Regular.ttf"], s)


def pixel_grid(d: ImageDraw.ImageDraw, w: int, h: int, step: int = 24, alpha: int = 26) -> None:
    """Faint cyan blueprint grid — the D0xed visual signature."""
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(0, 212, 255, alpha), width=1)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(0, 212, 255, alpha), width=1)


def terminal(cmd: str, body: str, caption: str, name: str, min_w: int = 1040) -> Path:
    """A dark terminal card: shell line, verbatim output, provenance footer."""
    MAXC = 132

    def _wrap(line: str) -> list[str]:
        if len(line) <= MAXC:
            return [line]
        out, cur = [], ""
        for chunk in line.split(" "):
            if len(cur) + len(chunk) + 1 <= MAXC:
                cur = f"{cur} {chunk}".strip()
            else:
                out.append(cur)
                cur = f"  {chunk}"
        if cur:
            out.append(cur)
        return out

    body_lines = [w for line in body.rstrip("\n").split("\n") for w in _wrap(line)]
    f = MONO(16)
    fb = MONO_B(16)
    fs = MONO(13)
    pad, bar = 26, 46
    line_h = 24
    widest = max([len(cmd) + 2] + [len(l) for l in body_lines])
    width = max(min_w, int(pad * 2 + widest * 9.7))
    height = bar + pad + line_h * (len(body_lines) + 1) + 30 + 34

    img = Image.new("RGB", (width, height), INK)
    d = ImageDraw.Draw(img, "RGBA")
    pixel_grid(d, width, height)
    d.rectangle([0, 0, width, bar], fill=(0, 18, 64, 235))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([18 + i * 22, bar // 2 - 6, 30 + i * 22, bar // 2 + 6], fill=c)
    d.text((width - pad - 150, bar // 2 - 8), "read-only", font=fs, fill=(0, 212, 255))

    y = bar + pad
    d.text((pad, y), "$", font=fb, fill=CYAN)
    d.text((pad + 18, y), cmd, font=f, fill=WHITE)
    y += line_h
    for line in body_lines:
        colour = DIM if line.strip().startswith(("#", "rpc ")) else TEXT
        d.text((pad, y), line, font=f, fill=colour)
        y += line_h
    y += 14
    d.line([(pad, y), (width - pad, y)], fill=(0, 212, 255, 70), width=1)
    d.text((pad, y + 12), caption, font=fs, fill=(0, 212, 255))

    path = SHOTS / name
    img.save(path, optimize=True)
    return path


def banner(name: str = "banner.png") -> Path:
    """1200x600 2:1 card image for the catalog."""
    w, h = 1200, 600
    img = Image.new("RGB", (w, h), NAVY)
    d = ImageDraw.Draw(img, "RGBA")
    pixel_grid(d, w, h, 24, 30)
    # square-bullet motif (the D0xed marker), drawn not typed
    for i in range(3):
        d.rectangle([70 + i * 26, 92, 82 + i * 26, 104], fill=CYAN)
    d.text((156, 74), "basekit", font=SANS_B(76), fill=WHITE)
    d.text((72, 176), "Live Base intelligence for Hermes Agent", font=SANS(30), fill=(150, 195, 255))
    d.line([(72, 232), (300, 232)], fill=CYAN, width=3)

    feats = [
        "6 read-only tools  \u00b7  1 pre_llm_call hook",
        "/basekit command  \u00b7  hermes basekit CLI",
        "desktop pane  \u00b7  status chip  \u00b7  dashboard API",
        "no PyPI dependencies  \u00b7  stdlib only",
    ]
    y = 268
    for line in feats:
        d.rectangle([74, y + 7, 84, y + 17], fill=CYAN)
        d.text((104, y), line, font=SANS(23), fill=TEXT)
        y += 42

    d.rectangle([0, h - 92, w, h], fill=(0, 10, 40, 240))
    d.rectangle([0, h - 92, w, h - 90], fill=CYAN)
    d.text((72, h - 62), "No keys.  No signing.  No txs.", font=SANS_B(30), fill=WHITE)
    d.text((760, h - 56), "every answer names its block", font=MONO(17), fill=CYAN)

    path = OUT / name
    img.save(path, optimize=True)
    return path


def gate_panel(name: str = "gate.png") -> Path:
    """The pre_llm_call decision table, from the captured gate JSON."""
    gate = json.load(open(Path("/opt/data/cache/scratch/hook-gate.json")))
    order = ["a Base turn", "unrelated work", "one ambiguous word",
             "two ambiguous words", "a bare Base address", "a tx hash"]
    rows = [(gate[k]["msg"], bool(gate[k]["injected"])) for k in order]

    w, pad, rh = 1180, 34, 46
    h = 150 + rh * (len(rows) + 1) + 40
    img = Image.new("RGB", (w, h), INK)
    d = ImageDraw.Draw(img, "RGBA")
    pixel_grid(d, w, h, 24, 22)
    d.text((pad, 30), "the hook decides before the model runs", font=SANS_B(30), fill=WHITE)
    d.text((pad, 74), "pre_llm_call called directly \u2014 no model, no network, no cost", font=MONO(15), fill=CYAN)
    d.line([(pad, 108), (w - pad, 108)], fill=(0, 212, 255, 90), width=2)

    y = 124
    d.text((pad, y), "user message", font=MONO_B(15), fill=DIM)
    d.text((w - 190, y), "result", font=MONO_B(15), fill=DIM)
    y += rh - 12
    for msg, injected in rows:
        d.text((pad, y + 10), f'"{msg}"', font=MONO(17), fill=TEXT)
        if injected:
            d.rectangle([w - 196, y + 4, w - pad, y + 38], fill=(0, 90, 120, 200))
            d.text((w - 182, y + 10), "INJECT", font=MONO_B(17), fill=CYAN)
        else:
            d.rectangle([w - 196, y + 4, w - pad, y + 38], fill=(40, 46, 66, 190))
            d.text((w - 166, y + 10), "NONE", font=MONO_B(17), fill=DIM)
        y += rh
    d.text((pad, h - 30), "captured from the installed plugin \u00b7 deterministic, re-runnable", font=MONO(13), fill=CYAN)

    path = SHOTS / name
    img.save(path, optimize=True)
    return path


def main() -> None:
    made: list[Path] = []
    made.append(banner())
    made.append(terminal(
        "hermes basekit status-full",
        (CAPTURES / "status.txt").read_text(),
        "real output, captured 2026-11-05 on Hermes \u00b7 copied verbatim",
        "status.png"))
    made.append(terminal(
        "hermes basekit wallet 0x4200000000000000000000000000000000000006",
        (CAPTURES / "wallet.txt").read_text(),
        "real output \u00b7 WETH contract address, a public wallet \u00b7 USD from live prices",
        "wallet.png"))
    made.append(terminal(
        "hermes basekit token 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        (CAPTURES / "token.txt").read_text(),
        "real output \u00b7 note the proxy detection and the honest heuristics note",
        "token.png"))
    made.append(gate_panel())
    for p in made:
        print(f"{p.relative_to(REPO)}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
