"""Expand an emulated terminal into ASCII art and update a real terminal.

Only generated cursor and SGR sequences cross the output boundary. Child
process text and status text always pass through the safe ASCII mapping.
"""

from dataclasses import dataclass
from itertools import groupby
import re

from .font import CELL_HEIGHT, CELL_WIDTH, GLYPH_HEIGHT, GLYPH_WIDTH, ascii_char, glyph


@dataclass(frozen=True)
class _Style:
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    reverse: bool = False


_DEFAULT = _Style()
_BLANK = (" ", _DEFAULT)
_ANSI_COLORS = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "brightblack": 8, "brightred": 9, "brightgreen": 10,
    "brightbrown": 11, "brightyellow": 11, "brightblue": 12,
    "brightmagenta": 13, "brightcyan": 14, "brightwhite": 15,
}


def _color_code(value: str, background: bool) -> str | None:
    index = _ANSI_COLORS.get(value)
    if index is not None:
        base = (40 if background else 30) if index < 8 else (100 if background else 90)
        return str(base + index % 8)
    if re.fullmatch(r"[0-9a-fA-F]{6}", value):
        rgb = ";".join(str(int(value[i:i + 2], 16)) for i in (0, 2, 4))
        return f"{48 if background else 38};2;{rgb}"
    return None


def _sgr(style: _Style) -> str:
    codes = ["0"]
    if style.bold:
        codes.append("1")
    if style.reverse:
        codes.append("7")
    for value, background in ((style.fg, False), (style.bg, True)):
        code = _color_code(value, background)
        if code:
            codes.append(code)
    return "\x1b[" + ";".join(codes) + "m"


def _validate_ink(ink: str | None) -> None:
    if ink is not None and (len(ink) != 1 or not " " <= ink <= "~"):
        raise ValueError("ink must be one printable ASCII character")


def render_plain(screen, ink: str | None = None) -> str:
    """Return a full rectangular snapshot with 6 by 8 cells and no cursor.

    Whitespace is retained, including one spacer column and row per source
    cell. The result contains only printable ASCII characters and newlines.
    """
    _validate_ink(ink)
    result = []
    for y in range(screen.lines):
        cells = [ascii_char(screen.buffer[y][x].data) for x in range(screen.columns)]
        for pixel_y in range(CELL_HEIGHT):
            result.append("".join(
                "".join((ink if ink is not None else char) if bit == "1" else " "
                        for bit in glyph(char)[pixel_y]) + " "
                if pixel_y < GLYPH_HEIGHT else " " * CELL_WIDTH
                for char in cells
            ))
    return "\n".join(result)


class Renderer:
    """Render line diffs, reserving the last row for status and last column.

    ``frame`` returns bytes-as-text to write to an ANSI terminal. A first
    frame (or an invalidated frame) clears the terminal and hides its native
    cursor. Later identical frames are empty. The caller restores terminal
    modes, including cursor visibility, when leaving the interactive session.
    """

    def __init__(self, color: bool = True, ink: str | None = None):
        _validate_ink(ink)
        self.color = color
        self.ink = ink
        self._previous = None
        self._dimensions = None
        self._row_cache = {}

    def invalidate(self) -> None:
        """Force a clear and complete repaint on the next frame."""
        self._previous = None
        self._row_cache.clear()

    def _expand_row(self, cells: tuple, width: int) -> tuple:
        """Expand immutable source cells, without the independently drawn cursor."""
        pixels = [[_BLANK] * width for _ in range(CELL_HEIGHT)]
        for x, cell in enumerate(cells):
            char = ascii_char(cell.data)
            style = _Style(cell.fg if self.color else "default",
                           cell.bg if self.color else "default",
                           cell.bold, cell.reverse)
            if char == " " and style == _DEFAULT:
                continue
            bitmap = glyph(char)
            ink = self.ink if self.ink is not None else char
            for dy in range(CELL_HEIGHT):
                target = pixels[dy]
                for dx in range(min(CELL_WIDTH, width - x * CELL_WIDTH)):
                    mark = ink if dy < GLYPH_HEIGHT and dx < GLYPH_WIDTH and bitmap[dy][dx] == "1" else " "
                    target[x * CELL_WIDTH + dx] = (mark, style)
        return tuple(tuple(row) for row in pixels)

    def frame(self, screen, columns: int, rows: int, status: str = "") -> str:
        """Return ANSI updates fitting the given physical terminal size."""
        width, height = max(0, columns - 1), max(0, rows)
        dimensions = (columns, rows)
        if dimensions != self._dimensions:
            self.invalidate()
            self._dimensions = dimensions
        if not width or not height:
            self._previous = None
            return ""

        body_height = height - 1
        blank_line = (_BLANK,) * width
        picture = [blank_line] * height
        next_cache = {}
        for y in range(min(screen.lines, (body_height + CELL_HEIGHT - 1) // CELL_HEIGHT)):
            cells = tuple(screen.buffer[y][x] for x in range(
                min(screen.columns, (width + CELL_WIDTH - 1) // CELL_WIDTH)))
            # Include complete Char records so attribute-only changes repaint.
            # Retain only this frame's rows to bound the cache during scrolling.
            key = (cells, width, self.color, self.ink)
            expanded = self._row_cache.get(key)
            if expanded is None:
                expanded = next_cache.get(key)
            if expanded is None:
                expanded = self._expand_row(cells, width)
            next_cache[key] = expanded
            for dy in range(min(CELL_HEIGHT, body_height - y * CELL_HEIGHT)):
                picture[y * CELL_HEIGHT + dy] = expanded[dy]
        self._row_cache = next_cache

        if not screen.cursor.hidden:
            cursor_x = min(max(0, screen.cursor.x), max(0, screen.columns - 1)) * CELL_WIDTH
            cursor_y = screen.cursor.y * CELL_HEIGHT + GLYPH_HEIGHT
            if 0 <= cursor_y < body_height:
                cursor_line = list(picture[cursor_y])
                for dx in range(min(GLYPH_WIDTH, max(0, width - cursor_x))):
                    cursor_line[cursor_x + dx] = ("_", _DEFAULT)
                picture[cursor_y] = tuple(cursor_line)

        safe_status = "".join(ascii_char(char) for char in status)[:width]
        picture[-1] = tuple((char, _DEFAULT) for char in safe_status) + blank_line[len(safe_status):]
        lines = picture
        fresh = self._previous is None
        output = ["\x1b[?25l\x1b[0m\x1b[2J"] if fresh else []
        for index, line in enumerate(lines):
            old = blank_line if fresh else self._previous[index]
            if line == old:
                continue
            end = width
            while end and line[end - 1] == _BLANK:
                end -= 1
            output.append(f"\x1b[{index + 1};1H")
            for style, pixels in groupby(line[:end], key=lambda pixel: pixel[1]):
                output.append(_sgr(style))
                output.append("".join(pixel[0] for pixel in pixels))
            output.append("\x1b[0m\x1b[K")
        self._previous = lines
        return "".join(output)
