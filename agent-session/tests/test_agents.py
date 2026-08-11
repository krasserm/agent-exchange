"""Unit tests for the pluggable agent registry and per-agent command building."""

from pathlib import Path

import pytest

from agent_session.agents import (
    DEFAULT_AGENT,
    LaunchSpec,
    Transport,
    agent_names,
    get_agent,
)
from agent_session.agents.claude import ClaudeAgent
from agent_session.agents.codex import CodexAgent


class TestRegistry:
    def test_default_is_claude(self) -> None:
        assert DEFAULT_AGENT == "claude"
        assert get_agent().name == "claude"

    def test_get_known_agents(self) -> None:
        assert get_agent("claude").name == "claude"
        assert get_agent("codex").name == "codex"

    def test_names_include_both(self) -> None:
        names = agent_names()
        assert "claude" in names
        assert "codex" in names

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown agent 'gemini'"):
            get_agent("gemini")


class TestClaudeAgent:
    def setup_method(self) -> None:
        self.agent = ClaudeAgent()

    def test_prefix_and_capabilities(self) -> None:
        assert self.agent.tmux_prefix == "cc"
        assert self.agent.supports_worktree
        assert self.agent.supports_chrome
        assert self.agent.supports_remote_control

    def test_container_credential_mounts(self) -> None:
        assert self.agent.container_config_dirname == ".claude-docker"
        mounts = self.agent.container_credential_mounts(
            config_dir_on_host="/h/.claude-docker", container_home="/home/coder",
        )
        assert "/h/.claude-docker:/home/coder/.claude" in mounts
        assert "/h/.claude-docker/.claude.json:/home/coder/.claude.json" in mounts

    def test_container_credential_readonly_overlay(self) -> None:
        mounts = self.agent.container_credential_mounts(
            config_dir_on_host="/h/.claude-docker",
            container_home="/home/coder",
            readonly=True,
        )
        assert (
            "/h/.claude-docker/.credentials.json:/home/coder/.claude/.credentials.json:ro"
            in mounts
        )

    def test_minimal_command(self) -> None:
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x")))
        assert cmd.startswith("unset CLAUDECODE && claude")
        assert "--plugin-dir" in cmd
        assert cmd.rstrip().endswith("plugin/claude")
        assert "--dangerously-skip-permissions" not in cmd

    def test_forces_inline_tui(self) -> None:
        # agent-session observes the agent purely through tmux capture/scrollback,
        # which only works when the TUI renders inline. A host/container claude
        # config with "tui": "fullscreen" would put the pane on the alternate
        # screen (no scrollback), so the launch overrides it unconditionally.
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x")))
        assert '--settings \'{"tui":"default"}\'' in cmd

    def test_all_flags(self) -> None:
        cmd = self._build(LaunchSpec(
            start_dir=Path("/tmp/x"),
            resume_session_id="abc-123",
            worktree="my-branch",
            skip_permissions=True,
            remote_control=True,
            remote_name="my-remote",
            chrome_access=True,
        ))
        assert "--resume abc-123" in cmd
        assert "--worktree my-branch" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--remote-control my-remote" in cmd
        assert "--chrome" in cmd
        assert "--plugin-dir" in cmd

    def test_remote_control_without_name(self) -> None:
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x"), remote_control=True))
        assert "--remote-control" in cmd
        assert "--remote-control " not in cmd.replace("--remote-control --", "")  # no dangling name

    def test_project_dir_worktree(self) -> None:
        # start_dir arrives normalized (AgentSession does that per transport), so
        # project_dir only hangs the worktree off it. /tmp is a symlink to
        # /private/tmp on macOS: a stray resolve() here would show up.
        # /tmp/x is in no repo, so the no-.git fallback keeps the old shape.
        start = Path("/tmp/x")
        assert self.agent.project_dir(start) == start
        assert self.agent.project_dir(start, "br") == start / ".claude" / "worktrees" / "br"

    def test_project_dir_worktree_at_repo_root(self, tmp_path: Path) -> None:
        """`claude --worktree` creates the worktree at the repo root, not the cwd.

        Verified empirically: launched from <repo>/sub/proj, claude created
        <repo>/.claude/worktrees/NAME. Anchoring on start_dir named a path git
        never created, so get_agent_info()'s existence check reported a live
        session as ended.
        """
        (tmp_path / ".git").mkdir()
        proj = tmp_path / "sub" / "proj"
        proj.mkdir(parents=True)
        assert (
            self.agent.project_dir(proj, "br")
            == tmp_path / ".claude" / "worktrees" / "br"
        )

    def test_project_dir_worktree_innermost_repo_wins(self, tmp_path: Path) -> None:
        """Nested repos resolve to the innermost one (~/Development is one too)."""
        (tmp_path / ".git").mkdir()
        inner = tmp_path / "inner"
        (inner / ".git").mkdir(parents=True)
        proj = inner / "sub"
        proj.mkdir()
        assert (
            self.agent.project_dir(proj, "br")
            == inner / ".claude" / "worktrees" / "br"
        )

    def test_project_dir_worktree_from_linked_worktree(self, tmp_path: Path) -> None:
        """Inside a linked worktree, the base is still the MAIN repo root.

        Verified: launched from <repo>/.claude/worktrees/probe1, claude created
        <repo>/.claude/worktrees/probe2 -- so the `.git` *file* must be followed
        to the main root rather than treated as a repo root itself.
        """
        main_root = tmp_path / "repo"
        (main_root / ".git" / "worktrees" / "wt1").mkdir(parents=True)
        linked = main_root / ".claude" / "worktrees" / "wt1"
        linked.mkdir(parents=True)
        (linked / ".git").write_text(
            f"gitdir: {main_root}/.git/worktrees/wt1\n"
        )
        assert (
            self.agent.project_dir(linked, "br")
            == main_root / ".claude" / "worktrees" / "br"
        )

    def test_project_dir_worktree_submodule_gitfile(self, tmp_path: Path) -> None:
        """A submodule's `.git` file points into .git/modules, not /worktrees."""
        parent = tmp_path / "parent"
        sub = parent / "sub"
        sub.mkdir(parents=True)
        (parent / ".git" / "modules" / "sub").mkdir(parents=True)
        (sub / ".git").write_text(f"gitdir: {parent}/.git/modules/sub\n")
        # No '/.git/worktrees/' segment to strip: the submodule checkout is its
        # own working tree, so it stays the base.
        assert (
            self.agent.project_dir(sub, "br")
            == sub / ".claude" / "worktrees" / "br"
        )

    def test_transcript_path(self) -> None:
        p = self.agent.transcript_path(Path("/Users/m/Development/proj"), "abc-1")
        assert p is not None
        assert p.name == "abc-1.jsonl"
        assert "projects" in str(p)

    def _build(self, spec: LaunchSpec) -> str:
        return self.agent.build_launch_command(
            spec, session_key="KEY", session_dir=Path("/tmp/sd"),
        )


class TestRemoteStartDirValidation:
    """A remote start_dir is interpreted by the *agent host*, not by us.

    Nothing local can expand ``~`` or anchor a relative path against the remote
    cwd, and every downstream consumer fails silently rather than loudly: docker
    turns a non-absolute ``-v`` source into a fresh empty *named volume*, and an
    unexpanded ``cd '~/proj'`` leaves the agent in ``$HOME``. Reject it up front.
    """

    REMOTE = Transport(location="remote", host="h")

    @pytest.mark.parametrize("agent", [ClaudeAgent(), CodexAgent()])
    @pytest.mark.parametrize("bad", ["rel/proj", "~/proj", "~", "."])
    def test_rejects_non_absolute_remote_start_dir(self, agent, bad: str) -> None:
        spec = LaunchSpec(start_dir=Path(bad), transport=self.REMOTE)
        with pytest.raises(ValueError, match="absolute"):
            agent.validate(spec)

    @pytest.mark.parametrize("agent", [ClaudeAgent(), CodexAgent()])
    def test_accepts_absolute_remote_start_dir(self, agent) -> None:
        agent.validate(LaunchSpec(start_dir=Path("/srv/proj"), transport=self.REMOTE))

    @pytest.mark.parametrize("agent", [ClaudeAgent(), CodexAgent()])
    def test_local_relative_start_dir_still_allowed(self, agent) -> None:
        """Local paths are resolved against this filesystem, so relative is fine."""
        agent.validate(LaunchSpec(start_dir=Path("rel/proj")))

    def test_tilde_error_mentions_the_tilde(self) -> None:
        """The tilde case is the confusing one; name it explicitly."""
        spec = LaunchSpec(start_dir=Path("~/proj"), transport=self.REMOTE)
        with pytest.raises(ValueError, match="~"):
            ClaudeAgent().validate(spec)


class TestCodexAgent:
    def setup_method(self) -> None:
        self.agent = CodexAgent()

    def test_prefix_and_capabilities(self) -> None:
        assert self.agent.tmux_prefix == "cx"
        assert not self.agent.supports_worktree
        assert not self.agent.supports_chrome
        assert not self.agent.supports_remote_control

    def test_minimal_command_uses_interactive_approval(self) -> None:
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x")))
        assert cmd.startswith("codex")
        assert "--ask-for-approval on-request" in cmd
        assert "--sandbox workspace-write" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    def test_command_omits_workdir_flag(self) -> None:
        # codex relies on the process cwd (set by the launcher) rather than -C.
        # Passing the host start_dir via -C breaks container/remote transports,
        # where that path does not exist in the runtime (see codex.py).
        cmd = self._build(LaunchSpec(start_dir=Path("/Users/me/Downloads")))
        assert "-C" not in cmd
        assert "/Users/me/Downloads" not in cmd

    def test_skip_permissions_bypasses(self) -> None:
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x"), skip_permissions=True))
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--ask-for-approval" not in cmd

    def test_container_bypasses_own_sandbox(self) -> None:
        # In a container the container is the sandbox boundary; codex's own
        # landlock/seccomp sandbox cannot init under --cap-drop ALL, so bypass.
        cmd = self._build(LaunchSpec(
            start_dir=Path("/tmp/x"),
            transport=Transport(runtime="container", container="cx-1"),
        ))
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--ask-for-approval" not in cmd

    def test_container_credential_mounts(self) -> None:
        assert self.agent.container_config_dirname == ".codex-docker"
        mounts = self.agent.container_credential_mounts(
            config_dir_on_host="/h/.codex-docker", container_home="/home/coder",
        )
        assert mounts == ["/h/.codex-docker:/home/coder/.codex"]

    def test_hooks_are_injected(self) -> None:
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x")))
        assert "--dangerously-bypass-hook-trust" in cmd
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
            assert f"hooks.{event}=" in cmd
        assert "log-event.sh" in cmd

    def test_forwards_full_env_to_commands(self) -> None:
        # codex must inherit the launch environment for executed commands (unlike
        # its default policy) so the `asn` message-plane shim on the launch PATH
        # is found and AGENT_SESSION_KEY/ASN_READ_TOKEN (matching codex's default
        # secret excludes) reach it -- mirroring claude's Bash tool. Without this,
        # every container-origin `asn` call fails "command not found".
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x")))
        assert 'shell_environment_policy.inherit="all"' in cmd
        assert "shell_environment_policy.ignore_default_excludes=true" in cmd

    def test_readiness_is_marker_based(self) -> None:
        # Codex emits no launch-time lifecycle event, so it must fall through to
        # the pane-marker readiness path rather than event-based readiness.
        assert self.agent.ready_event is None

    def test_ready_from_pane_detects_composer_chevron(self) -> None:
        pane = "  Tip: GPT-5.5 is now available in Codex.\n› Explain this codebase\n  gpt-5.5 · ~/workspace"
        assert self.agent.ready_from_pane(pane)

    def test_ready_from_pane_ignores_missing_banner(self) -> None:
        # The "OpenAI Codex" banner is often overwritten before capture; its
        # absence must not block readiness once the composer chevron is present.
        assert "OpenAI Codex" not in "› ready"
        assert self.agent.ready_from_pane("› ready")

    def test_ready_from_pane_waits_through_startup_modals(self) -> None:
        # Modals render the chevron on their selected item; readiness must hold
        # off until the modal is gone even though a chevron is on screen.
        trust = "Do you trust the files in this folder?\n› 1. Yes, proceed\n  2. No"
        update = "✨ Update available! 0.139.0 -> 0.142.5\n› 1. Update now\n  Press enter to continue"
        assert not self.agent.ready_from_pane(trust)
        assert not self.agent.ready_from_pane(update)

    def test_ready_from_pane_false_without_chevron(self) -> None:
        assert not self.agent.ready_from_pane("starting up, no prompt yet")

    def test_startup_prompts_dismiss_trust_and_update(self) -> None:
        prompts = dict(self.agent.startup_prompts)
        assert prompts["Do you trust"] == ("Enter",)
        # The update menu defaults to "Update now"; keys must navigate away
        # (Down) before confirming so an automated start never triggers it.
        update_keys = prompts["Update available"]
        assert update_keys[0] == "Down"
        assert update_keys[-1] == "Enter"

    def test_resume_uses_subcommand(self) -> None:
        cmd = self._build(LaunchSpec(start_dir=Path("/tmp/x"), resume_session_id="sess-9"))
        assert cmd.startswith("codex resume sess-9")

    def test_rejects_unsupported_features(self) -> None:
        for spec in (
            LaunchSpec(start_dir=Path("/tmp/x"), worktree="b"),
            LaunchSpec(start_dir=Path("/tmp/x"), chrome_access=True),
            LaunchSpec(start_dir=Path("/tmp/x"), remote_control=True),
        ):
            with pytest.raises(ValueError, match="does not support"):
                self.agent.validate(spec)

    def _build(self, spec: LaunchSpec) -> str:
        return self.agent.build_launch_command(
            spec, session_key="KEY", session_dir=Path("/tmp/sd"),
        )
