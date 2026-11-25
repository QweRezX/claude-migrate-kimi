#!/usr/bin/env python3
"""Generate docs/demo.gif — a scripted terminal demo of claude2kimi.

Renders a fake terminal (no screen recording needed) with typing animation
and realistic output. Demo data is fictional on purpose — do not put real
session titles or paths here.

    python scripts/make_demo_gif.py            # writes docs/demo.gif
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 860, 520
BG = "#0d1117"
FG = "#c9d1d9"
DIM = "#8b949e"
GREEN = "#3fb950"
CYAN = "#79c0ff"
YELLOW = "#e3b341"
PAD = 18
LINE_H = 24
FONT_SIZE = 17
OUT = Path(__file__).resolve().parent.parent / "docs" / "demo.gif"

FONT = None
for cand in ("consola.ttf", "CascadiaMono.ttf", "DejaVuSansMono.ttf",
             "/System/Library/Fonts/Menlo.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
    try:
        FONT = ImageFont.truetype(cand, FONT_SIZE)
        break
    except OSError:
        continue
if FONT is None:
    FONT = ImageFont.load_default()

PROMPT = "$ "
# (kind, text) — kind: cmd | out | ok | dim | blank
SCRIPT = [
    ("cmd", "cd claude-migrate"),
    ("cmd", "python3 claude2kimi.py --all --list"),
    ("out", "Found 3 Claude session file(s)."),
    ("out", "DATE       MSGS  PROJECT            TITLE"),
    ("out", "2026-11-02   18  ~/dev/my-app       Fix login redirect loop"),
    ("out", "2026-11-09   42  ~/dev/blog         Add RSS feed"),
    ("out", "2026-11-15    7  ~/dev/my-app       Refactor auth middleware"),
    ("dim", "3 session(s)."),
    ("blank", ""),
    ("cmd", "python3 claude2kimi.py --all"),
    ("out", "Found 3 Claude session file(s)."),
    ("ok", "OK  Fix login redirect loop (18 msgs)"),
    ("ok", "OK  Add RSS feed (42 msgs)"),
    ("ok", "OK  Refactor auth middleware (7 msgs)"),
    ("blank", ""),
    ("cmd", "python3 claude2kimi.py --undo --dry-run"),
    ("dim", "Dry run: 3 imported session(s) would be removed."),
    ("blank", ""),
    ("ok", "# Restart Kimi Code — your Claude chats are in the picker."),
]

COLORS = {"out": FG, "ok": GREEN, "dim": DIM, "cmd": FG, "blank": FG}


def render(lines, cursor=False):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # window chrome
    d.rectangle([0, 0, W, 34], fill="#161b22")
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        d.ellipse([14 + i * 22, 11, 26 + i * 22, 23], fill=c)
    y = PAD + 26
    for kind, text in lines[-(H // LINE_H - 2):]:
        x = PAD
        if kind == "cmd":
            d.text((x, y), PROMPT, font=FONT, fill=GREEN)
            x += d.textlength(PROMPT, font=FONT)
        color = COLORS.get(kind, FG)
        d.text((x, y), text, font=FONT, fill=color)
        if cursor and (kind, text) == lines[-1]:
            cx = x + d.textlength(text, font=FONT) + 2
            d.rectangle([cx, y + 2, cx + 9, y + LINE_H - 4], fill=FG)
        y += LINE_H
    return img


def main():
    frames, durations = [], []
    screen = []

    def push(dur, cursor=False):
        frames.append(render(screen, cursor))
        durations.append(dur)

    for kind, text in SCRIPT:
        if kind == "cmd":
            screen.append(("cmd", ""))
            typed = ""
            for ch in text:
                typed += ch
                screen[-1] = ("cmd", typed)
                push(28, cursor=True)
            push(350, cursor=True)
        else:
            screen.append((kind, text))
            push(120 if kind != "blank" else 60)
    push(2600)  # linger on the final screen

    OUT.parent.mkdir(exist_ok=True)
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"wrote {OUT} ({len(frames)} frames, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
