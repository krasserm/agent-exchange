"""Per-host Mutagen mirror of a remote agent host's ~/.agent-session tree.

A remote session's hook writes events to the *agent host's*
``~/.agent-session`` (directly for a native host, via the bind mount for a
container). The daemon never reaches across the network to read them; instead a
single one-way Mutagen replica per host mirrors that tree into a local
per-host directory, and the daemon's ``EventMonitor`` watches the mirror exactly
as it watches a local session. Mutagen owns the hard parts -- auto-reconnect,
resync-after-outage, atomic staged writes -- so a flaky link never loses events
or corrupts the local file (proposal section 6).

One sync session per host covers *all* that host's sessions (native and
containerized) at once. Sync names are ``asn-<sanitized-host>``, plus a
state-root discriminator when the root is not the default one (see
:func:`_state_root_suffix`).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess

from agent_session import _naming

logger = logging.getLogger(__name__)

# Seconds between Mutagen polls of the remote ~/.agent-session tree. Small tree,
# so a tight interval gives low event latency at negligible cost.
MIRROR_POLL_INTERVAL = 1


def _state_root_suffix() -> str:
    """Per-state-root discriminator, empty for the default root.

    The sync replicates into a mirror dir *under the state root*, so the name has
    to name the root too. Without this, two daemons on one machine -- the user's
    and the test suite's, which moves its root with ``ASN_HOME`` -- derive the
    same name, and :func:`ensure_host_mirror` reuses a sync by name alone: the
    second daemon adopts the first's sync, which replicates into the *other*
    root's mirror dir, and then watches its own dir that nothing writes to. Every
    remote session there times out with no event ever arriving, and in the other
    direction the live daemon starts writing into the test tree.

    The default root deliberately keeps the bare historical name so syncs created
    before this existed are still recognized and reused rather than orphaned
    alongside a duplicate.
    """
    if _naming.DAEMON_DIR == _naming.DEFAULT_DAEMON_DIR:
        return ""
    digest = hashlib.sha256(str(_naming.DAEMON_DIR).encode()).hexdigest()
    return f"-{digest[:8]}"


def _sync_name(host: str) -> str:
    # Mutagen session names must be alphanumeric + hyphen (no dots, '@', etc.),
    # so build the name from a stricter sanitization than the mirror *directory*
    # name (which may keep dots). e.g. "192.168.94.50" -> "asn-192-168-94-50".
    safe = "".join(c if c.isalnum() else "-" for c in host)
    return f"asn-{safe}{_state_root_suffix()}"


class MutagenError(RuntimeError):
    """A mutagen command failed or mutagen is unavailable."""


def _mutagen(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["mutagen", *args], capture_output=True)
    except FileNotFoundError as e:
        raise MutagenError(
            "mutagen not found; install with "
            "'brew install mutagen-io/mutagen/mutagen'"
        ) from e
    if check and proc.returncode != 0:
        raise MutagenError(
            f"mutagen {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')}"
        )
    return proc


def mirror_exists(host: str) -> bool:
    name = _sync_name(host)
    proc = _mutagen("sync", "list", name, check=False)
    return proc.returncode == 0


def ensure_host_mirror(host: str) -> "object":
    """Create the one-way replica for *host* if absent; return the mirror dir.

    Idempotent: an existing ``asn-<host>`` sync is reused. The mirror root is
    created locally; Mutagen replicates the host's ``~/.agent-session`` into it.
    """
    mirror_dir = _naming.make_mirror_dir(host)
    mirror_dir.mkdir(parents=True, exist_ok=True)
    if mirror_exists(host):
        return mirror_dir
    _mutagen(
        "sync", "create",
        "--name", _sync_name(host),
        "--sync-mode", "one-way-replica",
        # Force polling on the remote (alpha) endpoint: when the agent host is
        # macOS running Docker Desktop, a container's writes reach the bind-mount
        # source through virtiofs but do *not* reliably fire FSEvents, so an
        # event-based watch misses appends after the first scan. Polling makes
        # delivery deterministic (~poll-interval latency) on every host; the
        # ~/.agent-session tree is tiny, so the cost is negligible.
        "--watch-mode-alpha", "force-poll",
        "--watch-polling-interval-alpha", str(MIRROR_POLL_INTERVAL),
        f"{host}:.agent-session",
        str(mirror_dir),
    )
    return mirror_dir


def teardown_host_mirror(host: str) -> None:
    """Terminate the host's mirror sync (best-effort)."""
    try:
        _mutagen("sync", "terminate", _sync_name(host), check=False)
    except MutagenError:
        logger.warning("failed to terminate mutagen mirror for %s", host)
