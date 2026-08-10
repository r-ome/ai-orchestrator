FROM node:22-bookworm-slim

ARG CODEX_VERSION=latest
ARG PLAYWRIGHT_VERSION=1.62.1

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

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    NODE_PATH=/usr/local/lib/node_modules

RUN npm install --global "playwright@${PLAYWRIGHT_VERSION}" \
    && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0 playwright install --with-deps chromium \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}" \
    && rm -rf /var/lib/apt/lists/* \
    && npm cache clean --force

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

ENV CODEX_HOME=/auth \
    TERM=xterm-256color

WORKDIR /workspace

CMD ["codex"]
