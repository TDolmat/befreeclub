# Be Free Club - monorepo

Docelowe monorepo ekosystemu Be Free Club na VPS. Faza 1: panel admina (`admin.befreeclub.pro`) przepisany z Hono+Drizzle na FastAPI, działanie 1:1 ze starym kodem. Kolejne fazy: landing, toole AI.

Pełna architektura: `docs/ARCHITEKTURA.md`. Kontrakt 1:1 ze starym adminem: `docs/spec/`.

## Struktura

```
backend/     # jeden modularny monolit FastAPI (Python 3.12, uv)
frontends/   # pnpm workspace: admin (React SPA) + packages/shared
infra/       # Dockerfile'y, docker-compose, nginx conf, instrukcja deployu
scripts/     # data-migration: stara baza admina -> nowa (MIGRACJA_DANYCH.md)
docs/        # ARCHITEKTURA.md + spec/ (specyfikacje 1:1 starego kodu)
```

## Development

Pełna instrukcja lokalnego setupu (wymagania, pierwszy setup, Stripe lokalnie, mocki, baza): **`docs/DEV.md`**.

Najszybciej przez taski VS Code (`.vscode/tasks.json`): `Cmd+Shift+P` -> "Tasks: Run Task" -> **Run All (dev)** (backend + front) albo **Run All (dev + Stripe)**.

Backend (wymaga lokalnego Postgresa i pliku `backend/.env` z `backend/.env.example`):

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --port 3000 --reload
```

Frontend:

```bash
cd frontends
pnpm install
pnpm dev        # buduje @bfc/shared, potem Vite na :5173
```

Vite proxy `/api`, `/ws` i `/health` -> `localhost:3000` jest już skonfigurowane (`frontends/admin/vite.config.ts`). Dev backendu = brak auth (`NODE_ENV != production`, user `dev@local`).

Migracje po zmianie modeli:

```bash
cd backend
uv run alembic revision --autogenerate -m "opis"
uv run alembic upgrade head
```

## Model serwowania

- **Dev**: Vite serwuje SPA na :5173 i proxuje API do backendu na :3000.
- **Prod**: każdy frontend ma własny kontener nginx, backend to czyste API + WS. Caddy hosta robi split routing per domena: `/api/*`, `/ws`, `/health*` do backendu, reszta do nginx. Deploye frontu i backendu są niezależne.

Deploy: `infra/README.md`. Migracja danych ze starego admina: `scripts/data-migration/MIGRACJA_DANYCH.md`.
