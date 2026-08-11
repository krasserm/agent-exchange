"""Integration tests for TmuxManager against a real tmux server."""

import time
import uuid
from collections.abc import Callable
from pathlib import Path

import libtmux
import pytest

from agent_session._tmux import TmuxManager

# A pane application that records the raw bytes it receives, optionally
# requesting bracketed paste mode first. Byte-level assertions need terminal
# raw mode: under the default line discipline the tty rewrites CR to NL on the
# way in, which would hide exactly the CR-for-LF substitution these tests exist
# to catch.
_RAW_SINK = """\
import os, sys, termios, tty
out = open(sys.argv[1], "wb", buffering=0)
fd = sys.stdin.fileno()
tty.setraw(fd)
if sys.argv[2] == "bracketed":
    sys.stdout.write("\\x1b[?2004h")
    sys.stdout.flush()
while True:
    b = os.read(fd, 4096)
    if not b:
        break
    out.write(b)
"""


@pytest.fixture
def tmux_server() -> libtmux.Server:
    return libtmux.Server()


def _wait_for(
    path: Path, predicate: Callable[[bytes], bool], timeout: float = 5.0,
) -> bytes:
    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline:
        data = path.read_bytes() if path.exists() else b""
        if predicate(data):
            return data
        time.sleep(0.05)
    return data


@pytest.fixture
def sink(tmp_path: Path, tmux_server: libtmux.Server) -> tuple[TmuxManager, str, Path]:
    """A pane piping everything it receives to a file, via ``cat``.

    ``cat`` sits in the pty's canonical mode, so a line reaches the file only
    once Enter is sent -- which is exactly the two-phase send the session layer
    performs, and lets a test assert that text arrived at all. For assertions
    about the *bytes* delivered, use :func:`raw_sink` instead.
    """
    name = f"test-tmux-{uuid.uuid4().hex[:8]}"
    out = tmp_path / "typed.txt"
    tmux_server.new_session(
        session_name=name,
        start_directory=str(tmp_path),
        window_command=f"cat > {out}",
    )
    try:
        yield TmuxManager(), name, out
    finally:
        try:
            tmux_server.kill_session(name)
        except Exception:
            pass


@pytest.fixture
def raw_sink(
    tmp_path: Path, tmux_server: libtmux.Server,
) -> Callable[[bool], tuple[TmuxManager, str, Path]]:
    """Factory for a raw-mode pane recording the exact bytes it receives.

    The argument selects whether the pane's application requests bracketed
    paste mode, which is what decides whether tmux brackets a ``-p`` paste.
    """
    names: list[str] = []
    script = tmp_path / "raw_sink.py"
    script.write_text(_RAW_SINK)

    def _make(bracketed: bool) -> tuple[TmuxManager, str, Path]:
        name = f"test-tmux-raw-{uuid.uuid4().hex[:8]}"
        names.append(name)
        out = tmp_path / f"received-{name}.bin"
        mode = "bracketed" if bracketed else "plain"
        tmux_server.new_session(
            session_name=name,
            start_directory=str(tmp_path),
            window_command=f"python3 {script} {out} {mode}",
        )
        # The app must reach its read loop (and enable the mode) before the
        # paste arrives; a paste landing first would be silently unbracketed.
        _wait_for(out, lambda _: out.exists(), timeout=5.0)
        time.sleep(0.5)
        return TmuxManager(), name, out

    try:
        yield _make
    finally:
        for name in names:
            try:
                tmux_server.kill_session(name)
            except Exception:
                pass


def _typed(manager: TmuxManager, name: str, out: Path) -> str:
    manager.send_enter(name)
    return _wait_for(out, lambda d: d.endswith(b"\n")).decode()


class TestSendText:
    def test_plain_text(self, sink: tuple[TmuxManager, str, Path]) -> None:
        manager, name, out = sink
        manager.send_text(name, "hello test")
        assert _typed(manager, name, out) == "hello test\n"

    def test_leading_dash_is_not_read_as_a_flag(
        self, sink: tuple[TmuxManager, str, Path],
    ) -> None:
        # Sent as keys without `--`, tmux parses this as a flag cluster and
        # rejects it with "invalid flag"; libtmux swallows the non-zero exit,
        # so the message silently never reaches the agent.
        manager, name, out = sink
        manager.send_text(name, "- line one")
        assert _typed(manager, name, out) == "- line one\n"

    def test_bare_key_name_is_typed_literally(
        self, sink: tuple[TmuxManager, str, Path],
    ) -> None:
        # Sent as keys, tmux resolves "Up" to the arrow key and the pane
        # receives the escape sequence \033[A instead of the two characters.
        manager, name, out = sink
        manager.send_text(name, "Up")
        typed = _typed(manager, name, out)
        assert typed == "Up\n"
        assert "\x1b" not in typed

    def test_empty_text_is_a_noop(self, sink: tuple[TmuxManager, str, Path]) -> None:
        # set-buffer stores nothing for empty data, so a naive paste would fail
        # with "unknown buffer".
        manager, name, out = sink
        manager.send_text(name, "")
        assert _typed(manager, name, out) == "\n"

    def test_raises_on_unknown_session(self) -> None:
        manager = TmuxManager()
        with pytest.raises(RuntimeError, match="not found"):
            manager.send_text(f"test-tmux-missing-{uuid.uuid4().hex[:8]}", "hello")


class TestSendTextMultiline:
    def test_bracketed_when_the_app_requests_it(
        self, raw_sink: Callable[[bool], tuple[TmuxManager, str, Path]],
    ) -> None:
        # The point of the paste path: wrapped in bracket control codes, an
        # agent composer inserts the newline instead of reading it as submit,
        # so the whole message goes as one turn.
        manager, name, out = raw_sink(True)
        manager.send_text(name, "- line one\n- line two")
        received = _wait_for(out, lambda d: b"\x1b[201~" in d)
        assert received == b"\x1b[200~- line one\n- line two\x1b[201~"

    def test_unbracketed_fallback_still_preserves_linefeeds(
        self, raw_sink: Callable[[bool], tuple[TmuxManager, str, Path]],
    ) -> None:
        # Where the mode was never requested (a plain shell, or a proxy pane
        # whose client did not propagate it) the text must still arrive intact.
        # paste-buffer rewrites every LF to CR unless -r is given, and a CR is
        # Enter -- that default would submit each line separately.
        manager, name, out = raw_sink(False)
        manager.send_text(name, "line one\nline two")
        received = _wait_for(out, lambda d: b"line two" in d)
        assert received == b"line one\nline two"
        assert b"\r" not in received


class TestSendTextSurfacesTmuxFailures:
    """A failing tmux command must raise, not vanish.

    libtmux does not raise on a non-zero tmux exit, so before these checks a
    rejected command left the caller's Enter landing in an untouched composer:
    the send did nothing and reported success. Both legs are covered because
    they fail for different reasons -- ``set-buffer`` on the data, ``paste-buffer``
    on the target.
    """

    class _Result:
        def __init__(self, stderr: list[str]) -> None:
            self.stderr = stderr

    def _manager(
        self, monkeypatch: pytest.MonkeyPatch, *, set_buffer_err: list[str],
        paste_err: list[str],
    ) -> TmuxManager:
        mgr = TmuxManager()
        calls: list[str] = []

        class _Pane:
            def cmd(self_inner, *args: str, **kw: object):
                calls.append(args[0])
                return TestSendTextSurfacesTmuxFailures._Result(paste_err)

        class _Server:
            def cmd(self_inner, *args: str, **kw: object):
                calls.append(args[0])
                return TestSendTextSurfacesTmuxFailures._Result(set_buffer_err)

        monkeypatch.setattr(mgr, "_server", _Server())
        monkeypatch.setattr(mgr, "_get_active_pane", lambda name: _Pane())
        mgr._calls = calls  # type: ignore[attr-defined]
        return mgr

    def test_set_buffer_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mgr = self._manager(monkeypatch, set_buffer_err=["invalid flag"], paste_err=[])
        with pytest.raises(RuntimeError, match="set-buffer failed: invalid flag"):
            mgr.send_text("sess", "hello")
        # It must not go on to paste after the buffer was never stored.
        assert "paste-buffer" not in mgr._calls  # type: ignore[attr-defined]

    def test_paste_buffer_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mgr = self._manager(
            monkeypatch, set_buffer_err=[], paste_err=["no buffer asn-send-sess"]
        )
        with pytest.raises(RuntimeError, match="paste-buffer failed: no buffer"):
            mgr.send_text("sess", "hello")

    def test_success_path_raises_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mgr = self._manager(monkeypatch, set_buffer_err=[], paste_err=[])
        mgr.send_text("sess", "hello")
        assert mgr._calls == ["set-buffer", "paste-buffer"]  # type: ignore[attr-defined]
