from __future__ import annotations

import logging
from pathlib import Path

import libtmux

logger = logging.getLogger(__name__)


class TmuxManager:
    """Thin wrapper around libtmux for tmux session management."""

    def __init__(self) -> None:
        self._server = libtmux.Server()

    def session_exists(self, name: str) -> bool:
        return self._server.has_session(name)

    def create_session(self, name: str, start_directory: Path) -> None:
        self._server.new_session(
            session_name=name,
            start_directory=str(start_directory),
        )

    def set_environment(self, session_name: str, key: str, value: str) -> None:
        session = self._get_session(session_name)
        session.set_environment(key, value)

    def send_command(self, session_name: str, command: str) -> None:
        pane = self._get_active_pane(session_name)
        pane.send_keys(command, enter=True)

    def send_text(self, session_name: str, text: str) -> None:
        """Deliver *text* into the pane verbatim, without submitting it.

        Sent as a tmux *paste buffer* rather than as keystrokes. Typed as keys,
        a multi-line message puts a bare LF mid-stream and an agent composer
        reads that as "submit": the first line leaves as its own turn and the
        remainder lands in a fresh composer. A bracketed paste is inserted as
        text, newlines and all.

        Each flag is load-bearing:

        ``-p`` wraps the paste in bracket control codes (``ESC[200~`` /
        ``ESC[201~``) *if the pane's application has requested bracketed paste
        mode* -- that request is what makes a composer insert the newlines
        instead of acting on them. Where it has not been requested (a plain
        shell, or a proxy pane whose client did not propagate the mode) tmux
        sends the text unbracketed, i.e. no worse than sending keys.

        ``-r`` suppresses paste-buffer's default of rewriting every LF to CR
        -- that default submits each line, the very failure being avoided.

        ``-d`` drops the buffer afterwards, so message text does not
        accumulate in the tmux buffer stack.

        ``--`` keeps tmux from reading a message that opens with ``-`` as a
        flag cluster, which it rejects outright ("invalid flag"). libtmux does
        not raise on a non-zero tmux exit, so such a message previously
        vanished with no error anywhere: the caller's Enter landed in an
        untouched composer and the send silently did nothing. The stderr
        checks close that silent-failure class in general.

        ``set-buffer`` runs on the server, not the pane: its ``-t`` selects a
        target *client*, so libtmux's automatic pane ``-t`` injection would
        misbind it. The buffer is named per tmux session so that concurrent
        sends to different sessions cannot clobber one another.
        """
        pane = self._get_active_pane(session_name)
        if not text:
            # set-buffer stores nothing for empty data, leaving paste-buffer to
            # fail with "unknown buffer"; there is nothing to type anyway.
            return
        buffer_name = f"asn-send-{session_name}"
        result = self._server.cmd("set-buffer", "-b", buffer_name, "--", text)
        if result.stderr:
            raise RuntimeError(f"tmux set-buffer failed: {' '.join(result.stderr)}")
        result = pane.cmd("paste-buffer", "-d", "-p", "-r", "-b", buffer_name)
        if result.stderr:
            raise RuntimeError(f"tmux paste-buffer failed: {' '.join(result.stderr)}")

    def send_enter(self, session_name: str) -> None:
        pane = self._get_active_pane(session_name)
        pane.send_keys("", enter=True)

    def send_key(self, session_name: str, key: str) -> None:
        """Send a single named tmux key (e.g. ``Down``, ``Enter``, ``Up``).

        Unlike ``send_text`` this is interpreted by tmux as a key name, so it
        can drive TUI menus (arrow navigation, Enter to confirm). For a proxied
        session the key forwards through the local pane into the remote/container
        tmux to the agent.
        """
        pane = self._get_active_pane(session_name)
        pane.cmd("send-keys", key)

    def kill_session(self, session_name: str) -> None:
        try:
            self._server.kill_session(session_name)
        except Exception:
            logger.debug("kill_session: session '%s' already gone", session_name)

    def capture_pane(self, session_name: str, lines: int | None = 50) -> str:
        """Capture the pane's content, including scrollback.

        ``lines`` bounds the capture to the last N lines of history; ``None``
        returns the full scrollback (``-S -``, from the start of history).
        """
        pane = self._get_active_pane(session_name)
        if lines is None:
            result = pane.cmd("capture-pane", "-p", "-S", "-")
            return "\n".join(result.stdout)
        output = pane.capture_pane(start=-lines)
        return "\n".join(output)

    def capture_visible(self, session_name: str) -> str:
        """Capture only the pane's live visible frame (current screen).

        Unlike :meth:`capture_pane`, this never reaches into scrollback: it
        returns exactly what is on screen right now (``capture-pane -p`` with no
        ``-S``). Readiness/modal detection uses this so that text from a
        *dismissed* startup modal lingering in an inline agent's scrollback
        (codex 0.142.5 renders inline, alternate_on=0, keeping normal-buffer
        history) does not keep matching after the live frame has moved on to the
        composer. For an alternate-screen agent the alt buffer has no scrollback,
        so the visible frame is all there is -- this is a no-op difference there.
        """
        pane = self._get_active_pane(session_name)
        return "\n".join(pane.cmd("capture-pane", "-p").stdout)

    def _get_session(self, name: str) -> libtmux.Session:
        session = self._server.sessions.get(session_name=name, default=None)
        if session is None:
            raise RuntimeError(f"tmux session '{name}' not found")
        return session

    def _get_active_pane(self, name: str) -> libtmux.Pane:
        session = self._get_session(name)
        pane = session.active_pane
        if pane is None:
            raise RuntimeError(f"No active pane in session '{name}'")
        return pane
