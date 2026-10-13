"""Behavioral tests against the real byte parser and terminal buffers."""

import pytest

from asciish.terminal import Terminal


def test_split_sequences_and_utf8_share_one_persistent_parser():
    terminal = Terminal(10, 3)
    for fragment in (b"ab\x1b[", b"2;3H", b"\xc3", b"\xa9"):
        terminal.feed(fragment)
    assert terminal.screen.display == ["ab        ", "  é       ", "          "]
    assert (terminal.screen.cursor.x, terminal.screen.cursor.y) == (3, 1)


def test_cursor_movement_erase_and_scroll_are_emulated():
    terminal = Terminal(8, 3)
    terminal.feed(b"one\r\ntwo\r\nthree\r\nfour")
    assert terminal.screen.display == ["two     ", "three   ", "four    "]
    terminal.feed(b"\x1b[2;3H\x1b[KX")
    assert terminal.screen.display[1] == "thX     "
    terminal.feed(b"\x1b[2J\x1b[H!")
    assert terminal.screen.display == ["!       ", "        ", "        "]


def test_colors_and_attributes_are_kept_on_cells():
    terminal = Terminal(8, 2)
    terminal.feed(b"\x1b[1;31;44mR\x1b[38;2;12;34;56mT\x1b[0mN")
    red, truecolor, normal = [terminal.screen.buffer[0][x] for x in range(3)]
    assert (red.fg, red.bg, red.bold) == ("red", "blue", True)
    assert truecolor.fg == "0c2238"
    assert (normal.fg, normal.bg, normal.bold) == ("default", "default", False)


@pytest.mark.parametrize("mode", [47, 1047, 1049])
def test_alternate_buffer_restores_original_content_and_cursor(mode):
    terminal = Terminal(8, 3)
    terminal.feed(b"shell\x1b[2;4H\x1b[32m")
    terminal.feed(f"\x1b[?{mode}h".encode())
    assert terminal.alternate
    terminal.feed(b"\x1b[Heditor\x1b[3;2H")
    assert terminal.screen.display[0] == "editor  "
    terminal.feed(f"\x1b[?{mode}l".encode())
    assert not terminal.alternate
    assert terminal.screen.display == ["shell   ", "        ", "        "]
    assert (terminal.screen.cursor.x, terminal.screen.cursor.y) == (3, 1)
    terminal.feed(b"!")
    assert terminal.screen.buffer[1][3].fg == "green"


def test_repeated_1049_entry_does_not_clear_editor_or_overwrite_saved_state():
    terminal = Terminal(8, 2)
    terminal.feed(b"shell\x1b[?1049heditor\x1b[?1049h")
    assert terminal.screen.display[0] == "editor  "
    terminal.feed(b"\x1b[?1049l!\x1b[?1049l")
    assert terminal.screen.display[0] == "shell!  "
    assert terminal.screen.cursor.x == 6
    terminal.feed(b"\x1b[?1049h")
    assert terminal.screen.display == ["        ", "        "]


def test_47_preserves_alternate_contents_but_1047_exit_clears_them():
    terminal = Terminal(8, 2)
    terminal.feed(b"main\x1b[?47halt\x1b[?47l\x1b[?47h")
    assert terminal.screen.display[0] == "alt     "
    terminal.feed(b"\x1b[?1047l\x1b[?47h")
    assert terminal.screen.display == ["        ", "        "]


def test_modes_are_private_and_flags_survive_screen_switches():
    terminal = Terminal(8, 2)
    terminal.feed(b"\x1b[1;2004h")
    assert not terminal.application_cursor
    assert not terminal.bracketed_paste
    terminal.feed(b"\x1b[?1;2004;1049h")
    assert terminal.application_cursor
    assert terminal.bracketed_paste
    terminal.feed(b"\x1b[?1049l")
    assert terminal.application_cursor
    assert terminal.bracketed_paste
    terminal.feed(b"\x1b[?1;2004l")
    assert not terminal.application_cursor
    assert not terminal.bracketed_paste


def test_resize_affects_both_buffers_and_clamps_the_cursor():
    terminal = Terminal(10, 4)
    terminal.feed(b"\x1b[4;10HX\x1b[?1049h\x1b[4;10HY")
    terminal.resize(6, 2)
    assert (terminal.screen.columns, terminal.screen.lines) == (6, 2)
    assert (terminal.screen.cursor.x, terminal.screen.cursor.y) == (5, 1)
    terminal.feed(b"\x1b[?1049l")
    assert (terminal.screen.columns, terminal.screen.lines) == (6, 2)
    assert (terminal.screen.cursor.x, terminal.screen.cursor.y) == (5, 1)
    terminal.resize(12, 5)
    assert (len(terminal.screen.display), len(terminal.screen.display[0])) == (5, 12)


@pytest.mark.parametrize("columns,lines", [(0, 2), (2, 0), (-1, 2), (2, -1)])
def test_invalid_dimensions_are_rejected_on_create_and_resize(columns, lines):
    with pytest.raises(ValueError):
        Terminal(columns, lines)
    terminal = Terminal(8, 2)
    with pytest.raises(ValueError):
        terminal.resize(columns, lines)
    assert (terminal.screen.columns, terminal.screen.lines) == (8, 2)


def test_terminal_reset_clears_both_buffers_modes_and_saved_cursors():
    terminal = Terminal(8, 2)
    terminal.feed(b"main\x1b7\x1b[?1;2004;1049h\x1b[31meditor\x1bc")
    assert not terminal.alternate
    assert not terminal.application_cursor
    assert not terminal.bracketed_paste
    assert terminal.screen.display == ["        ", "        "]
    terminal.feed(b"\x1b8X")
    assert terminal.screen.display[0] == "X       "
    assert terminal.screen.buffer[0][0].fg == "default"
    terminal.feed(b"\x1b[?47h")
    assert terminal.screen.display == ["        ", "        "]


def test_queries_are_answered_to_the_child_and_drained_once(capsys):
    terminal = Terminal(8, 3)
    terminal.feed(b"\x1b[2;3H\x1b[5n\x1b[6n\x1b[?6n\x1b[c\x1b[>c")
    assert terminal.drain_responses() == b"\x1b[0n\x1b[2;3R\x1b[?2;3R\x1b[?6c\x1b[>0;0;0c"
    assert terminal.drain_responses() == b""
    assert terminal.screen.display == ["        "] * 3
    assert capsys.readouterr() == ("", "")


def test_split_secondary_query_retains_prefix_and_queries_use_active_screen():
    terminal = Terminal(8, 4)
    for fragment in (b"\x1b[>", b"0", b"c"):
        terminal.feed(fragment)
    assert terminal.drain_responses() == b"\x1b[>0;0;0c"
    terminal.feed(b"\x1b[?1049h\x1b[3;4H\x1b[6n")
    assert terminal.drain_responses() == b"\x1b[3;4R"
    terminal.feed(b"\x1b[?1049l\x1b[6n")
    assert terminal.drain_responses() == b"\x1b[1;1R"


def test_cursor_report_obeys_origin_mode_and_pending_wrap_position():
    terminal = Terminal(8, 5)
    terminal.feed(b"\x1b[2;4r\x1b[?6h\x1b[2;8HX\x1b[6n")
    assert terminal.drain_responses() == b"\x1b[2;8R"


def test_dec_g0_line_drawing_and_ascii_reset_in_utf8_mode():
    terminal = Terminal(16, 2)
    terminal.feed(b"\x1b(0lqqqk\x1b(Bhello")
    assert terminal.screen.display[0] == "┌───┐hello      "


def test_dec_g1_shift_controls_preserve_split_utf8_and_escape_sequences():
    terminal = Terminal(16, 2)
    for fragment in (b"\x1b", b")", b"0\x0e", b"lqqk", b"\xc3", b"\xa9", b"\x0f", b" q"):
        terminal.feed(fragment)
    assert terminal.screen.display[0] == "┌──┐é q         "


def test_charset_escapes_inside_title_do_not_change_text_charset():
    terminal = Terminal(8, 2)
    terminal.feed(b"\x1b]2;example\x1b(0\x07lqqk")
    assert terminal.screen.display[0] == "lqqk    "


def test_dec_charset_switch_does_not_disable_incremental_utf8_decoding():
    terminal = Terminal(12, 2)
    for fragment in (b"\x1b(0lq", b"\x1b(", b"B", b"caf\xc3", b"\xa9"):
        terminal.feed(fragment)
    assert terminal.screen.display[0] == "┌─café      "
