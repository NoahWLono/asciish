#!/usr/bin/env python3
"""Record real asciish output and render demo.cast, demo.gif, and preview.png.

Run from any directory after installing the project's ``demo`` extra:
    python tools/record_demo.py

The shell runs without startup files, with a temporary home and a small public
environment. Every giant character in the images comes from captured PTY output;
Pillow adds only the surrounding window and title. No mock terminal is used.
"""

from __future__ import annotations

import argparse
import codecs
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

import pyte
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
COLUMNS, ROWS = 241, 73
CELL_WIDTH, CELL_HEIGHT = 6, 10
WIDTH, HEIGHT = 1536, 896
LEFT, TOP = 45, 114
FPS = 12
BACKGROUND = "#0a0e14"
TERMINAL_BG = "#111820"
FOREGROUND = "#d8e0e8"
COLORS = {
    "black": "#18212c", "red": "#f07178", "green": "#a6e3a1",
    "brown": "#e5c07b", "yellow": "#e5c07b", "blue": "#82aaff",
    "magenta": "#c792ea", "cyan": "#70e1e8", "white": "#d8e0e8",
    "brightblack": "#6c7b8e", "brightred": "#ff9a9f", "brightgreen": "#baf7ba",
    "brightbrown": "#ffdf98", "brightyellow": "#ffdf98", "brightblue": "#a8c4ff",
    "brightmagenta": "#dfb7ff", "brightcyan": "#a0f3f4", "brightwhite": "#ffffff",
}


def font_path(requested: str | None, family: str, fallbacks: list[str]) -> str:
    if requested:
        return requested
    if shutil.which("fc-match"):
        found = subprocess.check_output(
            ["fc-match", "-f", "%{file}", family], text=True
        ).strip()
        if found and Path(found).is_file():
            return found
    for candidate in fallbacks:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("No suitable font found; pass --font and --ui-font paths.")


def record() -> tuple[list[list[object]], float]:
    """Capture the actual app under an outer controlling PTY."""
    events: list[list[object]] = []
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with tempfile.TemporaryDirectory(prefix="asciish-demo-") as home:
        pid, master = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
            env = {
                "PATH": "/usr/bin:/bin", "HOME": home, "TERM": "xterm-256color",
                "LANG": "C.UTF-8", "PS1": "$ ", "PS2": "> ",
                "PROMPT_COMMAND": "", "HISTFILE": "/dev/null",
                "PYTHONPATH": str(ROOT / "src"),
            }
            os.chdir(home)
            os.execve(sys.executable, [
                sys.executable, "-m", "asciish", "--", "/bin/bash", "--noprofile", "--norc"
            ], env)

        start = time.monotonic()
        os.set_blocking(master, False)
        child_status = None

        def collect_for(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        raise RuntimeError("asciish exited before the demonstration finished") from error
                    raise
                if not data:
                    raise RuntimeError("asciish closed the PTY before the demonstration finished")
                text = decoder.decode(data)
                if text:
                    events.append([round(time.monotonic() - start, 6), "o", text])

        try:
            collect_for(0.8)
            commands = [
                r"printf 'hello cafkafk\n'",
                r"printf 'ASCII made of ASCII\n'",
                r"printf '\e[36mreal shell\e[0m\n'",
            ]
            for command in commands:
                for character in command:
                    os.write(master, character.encode("ascii"))
                    collect_for(0.045)
                collect_for(0.15)
                os.write(master, b"\r")
                collect_for(0.9)
            collect_for(2.5)
            duration = time.monotonic() - start
            events.append([round(duration, 6), "o", ""])
        finally:
            # End the real application cleanly after the captured segment. Its
            # normal-screen restoration is intentionally outside this recording.
            try:
                os.write(master, b"\x1d")
            except OSError:
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                found, status = os.waitpid(pid, os.WNOHANG)
                if found:
                    child_status = status
                    break
                time.sleep(0.02)
            if child_status is None:
                os.kill(pid, signal.SIGTERM)
                _, child_status = os.waitpid(pid, 0)
            os.close(master)
        if os.waitstatus_to_exitcode(child_status) != 0:
            raise RuntimeError(f"asciish exited with status {child_status}")
    return events, duration


def color(value: str, background: bool = False) -> str:
    if value == "default":
        return TERMINAL_BG if background else FOREGROUND
    if value in COLORS:
        return COLORS[value]
    if len(value) == 6 and all(character in "0123456789abcdefABCDEF" for character in value):
        return "#" + value
    return TERMINAL_BG if background else FOREGROUND


def canvas(ui_font: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((23, 24, WIDTH - 24, HEIGHT - 24), 18, fill=TERMINAL_BG, outline="#2b3745", width=1)
    draw.rounded_rectangle((24, 25, WIDTH - 25, 94), 17, fill="#18212c")
    draw.rectangle((24, 73, WIDTH - 25, 94), fill="#18212c")
    for x, fill in [(48, "#f07178"), (72, "#e5c07b"), (96, "#a6e3a1")]:
        draw.ellipse((x, 52, x + 11, 63), fill=fill)
    title_font = ImageFont.truetype(ui_font, 22)
    detail_font = ImageFont.truetype(ui_font, 15)
    draw.text((134, 43), "asciish", font=title_font, fill="#edf3fa")
    draw.text((231, 48), "/  real shell  /  zoom out", font=detail_font, fill="#97a9bd")
    draw.rounded_rectangle((WIDTH - 226, 43, WIDTH - 47, 73), 15, fill="#223540")
    draw.text((WIDTH - 211, 47), "LIVE PTY CAPTURE", font=detail_font, fill="#85dfc4")
    return image


def paint(screen: pyte.Screen, base: Image.Image, font: ImageFont.FreeTypeFont) -> Image.Image:
    image = base.copy()
    draw = ImageDraw.Draw(image)
    for y in range(screen.lines):
        row = screen.buffer[y]
        for x, cell in row.items():
            if x >= COLUMNS:
                continue
            foreground, background = color(cell.fg), color(cell.bg, True)
            if cell.reverse:
                foreground, background = background, foreground
            left, top = LEFT + x * CELL_WIDTH, TOP + y * CELL_HEIGHT
            if background != TERMINAL_BG:
                draw.rectangle((left, top, left + CELL_WIDTH - 1, top + CELL_HEIGHT - 1), fill=background)
            if cell.data and cell.data != " ":
                draw.text((left, top - 2), cell.data, font=font, fill=foreground)
    return image


def render(events: list[list[object]], duration: float, output: Path, mono_font: str, ui_font: str) -> int:
    """Rasterize the captured outer terminal, including its actual footer."""
    screen = pyte.Screen(COLUMNS, ROWS)
    stream = pyte.Stream(screen)
    base = canvas(ui_font)
    font = ImageFont.truetype(mono_font, 10)
    frames: list[Image.Image] = []
    frame_durations: list[int] = []
    event_index = 0
    last_signature = None
    frame_ms = round(1000 / FPS)
    for frame_index in range(int(duration * FPS) + 1):
        timestamp = min(frame_index / FPS, duration)
        while event_index < len(events) and float(events[event_index][0]) <= timestamp:
            stream.feed(str(events[event_index][2]))
            event_index += 1
        signature = tuple(
            tuple(sorted(screen.buffer[y].items())) for y in range(screen.lines)
        )
        if signature == last_signature:
            frame_durations[-1] += frame_ms
            continue
        last_signature = signature
        frames.append(paint(screen, base, font))
        frame_durations.append(frame_ms)

    # Quantize every frame against one palette; retaining a shared palette keeps
    # both colors and the tiny ASCII marks stable throughout the animation.
    palette = frames[-1].quantize(colors=128)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    indexed[0].save(output / "demo.gif", save_all=True, append_images=indexed[1:],
                    duration=frame_durations, loop=0, optimize=True, disposal=1)
    frames[-1].save(output / "preview.png", optimize=True)
    return len(frames)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "assets")
    parser.add_argument("--font", help="path to a monospace TrueType/OpenType font")
    parser.add_argument("--ui-font", help="path to a title TrueType/OpenType font")
    args = parser.parse_args()
    mono = font_path(args.font, "monospace", [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ])
    ui = font_path(args.ui_font, "sans-serif", [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ])
    args.output.mkdir(parents=True, exist_ok=True)
    events, duration = record()
    header = {
        "version": 2, "width": COLUMNS, "height": ROWS, "duration": round(duration, 6),
        "title": "asciish — a real shell rendered as giant ASCII",
        "command": "asciish -- /bin/bash --noprofile --norc",
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
    }
    with (args.output / "demo.cast").open("w", encoding="utf-8") as recording:
        for record_line in [header, *events]:
            recording.write(json.dumps(record_line, ensure_ascii=False) + "\n")
    count = render(events, duration, args.output, mono, ui)
    print(f"Captured {len(events)} real PTY output chunks; {duration:.2f}s; {count} distinct frames.")
    for name in ("demo.cast", "demo.gif", "preview.png"):
        path = args.output / name
        print(f"{path}: {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
