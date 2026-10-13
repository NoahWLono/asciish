"""An in-memory VT terminal with independent normal and alternate buffers.

Child output is parsed here, never written to the outer terminal. Device
responses are queued separately so the PTY owner can send them to the child.
"""

from __future__ import annotations

from typing import Any, Callable

import pyte
from pyte import modes


class _ByteStream(pyte.ByteStream):
    """Retain query prefixes and allow DEC line drawing with UTF-8 input."""

    escape = {**pyte.ByteStream.escape, "Z": "report_device_attributes"}

    def __init__(self, listener: Terminal) -> None:
        self.csi_prefix = ""
        self._after_escape = False
        self._in_csi = False
        super().__init__(listener)

    def _send_to_parser(self, data: str) -> bool | None:
        final = False
        if data == "\x1b":
            self._after_escape = True
            self._in_csi = False
            self.csi_prefix = ""
        elif data == "\x9b" or (self._after_escape and data == "["):
            self._after_escape = False
            self._in_csi = True
            self.csi_prefix = ""
        elif self._after_escape:
            self._after_escape = False
        elif self._in_csi:
            if data in "?<=>":
                self.csi_prefix += data
            final = "@" <= data <= "~" or data in "\x18\x1a"

        # ByteStream has already decoded this character. pyte also uses its
        # encoding flag to suppress G0/G1 designation and SI/SO dispatch; allow
        # only DEC graphics (0), ASCII (B), and those shifts through that gate.
        # The real parser still decides whether a designation is syntactically
        # active, so an ESC ( 0 inside an OSC title remains inert title text.
        charset_control = self.use_utf8 and data in ("0", "B", "\x0e", "\x0f")
        if charset_control:
            self.use_utf8 = False
        try:
            return super()._send_to_parser(data)
        finally:
            if charset_control:
                self.use_utf8 = True
            if final:
                self._in_csi = False
                self.csi_prefix = ""


class Terminal:
    """Parse bytes from a PTY into a :class:`pyte.Screen` for rendering.

    ``screen`` always refers to the active buffer. The stream keeps one stable
    listener, whose handlers resolve that buffer at dispatch time; reattaching
    a pyte stream mid-sequence would discard its parser state.
    """

    def __init__(self, columns: int, lines: int) -> None:
        self._validate_size(columns, lines)
        self._normal_screen = pyte.Screen(columns, lines)
        self._alternate_screen = pyte.Screen(columns, lines)
        self.screen = self._normal_screen
        self.application_cursor = False
        self.bracketed_paste = False
        self.alternate = False
        self._responses = bytearray()
        self._stream = _ByteStream(self)

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name not in pyte.Stream.events:
            raise AttributeError(name)

        def dispatch(*args: Any, **kwargs: Any) -> None:
            getattr(self.screen, name)(*args, **kwargs)

        return dispatch

    @staticmethod
    def _validate_size(columns: int, lines: int) -> None:
        if columns < 1 or lines < 1:
            raise ValueError("terminal columns and lines must be positive")

    def feed(self, data: bytes) -> None:
        """Consume a possibly partial UTF-8 string or escape sequence."""
        self._stream.feed(data)

    def resize(self, columns: int, lines: int) -> None:
        """Resize both buffers without reinitializing the input parser."""
        self._validate_size(columns, lines)
        for screen in (self._normal_screen, self._alternate_screen):
            if (screen.columns, screen.lines) != (columns, lines):
                screen.resize(lines=lines, columns=columns)
                # pyte resizes the grid but leaves its cursor outside new bounds.
                screen.ensure_hbounds()
                screen.ensure_vbounds()

    def drain_responses(self) -> bytes:
        """Return and remove replies destined for the child process."""
        result = bytes(self._responses)
        self._responses.clear()
        return result

    def reset(self) -> None:
        """Reset both buffers and terminal modes (RIS, ESC c)."""
        for screen in (self._normal_screen, self._alternate_screen):
            screen.reset()
            screen.savepoints.clear()
        self.screen = self._normal_screen
        self.alternate = False
        self.application_cursor = False
        self.bracketed_paste = False

    def set_mode(self, *values: int, private: bool = False) -> None:
        self._change_modes(values, True, private)

    def reset_mode(self, *values: int, private: bool = False) -> None:
        self._change_modes(values, False, private)

    def _change_modes(self, values: tuple[int, ...], enabled: bool, private: bool) -> None:
        for value in values:
            if private and value in (47, 1047, 1049):
                self._switch_buffer(enabled, value)
            elif private and value == 1:
                self.application_cursor = enabled
            elif private and value == 2004:
                self.bracketed_paste = enabled
            else:
                method = self.screen.set_mode if enabled else self.screen.reset_mode
                method(value, private=private)

    def _switch_buffer(self, enabled: bool, mode: int) -> None:
        if enabled == self.alternate:
            return
        if enabled:
            if mode == 1049:
                self._alternate_screen.reset()
                self._alternate_screen.savepoints.clear()
            self.screen = self._alternate_screen
        else:
            if mode in (1047, 1049):
                self._alternate_screen.reset()
                self._alternate_screen.savepoints.clear()
            self.screen = self._normal_screen
        self.alternate = enabled
        self.screen.dirty.update(range(self.screen.lines))

    def report_device_attributes(self, mode: int = 0, private: bool = False) -> None:
        if mode != 0 or private:
            return
        if self._stream.csi_prefix == ">":
            self._responses.extend(b"\x1b[>0;0;0c")
        elif not self._stream.csi_prefix:
            self._responses.extend(b"\x1b[?6c")

    def report_device_status(self, mode: int = 0, private: bool = False) -> None:
        if mode == 5 and not private:
            self._responses.extend(b"\x1b[0n")
        elif mode == 6:
            screen = self.screen
            # pyte uses x == columns for a pending automatic wrap. A cursor
            # report still identifies the last visible column in that state.
            column = min(screen.cursor.x, screen.columns - 1) + 1
            line = screen.cursor.y + 1
            if modes.DECOM in screen.mode and screen.margins is not None:
                line -= screen.margins.top
            prefix = "?" if private else ""
            self._responses.extend(f"\x1b[{prefix}{line};{column}R".encode("ascii"))
