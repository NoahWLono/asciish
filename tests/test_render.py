"""The rendered terminal must preserve source identity without leaking controls."""

import re

import pyte
import pytest

from asciish.font import glyph
from asciish.render import Renderer, render_plain


def screen_with(text, columns=2, lines=1):
    screen = pyte.Screen(columns, lines)
    pyte.Stream(screen).feed(text)
    screen.cursor.hidden = True
    return screen


def host_with(frame, columns=25, rows=10):
    host = pyte.Screen(columns, rows)
    pyte.Stream(host).feed(frame)
    return host


def test_original_character_is_its_own_ink():
    screen = screen_with("A!", columns=2)
    picture = render_plain(screen)
    assert len(picture.splitlines()) == 8
    assert all(len(row) == 12 for row in picture.splitlines())
    assert picture.splitlines()[0] == " AAA    !   "
    assert set(picture) == {"A", "!", " ", "\n"}


def test_lowercase_is_not_capitalized():
    assert glyph("a") != glyph("A")
    picture = render_plain(screen_with("a", columns=1))
    assert "a" in picture
    assert "A" not in picture
    assert picture.splitlines()[0] == "      "


def test_every_printable_ascii_character_has_a_valid_nonempty_glyph():
    for code in range(32, 127):
        char = chr(code)
        bitmap = glyph(char)
        assert len(bitmap) == 7, char
        assert all(len(row) == 5 and set(row) <= {"0", "1"} for row in bitmap), char
        assert ("1" in "".join(bitmap)) == (char != " "), char
    assert len({glyph(chr(code)) for code in range(33, 127)}) == 94


def test_unicode_is_readable_ascii_and_never_passed_through():
    picture = render_plain(screen_with("é─│雪", columns=5))
    assert set(picture) <= set("e-|? \n")
    assert all(char in picture for char in "e-|?")
    assert picture.isascii()


def test_plain_render_contains_no_terminal_controls():
    screen = screen_with("\x1b[31mA\x1b[0m\x1b]0;BAD\x07", columns=1)
    picture = render_plain(screen)
    assert set(picture) <= {"A", " ", "\n"}


def test_frame_stays_inside_host_and_reserves_last_column():
    frame = Renderer(color=False).frame(screen_with("AB", columns=2), 10, 6, "status")
    host = host_with(frame, columns=10, rows=6)
    assert all(row[-1] == " " for row in host.display)
    assert host.display[-1] == "status    "
    assert "A" in "".join(host.display[:-1])
    assert all(1 <= int(row) <= 6 and 1 <= int(col) <= 9
               for row, col in re.findall(r"\x1b\[(\d+);(\d+)H", frame))


def test_empty_space_is_free_after_initial_clear():
    renderer = Renderer()
    screen = screen_with("", columns=3)
    frame = renderer.frame(screen, 25, 10)
    assert "\x1b[2J" in frame
    assert not re.search(r"\x1b\[\d+;\d+H", frame)
    assert renderer.frame(screen, 25, 10) == ""


def test_diff_updates_remove_old_text_and_skip_unchanged_lines():
    renderer = Renderer(color=False)
    screen = screen_with("A", columns=2)
    host = host_with(renderer.frame(screen, 25, 10))
    assert renderer.frame(screen, 25, 10) == ""
    pyte.Stream(screen).feed("\r ")
    update = renderer.frame(screen, 25, 10)
    pyte.Stream(host).feed(update)
    assert all(row == " " * 25 for row in host.display)
    assert "\x1b[2J" not in update


def test_dimensions_and_invalidation_force_a_full_repaint():
    renderer = Renderer()
    screen = screen_with("A", columns=2)
    renderer.frame(screen, 25, 10)
    assert "\x1b[2J" in renderer.frame(screen, 26, 10)
    renderer.invalidate()
    assert "\x1b[2J" in renderer.frame(screen, 26, 10)


def test_foreground_background_bold_and_reverse_survive_expansion():
    screen = screen_with("\x1b[31;44;1;7mA", columns=1)
    host = host_with(Renderer().frame(screen, 25, 10))
    pixel = host.buffer[0][1]
    assert (pixel.data, pixel.fg, pixel.bg, pixel.bold, pixel.reverse) == (
        "A", "red", "blue", True, True)
    assert host.buffer[0][0].bg == "blue"


def test_truecolor_is_preserved():
    screen = screen_with("\x1b[38;2;12;34;56mA", columns=1)
    host = host_with(Renderer().frame(screen, 25, 10))
    assert host.buffer[0][1].fg == "0c2238"


def test_no_color_retains_content_and_reverse_without_color_codes():
    screen = screen_with("\x1b[31;44;1;7mA", columns=1)
    host = host_with(Renderer(color=False).frame(screen, 25, 10))
    pixel = host.buffer[0][1]
    assert pixel.data == "A"
    assert (pixel.fg, pixel.bg, pixel.bold, pixel.reverse) == ("default", "default", True, True)


def test_visible_cursor_moves_and_hidden_cursor_is_erased():
    renderer = Renderer(color=False)
    screen = screen_with("", columns=2)
    screen.cursor.hidden = False
    host = host_with(renderer.frame(screen, 25, 10))
    assert host.display[7].startswith("_____ ")
    screen.cursor.x = 1
    pyte.Stream(host).feed(renderer.frame(screen, 25, 10))
    assert host.display[7].startswith("      _____ ")
    screen.cursor.hidden = True
    pyte.Stream(host).feed(renderer.frame(screen, 25, 10))
    assert host.display[7] == " " * 25


def test_cursor_moves_between_cached_rows_without_leaving_underscores():
    renderer = Renderer(color=False)
    screen = screen_with("A\r\nB", columns=2, lines=2)
    screen.cursor.hidden = False
    screen.cursor.x = 0
    screen.cursor.y = 0
    host = host_with(renderer.frame(screen, 25, 18), rows=18)
    assert host.display[7].startswith("_____ ")
    screen.cursor.y = 1
    pyte.Stream(host).feed(renderer.frame(screen, 25, 18))
    assert host.display[7] == " " * 25
    assert host.display[15].startswith("_____ ")
    screen.cursor.hidden = True
    pyte.Stream(host).feed(renderer.frame(screen, 25, 18))
    assert host.display[7] == " " * 25
    assert host.display[15] == " " * 25
    assert "A" in "".join(host.display[:7])
    assert "B" in "".join(host.display[8:15])


def test_cached_rows_refresh_when_text_or_style_changes():
    renderer = Renderer()
    screen = screen_with("A\r\nB", columns=2, lines=2)
    host = host_with(renderer.frame(screen, 25, 18), rows=18)
    # Same letter, changed foreground and background: a data-only key is stale.
    pyte.Stream(screen).feed("\x1b[H\x1b[31;44;7mA")
    pyte.Stream(host).feed(renderer.frame(screen, 25, 18))
    assert host.buffer[0][1].fg == "red"
    assert host.buffer[0][1].bg == "blue"
    assert host.buffer[0][1].reverse is True
    pyte.Stream(screen).feed("\x1b[0m\x1b[2;1HC")
    pyte.Stream(host).feed(renderer.frame(screen, 25, 18))
    assert "C" in "".join(host.display[8:15])
    assert "B" not in "".join(host.display[8:15])


def test_reverse_blank_cells_remain_visible_without_color():
    screen = screen_with("\x1b[31;44;7m ", columns=1)
    host = host_with(Renderer(color=False).frame(screen, 25, 10))
    for y in range(8):
        for x in range(6):
            pixel = host.buffer[y][x]
            assert pixel.data == " "
            assert pixel.reverse is True
            assert (pixel.fg, pixel.bg) == ("default", "default")


def test_status_and_source_cannot_inject_terminal_sequences():
    screen = screen_with("A", columns=1)
    frame = Renderer().frame(screen, 25, 10, "ok\x1b]0;evil\x07\né")
    assert "\x1b]" not in frame
    host = host_with(frame)
    assert host.display[-1].startswith("ok?]0;evil??e")
    assert frame.isascii()


def test_ink_override_and_validation():
    host = host_with(Renderer(ink="#").frame(screen_with("A!"), 25, 10))
    assert "#" in "".join(host.display)
    assert not set("A!") & set("".join(host.display))
    for invalid in ("", "ab", "\x1b", "é", "\n"):
        with pytest.raises(ValueError):
            Renderer(ink=invalid)


def test_plain_render_supports_the_same_validated_ink_override():
    picture = render_plain(screen_with("A!"), ink="#")
    assert set(picture) == {"#", " ", "\n"}
    with pytest.raises(ValueError):
        render_plain(screen_with("A!"), ink="\x1b")


@pytest.mark.parametrize("columns, rows", [(0, 0), (1, 1), (3, 1), (1, 5)])
def test_tiny_terminals_do_not_crash_or_draw_outside_bounds(columns, rows):
    frame = Renderer().frame(screen_with("A", columns=1), columns, rows, "status")
    for row, col in re.findall(r"\x1b\[(\d+);(\d+)H", frame):
        assert 1 <= int(row) <= rows
        assert 1 <= int(col) < columns
