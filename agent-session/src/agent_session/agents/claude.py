from __future__ import annotations

from pathlib import Path

from agent_session.agents._base import Agent, LaunchSpec


def _main_root_from_gitfile(git_file: Path) -> Path | None:
    """Resolve a ``.git`` *file* to the main worktree root, or None.

    A linked worktree's ``.git`` is a file holding ``gitdir: <main>/.git/
    worktrees/<name>``, so the main root is the path before that suffix. A
    submodule's pointer names ``.git/modules/...`` instead and has no such
    suffix; it is its own working tree, so None (keep the containing dir).
    """
    try:
        content = git_file.read_text()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        # Some setups record the pointer relative to the worktree.
        gitdir = git_file.parent / gitdir
    parts = gitdir.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".git" and parts[i + 1] == "worktrees":
            return Path(*parts[:i]) if i else None
    return None


def _worktree_base(start_dir: Path) -> Path:
    """Directory ``claude --worktree`` hangs ``.claude/worktrees/`` off.

    Verified empirically: claude creates the worktree at the **main repo root**,
    not relative to the launch directory -- from ``<repo>/sub/proj`` it created
    ``<repo>/.claude/worktrees/NAME``, and from inside a linked worktree it
    still used the main root. So walk up to the nearest ancestor holding a
    ``.git`` entry (innermost wins: nested repos are normal here), following a
    ``.git`` *file* to the main root. Outside any repo, fall back to
    *start_dir*: claude will refuse to create a worktree there anyway.

    Pure path logic, no subprocess: the answer must be derivable in the unit
    tests and cheap enough for a property that is read on every status call.
    """
    for candidate in (start_dir, *start_dir.parents):
        git = candidate / ".git"
        if not git.exists():
            continue
        if git.is_dir():
            return candidate
        return _main_root_from_gitfile(git) or candidate
    return start_dir


class ClaudeAgent(Agent):
    """Anthropic Claude Code CLI."""

    name = "claude"
    tmux_prefix = "cc"
    supports_worktree = True
    supports_chrome = True
    supports_remote_control = True
    container_config_dirname = ".claude-docker"

    # Startup confirmations a fresh claude config raises before it is ready:
    #  - "Bypass Permissions mode" (only with --dangerously-skip-permissions):
    #    default is "No, exit", so move to "Yes, I accept" (Down) then confirm.
    #  - The trust-folder "Quick safety check" prompt on first visit to a dir
    #    (shown unless the config runs in auto mode): default is already
    #    "Yes, I trust this folder", so a plain Enter accepts.
    # The session opened this directory deliberately, so trusting it is correct.
    startup_prompts = (
        ("Bypass Permissions mode", ("Down", "Enter")),
        ("Quick safety check", ("Enter",)),
    )

    def build_launch_command(
        self,
        spec: LaunchSpec,
        *,
        session_key: str,
        session_dir: Path,
        plugin_dir: Path | None = None,
    ) -> str:
        plugin_root = plugin_dir if plugin_dir is not None else self.plugin_dir()
        parts = ["unset CLAUDECODE && claude"]

        if spec.resume_session_id is not None:
            parts.append(f"--resume {spec.resume_session_id}")
        if spec.worktree is not None:
            parts.append(f"--worktree {spec.worktree}")
        if spec.skip_permissions:
            parts.append("--dangerously-skip-permissions")
        if spec.remote_control:
            if spec.remote_name is not None:
                parts.append(f"--remote-control {spec.remote_name}")
            else:
                parts.append("--remote-control")
        if spec.chrome_access:
            parts.append("--chrome")

        # Force the non-fullscreen TUI regardless of the host/container claude
        # config. A config with "tui": "fullscreen" renders on the terminal's
        # alternate screen, which has no scrollback -- breaking both interactive
        # scrollback of the (proxied) tmux pane and the `capture-pane -S -`
        # history read. "default" is the inline mode (the only valid values are
        # "default" and "fullscreen"); --settings JSON overrides settings.json.
        parts.append('--settings \'{"tui":"default"}\'')

        parts.append(f"--plugin-dir {plugin_root / 'claude'}")
        return " ".join(parts)

    def container_credential_mounts(
        self,
        *,
        config_dir_on_host: str,
        container_home: str,
        readonly: bool = False,
    ) -> list[str]:
        # ``~/.claude-docker`` holds the OAuth token + settings (mounted at
        # ~/.claude); ``.claude.json`` (theme/preferences) sits beside it and is
        # mounted separately since it lives at ~/.claude.json, not inside ~/.claude.
        mounts = [
            f"{config_dir_on_host}:{container_home}/.claude",
            f"{config_dir_on_host}/.claude.json:{container_home}/.claude.json",
        ]
        if readonly:
            mounts.append(
                f"{config_dir_on_host}/.credentials.json:"
                f"{container_home}/.claude/.credentials.json:ro"
            )
        return mounts

    def prepare_container_config(self, config_dir_on_host: Path) -> None:
        # A missing .claude.json would otherwise bind-mount as a directory.
        config_dir_on_host.mkdir(parents=True, exist_ok=True)
        prefs = config_dir_on_host / ".claude.json"
        if not prefs.exists() or prefs.stat().st_size == 0:
            prefs.write_text("{}")

    def project_dir(
        self, start_dir: Path, worktree: str | None = None, *, local: bool = True
    ) -> Path:
        if worktree is None:
            return start_dir
        # Only a local *start_dir* can be walked: for an agent running elsewhere
        # the path belongs to another filesystem, and a control-host ancestor
        # that happens to exist under the same name is not evidence about it.
        base = _worktree_base(start_dir) if local else start_dir
        return base / ".claude" / "worktrees" / worktree

    def transcript_path(self, project_dir: Path, session_id: str) -> Path | None:
        resolved = project_dir.resolve()
        dir_name = "-" + str(resolved).replace("/", "-").replace(".", "-").lstrip("-")
        return Path.home() / ".claude" / "projects" / dir_name / f"{session_id}.jsonl"
