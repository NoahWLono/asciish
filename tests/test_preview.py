from asciish.cli import main


def test_tabs_in_preview_do_not_discard_preceding_text(capsys):
    assert main(["--preview", "A\tB"]) == 0
    picture = capsys.readouterr().out
    assert "AAA" in picture
    assert "BBB" in picture
    assert len(picture.splitlines()[0]) == 9 * 6


def test_preview_treats_controls_as_visible_fallback_not_terminal_commands(capsys):
    assert main(["--preview", "A\x1b[2JB"]) == 0
    picture = capsys.readouterr().out
    assert "AAA" in picture
    assert "BBB" in picture
    assert "?" in picture
    assert "\x1b" not in picture
