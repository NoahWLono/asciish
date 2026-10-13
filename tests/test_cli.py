"""CLI behavior is checked at the installed module boundary."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


def cli(*args, **kwargs):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    return subprocess.run(
        [sys.executable, "-m", "asciish", *args],
        capture_output=True, text=True, env=env, timeout=10, **kwargs,
    )


def test_help_and_version_do_not_require_a_terminal():
    help_result = cli("--help")
    assert help_result.returncode == 0
    assert "--preview" in help_result.stdout
    version = cli("--version")
    assert version.returncode == 0
    assert version.stdout.startswith("asciish ")


def test_interactive_mode_rejects_piped_stdio_cleanly():
    result = cli("--", "/bin/sh")
    assert result.returncode == 1
    assert "needs a terminal" in result.stderr
    assert "Traceback" not in result.stderr


def test_preview_is_ascii_and_supports_custom_ink():
    result = cli("--ink", "#", "--preview", "Hi\nthere")
    assert result.returncode == 0
    assert set(result.stdout) <= {"#", " ", "\n"}
    assert "#" in result.stdout
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize("ink", ["", "ab", " ", "\t", "é"])
def test_invalid_ink_is_an_argument_error(ink):
    result = cli("--ink", ink, "--preview", "A")
    assert result.returncode == 2
    assert "one visible ASCII character" in result.stderr


@pytest.mark.parametrize("preview", ["A" * 1001, "A\n" * 1001])
def test_preview_rejects_unbounded_geometry(preview):
    result = cli("--preview", preview)
    assert result.returncode == 2
    assert "1000 columns and 1000 lines" in result.stderr
