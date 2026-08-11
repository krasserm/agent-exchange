import os
import uuid
from pathlib import Path

# Control-host state root (daemon files + per-session dirs). Overridable via
# ASN_HOME so a second daemon -- notably the e2e suite's -- works off its own
# state tree instead of the user's live one. Read once at import, like
# ASN_SOCKET_PATH in _daemon.py; the value is exported into every local session's
# launch script so the agent's hooks write their events under the same root.
DEFAULT_DAEMON_DIR = Path("~/.agent-session").expanduser()
DAEMON_DIR = Path(os.environ.get("ASN_HOME") or DEFAULT_DAEMON_DIR).expanduser()
SESSIONS_DIR = DAEMON_DIR / "sessions"
# Per-remote-host mirrors of each host's ~/.agent-session tree (one-way Mutagen
# replica). The daemon watches a remote session's events file under here exactly
# as it watches a local one under SESSIONS_DIR.
MIRRORS_DIR = DAEMON_DIR / "mirrors"


def sanitize_host(host: str) -> str:
    """Turn an ssh target into a filesystem-safe mirror dir name."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in host)


def tmux_safe(s: str) -> str:
    """Make a string safe for a tmux session name (no '.' or ':')."""
    return "".join(c if c.isalnum() or c == "-" else "_" for c in s)


def make_mirror_dir(host: str) -> Path:
    """Local mirror root for a remote host's ~/.agent-session tree."""
    return MIRRORS_DIR / sanitize_host(host)


def make_mirror_session_dir(host: str, session_key: str) -> Path:
    """Local mirror path of a remote session's per-key dir."""
    return make_mirror_dir(host) / "sessions" / session_key


def get_dev_root() -> Path:
    return Path(os.environ.get("AGENT_SESSION_DEV_ROOT", "~")).expanduser()


def generate_session_key() -> str:
    return uuid.uuid4().hex


def make_session_dir(session_key: str) -> Path:
    return SESSIONS_DIR / session_key


def make_tmux_session_name(
    start_dir: Path,
    dev_root: Path,
    worktree: str | None = None,
    session_key: str | None = None,
    prefix: str = "cc",
) -> str:
    resolved = start_dir.resolve()
    root = dev_root.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"'{resolved}' is not under dev root '{root}'")

    name = str(rel).replace("/", "-")
    name = name.replace(".", "_").replace(":", "_")
    name = f"{prefix}-{name}"

    if worktree is not None:
        name = f"{name}-worktree-{worktree}"

    if session_key is not None:
        name = f"{name}-{session_key}"

    return name
