"""Command-line entry point. No shell parsing of user-provided commands."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def _ink(value: str) -> str:
    if len(value) != 1 or not 33 <= ord(value) <= 126:
        raise argparse.ArgumentTypeError("ink must be one visible ASCII character")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A real shell rendered as giant ASCII. Zoom your terminal out.",
        epilog="Ctrl-] exits asciish. All other keys go to the child. Linux, macOS, or WSL.",
    )
    parser.add_argument("--version", action="version", version=f"asciish {__version__}")
    parser.add_argument("--no-color", action="store_true", help="disable output colors")
    parser.add_argument("--ink", type=_ink, help="draw every glyph with this character instead of itself")
    parser.add_argument("--preview", metavar="TEXT", help="print static ASCII text and exit (works in pipes)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments, after -- (default: $SHELL)")
    args = parser.parse_args(argv)

    if args.preview is not None:
        from .terminal import Terminal
        from .render import render_plain
        from .font import ascii_char
        # Preview accepts text, not a terminal byte stream. Normalize first so
        # tabs and control characters cannot invalidate the measured geometry.
        lines = ["".join(ascii_char(char) for char in line.expandtabs(8))
                 for line in args.preview.splitlines()] or [""]
        width = max(1, max(map(len, lines)))
        if width > 1000 or len(lines) > 1000:
            parser.error("preview is limited to 1000 columns and 1000 lines")
        terminal = Terminal(width, len(lines))
        terminal.feed("\r\n".join(lines).encode())
        print(render_plain(terminal.screen, ink=args.ink))
        return 0

    if os.name != "posix":
        parser.error("interactive mode needs Unix pseudo-terminals; on Windows, use WSL")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        import pwd
        shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
        command = [shell]
    from .session import run
    try:
        return run(command, color=not (args.no_color or "NO_COLOR" in os.environ), ink=args.ink)
    except (ValueError, OSError) as error:
        print(f"asciish: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
