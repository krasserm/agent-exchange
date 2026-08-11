FROM node:22-bookworm-slim

ARG UID=1000
ARG GID=1000
ARG USERNAME=coder

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget ca-certificates openssh-client gnupg \
    build-essential \
    python3 python3-venv python3-dev \
    jq ripgrep less vim-tiny sudo tini tmux \
    && rm -rf /var/lib/apt/lists/*

# uv (Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Create non-root user with matching UID/GID
RUN if [ "${UID}" = "1000" ] && id -u node >/dev/null 2>&1; then \
      usermod -l "${USERNAME}" -d "/home/${USERNAME}" -m node && \
      groupmod -n "${USERNAME}" node; \
    else \
      userdel -r node 2>/dev/null || true; \
      groupadd -g "${GID}" "${USERNAME}" 2>/dev/null || true; \
      useradd --create-home --no-log-init \
        -u "${UID}" -g "${GID}" -s /bin/bash "${USERNAME}"; \
    fi \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" \
       > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

RUN mkdir -p /home/${USERNAME}/.claude /home/${USERNAME}/workspace \
    && chown -R ${UID}:${GID} /home/${USERNAME}

# Default Claude Code to auto-accept mode for this sandboxed container. Baked as
# *managed* settings because the runtime bind-mount over /home/coder/.claude
# (for credentials/transcripts) would shadow a user-level settings.json. Managed
# settings are the highest precedence and are enforced (not overridable per
# session); that is acceptable here since the container is a sandbox meant to run
# in auto mode. To use a different mode, remove this file (rebuild) or override
# the image's CMD.
RUN mkdir -p /etc/claude-code \
    && printf '{\n  "permissions": { "defaultMode": "auto" }\n}\n' \
       > /etc/claude-code/managed-settings.json

USER ${USERNAME}
ENV HOME=/home/${USERNAME}
ENV TERM=xterm-256color
ENV TERM_PROGRAM=Apple_Terminal
ENV DISABLE_AUTOUPDATER=1
# UTF-8 locale so the Claude Code TUI draws Unicode box-drawing instead of the
# VT100/ASCII fallback (stray _ and q glyphs) of the default POSIX locale --
# worst under the nested tmux of a proxied agent-session. C.UTF-8 is built into
# glibc, so no locale-gen or extra packages are needed.
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
# Package caches live under a shared volume mounted at ${HOME}/.cache.
# UV_LINK_MODE=copy because the cache (named volume) and the venv (project
# bind mount) sit on different filesystems, so uv cannot hardlink.
ENV UV_CACHE_DIR=${HOME}/.cache/uv
ENV PIP_CACHE_DIR=${HOME}/.cache/pip
ENV UV_LINK_MODE=copy

# pre-commit
RUN uv tool install pre-commit

# Claude Code (native installer)
ARG CLAUDE_CACHE_BUST=0
RUN curl -fsSL https://claude.ai/install.sh | bash -s latest
ENV PATH="${HOME}/.local/bin:${PATH}"

# Codex CLI (npm). Installed under the user's ~/.local prefix -- not the default
# root-owned /usr/local global prefix, which the non-root coder user cannot write
# -- so the `codex` binary lands in ~/.local/bin alongside claude, already on PATH.
ARG CODEX_CACHE_BUST=0
RUN npm config set prefix "${HOME}/.local" \
    && npm install -g @openai/codex

WORKDIR /home/${USERNAME}/workspace

ENTRYPOINT ["tini", "--"]
CMD ["bash"]
