from __future__ import annotations

import shlex
from pathlib import Path

from agent_session.agents._base import Agent, LaunchSpec, Transport

# Codex lifecycle hooks we wire to the shared event logger. Codex uses the
# same event names and payload shape as Claude Code, so the same script and
# the same events.jsonl schema work for both.
_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "SubagentStart",
    "SubagentStop",
    "Stop",
)


class CodexAgent(Agent):
    """OpenAI Codex CLI."""

    name = "codex"
    tmux_prefix = "cx"
    supports_worktree = False
    supports_chrome = False
    supports_remote_control = False
    container_config_dirname = ".codex-docker"

    # Codex dispatches its SessionStart hook only once the first turn begins
    # (it is queued at session init and flushed from the turn loop), so there is
    # no launch-time event to wait on -- readiness is read from the TUI instead.
    ready_event = None
    # One-time startup modals to auto-dismiss:
    #  - trust dialog (first time hooks/config load in a dir): accept the
    #    default with Enter.
    #  - "update available" nag (outdated codex): its default option is
    #    "Update now", so navigate Down to "Skip" and confirm -- an automated
    #    start must neither run an update nor stall on the menu. Fires once, on
    #    the modal's first appearance, not the harmless post-skip info box.
    startup_prompts = (
        ("Do you trust", ("Enter",)),
        ("Update available", ("Down", "Enter")),
    )

    # Readiness marker: codex's composer draws a leading chevron once it can
    # accept input. Match that rather than the "OpenAI Codex" banner -- the
    # banner is codex's top render line and gets overwritten in place as async
    # startup notices load, so it is frequently absent from a pane capture,
    # whereas the composer chevron is always in the live frame. The chevron also
    # marks the selected item in codex's startup modals (the trust dialog and
    # the "update available" prompt), so treat the pane as ready only while no
    # such modal is present.
    _READY_CHEVRON = "›"  # U+203A single right-pointing angle quotation mark
    _STARTUP_MODALS = ("Do you trust", "Press enter to continue")

    def ready_from_pane(self, pane: str) -> bool:
        if any(modal in pane for modal in self._STARTUP_MODALS):
            return False
        return self._READY_CHEVRON in pane

    def build_launch_command(
        self,
        spec: LaunchSpec,
        *,
        session_key: str,
        session_dir: Path,
        plugin_dir: Path | None = None,
    ) -> str:
        plugin_root = plugin_dir if plugin_dir is not None else self.plugin_dir()
        log_script = plugin_root / "log-event.sh"

        parts = ["codex"]
        if spec.resume_session_id is not None:
            parts += ["resume", shlex.quote(spec.resume_session_id)]

        # No explicit workdir flag: codex runs in the process cwd, which the
        # launcher already sets to the project dir (tmux session cwd for
        # local-native; the inner launch script's `cd` for container/remote).
        # Passing `-C spec.start_dir` here would be wrong for those transports --
        # start_dir is a *host* path (e.g. /Users/.../Downloads) that does not
        # exist inside the container, so codex would exit before its TUI starts.
        # This mirrors claude, which likewise relies on cwd.

        # Approvals / sandbox. Inside a container the container itself is the
        # sandbox boundary (agent-docker runs with --cap-drop ALL /
        # no-new-privileges), so codex's own OS sandbox (landlock+seccomp) cannot
        # initialize and would block every tool. Bypass it there -- the mirror of
        # claude running in the container's baked-in auto mode.
        if spec.skip_permissions or spec.transport.runtime == "container":
            parts.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            parts += ["--ask-for-approval", "on-request", "--sandbox", "workspace-write"]

        # Message-plane env parity with claude. The inner launch script exports
        # PATH (with the plugin dir that holds the `asn` shim), AGENT_SESSION_KEY
        # (the shim's `from`/outbox derivation), and ASN_READ_ADDR/ASN_READ_TOKEN
        # (read-endpoint auth) into the agent process. Claude's Bash tool forwards
        # that full environment to the commands it runs, so `asn` resolves and
        # authenticates. Codex instead applies an environment policy that forwards
        # neither the launch PATH (so container-origin `asn` fails "command not
        # found") nor the message-plane vars -- AGENT_SESSION_KEY and
        # ASN_READ_TOKEN also match its default secret excludes. Inherit the full
        # launch environment, excludes off, so executed commands see the shim and
        # its key/token, restoring parity with claude.
        parts += ["-c", shlex.quote('shell_environment_policy.inherit="all"')]
        parts += ["-c", shlex.quote("shell_environment_policy.ignore_default_excludes=true")]

        # Lifecycle hooks: inject via config overrides and skip the trust prompt.
        parts.append("--dangerously-bypass-hook-trust")
        for event in _HOOK_EVENTS:
            command = f"{log_script} {event}"
            value = (
                f'hooks.{event}=[{{matcher="",hooks='
                f'[{{type="command",command="{command}"}}]}}]'
            )
            parts += ["-c", shlex.quote(value)]

        return " ".join(parts)

    def launch_shell_prelude(
        self, transport: Transport, *, plugin_root: Path
    ) -> list[str]:
        # Codex runs every shell-tool command through a *login* shell, which
        # re-sources the profile. The container image's /etc/profile assigns
        # PATH absolutely, wiping the plugin dir the inner launch script exported
        # -- so `asn` fails "command not found" (only in the container; native
        # profiles append to PATH, keeping the exported dir). The container's
        # ~/.profile does re-add ~/.local/bin to PATH after that reset, so link
        # the `asn` shim there to survive it. `inherit="all"` still carries the
        # message-plane vars (the profile touches only PATH), so this restores
        # full parity with claude's Bash tool.
        if transport.runtime != "container":
            return []
        shim = shlex.quote(str(plugin_root / "asn"))
        return [f'mkdir -p "$HOME/.local/bin" && ln -sf {shim} "$HOME/.local/bin/asn"']

    def container_credential_mounts(
        self,
        *,
        config_dir_on_host: str,
        container_home: str,
        readonly: bool = False,
    ) -> list[str]:
        # Codex keeps auth.json (and config.toml) inside ~/.codex, so a single
        # dir mount shares the host login; no separate file overlay is needed.
        return [f"{config_dir_on_host}:{container_home}/.codex"]

    def transcript_path(self, project_dir: Path, session_id: str) -> Path | None:
        # Codex reports the transcript path on its hook events; prefer that.
        return None
