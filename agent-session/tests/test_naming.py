from pathlib import Path

import pytest

from agent_session._naming import (
    SESSIONS_DIR,
    generate_session_key,
    make_session_dir,
    make_tmux_session_name,
)


class TestMakeTmuxSessionName:
    def test_basic_path(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "automation" / "agent-coord"
        start_dir.mkdir(parents=True)
        assert make_tmux_session_name(start_dir, dev_root) == "cc-automation-agent-coord"

    def test_custom_prefix(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "automation" / "agent-coord"
        start_dir.mkdir(parents=True)
        result = make_tmux_session_name(start_dir, dev_root, prefix="cx")
        assert result == "cx-automation-agent-coord"

    def test_with_worktree(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "myorg" / "myproj"
        start_dir.mkdir(parents=True)
        result = make_tmux_session_name(start_dir, dev_root, worktree="mybranch")
        assert result == "cc-myorg-myproj-worktree-mybranch"

    def test_dot_replaced(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "my.org" / "my.proj"
        start_dir.mkdir(parents=True)
        assert make_tmux_session_name(start_dir, dev_root) == "cc-my_org-my_proj"

    def test_colon_replaced(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "my:proj"
        start_dir.mkdir(parents=True)
        assert make_tmux_session_name(start_dir, dev_root) == "cc-my_proj"

    def test_with_session_key(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "automation" / "agent-coord"
        start_dir.mkdir(parents=True)
        result = make_tmux_session_name(start_dir, dev_root, session_key="a1b2c3d4")
        assert result == "cc-automation-agent-coord-a1b2c3d4"

    def test_with_worktree_and_session_key(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = dev_root / "myorg" / "myproj"
        start_dir.mkdir(parents=True)
        result = make_tmux_session_name(
            start_dir, dev_root, worktree="mybranch", session_key="deadbeef",
        )
        assert result == "cc-myorg-myproj-worktree-mybranch-deadbeef"

    def test_not_under_dev_root(self, tmp_path: Path) -> None:
        dev_root = tmp_path / "Development"
        start_dir = tmp_path / "other" / "project"
        start_dir.mkdir(parents=True)
        dev_root.mkdir(parents=True)
        with pytest.raises(ValueError, match="is not under dev root"):
            make_tmux_session_name(start_dir, dev_root)


class TestGenerateSessionKey:
    def test_length_and_hex(self) -> None:
        key = generate_session_key()
        assert len(key) == 32
        int(key, 16)  # raises ValueError if not valid hex

    def test_unique(self) -> None:
        keys = {generate_session_key() for _ in range(100)}
        assert len(keys) == 100


class TestMakeSessionDir:
    def test_basic(self) -> None:
        result = make_session_dir("abcd1234")
        assert result == SESSIONS_DIR / "abcd1234"
