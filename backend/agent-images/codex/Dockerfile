FROM node:22-bookworm-slim

ARG CODEX_VERSION=latest

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

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

ENV CODEX_HOME=/auth \
    TERM=xterm-256color

WORKDIR /workspace

CMD ["codex"]
