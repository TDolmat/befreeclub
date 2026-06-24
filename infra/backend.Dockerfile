# syntax=docker/dockerfile:1.7
#
# Obraz SAMEGO backendu (czyste API + WS). SPA serwuje osobny kontener nginx
# (infra/frontend-admin.Dockerfile). Kod serwowania SPA w app/main.py zostaje
# (parytet 1:1 ze starym adminem), ale WEB_DIST_PATH jest pusty, wiec nieaktywny.
#
# Build context = root monorepo (compose: context .., dockerfile infra/backend.Dockerfile).

FROM python:3.12-slim

# Narzedzia dla Claude CLI (git/file ops) + curl do healthchecku.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Node 24 (LTS) WYLACZNIE dla Claude Code CLI. Binarka node + npm kopiowane
# z oficjalnego obrazu (ta sama baza: Debian bookworm), bez apt/nodesource.
COPY --from=node:24-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @anthropic-ai/claude-code

# uv - zaleznosci Pythona
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# WEB_DIST_PATH celowo NIEUSTAWIONY (front serwuje nginx).
ENV NODE_ENV=production \
    PORT=3000 \
    CLAUDE_BIN_PATH=/usr/local/bin/claude \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Najpierw manifesty - layer cache na instalacji zaleznosci.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Kod backendu (app/, alembic/, alembic.ini, scripts/).
# Root .dockerignore wycina .venv, .env i __pycache__.
COPY backend/ ./

# User uid=1000: Claude Code CLI odmawia --permission-mode=bypassPermissions
# jako root. /home/bfc/.claude pre-created z chown, zeby named volume
# claude-config zamontowal sie z poprawnym ownerem (tokeny `claude login`).
RUN useradd -u 1000 -m bfc \
    && mkdir -p /home/bfc/.claude \
    && chown -R bfc:bfc /home/bfc /app

# Shim operacyjny: docker compose exec backend set-auth-password --email ...
RUN printf '#!/bin/sh\nexec /app/.venv/bin/python /app/scripts/set_auth_password.py "$@"\n' \
    > /usr/local/bin/set-auth-password \
    && chmod +x /usr/local/bin/set-auth-password

USER bfc

# Persystencja credentiali Claude CLI miedzy restartami (named volume).
VOLUME ["/home/bfc/.claude"]

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

# Migracje przy KAZDYM starcie (idempotentne), potem serwer. Blad migracji =
# brak startu, restart: unless-stopped ponawia.
# `python -m app.main` = uvicorn app.main:app na 0.0.0.0:$PORT z JEDNYM
# workerem (stan in-memory) i ws_ping_interval=None (kontrakt portu - binarka
# uvicorn nie umie wylaczyc pingow WS z CLI, dlatego nie wolamy jej wprost).
CMD ["sh", "-c", "alembic upgrade head && exec python -m app.main"]
