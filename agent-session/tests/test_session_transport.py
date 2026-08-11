"""Unit tests for transport handling at the session/daemon construction level.

These construct an AgentSession (cheap; libtmux is lazy) and inspect derived
state without touching tmux, ssh, docker, or mutagen.
"""

from pathlib import Path, PurePosixPath

import pytest

from agent_session import _naming, _provision
from agent_session._daemon import _build_transport
from agent_session._session import AgentSession
from agent_session.agents import LaunchSpec, Transport
from agent_session.agents.claude import ClaudeAgent


class TestBuildTransport:
    def test_defaults_to_local_native(self) -> None:
        t = _build_transport({})
        assert t.is_local_native

    def test_remote_from_params(self) -> None:
        t = _build_transport({"location": "remote", "host": "192.168.94.50"})
        assert t.location == "remote"
        assert t.host == "192.168.94.50"
        assert t.runtime == "native"

    def test_container_from_params(self) -> None:
        t = _build_transport({"runtime": "container"})
        assert t.runtime == "container"
        assert t.container is None  # derived later by the session

    def test_explicit_container_name(self) -> None:
        t = _build_transport({"runtime": "container", "container": "my-ctr"})
        assert t.container == "my-ctr"


class TestSessionTransportDerivation:
    def _session(self, transport: Transport, start_dir: str = "/srv/proj") -> AgentSession:
        return AgentSession(
            ClaudeAgent(), LaunchSpec(start_dir=Path(start_dir), transport=transport)
        )

    def test_local_native_unchanged(self) -> None:
        s = self._session(Transport(), start_dir=str(Path.home() / "proj"))
        assert s.transport.is_local_native
        assert s.remote_session_name is None
        # events go under the local control-host session dir
        assert s._events_path == s.session_dir / "events.jsonl"

    def test_container_name_derived_from_key(self) -> None:
        s = self._session(
            Transport(runtime="container"), start_dir=str(Path.home() / "proj")
        )
        assert s.transport.container == f"cc-{s.session_key[:8]}"
        assert s.remote_session_name == f"cc-{s.session_key[:8]}"

    def test_explicit_container_name_preserved(self) -> None:
        s = self._session(
            Transport(runtime="container", container="pinned"),
            start_dir=str(Path.home() / "proj"),
        )
        assert s.transport.container == "pinned"

    def test_remote_session_name_and_local_proxy_name(self) -> None:
        s = self._session(Transport(location="remote", host="192.168.94.50"))
        # local proxy tmux name is host+key based (remote path not under dev root)
        # and must be tmux-safe: no '.' or ':' allowed in a session name.
        assert s.session_name == f"cc-192_168_94_50-{s.session_key[:8]}"
        assert "." not in s.session_name and ":" not in s.session_name
        assert s.remote_session_name == f"cc-{s.session_key[:8]}"

    def test_remote_events_path_is_mirror(self) -> None:
        s = self._session(Transport(location="remote", host="192.168.94.50"))
        expected = (
            _naming.make_mirror_session_dir("192.168.94.50", s.session_key)
            / "events.jsonl"
        )
        assert s._events_path == expected

    def test_remote_start_dir_not_resolved_locally(self) -> None:
        s = self._session(Transport(location="remote", host="h"), start_dir="/srv/proj")
        # The remote path is kept verbatim, not resolved against the local fs.
        assert str(s.start_dir) == "/srv/proj"

    def test_remote_project_dir_not_resolved_locally(self, tmp_path: Path) -> None:
        """``project_dir`` names a path on the *agent* host.

        Resolving it against the control host's filesystem rewrites any prefix
        that happens to be a local symlink -- which is how a remote
        ``/home/martin/Downloads`` came back as
        ``/System/Volumes/Data/home/martin/Downloads`` on macOS, where ``/home``
        is a symlink.
        """
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        remote_path = link / "proj"

        s = self._session(
            Transport(location="remote", host="h"), start_dir=str(remote_path)
        )

        assert s.project_dir == remote_path
        assert "real" not in str(s.project_dir)

    def test_local_project_dir_still_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local sessions keep resolving: the path is on this machine."""
        monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(tmp_path))
        real = tmp_path / "real"
        real.mkdir()
        (real / "proj").mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        s = self._session(Transport(), start_dir=str(link / "proj"))

        assert s.project_dir == (real / "proj").resolve()


class TestLocalContainerStateRoot:
    """A local container must write its events where the daemon watches.

    The container bind-mounts the control host's state root at the fixed
    in-container ``~/.agent-session``, and its hook writes events under that
    mount. The daemon watches ``DAEMON_DIR/sessions/<key>/events.jsonl``. Derive
    the mount source from ``$HOME`` instead of ``DAEMON_DIR`` and the two part
    company under ``ASN_HOME``: the hook writes into a tree nobody watches, no
    readiness event ever arrives, and ``start`` fails with a bare timeout.
    """

    async def test_bind_mount_source_is_the_watched_state_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        monkeypatch.setattr(_naming, "DAEMON_DIR", state)
        monkeypatch.setattr(_naming, "SESSIONS_DIR", state / "sessions")

        # Stub only the two functions that shell out (rsync-over-ssh / docker);
        # everything that decides *paths* is the real code under test. Raising
        # from the container step stops the launch before tmux gets involved.
        captured: dict[str, str] = {}

        def fake_ensure_container(transport: Transport, **kwargs: object) -> None:
            captured.update({k: v for k, v in kwargs.items() if isinstance(v, str)})
            raise _provision.ProvisionError("stop here")

        monkeypatch.setattr(_provision, "ensure_plugin", lambda *a, **kw: None)
        monkeypatch.setattr(_provision, "ensure_container", fake_ensure_container)

        s = AgentSession(
            ClaudeAgent(),
            LaunchSpec(
                start_dir=Path.home() / "proj", transport=Transport(runtime="container")
            ),
        )
        with pytest.raises(_provision.ProvisionError):
            await s._start_proxied(timeout=1)

        # Walk the path the hook's events actually travel: the in-container
        # session dir, mapped back through the bind mount onto the host.
        in_container = PurePosixPath(
            _provision.agent_host_session_dir(
                s.transport, s.session_key, local_home=str(Path.home())
            )
        )
        rel = in_container.relative_to(_provision.CONTAINER_AGENT_SESSION)
        landed = Path(captured["agent_session_on_host"]) / rel / "events.jsonl"

        assert landed == s._events_path

    def test_plugin_lands_under_the_same_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The plugin reaches the container through the same bind mount.

        Deploying it under ``$HOME`` while the container mounts ``DAEMON_DIR``
        leaves ``--plugin-dir`` pointing at nothing, so no hook runs and no event
        is ever emitted -- the same invisible timeout by another route.
        """
        state = tmp_path / "state"
        monkeypatch.setattr(_naming, "DAEMON_DIR", state)
        written: list[str] = []
        monkeypatch.setattr(
            _provision,
            "write_agent_host_file",
            lambda t, path, content, **kw: written.append(path),
        )

        transport = Transport(runtime="container", container="cc-1")
        _provision.ensure_plugin(
            transport, ClaudeAgent().plugin_dir(), remote_home=None
        )

        assert written, "plugin was not deployed"
        for path in written:
            assert path.startswith(f"{state}/plugin/")


class TestMirrorNaming:
    def test_sanitize_and_mirror_dirs(self) -> None:
        assert _naming.sanitize_host("user@192.168.94.50") == "user_192.168.94.50"
        d = _naming.make_mirror_session_dir("192.168.94.50", "KEY")
        assert d.name == "KEY"
        assert "192.168.94.50" in str(d)
        assert "mirrors" in str(d)

    def test_mutagen_sync_name_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mutagen session names must be alphanumeric + hyphen (no dots/'@').
        from agent_session._mirror import _sync_name

        monkeypatch.setattr(_naming, "DAEMON_DIR", _naming.DEFAULT_DAEMON_DIR)
        for host in ("192.168.94.50", "user@host.local"):
            name = _sync_name(host)
            assert name.startswith("asn-")
            assert all(c.isalnum() or c == "-" for c in name), name
        assert _sync_name("192.168.94.50") == "asn-192-168-94-50"


class TestMirrorSyncNamePerStateRoot:
    """Two daemons on one machine must never share a mirror sync.

    ``ensure_host_mirror`` reuses an existing sync when ``mirror_exists`` finds
    one *by name*, and the mirror directory it replicates into lives under the
    state root. So if the name ignored the state root, the second daemon to start
    -- the test suite's, via ``ASN_HOME`` -- would adopt the first's sync,
    replicating into the *other* root's mirror dir, and then watch its own empty
    one: every remote session would time out with no event ever arriving. The
    reverse is worse, since the live daemon would silently start writing into the
    test tree.
    """

    def _name(self, monkeypatch: pytest.MonkeyPatch, root: Path, host: str = "h.local") -> str:
        from agent_session._mirror import _sync_name

        monkeypatch.setattr(_naming, "DAEMON_DIR", root)
        return _sync_name(host)

    def test_default_root_keeps_the_historical_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Syncs created before this discriminator existed must still be reused."""
        assert self._name(monkeypatch, _naming.DEFAULT_DAEMON_DIR) == "asn-h-local"

    def test_overridden_root_gets_its_own_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = self._name(monkeypatch, tmp_path / "state")
        assert name != "asn-h-local"
        assert name.startswith("asn-h-local-")
        assert all(c.isalnum() or c == "-" for c in name), name

    def test_distinct_roots_never_collide(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a = self._name(monkeypatch, tmp_path / "a")
        b = self._name(monkeypatch, tmp_path / "b")
        default = self._name(monkeypatch, _naming.DEFAULT_DAEMON_DIR)
        assert len({a, b, default}) == 3

    def test_same_root_is_stable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reuse depends on the name being identical run to run."""
        root = tmp_path / "state"
        assert self._name(monkeypatch, root) == self._name(monkeypatch, root)

    def test_the_e2e_root_differs_from_the_live_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The concrete case: this suite's root vs the user's default root."""
        e2e = self._name(monkeypatch, Path("~/.agent-session-e2e").expanduser())
        live = self._name(monkeypatch, _naming.DEFAULT_DAEMON_DIR)
        assert e2e != live


class TestMirrorTeardownTargeting:
    """Tearing down a mirror must only ever reach this state root's sync.

    Nothing in production calls ``teardown_host_mirror``, so the test suite calls
    it to clean up after itself. That is only safe because the name carries the
    state-root discriminator: without it the call would terminate the *user's*
    live sync for the same host.
    """

    def _capture(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> list[tuple]:
        from types import SimpleNamespace

        from agent_session import _mirror

        calls: list[tuple] = []
        monkeypatch.setattr(_naming, "DAEMON_DIR", root)
        monkeypatch.setattr(
            _mirror, "_mutagen",
            lambda *args, **kw: calls.append(args)
            or SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        )
        _mirror.teardown_host_mirror("192.168.94.50")
        return calls

    def test_terminates_this_roots_sync(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = self._capture(monkeypatch, tmp_path / "state")
        assert len(calls) == 1
        verb, action, name = calls[0]
        assert (verb, action) == ("sync", "terminate")
        assert name.startswith("asn-192-168-94-50-")

    def test_never_terminates_the_default_roots_sync(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The concrete hazard: the user's live sync for the same host."""
        calls = self._capture(monkeypatch, tmp_path / "state")
        (_, _, name) = calls[0]
        assert name != "asn-192-168-94-50", "would have killed the live daemon's sync"
