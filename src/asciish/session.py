"""Unix PTY transport and host terminal lifecycle."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time
import tty

from .render import Renderer
from .terminal import Terminal


ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[0m\x1b[2J\x1b[H"
LEAVE = "\x1b[0m\x1b[?2004l\x1b[?1l\x1b>\x1b[?7h\x1b[?25h\x1b[?1049l"


def logical_size(columns: int, rows: int) -> tuple[int, int]:
    """Allow room for the footer and avoid the host's bottom-right cell."""
    return max(1, (columns - 1) // 6), max(1, (rows - 1) // 8)


def _size(fd: int) -> tuple[int, int]:
    size = os.get_terminal_size(fd)
    return max(1, size.columns), max(1, size.lines)


def _resize(fd: int, columns: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _write(fd: int, data: bytes) -> None:
    """Write a complete host frame, including when stdout makes short writes."""
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        view = view[count:]


def _reap(pid: int, status: int | None) -> int:
    """Bound cleanup even when a child ignores the PTY's hangup."""
    if status is not None:
        return status
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            found, result = os.waitpid(pid, os.WNOHANG)
            if found:
                return result
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + (0.3 if sig != signal.SIGKILL else 1.0)
        while time.monotonic() < deadline:
            found, result = os.waitpid(pid, os.WNOHANG)
            if found:
                return result
            time.sleep(0.01)
    # SIGKILL is delivered; wait for the process table entry to disappear.
    return os.waitpid(pid, 0)[1]


def run(command: list[str], *, color: bool = True, ink: str | None = None) -> int:
    """Run a real command with a controlling PTY and render its virtual screen."""
    input_fd, output_fd = 0, 1
    if not os.isatty(input_fd) or not os.isatty(output_fd):
        raise ValueError("interactive mode needs a terminal; try --preview 'hello'")
    if not command:
        raise ValueError("a command is required")

    columns, rows = _size(output_fd)
    inner_columns, inner_rows = logical_size(columns, rows)
    terminal = Terminal(inner_columns, inner_rows)
    renderer = Renderer(color=color, ink=ink)
    saved = termios.tcgetattr(input_fd)
    old_handlers = {}
    resized = False
    interrupted = 0
    status = None
    entered = False

    def on_resize(signum, frame):
        nonlocal resized
        resized = True

    def on_signal(signum, frame):
        nonlocal interrupted
        interrupted = signum

    pid, master = pty.fork()
    if pid == 0:
        try:
            _resize(0, inner_columns, inner_rows)
            env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", ASCIISH="1")
            # Shells must obtain the reduced geometry from the PTY.
            env.pop("LINES", None)
            env.pop("COLUMNS", None)
            os.execvpe(command[0], command, env)
        except OSError as error:
            os.write(2, f"asciish: {error}\r\n".encode("utf-8", "replace"))
            os._exit(127 if error.errno == errno.ENOENT else 126)

    try:
        for sig, handler in (
            (signal.SIGWINCH, on_resize), (signal.SIGTERM, on_signal),
            (signal.SIGHUP, on_signal), (signal.SIGINT, on_signal),
        ):
            old_handlers[sig] = signal.signal(sig, handler)
        tty.setraw(input_fd, termios.TCSANOW)
        os.set_blocking(master, False)
        entered = True
        _write(output_fd, ENTER.encode())
        pending = bytearray()
        changed = True
        last_frame = 0.0
        last_modes = None
        child_finished_at = None
        eof = False
        escape_exit = False

        while not interrupted:
            if resized:
                resized = False
                columns, rows = _size(output_fd)
                inner_columns, inner_rows = logical_size(columns, rows)
                terminal.resize(inner_columns, inner_rows)
                _resize(master, inner_columns, inner_rows)
                renderer.invalidate()
                changed = True

            now = time.monotonic()
            if changed and (now - last_frame >= 1 / 30 or eof):
                modes = (terminal.application_cursor, terminal.bracketed_paste)
                if modes != last_modes:
                    # Let the outer terminal encode arrows and paste for the child.
                    _write(output_fd, (
                        f"\x1b[?1{'h' if modes[0] else 'l'}"
                        f"\x1b[?2004{'h' if modes[1] else 'l'}"
                    ).encode())
                    last_modes = modes
                footer = f" asciish | {inner_columns}x{inner_rows} | zoom out for more room | Ctrl-] exit"
                _write(output_fd, renderer.frame(terminal.screen, columns, rows, footer).encode("ascii"))
                changed = False
                last_frame = now
            if status is None:
                found, result = os.waitpid(pid, os.WNOHANG)
                if found:
                    status = result
                    child_finished_at = now
            # Closing stdio is not an exit: a command may still be computing.
            # Keep input/signals alive but stop selecting the exhausted PTY.
            if eof and status is not None:
                break
            # A background descendant can keep the slave open after the shell exits.
            if child_finished_at is not None and now - child_finished_at > 0.15:
                eof = True
                continue

            readable, writable, _ = select.select(
                [input_fd] + ([] if eof else [master]),
                [master] if pending and not eof else [], [], 1 / 30
            )
            if input_fd in readable:
                data = os.read(input_fd, 65536)
                if not data or b"\x1d" in data:
                    escape_exit = True
                    break
                pending.extend(data)
            if master in writable:
                try:
                    written = os.write(master, pending)
                    del pending[:written]
                except BlockingIOError:
                    pass
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    eof = True
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno not in (errno.EIO, errno.EBADF):
                        raise
                    data = b""
                if data:
                    terminal.feed(data)
                    pending.extend(terminal.drain_responses())
                    changed = True
                else:
                    eof = True
        if interrupted:
            return 128 + interrupted
        if escape_exit:
            return 0
    finally:
        # Restore the user's terminal before waiting for any reluctant child.
        try:
            termios.tcsetattr(input_fd, termios.TCSANOW, saved)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            try:
                if entered:
                    _write(output_fd, LEAVE.encode())
            finally:
                os.close(master)
                status = _reap(pid, status)
    exitcode = os.waitstatus_to_exitcode(status)
    return exitcode if exitcode >= 0 else 128 - exitcode
