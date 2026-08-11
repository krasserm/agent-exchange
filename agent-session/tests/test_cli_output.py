"""Unit tests for CLI output formatting (no daemon, no I/O)."""

from __future__ import annotations

import json
import os

import pytest

from agent_session import cli


class TestStartOutput:
    result = {
        "session_key": "a" * 32,
        "tmux_session_name": "agent-ses0",
        "agent": "codex",
    }

    def test_json_option_is_accepted(self) -> None:
        args = cli._build_parser().parse_args(["start", "/project", "--json"])
        assert args.as_json is True

    def test_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_start_result(self.result, as_json=True)

        assert json.loads(capsys.readouterr().out) == self.result

    def test_human_output_is_unchanged(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_start_result(self.result, as_json=False)

        assert capsys.readouterr().out == (
            f"Started codex session {'a' * 32}\n"
            "Tmux: agent-ses0\n"
        )


class TestFormatDir:
    """The DIR cell must not describe a remote path in local terms.

    ``asn list`` shortened any ``start_dir`` beginning with the *control host's*
    ``$HOME`` to ``~/...``. For a remote session that path belongs to another
    machine, so the tilde silently reattributes it to this one -- and where the
    two homes share a prefix (both ``/Users/martin``, or both ``/home/x``) the
    output is indistinguishable from a local session in the same directory. That
    is the misreading 539cc6c set out to stop.
    """

    def test_local_path_is_shortened(self) -> None:
        home = os.path.expanduser("~")
        assert cli._format_dir(f"{home}/proj", None) == "~/proj"

    def test_remote_path_keeps_its_own_shape(self) -> None:
        home = os.path.expanduser("~")
        got = cli._format_dir(f"{home}/proj", "192.168.94.50")
        assert "~" not in got
        assert got == f"192.168.94.50:{home}/proj"

    def test_remote_path_is_labelled_with_its_host(self) -> None:
        assert cli._format_dir("/srv/proj", "linbox") == "linbox:/srv/proj"

    def test_local_and_remote_same_path_are_distinguishable(self) -> None:
        """The concrete hazard: identical paths on two different machines."""
        home = os.path.expanduser("~")
        local = cli._format_dir(f"{home}/proj", None)
        remote = cli._format_dir(f"{home}/proj", "192.168.94.51")
        assert local != remote

    @pytest.mark.parametrize("host", [None, "192.168.94.50"])
    def test_long_paths_are_truncated_from_the_left(self, host: str | None) -> None:
        """Truncation keeps the tail: the leaf directory is the identifying part."""
        got = cli._format_dir("/very/long/" + "x" * 80 + "/leaf", host, width=34)
        assert len(got) <= 34
        assert got.startswith("...")
        assert got.endswith("leaf")

    def test_missing_dir_is_empty_not_invented(self) -> None:
        assert cli._format_dir("", None) == ""
