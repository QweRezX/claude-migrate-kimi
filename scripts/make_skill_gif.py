#!/usr/bin/env python3
"""Generate docs/skill-demo.gif — one request inside Kimi Code does it all.

Renders a fake chat (no screen recording): user asks in natural language,
the agent runs the claude-migrate skill. Demo data is fictional — keep it
that way.

    python scripts/make_skill_gif.py            # writes docs/skill-demo.gif
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 860, 760
BG = "#0d1117"
PANEL = "#161b22"
FG = "#c9d1d9"
DIM = "#8b949e"
GREEN = "#3fb950"
BUBBLE = "#1f6feb"
BUBBLE_FG = "#ffffff"
PAD = 18
LINE_H = 24
FONT_SIZE = 17
OUT = Path(__file__).resolve().parent.parent / "docs" / "skill-demo.gif"


def font(size=FONT_SIZE):
    for cand in ("consola.ttf", "CascadiaMono.ttf", "DejaVuSansMono.ttf",
                 "/System/Library/Fonts/Menlo.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    return ImageFont.load_default()


F = font()
FB = font(FONT_SIZE + 1)

# Timeline items:
# ("user", text)      — right-aligned bubble, typed char by char
# ("agent", text)     — plain agent line
# ("tool", [lines])   — terminal-style block
SCRIPT = [
    ("user", "Install the claude-migrate skill from "
             "github.com/QweRezX/claude-migrate-kimi and import my "
             "Claude Code sessions"),
    ("agent", "I'll install the skill, then import your sessions."),
    ("tool", ["$ git clone https://github.com/QweRezX/claude-migrate-kimi \\",
              "    ~/.kimi-code/skills/claude-migrate",
              "Cloning into 'claude-migrate'... done."]),
    ("agent", "Skill installed. Checking your Claude sessions..."),
    ("tool", ["$ python3 ~/.kimi-code/skills/claude-migrate/scripts/"
              "claude2kimi.py --all --list",
              "DATE       MSGS  PROJECT        TITLE",
              "2026-11-02   18  ~/dev/my-app   Fix login redirect loop",
              "2026-11-09   42  ~/dev/blog     Add RSS feed",
              "2026-11-15    7  ~/dev/my-app   Refactor auth middleware",
              "3 session(s)."]),
    ("agent", "Found 3 sessions (dry run — nothing written yet). Import all?"),
    ("user", "yes, everything"),
    ("tool", ["$ python3 .../claude2kimi.py --all",
              "OK  Fix login redirect loop (18 msgs)",
              "OK  Add RSS feed (42 msgs)",
              "OK  Refactor auth middleware (7 msgs)"]),
    ("agent", "Done — sessions imported, and the skill stays installed: next "
              "time just ask. Restart Kimi Code to see your chats; undo "
              "anytime with --undo."),
]


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(items, typed=None):
    """items: list of timeline entries fully shown; typed: partial user text."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 34], fill=PANEL)
    d.text((PAD, 8), "Kimi Code", font=FB, fill=DIM)
    y = 48

    def draw_entry(kind, payload, partial=None):
        nonlocal y
        if kind == "user":
            text = partial if partial is not None else payload
            lines = wrap(d, text, F, W - 320)
            bw = max(d.textlength(l, font=F) for l in lines) + 28
            bh = len(lines) * LINE_H + 16
            x0 = W - PAD - bw
            d.rounded_rectangle([x0, y, W - PAD, y + bh], radius=10, fill=BUBBLE)
            ty = y + 8
            for l in lines:
                d.text((x0 + 14, ty), l, font=F, fill=BUBBLE_FG)
                ty += LINE_H
            y += bh + 14
        elif kind == "agent":
            d.ellipse([PAD, y + 6, PAD + 10, y + 16], fill=GREEN)
            for l in wrap(d, payload, F, W - 2 * PAD - 24):
                d.text((PAD + 22, y), l, font=F, fill=FG)
                y += LINE_H
            y += 10
        elif kind == "tool":
            bh = len(payload) * LINE_H + 16
            d.rounded_rectangle([PAD + 22, y, W - PAD, y + bh], radius=8,
                                fill=PANEL)
            ty = y + 8
            for l in payload:
                color = GREEN if l.startswith("$") else DIM
                d.text((PAD + 36, ty), l, font=F, fill=color)
                ty += LINE_H
            y += bh + 14

    for kind, payload in items:
        draw_entry(kind, payload)
    if typed is not None:
        draw_entry("user", "", partial=typed)
    return img


def main():
    frames, durations = [], []
    shown = []

    def push(dur):
        frames.append(render(shown))
        durations.append(dur)

    for kind, payload in SCRIPT:
        if kind == "user":
            typed = ""
            for ch in payload:
                typed += ch
                frames.append(render(shown, typed=typed))
                durations.append(35)
            push(400)
            shown.append((kind, payload))
        else:
            shown.append((kind, payload))
            push(500 if kind == "agent" else 250)
    push(3200)

    OUT.parent.mkdir(exist_ok=True)
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"wrote {OUT} ({len(frames)} frames, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
