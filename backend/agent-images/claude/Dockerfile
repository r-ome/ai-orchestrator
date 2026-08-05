FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        openssh-client \
        ripgrep \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

ENV CLAUDE_CONFIG_DIR=/auth \
    DISABLE_AUTOUPDATER=1 \
    TERM=xterm-256color

WORKDIR /workspace

CMD ["claude"]
