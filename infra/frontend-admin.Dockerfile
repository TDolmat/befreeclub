# syntax=docker/dockerfile:1.7
#
# Frontend admina: build Vite (pnpm workspace) -> statyczny nginx.
# Zero proxowania /api - split routing robi Caddy hosta (patrz infra/README.md).
#
# Build context = root monorepo (compose: context .., dockerfile infra/frontend-admin.Dockerfile).

FROM node:22-bookworm-slim AS builder

RUN corepack enable

WORKDIR /repo

# Manifesty najpierw - layer cache na instalacji zaleznosci.
COPY frontends/package.json frontends/pnpm-lock.yaml frontends/pnpm-workspace.yaml frontends/tsconfig.base.json ./
COPY frontends/admin/package.json ./admin/
COPY frontends/packages/shared/package.json ./packages/shared/

RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

# Zrodla (root .dockerignore wycina node_modules i dist).
COPY frontends/ ./

# pnpm -r buduje topologicznie: @bfc/shared przed @bfc/admin-web.
RUN pnpm -r build

FROM nginx:alpine

COPY infra/nginx-admin.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /repo/admin/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO /dev/null http://localhost/ || exit 1
