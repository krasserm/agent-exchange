"""Check and prepare a machine to run remote/container agent sessions.

`asn start --host H [--docker]` assumes the target already has its prerequisites
(tmux/jq/agent CLI, or docker + the ``agent-docker`` image, plus auth). This
module is the *check + prepare* path behind ``asn prep``: it probes those
prerequisites and, for the two things it can fix correctly and reversibly over
SSH, fixes them -- deploying the hook plugin (idempotent, via
:func:`_provision.ensure_plugin`) and **building the ``agent-docker`` image
natively on the agent host** when it is missing or its content hash is stale.

Everything else (missing system binaries, agent auth, remote sshd
``GatewayPorts``, Mutagen on the control host) is *diagnosed only* -- reported
with an exact remediation command, never mutated, since it needs root, an
interactive OAuth, or an sshd edit.

Design mirrors :mod:`_provision`: pure helpers (hashing, argv construction,
state decisions) are unit-testable without I/O; the ``ssh``/``docker`` shell-outs
are plain :mod:`subprocess` calls, and the CLI wraps the top-level entry points in
``asyncio.to_thread`` so they never block the event loop. Prep runs *client-side*
(not through the daemon): it holds no session state and a remote image build can
take minutes, which must not stall the single-threaded session registry.

The image-build context is the single, self-contained ``agent-docker/Dockerfile``
(its only ``COPY`` is ``--from`` a registry image, so it builds from an *empty*
context). That Dockerfile is streamed on stdin to ``docker build -`` -- the same
transport shape as the plugin deploy -- so no context tarball is shipped, and
because the build runs on the host it is arch-correct by construction.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from agent_session import _provision
from agent_session._provision import ProvisionError
from agent_session.agents import Transport, get_agent

# PATH the agent host actually gets from the launch script; prepended to remote
# probe commands so a login-but-non-interactive SSH shell (zsh/macOS skips
# ~/.zshrc) still finds ~/.local/bin and Homebrew binaries. Single source of
# truth shared with the launch script and the e2e preflight.
PATH_PREFIX = _provision.REMOTE_PATH_PREFIX

# Image label carrying the build's content hash, for staleness detection. Set via
# ``docker build --label`` (not a Dockerfile ``LABEL`` line) so the committed
# Dockerfile stays clean and the hash stays authoritative.
IMAGE_HASH_LABEL = "agent-session.context-hash"


class Status(str, Enum):
    OK = "ok"          # prerequisite already satisfied
    FIXED = "fixed"    # prep just satisfied it (plugin deployed, image built)
    WARN = "warn"      # non-blocking: session starts, some feature degraded
    FAIL = "fail"      # blocking: a real session cannot start until fixed


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    remediation: str | None = None


@dataclass
class PrepReport:
    transport_label: str
    checks: list[Check] = field(default_factory=list)

    def add(
        self, name: str, status: Status, detail: str = "", remediation: str | None = None
    ) -> None:
        self.checks.append(Check(name, status, detail, remediation))

    @property
    def ready(self) -> bool:
        """True iff no check is blocking (warnings do not block)."""
        return not any(c.status is Status.FAIL for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "target": self.transport_label,
            "ready": self.ready,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "detail": c.detail,
                    "remediation": c.remediation,
                }
                for c in self.checks
            ],
        }


@dataclass(frozen=True)
class BuildFacts:
    """Host facts that determine the image build (and its content hash)."""

    uname: str  # "Darwin" | "Linux"
    uid: int
    gid: int


# --- low-level probes (shared with the e2e preflight) -----------------------


def _local(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _remote_login(
    host: str, snippet: str, timeout: int = 20
) -> subprocess.CompletedProcess | None:
    """Run *snippet* on *host* through a login shell (so PATH resolves)."""
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
             f"$SHELL -lc {shlex.quote(snippet)}"],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _remote_str(host: str, snippet: str) -> str:
    r = _remote_login(host, snippet)
    if r is None or r.returncode != 0:
        raise ProvisionError(f"remote command failed on {host}: {snippet}")
    return r.stdout.decode().strip()


def ssh_ok(host: str) -> bool:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def mutagen_ok() -> bool:
    """Mutagen present on the *control* host (needed for any remote mirror)."""
    return shutil.which("mutagen") is not None


def command_on_path(cmd: str, host: str | None) -> bool:
    if host is None:
        return shutil.which(cmd) is not None
    r = _remote_login(host, f"PATH={PATH_PREFIX}:$PATH command -v {shlex.quote(cmd)}")
    return r is not None and r.returncode == 0


def docker_ok(host: str | None) -> bool:
    if host is None:
        r = _local(["docker", "info"])
        return r is not None and r.returncode == 0
    r = _remote_login(host, f"PATH={PATH_PREFIX}:$PATH docker info")
    return r is not None and r.returncode == 0


def image_id(image: str, host: str | None) -> str | None:
    """Resolve an image reference to its local image ID, or None if absent.

    Resolution goes through ``docker image ls -q`` rather than ``docker image
    inspect``, because on docker's containerd image store
    (``io.containerd.snapshotter.v1``, seen on docker 29.2.0) ``inspect`` does
    not apply the implicit ``docker.io/library/...:latest`` normalization to a
    short name: ``docker image inspect agent-docker`` reports "No such image"
    while the image is listed and ``docker run agent-docker`` starts it. The
    listing resolves short names on both stores, and it answers the question the
    callers actually have -- whether ``docker run`` will find the image locally.

    Using an image (a ``run``, a ``tag``) materializes the index entry that
    ``inspect`` wants, so the gap closes after first use. That leaves it hitting
    exactly the fresh-host window that ``asn prep`` and the container preflight
    exist to check.
    """
    argv = ["docker", "image", "ls", "-q", image]
    if host is None:
        r = _local(argv)
    else:
        r = _remote_login(host, f"PATH={PATH_PREFIX}:$PATH {shlex.join(argv)}")
    if r is None or r.returncode != 0:
        return None
    # Absent is an empty listing with a zero exit, not a failure.
    first = r.stdout.decode().strip().splitlines()
    return first[0] if first else None


def image_present(image: str, host: str | None) -> bool:
    return image_id(image, host) is not None


def agent_cli_ok(agent_name: str, host: str | None) -> bool:
    return command_on_path(agent_name, host)


def _file_exists(path: str, host: str | None) -> bool:
    if host is None:
        return Path(path).exists()
    r = _remote_login(host, f"test -f {shlex.quote(path)}")
    return r is not None and r.returncode == 0


def _read_file(path: str, host: str | None) -> str | None:
    """Return *path*'s contents from the agent host, or None if unreadable."""
    if host is None:
        try:
            return Path(path).read_text()
        except OSError:
            return None
    r = _remote_login(host, f"cat {shlex.quote(path)}")
    if r is None or r.returncode != 0:
        return None
    return r.stdout.decode(errors="replace")


def refresh_token_expiry(cred_json: str) -> float | None:
    """Epoch seconds at which claude's OAuth *refresh* token expires, or None.

    Reads ``claudeAiOauth.refreshTokenExpiresAt`` (epoch ms) and nothing else --
    no token value is returned, logged, or printed. None whenever the file is
    unparseable or predates the field, so a credential shape we do not
    understand degrades to today's presence-only answer.
    """
    try:
        oauth = json.loads(cred_json).get("claudeAiOauth")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    if not isinstance(oauth, dict):
        return None
    value = oauth.get("refreshTokenExpiresAt")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value / 1000.0


def classify_refresh_expiry(
    expiry: float | None, *, now: float | None = None, warn_within: float = 86400.0
) -> tuple[Status, str]:
    """Grade a refresh-token expiry into a check status and a detail string.

    Only the *refresh* token's expiry is a usable offline signal. Verified
    against the real credential files: a healthy ``~/.claude-docker`` copy reads
    an ``expiresAt`` 28.5h in the *past* (the access token is refreshed on use),
    so checking that would warn on every healthy install, and codex's access
    token reads valid for days while its id_token reads long expired. Once
    ``refreshTokenExpiresAt`` passes, though, no refresh can succeed: the
    session starts, then dies on its first turn.
    """
    now = time.time() if now is None else now
    if expiry is None:
        return Status.OK, "refresh-token expiry not recorded in the file"
    if expiry <= now:
        return Status.WARN, "refresh token expired"
    if expiry - now <= warn_within:
        return Status.WARN, f"refresh token expires in {_duration(expiry - now)}"
    return Status.OK, f"refresh token valid for another {_duration(expiry - now)}"


def _duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{max(seconds, 0) / 60:.0f}m"


def remote_uname(host: str) -> str:
    """Best-effort remote OS name (``Darwin`` / ``Linux`` / ``""``).

    Returns the empty string when the probe cannot run, so callers can treat an
    undetermined OS conservatively (the non-macOS ``GatewayPorts`` path).
    """
    r = _remote_login(host, "uname")
    if r is None or r.returncode != 0:
        return ""
    return r.stdout.decode().strip()


def gatewayports_status(host: str) -> str:
    """Return ``enabled`` / ``disabled`` / ``unknown`` for the remote sshd.

    ``enabled`` means ``clientspecified`` or ``yes`` -- required for the
    non-loopback ``-R`` bind a remote *container* reaches through its host
    gateway (message-plane reads). Remote-native does not need it.
    """
    r = _remote_login(host, "sshd -T 2>/dev/null | grep -i '^gatewayports'")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        val = r.stdout.decode().split()[-1].lower()
        return "enabled" if val in ("yes", "clientspecified") else "disabled"
    # Fall back to the config file (sshd -T often needs root).
    r = _remote_login(
        host, "grep -Ei '^[[:space:]]*GatewayPorts[[:space:]]' /etc/ssh/sshd_config 2>/dev/null | tail -1"
    )
    if r is not None and r.returncode == 0 and r.stdout.strip():
        val = r.stdout.decode().split()[-1].lower()
        return "enabled" if val in ("yes", "clientspecified") else "disabled"
    return "unknown"


# --- image build context, hash, and argv (pure) -----------------------------


def dockerfile_path() -> Path:
    """Locate the ``agent-docker/Dockerfile`` build context.

    ``AGENT_DOCKER_DIR`` overrides; otherwise resolve the monorepo sibling
    (``agent-session/src/agent_session`` -> ``agent-exchange/agent-docker``). The
    remote build path is a monorepo dev tool; ``AGENT_DOCKER_DIR`` covers any
    relocation (e.g. a wheel-installed package outside the monorepo).
    """
    env = os.environ.get("AGENT_DOCKER_DIR")
    if env:
        return Path(env).expanduser() / "Dockerfile"
    import agent_session

    return Path(agent_session.__file__).parents[3] / "agent-docker" / "Dockerfile"


def dockerfile_bytes() -> bytes:
    p = dockerfile_path()
    if not p.is_file():
        raise ProvisionError(
            f"agent-docker Dockerfile not found at {p}; set AGENT_DOCKER_DIR"
        )
    return p.read_bytes()


def context_hash(df_bytes: bytes, facts: BuildFacts) -> str:
    """Content hash of the build: Dockerfile bytes + host-dependent build args.

    ``*_CACHE_BUST`` are deliberately excluded -- they are a manual
    "pull a newer upstream CLI" bust, not part of content identity, so a rebuild
    with them must not later read as stale.
    """
    h = hashlib.sha256()
    h.update(df_bytes)
    h.update(b"\0")
    h.update(f"uname={facts.uname};uid={facts.uid};gid={facts.gid}".encode())
    return h.hexdigest()


def build_image_argv(
    image: str, *, facts: BuildFacts, context_hash: str, cache_bust: int | None = None
) -> list[str]:
    """``docker build`` argv that builds *image* from a stdin Dockerfile.

    Ends with ``-`` (Dockerfile read from stdin, empty context). Passes
    ``--build-arg UID/GID`` only on Linux (macOS pins the image to 1000, matching
    ``agent-docker.sh``); ``*_CACHE_BUST`` only when *cache_bust* is given
    (``--rebuild``).
    """
    argv = [
        "docker", "build", "-t", image,
        "--label", f"{IMAGE_HASH_LABEL}={context_hash}",
    ]
    if facts.uname == "Linux":
        argv += ["--build-arg", f"UID={facts.uid}", "--build-arg", f"GID={facts.gid}"]
    if cache_bust is not None:
        argv += [
            "--build-arg", f"CLAUDE_CACHE_BUST={cache_bust}",
            "--build-arg", f"CODEX_CACHE_BUST={cache_bust}",
        ]
    argv.append("-")
    return argv


def image_state(have: str | None, want: str) -> str:
    """Decide image staleness from the built-in label vs. the wanted hash."""
    if have is None:
        return "missing"
    if have != want:
        return "stale"
    return "up-to-date"


# --- image build/inspect I/O ------------------------------------------------


def resolve_build_facts(transport: Transport) -> BuildFacts:
    """Query the host's OS and (on Linux) uid/gid for the build."""
    if transport.location == "remote":
        host = transport.host
        assert host is not None
        uname = _remote_str(host, "uname")
        if uname == "Linux":
            return BuildFacts(uname, int(_remote_str(host, "id -u")), int(_remote_str(host, "id -g")))
        return BuildFacts(uname, 1000, 1000)
    uname = platform.system()
    if uname == "Linux":
        return BuildFacts(uname, os.getuid(), os.getgid())
    return BuildFacts(uname, 1000, 1000)


def image_context_hash(image: str, host: str | None) -> str | None:
    """Read *image*'s content-hash label, or None if missing/unlabeled.

    Inspects the resolved image *ID*, not the reference: an ID always resolves,
    while a short name may not (see :func:`image_id`). Reading the label through
    the name would come back empty on a containerd-store host, which
    :func:`image_state` reads as "missing" -- rebuilding a 2 GB image on every
    prep.
    """
    resolved = image_id(image, host)
    if resolved is None:
        return None
    fmt = '{{ index .Config.Labels "%s" }}' % IMAGE_HASH_LABEL
    argv = ["docker", "image", "inspect", "-f", fmt, resolved]
    if host is None:
        r = _local(argv)
    else:
        r = _remote_login(host, f"PATH={PATH_PREFIX}:$PATH {shlex.join(argv)}")
    if r is None or r.returncode != 0:
        return None
    out = r.stdout.decode().strip()
    if out in ("", "<no value>"):
        return None
    return out


def _run_build(cmd: list[str], df_bytes: bytes) -> None:
    """Run a ``docker build`` streaming the Dockerfile on stdin.

    stdout/stderr are inherited (not captured) so the user sees live build
    progress; stdin carries the Dockerfile to ``docker build -`` (locally, or
    forwarded through ssh to the remote docker).
    """
    proc = subprocess.run(cmd, input=df_bytes)
    if proc.returncode != 0:
        raise ProvisionError(f"image build failed ({proc.returncode})")


def build_image(
    transport: Transport,
    image: str,
    *,
    facts: BuildFacts,
    df_bytes: bytes,
    ctx_hash: str,
    rebuild: bool,
) -> None:
    """Build *image* on the agent host (local, or over ssh with stdin Dockerfile)."""
    cache_bust = int(time.time()) if rebuild else None
    argv = build_image_argv(image, facts=facts, context_hash=ctx_hash, cache_bust=cache_bust)
    if transport.location == "remote":
        cmd = ["ssh", transport.host, f"$SHELL -lc {shlex.quote(shlex.join(argv))}"]
    else:
        cmd = argv
    _run_build(cmd, df_bytes)


# --- reporting helpers ------------------------------------------------------


def describe_transport(transport: Transport, agent_name: str) -> str:
    loc = "remote" if transport.location == "remote" else "local"
    rt = "container" if transport.runtime == "container" else "native"
    label = f"{loc}-{rt}"
    if transport.host:
        label += f" @ {transport.host}"
    return f"{label} (agent: {agent_name})"


def _prep_flags(transport: Transport) -> str:
    parts = ""
    if transport.location == "remote":
        parts += f" --host {transport.host}"
    if transport.runtime == "container":
        parts += " --docker"
    return parts


def _install_hint(tool: str, host: str | None) -> str:
    where = f" on {host}" if host else ""
    return f"install {tool}{where} (e.g. 'brew install {tool}' or your package manager)"


def _agent_install_hint(agent_name: str) -> str:
    if agent_name == "codex":
        return "install Codex: npm install -g @openai/codex"
    return "install Claude Code: curl -fsSL https://claude.ai/install.sh | bash"


def render_report(report: PrepReport) -> str:
    sym = {
        Status.OK: "ok   ",
        Status.FIXED: "fixed",
        Status.WARN: "warn ",
        Status.FAIL: "fail ",
    }
    lines = [f"Prep report: {report.transport_label}", ""]
    for c in report.checks:
        lines.append(f"  [{sym[c.status]}] {c.name}: {c.detail}")
        if c.remediation:
            lines.append(f"            -> {c.remediation}")
    lines.append("")
    n_fail = sum(1 for c in report.checks if c.status is Status.FAIL)
    n_warn = sum(1 for c in report.checks if c.status is Status.WARN)
    if report.ready:
        tail = "Ready." + (f" ({n_warn} warning(s))" if n_warn else "")
    else:
        tail = f"Not ready: {n_fail} blocking issue(s). Fix the [fail] items above."
    lines.append(tail)
    return "\n".join(lines)


# --- orchestration ----------------------------------------------------------


def prep_target(
    transport: Transport,
    *,
    agent_name: str,
    image: str | None = None,
    check_only: bool,
    rebuild: bool,
) -> PrepReport:
    """Check (and, unless *check_only*, prepare) *transport* for a session."""
    transport.validate()
    image = image or _provision.DEFAULT_IMAGE
    host = transport.host if transport.location == "remote" else None
    report = PrepReport(describe_transport(transport, agent_name))

    home: str | None = None
    if transport.location == "remote":
        if not ssh_ok(host):
            report.add("ssh", Status.FAIL, f"{host} not reachable over ssh",
                       remediation="check ssh access / key auth")
            return report  # nothing else is runnable without ssh
        report.add("ssh", Status.OK, f"{host} reachable")
        try:
            home = _provision.resolve_remote_home(host)
            report.add("login-shell", Status.OK, f"$HOME resolves to {home}")
        except ProvisionError:
            report.add("login-shell", Status.FAIL, "login shell did not return $HOME",
                       remediation="ensure the account has a working login shell")
            return report
        if mutagen_ok():
            report.add("mutagen", Status.OK, "installed on control host")
        else:
            report.add("mutagen", Status.FAIL, "missing on control host (needed for the event mirror)",
                       remediation="brew install mutagen-io/mutagen/mutagen")

    if transport.runtime == "container":
        _prep_container(report, transport, agent_name, image, host, home, check_only, rebuild)
    else:
        _prep_native(report, transport, agent_name, host, home)

    _prep_plugin(report, transport, agent_name, home, check_only)
    return report


def _prep_native(
    report: PrepReport, transport: Transport, agent_name: str, host: str | None, home: str | None
) -> None:
    for tool in ("tmux", "jq"):
        ok = command_on_path(tool, host)
        report.add(tool, Status.OK if ok else Status.FAIL,
                   "on PATH" if ok else "not found on PATH",
                   remediation=None if ok else _install_hint(tool, host))
    ok = agent_cli_ok(agent_name, host)
    report.add(agent_name, Status.OK if ok else Status.FAIL,
               "on PATH" if ok else "not found on PATH",
               remediation=None if ok else _agent_install_hint(agent_name))
    _check_auth(report, agent_name, host, home, container=False)


def _prep_container(
    report: PrepReport,
    transport: Transport,
    agent_name: str,
    image: str,
    host: str | None,
    home: str | None,
    check_only: bool,
    rebuild: bool,
) -> None:
    if not docker_ok(host):
        report.add("docker", Status.FAIL, f"daemon not reachable on {host or 'local host'}",
                   remediation="start Docker / configure the docker CLI")
    else:
        report.add("docker", Status.OK, "daemon reachable")
        _prep_image(report, transport, image, host, check_only, rebuild)
    _check_auth(report, agent_name, host, home, container=True)
    if transport.location == "remote":
        _check_gatewayports(report, host)


def _prep_image(
    report: PrepReport,
    transport: Transport,
    image: str,
    host: str | None,
    check_only: bool,
    rebuild: bool,
) -> None:
    facts = resolve_build_facts(transport)
    df = dockerfile_bytes()
    want = context_hash(df, facts)
    state = image_state(image_context_hash(image, host), want)
    arch = facts.uname.lower()

    if check_only:
        flags = _prep_flags(transport)
        match state:
            case "missing":
                report.add("image", Status.FAIL, f"'{image}' not present on {host or 'local host'}",
                           remediation=f"run: asn prep{flags}")
            case "stale":
                report.add("image", Status.WARN,
                           f"'{image}' present but outdated (Dockerfile/build-args changed)",
                           remediation=f"run: asn prep{flags}")
            case _:
                report.add("image", Status.OK, f"'{image}' present and up-to-date ({arch})")
        return

    if state == "up-to-date" and not rebuild:
        report.add("image", Status.OK, f"'{image}' already up-to-date ({arch})")
        return

    verb = "built" if state == "missing" else "rebuilt"
    try:
        build_image(transport, image, facts=facts, df_bytes=df, ctx_hash=want, rebuild=rebuild)
        report.add("image", Status.FIXED, f"{image} {verb} natively on {host or 'local host'} ({arch})")
    except ProvisionError as e:
        report.add("image", Status.FAIL, f"build failed: {e}",
                   remediation="see docker build output above")


def _check_auth(
    report: PrepReport, agent_name: str, host: str | None, home: str | None, *, container: bool
) -> None:
    base = home if host else str(Path.home())
    where = f"   (on {host})" if host else ""
    if container:
        if agent_name == "codex":
            cred = f"{base}/.codex-docker/auth.json"
            rem = f"seed ~/.codex-docker/auth.json from ~/.codex{where} (see agent-docker/README.md)"
        else:
            cred = f"{base}/.claude-docker/.credentials.json"
            rem = f"run: agent-docker.sh login{where}"
        name = "container-auth"
    else:
        if agent_name == "codex":
            cred = f"{base}/.codex/auth.json"
            rem = f"run: codex login{where}"
        else:
            cred = f"{base}/.claude/.credentials.json"
            rem = f"run: claude login{where}"
        name = "auth"

    if _file_exists(cred, host):
        if agent_name == "codex":
            # No offline validity signal exists: codex's refresh token is opaque
            # and its auth.json records no refresh expiry. Say that, rather than
            # implying something was checked.
            report.add(name, Status.OK,
                       "codex credentials present (file only; codex records no "
                       "refresh-token expiry, so validity is not checkable offline)")
            return
        status, detail = classify_refresh_expiry(
            refresh_token_expiry(_read_file(cred, host) or "")
        )
        report.add(name, status, f"claude credentials present, {detail}",
                   remediation=rem if status is Status.WARN else None)
    elif container:
        # Container auth is file-based (agent-docker.sh login writes the token
        # into the mounted config dir), so an absent file is a real blocker.
        report.add(name, Status.FAIL, f"{agent_name} container login missing on {host or 'local host'}",
                   remediation=rem)
    else:
        # Native auth may live outside a file (claude uses the macOS Keychain),
        # so absence is not conclusive -- warn instead of blocking.
        report.add(name, Status.WARN,
                   f"no {agent_name} credential file on {host or 'local host'} "
                   "(may be stored in the OS keychain, which prep does not read)",
                   remediation=rem)


def _check_gatewayports(report: PrepReport, host: str | None) -> None:
    """Check whether remote-container message *reads* can reach the host gateway.

    The requirement is OS-specific. On **Linux** the container reaches the host
    via the ``docker0`` bridge gateway (a non-loopback address like
    ``172.17.0.1``), so the ``-R`` read tunnel must bind non-loopback -- which
    needs the remote sshd's ``GatewayPorts`` (``clientspecified``/``yes``). On
    **macOS** Docker Desktop routes ``host.docker.internal`` to the host loopback
    via its VM network stack, so the loopback-downgraded ``-R`` bind is already
    reachable and ``GatewayPorts`` is *not* required (empirically confirmed).
    """
    assert host is not None
    if remote_uname(host) == "Darwin":
        report.add("gatewayports", Status.OK,
                   "not required on macOS: Docker Desktop routes host.docker.internal to the "
                   "host loopback, which the loopback-downgraded -R bind already serves")
        return
    # Linux (or an OS the probe could not identify -- treated conservatively as
    # the Linux case, since it is the one that needs GatewayPorts).
    match gatewayports_status(host):
        case "enabled":
            report.add("gatewayports", Status.OK,
                       "remote sshd allows -R non-loopback binds (remote-container reads work)")
        case "disabled":
            report.add("gatewayports", Status.WARN,
                       "remote sshd GatewayPorts not enabled on this Linux host; remote-container "
                       "message reads will not work (native transport and sends are unaffected)",
                       remediation="set 'GatewayPorts clientspecified' in sshd_config and reload sshd (manual)")
        case _:
            report.add("gatewayports", Status.WARN,
                       "could not determine remote sshd GatewayPorts on this Linux host "
                       "('sshd -T' needs root, so a default host reads as unknown, not disabled)",
                       remediation="ensure 'GatewayPorts clientspecified' in sshd_config for remote-container message reads")


def _prep_plugin(
    report: PrepReport, transport: Transport, agent_name: str, home: str | None, check_only: bool
) -> None:
    if check_only:
        report.add("plugin", Status.OK, "hook plugin auto-deployed at prep/session start")
        return
    try:
        _provision.ensure_plugin(transport, get_agent(agent_name).plugin_dir(), remote_home=home)
        report.add("plugin", Status.FIXED, "hook plugin deployed/up-to-date on agent host")
    except ProvisionError as e:
        report.add("plugin", Status.FAIL, f"plugin deploy failed: {e}")


def diagnose_container_start_failure(transport: Transport, image: str) -> str | None:
    """Cheap post-readiness-failure check: is the image/docker the likely cause?

    Returns an actionable message when a container-transport start times out
    because docker is down or the image is missing, else None (unknown cause).
    Presence-only (no hash compute) so it stays fast on the failure path.
    """
    host = transport.host if transport.location == "remote" else None
    flags = _prep_flags(transport)
    if not docker_ok(host):
        return (f"agent container did not become ready and docker is not reachable on "
                f"{host or 'the local host'}; run: asn prep{flags}")
    if not image_present(image, host):
        return (f"agent container did not become ready and image '{image}' is missing on "
                f"{host or 'the local host'}; run: asn prep{flags}")
    return None
