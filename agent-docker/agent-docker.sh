#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# agent-docker: run a coding agent (Claude Code / Codex) in a hardened container
#
# Usage:
#   ./agent-docker.sh login             # one-time OAuth login
#   ./agent-docker.sh /path/to/project  # open a bash shell in the project
#
# The container starts a bash shell; run `claude` inside it manually.
#
# Examples:
#   ./agent-docker.sh login
#   ./agent-docker.sh .
#   ./agent-docker.sh ~/projects/myapp
#
# Environment:
#   AGENT_IMAGE: override image name (default: agent-docker)
#   AGENT_BUILD: set to 1 to force rebuild
# ============================================================

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
while [[ -L "${SCRIPT_SOURCE}" ]]; do
    dir="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
    SCRIPT_SOURCE="$(readlink "${SCRIPT_SOURCE}")"
    [[ "${SCRIPT_SOURCE}" != /* ]] && SCRIPT_SOURCE="${dir}/${SCRIPT_SOURCE}"
done
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
IMAGE_NAME="${AGENT_IMAGE:-agent-docker}"
CLAUDE_CONFIG_DIR="${HOME}/.claude-docker"
CACHE_VOLUME="shared-xdg-cache"

# --- Ensure config dir and preferences file exist ---

mkdir -p "${CLAUDE_CONFIG_DIR}" "${CLAUDE_CONFIG_DIR}/agents" "${CLAUDE_CONFIG_DIR}/skills"
[[ -s "${CLAUDE_CONFIG_DIR}/.claude.json" ]] || echo '{}' > "${CLAUDE_CONFIG_DIR}/.claude.json"

# --- Container UID/GID ---

if [[ "$(uname)" == "Darwin" ]]; then
    CONTAINER_UID=1000
    CONTAINER_GID=1000
else
    CONTAINER_UID=$(id -u)
    CONTAINER_GID=$(id -g)
fi

# --- Build (once, or when forced) ---

build_image() {
    echo "Building ${IMAGE_NAME}..."
    local build_args=()

    if [[ "$(uname)" != "Darwin" ]]; then
        # Linux: match host UID/GID for volume permissions
        build_args+=(--build-arg "UID=${CONTAINER_UID}" --build-arg "GID=${CONTAINER_GID}")
    fi

    if [[ "${AGENT_BUILD:-0}" == "1" ]]; then
        local bust
        bust="$(date +%s)"
        build_args+=(--build-arg "CLAUDE_CACHE_BUST=${bust}")
        build_args+=(--build-arg "CODEX_CACHE_BUST=${bust}")
    fi

    docker build ${build_args[@]+"${build_args[@]}"} -t "${IMAGE_NAME}" "${SCRIPT_DIR}"
}

# Build if image doesn't exist or rebuild is forced
if [[ "${AGENT_BUILD:-0}" == "1" ]] || ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
    build_image
fi

# --- Common docker args ---

base_docker_args=(
    -it --rm
    -v "${CLAUDE_CONFIG_DIR}:/home/coder/.claude"
    -v "${CLAUDE_CONFIG_DIR}/.claude.json:/home/coder/.claude.json"
    --cap-drop ALL
    --security-opt no-new-privileges:true
)

# --- Handle login subcommand ---

if [[ "${1:-}" == "login" ]]; then
    echo "Logging in to Claude (Max subscription)..."
    echo "A URL will be printed; open it in your browser to authenticate."
    echo "Credentials will be stored in ${CLAUDE_CONFIG_DIR}"
    echo ""
    exec docker run "${base_docker_args[@]}" "${IMAGE_NAME}" claude login
fi

# --- Argument parsing ---

if [ $# -lt 1 ]; then
    echo "Usage: $(basename "$0") <project-dir>"
    echo "       $(basename "$0") login"
    echo ""
    echo "First run:"
    echo "  $(basename "$0") login        # authenticate with Max subscription"
    echo ""
    echo "Then:"
    echo "  $(basename "$0") .            # open a bash shell in the project; run 'claude' inside"
    exit 1
fi

PROJECT_DIR="$(cd "$1" && pwd)"  # resolve to absolute path

# --- Shared package cache (uv, pip, ...) shared across all containers ---

if ! docker volume inspect "${CACHE_VOLUME}" &>/dev/null; then
    docker volume create "${CACHE_VOLUME}" >/dev/null
    docker run --rm --user root -v "${CACHE_VOLUME}:/mnt" "${IMAGE_NAME}" chown "${CONTAINER_UID}:${CONTAINER_GID}" /mnt
fi

# --- Run ---

docker_args=(
    "${base_docker_args[@]}"
    # Mount project and git config
    -v "${PROJECT_DIR}:/home/coder/workspace"
    -v "${HOME}/.gitconfig:/home/coder/.gitconfig:ro"
    # Shared package cache
    -v "${CACHE_VOLUME}:/home/coder/.cache"
    # Security limits
    --memory 8g
    --pids-limit 256
)

exec docker run "${docker_args[@]}" "${IMAGE_NAME}"
