"""Unit tests for the PTY bridge primitives in terminal.py."""

from __future__ import annotations

import fcntl
import os
import pty
import struct
import termios

import pytest

from agent_session_web import terminal


def test_set_winsize_applies_dimensions() -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        terminal._set_winsize(master_fd, rows=40, cols=120)
        packed = fcntl.ioctl(slave_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (rows, cols) == (40, 120)
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_grouped_member_name_is_unique_and_prefixed() -> None:
    key = "abcdef0123456789" * 2  # 32 chars
    a = terminal._grouped_member_name(key)
    b = terminal._grouped_member_name(key)
    assert a.startswith("agent-session-web-abcdef01-")
    assert a != b  # randomized suffix


async def test_resize_control_message_calls_set_winsize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[int, int, int]] = []

    def fake_set(fd: int, rows: int, cols: int) -> None:
        captured.append((fd, rows, cols))

    monkeypatch.setattr(terminal, "_set_winsize", fake_set)

    handled = terminal._handle_control_frame(
        master_fd=7, text='{"type":"resize","cols":100,"rows":30}'
    )
    assert handled is True
    assert captured == [(7, 30, 100)]


async def test_handle_control_frame_ignores_unknown() -> None:
    assert terminal._handle_control_frame(master_fd=7, text='{"type":"nope"}') is False
    assert terminal._handle_control_frame(master_fd=7, text="not json") is False


async def test_pty_write_drains_partial_writes() -> None:
    """A non-blocking master write should deliver all bytes even if partial."""
    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    try:
        data = b"hello world\n"
        await terminal._pty_write_all(master_fd, data)
        # Read it back from the slave side.
        os.set_blocking(slave_fd, False)
        received = b""
        for _ in range(100):
            try:
                chunk = os.read(slave_fd, 1024)
            except BlockingIOError:
                break
            if not chunk:
                break
            received += chunk
            if len(received) >= len(data):
                break
        assert b"hello world" in received
    finally:
        os.close(master_fd)
        os.close(slave_fd)
