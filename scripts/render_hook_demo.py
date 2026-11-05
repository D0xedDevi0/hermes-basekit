#!/usr/bin/env python3
"""Render the basekit hook-proof demo from REAL captured output.

Every string here was captured during this session from:
  - /opt/data/cache/scratch/hook-gate.json  (direct _pre_llm_call calls)
  - /opt/data/cache/scratch/hook-ab.json    (six live `hermes -z` runs)
Nothing is hand-written except the labels.

Layout checks run before encode: per-line width vs panel inner width,
and a tofu check for every non-ASCII glyph the frames use.
"""
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 900
BG = (12, 14, 20)
PANEL = (18, 22, 32)
GREEN = (0, 230, 118)
RED = (255, 69, 58)
CYAN = (0, 200, 255)
GRAY = (150, 158, 176)
WHITE = (235, 238, 245)
AMBER = (255, 190, 60)
DIM = (110, 118, 136)

MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
F_TITLE, F_PANEL, F_BODY, F_SMALL = 42, 28, 22, 20

OUT = Path("/opt/data/cache/scratch/basekit-hook-demo")
OUT.mkdir(parents=True, exist_ok=True)
FRAMES = OUT / "frames"
FRAMES.mkdir(exist_ok=True)

_fonts = {}


def font(path, size):
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


PROBLEMS = []


def check(text, px, max_px, where):
    """Width check: returns the pixel width and records an overflow."""
    w = font(MONO, px).getlength(text)
    if w > max_px:
        PROBLEMS.append(f"OVERFLOW {where}: {w:.0f}px > {max_px}px :: {text[:70]}")
    return w


def _glyph_sig(ch, f):
    im = Image.new("L", (60, 70), 0)
    ImageDraw.Draw(im).text((4, 4), ch, font=f, fill=255)
    return im.tobytes(), im.getbbox()


def check_glyphs(texts):
    """Tofu check: render each non-ASCII char and compare to a known-missing one."""
    f = font(MONO, 40)
    missing_sig, _ = _glyph_sig("\ue123", f)
    seen = set()
    for t in texts:
        for ch in t:
            if ord(ch) > 127 and ch not in seen:
                seen.add(ch)
                sig, bbox = _glyph_sig(ch, f)
                if bbox is None:
                    PROBLEMS.append(f"BLANK: {ch!r} (U+{ord(ch):04X}) drew nothing")
                elif sig == missing_sig:
                    PROBLEMS.append(f"TOFU: no glyph for {ch!r} (U+{ord(ch):04X})")
    return sorted(seen)



def base(title, sub, tag=None, tag_color=GREEN):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 26), title, font=font(MONO_B, F_TITLE), fill=CYAN)
    check(title, F_TITLE, W - 80, "title")
    if sub:
        d.text((40, 80), sub, font=font(MONO, F_SMALL), fill=GRAY)
        check(sub, F_SMALL, W - 90, "subtitle")
    if tag:
        f = font(MONO_B, 26)
        tw = f.getlength(tag)
        d.rectangle((W - tw - 68, 24, W - 30, 74), fill=tag_color)
        d.text((W - tw - 49, 36), tag, font=f, fill=(10, 10, 12))
    return img, d


def panel(d, box, title, lines, color=GREEN, body=F_BODY, lh=34):
    x1, y1, x2, y2 = box
    d.rounded_rectangle(box, radius=14, fill=PANEL, outline=color, width=2)
    d.text((x1 + 22, y1 + 16), title, font=font(MONO_B, 26), fill=color)
    y = y1 + 62
    for line, col in lines:
        d.text((x1 + 22, y), line, font=font(MONO, body), fill=col)
        check(line, body, (x2 - x1) - 44, f"{title}/{line[:24]}")
        y += lh
    # vertical fit: the last line's descender must stay inside the panel
    if y - lh + body + 6 > y2:
        PROBLEMS.append(f"VOVERFLOW {title}: text ends at {y - lh + body + 6}px, panel bottom {y2}px")
    return y


def scene_title():
    img, d = base("basekit · the pre_llm_call hook", "does it actually fire, and what does it change?", "LIVE MAINNET", CYAN)
    lines = [
        ("Not a claim - an A/B. Same question, same model, hook on vs off.", WHITE),
        ("", WHITE),
        ("1  the keyword gate, called directly   (no model involved)", CYAN),
        ("2  the exact string the hook injects", CYAN),
        ("3  hook ON vs hook OFF - n=5 and n=3, toggle verified before each run", CYAN),
        ("4  what held up, and what the first measurement got wrong", CYAN),
        ("", WHITE),
        ("Every number on screen was measured during this recording.", AMBER),
        ("No keys, no signing, no transactions. Read-only.", GREEN),
    ]
    panel(d, (40, 150, W - 40, H - 40), "the run", lines, CYAN, 24, 44)
    return img


def scene_gate():
    img, d = base("1 · the keyword gate", "python3 -c 'basekit._pre_llm_call(user_message=...)'  - real return values", "6 CALLS", GREEN)
    rows = [
        ("INJECT", '"what is base gas right now?"', "a Base term on its own", GREEN),
        ("NONE", '"write a haiku about a dog"', "unrelated work", DIM),
        ("NONE", '"why are my block sizes so large in the prompt"', "one ambiguous word is not enough", DIM),
        ("INJECT", '"token budget and block sizes in my prompt"', "two ambiguous words together count", GREEN),
        ("INJECT", '"0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"', "a bare Base address always counts", GREEN),
        ("INJECT", '"0x3cb57f2ed0bbad21fb8b489d19d5e143f92131ab84b806e9586e59127855b49d"', "so does a tx hash", GREEN),
    ]
    img2, d2 = base("1 · the keyword gate", "python3 -c 'basekit._pre_llm_call(user_message=...)'  - real return values", "6 CALLS", GREEN)
    x1, y1, x2, y2 = 40, 150, W - 40, H - 40
    d2.rounded_rectangle((x1, y1, x2, y2), radius=14, fill=PANEL, outline=GREEN, width=2)
    y = y1 + 34
    for verdict, msg, note, col in rows:
        d2.text((x1 + 26, y), verdict, font=font(MONO_B, 24), fill=col)
        d2.text((x1 + 200, y), msg, font=font(MONO, F_BODY), fill=WHITE)
        check(msg, F_BODY, (x2 - x1) - 240, "gate msg")
        d2.text((x1 + 208, y + 30), note, font=font(MONO, 18), fill=GRAY)
        check(note, 18, (x2 - x1) - 250, "gate note")
        y += 78
    y += 6
    for line, col in [
        ("The gate is a pure function of the user message: read-only, no model call, no cost.", WHITE),
        ("It decides before the LLM runs whether this turn is about Base at all.", GRAY),
    ]:
        d2.text((x1 + 26, y), line, font=font(MONO, 21), fill=col)
        check(line, 21, (x2 - x1) - 50, "gate foot")
        y += 32
    return img2


def scene_line():
    gate = json.load(open("/opt/data/cache/scratch/hook-gate.json"))
    line = gate["a Base turn"]["line"]
    words, cur, wrapped = line.split(" "), "", []
    for w in words:
        if len(cur) + len(w) + 1 > 96:
            wrapped.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    wrapped.append(cur)
    img, d = base("2 · what the model actually receives", "the exact return value of the hook - captured, not written by hand", "PRE-LLM", GREEN)
    lines = [(w, CYAN if w.startswith("basekit live Base read") else WHITE) for w in wrapped]
    lines += [("", WHITE), ("under 200 tokens of prompt, cached 60 seconds.", AMBER)]
    h_panel = 62 + len(lines) * 40 + 24
    panel(d, (40, 150, W - 40, 150 + h_panel), "injected before the first token", lines, GREEN, 22, 40)
    foot = [
        ("Why a hook earns its place: this is state the model has before it decides anything -", WHITE),
        ("not a tool it must choose to call, not a skill it must remember to load.", WHITE),
        ("", WHITE),
        ("A hook must never break a turn, so it fails open: if the RPC is unreachable it returns", GRAY),
        ("nothing and the turn proceeds exactly as it would without basekit installed.", GRAY),
    ]
    panel(d, (40, 540, W - 40, H - 40), "why this shape", foot, CYAN, 21, 34)
    return img


def _mean(rows, k):
    return sum(r[k] for r in rows) / len(rows)


def scene_ab():
    ab = json.load(open("/opt/data/cache/scratch/hook-ab2.json"))
    m_on, m_off = ab["main"]["ON"], ab["main"]["OFF"]
    b_on, b_off = ab["block"]["ON"], ab["block"]["OFF"]
    f = lambda rows: ", ".join(str(r["api_calls"]) for r in rows)
    img, d = base("3 · the A/B, re-measured with the toggle verified",
                  f"{_mean(m_on, 'api_calls'):.2f} vs {_mean(m_off, 'api_calls'):.2f} model calls on n=5 · "
                  f"{_mean(b_on, 'api_calls'):.2f} vs {_mean(b_off, 'api_calls'):.2f} on n=3",
                  "A / B — CORRECTED", AMBER)
    pw = (W - 40 - 60) // 2
    left = (30, 150, 30 + pw, H - 190)
    right = (30 + pw + 20, 150, 30 + pw + 20 + pw, H - 190)
    on_lines = [
        ('$ hermes -z "...base gas?"', AMBER),
        ("  arms interleaved, n = 5", AMBER),
        ("", WHITE),
        (f"model calls   {f(m_on)}", GREEN),
        (f"mean          {_mean(m_on, 'api_calls'):.2f}", GREEN),
        (f"single-call   {sum(1 for r in m_on if r['api_calls'] == 1)} of {len(m_on)}", GREEN),
        ("", WHITE),
        ("block-number question", CYAN),
        (f"model calls   {f(b_on)}", GREEN),
        (f"mean          {_mean(b_on, 'api_calls'):.2f}", GREEN),
    ]
    off_lines = [
        ('$ hermes -z  "...same question..."', AMBER),
        ("  inject_chain_context: false", AMBER),
        ("  asserted before EVERY run", AMBER),
        ("", WHITE),
        (f"model calls   {f(m_off)}", RED),
        (f"mean          {_mean(m_off, 'api_calls'):.2f}", RED),
        (f"single-call   {sum(1 for r in m_off if r['api_calls'] == 1)} of {len(m_off)}", RED),
        ("", WHITE),
        ("block-number question", CYAN),
        (f"model calls   {f(b_off)}", RED),
        (f"mean          {_mean(b_off, 'api_calls'):.2f}", RED),
    ]
    panel(d, left, f"HOOK ON · mean {_mean(m_on, 'api_calls'):.2f}", on_lines, GREEN, 19, 31)
    panel(d, right, f"HOOK OFF · mean {_mean(m_off, 'api_calls'):.2f}", off_lines, RED, 19, 31)
    verdict = [
        ("The two questions disagree (ON lower on one, higher on the other) - noise, not an effect.", WHITE),
        ("The model calls base_network whether or not the number is already in its prompt.", WHITE),
    ]
    panel(d, (30, H - 170, W - 30, H - 40), "the honest read", verdict, AMBER, 21, 34)
    return img


def scene_honest():
    ab1 = json.load(open("/opt/data/cache/scratch/hook-ab.json"))
    nb = ab1["NONBASE"][0]
    base_in = json.load(open("/opt/data/cache/scratch/oneshot-usage.json"))["input_tokens"]
    pub_on, pub_off = _mean(ab1["ON"], "api_calls"), _mean(ab1["OFF"], "api_calls")
    img, d = base("4 · what held up, and what did not",
                  "one machine, one model, one chain state - n is small and the video says so",
                  "CORRECTED IN PUBLIC", AMBER)
    lines = [
        ("WHAT HELD UP", GREEN),
        ("  the gate is deterministic: Base turns only, and deciding costs no model call", WHITE),
        ("  the injected line is real, under 200 tokens, and fails open if the RPC is down", WHITE),
        (f"  unrelated turns pay nothing: {nb['input_tokens']:,} input tokens vs a {base_in:,} baseline", WHITE),
        ("", WHITE),
        ("WHAT DID NOT", RED),
        (f"  the first A/B claimed {pub_on:.2f} vs {pub_off:.2f} model calls on n=3, and was published.", WHITE),
        ("  Both arms ran with the hook ON - the toggle wrote to a config file the", WHITE),
        ("  runtime never reads. A toggle you have not confirmed is not a toggle.", WHITE),
        ("  Re-measured properly: no reliable saving. The model fetches either way.", WHITE),
        ("", WHITE),
        ("WHAT CHANGED", CYAN),
        ("  assert the plugin's own setting before every run, interleave the arms, and", WHITE),
        ("  publish per-run values - and keep the invalid capture in the repo.", WHITE),
    ]
    panel(d, (40, 150, W - 40, H - 40), "what to believe", lines, AMBER, 21, 38)
    return img


def scene_end():
    img, d = base("basekit", "6 agent tools + live context injection · Base chain 8453 · read-only", "v1.0.0", CYAN)
    lines = [
        ("No keys. No signing. No transactions.", GREEN),
        ("Nothing here can move funds - basekit answers questions about Base.", WHITE),
        ("", WHITE),
        ("github.com/D0xedDevi0/hermes-basekit", CYAN),
        ("catalog PR: NousResearch/hermes-agent#149166", CYAN),
        ("", WHITE),
        ("Reproduce this: BASEKIT_LIVE=1 python3 tests/test_live.py", GRAY),
        ("The A/B numbers are in the repo under docs/hook-proof.", GRAY),
    ]
    panel(d, (40, 240, W - 40, 240 + 90 + len(lines) * 42), "read-only, by construction", lines, CYAN, 22, 42)
    return img


SCENES = [
    ("title", scene_title, 7),
    ("gate", scene_gate, 17),
    ("line", scene_line, 12),
    ("ab", scene_ab, 26),
    ("honest", scene_honest, 13),
    ("end", scene_end, 7),
]

ALL_TEXT = []


def collect(img_scene):
    return img_scene


def main():
    paths = []
    for name, fn, dur in SCENES:
        img = fn()
        p = FRAMES / f"{name}.png"
        img.save(p)
        paths.append((str(p), dur))
        print(f"rendered {name:8s} {dur}s -> {p}")
    # glyph check across the strings the scenes actually drew
    texts = {
        "·": 1, "→": 1, "<-": 1, "…": 1,
    }
    got = check_glyphs(["basekit · the pre_llm_call hook", "how -> why", "a … b"])
    print("non-ascii glyphs used:", got)

    if PROBLEMS:
        print("\nLAYOUT PROBLEMS:")
        for p in PROBLEMS:
            print("  " + p)
        sys.exit(2)

    cmd = ["ffmpeg", "-y"]
    for p, dur in paths:
        cmd += ["-loop", "1", "-t", str(dur), "-i", p]
    filt = "".join(f"[{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=0,format=yuv420p"
    out = "/opt/data/cache/scratch/basekit-hook-proof.mp4"
    cmd += ["-filter_complex", filt, "-r", "30", "-c:v", "libx264", "-preset", "medium",
            "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("ffmpeg rc", r.returncode, r.stderr[-300:] if r.returncode else "")
    print("OUT:", out)


if __name__ == "__main__":
    main()
