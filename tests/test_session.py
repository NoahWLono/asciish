"""Behavioral tests use actual pseudo-terminals, not a simulated subprocess."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest

from asciish.session import logical_size


def test_logical_size_accounts_for_glyphs_status_and_last_column():
    assert logical_size(481, 193) == (80, 24)
    assert logical_size(181, 57) == (30, 7)
    assert logical_size(1, 1) == (1, 1)


class OuterTerminal:
    def __init__(self, args, columns=181, rows=57, bootstrap=None):
        self.master, self.slave = pty.openpty()
        self.resize(columns, rows)
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
        entrypoint = ["-m", "asciish"] if bootstrap is None else ["-c", bootstrap]
        self.process = subprocess.Popen(
            [sys.executable, *entrypoint, *args],
            stdin=self.slave, stdout=self.slave, stderr=self.slave, env=env,
        )
        self.output = bytearray()

    def resize(self, columns, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        if hasattr(self, "process"):
            self.process.send_signal(signal.SIGWINCH)

    def pump(self, timeout=0.05):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError as error:
                if error.errno != errno.EIO:
                    raise

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if condition():
                return
        raise AssertionError(f"PTY condition timed out; exit={self.process.poll()}, tail={bytes(self.output[-300:])!r}")

    def finish(self, expected=0):
        self.wait_for(lambda: self.process.poll() is not None)
        while select.select([self.master], [], [], 0)[0]:
            self.pump(0)
        assert self.process.returncode == expected, bytes(self.output[-4000:])
        restored = termios.tcgetattr(self.slave)
        expected_attrs = self.before.copy()
        if sys.platform == "darwin":
            # Darwin sets this transient bit when TCSANOW enables ICANON.
            # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/tty.c#L1358-L1391
            restored[3] &= ~termios.PENDIN
            expected_attrs[3] &= ~termios.PENDIN
        assert restored == expected_attrs
        assert b"\x1b[?1049l" in self.output

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def outer():
    sessions = []
    def create(*args, **kwargs):
        session = OuterTerminal(*args, **kwargs)
        sessions.append(session)
        return session
    yield create
    for session in sessions:
        session.close()


def test_child_has_real_tty_correct_size_and_exit_code(outer, tmp_path):
    report = tmp_path / "tty.json"
    code = (
        "import os,json,sys; "
        "open(sys.argv[1],'w').write(json.dumps([os.isatty(0),os.isatty(1),list(os.get_terminal_size()),os.environ['TERM']])); "
        "print('HELLO'); sys.exit(7)"
    )
    terminal = outer(["--", sys.executable, "-c", code, str(report)])
    terminal.finish(7)
    assert json.loads(report.read_text()) == [True, True, [30, 7], "xterm-256color"]
    assert b"HHH" in terminal.output


def test_keyboard_interrupt_and_resize_reach_shell(outer, tmp_path):
    ready = tmp_path / "ready"
    result = tmp_path / "size"
    child_ready = tmp_path / "child-ready"
    interrupted = tmp_path / "interrupted"
    returned = tmp_path / "returned"
    child = tmp_path / "foreground.py"
    child.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def interrupt(signum, frame):\n"
        f"    Path({str(interrupted)!r}).write_text('SIGINT')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, interrupt)\n"
        f"Path({str(child_ready)!r}).write_text('ready')\n"
        "while True: time.sleep(60)\n"
    )
    terminal = outer(["--", "/bin/sh"])
    # Send before ENTER: startup must retain already queued terminal input.
    os.write(terminal.master, f"printf ready > '{ready}'\n".encode())
    terminal.wait_for(ready.exists)
    terminal.resize(241, 81)
    os.write(terminal.master, f"stty size > '{result}'\n".encode())
    terminal.wait_for(lambda: result.exists() and result.read_text().strip() == "10 40")
    command = f"{shlex.join([sys.executable, str(child)])}; printf returned > {shlex.quote(str(returned))}\n"
    os.write(terminal.master, command.encode())
    terminal.wait_for(child_ready.exists)
    os.write(terminal.master, b"\x03")
    terminal.wait_for(returned.exists)
    assert interrupted.read_text() == "SIGINT"
    os.write(terminal.master, b"exit 0\n")
    terminal.finish()


def test_emergency_exit_restores_terminal(outer):
    terminal = outer(["--", "/bin/sh"])
    terminal.wait_for(lambda: b"\x1b[?1049h" in terminal.output)
    os.write(terminal.master, b"\x1d")
    terminal.finish()


def test_missing_command_restores_terminal(outer):
    terminal = outer(["--", "/asciish-test/no-such-command"])
    terminal.finish(127)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_external_termination_restores_terminal(outer, signum):
    terminal = outer(["--", "/bin/sh"])
    terminal.wait_for(lambda: b"\x1b[?1049h" in terminal.output)
    terminal.process.send_signal(signum)
    terminal.finish(128 + signum)


def test_ctrl_z_fg_and_ctrl_c_preserve_shell_job_control(outer, tmp_path):
    ready = tmp_path / "ready"
    continued = tmp_path / "continued"
    returned = tmp_path / "returned"
    resumed_returned = tmp_path / "resumed-returned"
    child = tmp_path / "job.py"
    child.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        f"signal.signal(signal.SIGCONT, lambda *_: Path({str(continued)!r}).write_text('continued'))\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(60)\n"
    )
    terminal = outer(["--", "/bin/sh"])
    command = f"{shlex.join([sys.executable, str(child)])}; printf returned > {shlex.quote(str(returned))}\n"
    os.write(terminal.master, command.encode())
    terminal.wait_for(lambda: ready.exists() and ready.read_text().isdigit())
    pid = int(ready.read_text())
    os.write(terminal.master, b"\x1a")

    def stopped():
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return state.stdout.lstrip().startswith("T")

    terminal.wait_for(stopped)
    os.write(terminal.master, f"fg; printf returned > {shlex.quote(str(resumed_returned))}\n".encode())
    terminal.wait_for(continued.exists)
    os.write(terminal.master, b"\x03")
    terminal.wait_for(resumed_returned.exists)
    os.write(terminal.master, b"exit 0\n")
    terminal.finish()


def test_terminal_queries_receive_replies_through_the_pty(outer, tmp_path):
    report = tmp_path / "responses"
    code = (
        "import os,select,sys,time,tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'\\x1b[3;4H\\x1b[6n\\x1b[c\\x1b[>c\\x1b[5n')\n"
        "response = b''\n"
        "deadline = time.monotonic() + 5\n"
        "while not response.endswith(b'\\x1b[0n') and time.monotonic() < deadline:\n"
        "    if select.select([0], [], [], .1)[0]: response += os.read(0, 4096)\n"
        "open(sys.argv[1], 'wb').write(response)\n"
    )
    terminal = outer(["--", sys.executable, "-c", code, str(report)])
    terminal.finish()
    assert report.read_bytes() == b"\x1b[3;4R\x1b[?6c\x1b[>0;0;0c\x1b[0n"


def test_nested_interactive_shell_can_exit_to_parent(outer, tmp_path):
    nested = tmp_path / "nested"
    returned = tmp_path / "returned"
    terminal = outer(["--", "/bin/sh"])
    os.write(terminal.master, f"sh; printf returned > {shlex.quote(str(returned))}\n".encode())
    os.write(terminal.master, f"printf nested > {shlex.quote(str(nested))}\n".encode())
    terminal.wait_for(nested.exists)
    assert not returned.exists()
    os.write(terminal.master, b"exit 17\n")
    terminal.wait_for(returned.exists)
    os.write(terminal.master, b"exit 0\n")
    terminal.finish()


def test_child_modes_are_forwarded_then_cleaned_up(outer, tmp_path):
    ready = tmp_path / "ready"
    code = (
        "import os,sys,time\n"
        "os.write(1, b'\\x1b[?1h\\x1b[?2004h')\n"
        "open(sys.argv[1], 'w').write('ready')\n"
        "time.sleep(60)\n"
    )
    terminal = outer(["--", sys.executable, "-c", code, str(ready)])
    terminal.wait_for(lambda: ready.exists() and b"\x1b[?2004h" in terminal.output)
    assert b"\x1b[?1h" in terminal.output
    os.write(terminal.master, b"\x1d")
    terminal.finish()
    assert terminal.output.rfind(b"\x1b[?2004l") > terminal.output.rfind(b"\x1b[?2004h")
    assert terminal.output.rfind(b"\x1b[?1l") > terminal.output.rfind(b"\x1b[?1h")


def test_output_flood_drains_and_returns_exit_status(outer, tmp_path):
    done = tmp_path / "done"
    code = (
        "import os,sys\n"
        "for _ in range(128): os.write(1, b'X' * 4096)\n"
        "os.write(1, b'\\r\\nFINAL')\n"
        "open(sys.argv[1], 'w').write('done')\n"
        "sys.exit(9)\n"
    )
    terminal = outer(["--", sys.executable, "-c", code, str(done)])
    terminal.wait_for(done.exists, timeout=15)
    terminal.finish(9)
    assert b"FFF" in terminal.output


def test_signal_exit_status_is_propagated(outer):
    code = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    terminal = outer(["--", sys.executable, "-c", code])
    terminal.finish(128 + signal.SIGTERM)


def test_emergency_exit_reaps_a_child_ignoring_hangup_and_termination(outer, tmp_path):
    ready = tmp_path / "ready"
    code = (
        "import os,signal,sys,time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    terminal = outer(["--", sys.executable, "-c", code, str(ready)])
    terminal.wait_for(lambda: ready.exists() and ready.read_text().isdigit())
    pid = int(ready.read_text())
    os.write(terminal.master, b"\x1d")
    terminal.finish()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_control_d_reaches_child_as_terminal_eof(outer, tmp_path):
    ready = tmp_path / "ready"
    command = f"printf ready > {shlex.quote(str(ready))}; read answer"
    terminal = outer(["--", "/bin/sh", "-c", command])
    terminal.wait_for(ready.exists)
    os.write(terminal.master, b"\x04")
    terminal.finish(1)


def test_closed_child_stdio_does_not_change_child_exit_status(outer):
    code = "import os,time,sys; [os.close(fd) for fd in (0, 1, 2)]; time.sleep(.2); sys.exit(7)"
    terminal = outer(["--", sys.executable, "-c", code])
    terminal.finish(7)


def test_nonblocking_read_retries_when_readiness_changes(outer):
    bootstrap = (
        "import errno, os, sys\n"
        "from asciish.cli import main\n"
        "real_read = os.read\n"
        "injected = False\n"
        "def read(fd, count):\n"
        "    global injected\n"
        "    if fd > 2 and not injected:\n"
        "        injected = True\n"
        "        raise BlockingIOError(errno.EAGAIN, 'simulated readiness race')\n"
        "    return real_read(fd, count)\n"
        "os.read = read\n"
        "sys.exit(main())\n"
    )
    terminal = outer(
        ["--", sys.executable, "-c", "import sys; print('HELLO'); sys.exit(7)"],
        bootstrap=bootstrap,
    )
    terminal.finish(7)
    assert b"HHH" in terminal.output


@pytest.mark.parametrize(
    ("shell", "options"),
    [("bash", ["--noprofile", "--norc", "-i"]), ("fish", ["--no-config", "--interactive"])],
)
def test_available_interactive_shells_run_commands(outer, tmp_path, shell, options):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    report = tmp_path / "report"
    terminal = outer(["--", executable, *options])
    command = f"printf '%s' $ASCIISH > {shlex.quote(str(report))}; exit 7\n"
    os.write(terminal.master, command.encode())
    terminal.finish(7)
    assert report.read_text() == "1"
