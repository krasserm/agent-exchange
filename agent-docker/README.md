# agent-docker

`agent-docker` is the container image used by `agent-session` when a session is started with `--docker`. It supplies Claude Code, Codex, `tmux`, Git, Python, Node.js, and common command-line development tools in a consistent Debian environment.

The image is meant for disposable agent runtimes, not for packaging the `agent-exchange` services. `agent-session` starts one container per managed session, mounts the selected project and agent configuration into it, and keeps lifecycle and messaging state connected to the control host.

Most users interact with the image through `asn prep` and `asn start`; [`agent-docker.sh`](agent-docker.sh) is available for Claude Code login or for opening Bash in a temporary container outside `agent-session`.

## Requirements

- Docker with a running daemon
- an `agent-exchange` source checkout, which provides the Docker build context
- credentials for the selected agent on each machine that will run containers

The image is built on the machine where it will run. This selects the correct CPU architecture and, on Linux, matches the container user's UID and GID to the host so project files remain usable.

## Use with `agent-session`

From the repository root, authenticate Claude Code for local containers, prepare the image, and start a session:

```bash
./agent-docker/agent-docker.sh login
asn prep --docker --agent claude
asn start /absolute/path/to/project --docker
```

Codex uses a separate container configuration directory. After a native login, seed it before preparation:

```bash
codex login
mkdir -p ~/.codex-docker
cp ~/.codex/auth.json ~/.codex-docker/auth.json
asn prep --docker --agent codex
asn start /absolute/path/to/project --docker --agent codex
```

For a container on another machine, prepare and start it from the control host after placing the appropriate credentials on that remote machine:

```bash
asn prep --host example-host --docker --agent codex
asn start /srv/project --host example-host --docker --agent codex
```

`asn prep` rebuilds a missing or source-incompatible image. Add `--rebuild` to refresh the installed agent CLIs even when the local build inputs have not changed.

## Inspect the image interactively

```bash
./agent-docker/agent-docker.sh /absolute/path/to/project
```

This starts a temporary container from the image and opens Bash with the project mounted at `/home/coder/workspace`. It mounts the Claude Code configuration, not `~/.codex-docker`. Use it to inspect the container environment or run Claude Code without registering an `agent-session` session. The container does not include the lifecycle and messaging mounts that `agent-session` adds to managed containers.

Set `AGENT_BUILD=1` to rebuild first or `AGENT_IMAGE` to select another image name.

## Security boundary

Managed containers run as `coder`, drop Linux capabilities, set `no-new-privileges`, and apply memory and process limits. These are useful controls, not isolation for hostile code.

The project directory and agent configuration are mounted into the container, and the container has normal network access and shares the host kernel. Code running inside it can change the project and may read or alter the mounted credentials. The image also contains passwordless `sudo` for development use, although `no-new-privileges` constrains ordinary escalation in the managed runtime.

Use a dedicated checkout and scoped credentials for autonomous work. Do not treat `--docker` as a reason to run untrusted code on a sensitive host.
