# Lokalny development

Jak odpalić cały stack na Macu. Backend FastAPI na :3000, admin front (Vite) na :5173, Postgres lokalny.

## Wymagania

- **uv** (Python 3.12+): `brew install uv`
- **pnpm** (Node 20+): `brew install pnpm`
- **Postgres.app**: lokalny Postgres, user systemowy (`tomasz`), peer auth bez hasła
- **Stripe CLI** (do webhooków): `brew install stripe/stripe-cli/stripe`

## Pierwszy setup

```bash
# 1. Baza dev
createdb befreeclub

# 2. Env backendu
cd backend
cp .env.example .env        # defaulty pasują do Postgres.app, sekrety uzupełniasz w razie potrzeby

# 3. Zależności + migracje
uv sync
uv run alembic upgrade head

# 4. Frontend
cd ../frontends
pnpm install
```

## Odpalanie

Najprościej z VS Code: `Cmd+Shift+P` -> "Tasks: Run Task":

- **Run All (dev)** - backend + admin front równolegle, każdy w swoim panelu
- **Run All (dev + Stripe)** - jak wyżej, plus `stripe listen`
- Pojedyncze taski: backend, front, migracje, testy, lint

Albo ręcznie:

```bash
cd backend && uv run uvicorn app.main:app --port 3000 --reload
cd frontends && pnpm dev
```

URL-e:

- Front: http://localhost:5173 (Vite proxuje `/api`, `/ws` i `/health` na :3000)
- Backend: http://localhost:3000/api
- Healthcheck: http://localhost:3000/health

W dev nie ma auth (`NODE_ENV != production`, user `dev@local`).

## Stripe lokalnie

Stripe nigdy nie jest mockowany. Lokalnie używasz **test mode**:

1. W dashboardzie Stripe przełącz się na tryb testowy i skopiuj klucz `sk_test_...` do `STRIPE_SECRET_KEY` w `backend/.env`.
2. Zaloguj CLI: `stripe login`.
3. Odpal nasłuch webhooków (task "Stripe webhooki (CLI listen)" albo ręcznie):

```bash
stripe listen --forward-to localhost:3000/api/billing/webhooks/stripe/current
```

4. CLI wypisze na starcie `whsec_...`. Wklej go do `STRIPE_WEBHOOK_SECRET` w `backend/.env` i zrestartuj backend. Bez tego weryfikacja podpisu webhooka odrzuci eventy.

Testowanie:

- Karta testowa: `4242 4242 4242 4242`, dowolna przyszła data, dowolny CVC. Inne scenariusze (odrzucenia, 3DS) w docs Stripe: "test cards".
- Sztuczny event do webhooka: `stripe trigger payment_intent.succeeded` (działa przy włączonym `stripe listen`).

## Co jest mockowane lokalnie

Sterują tym flagi `MOCK_*` w `backend/.env` (w dev auto-włączone, szczegóły w `.env.example`):

- **Email (Resend)**: maile nie wychodzą, lądują jako pliki w `backend/.dev-outbox/`.
- **Sender.net (newsletter)**: wywołania tylko logowane.
- **Circle members**: fake klient, zero requestów do realnego Circle.
- **Stripe**: NIGDY nie mockowany. Zawsze realne API w test mode (sekcja wyżej).
- **Circle DM**: używa realnego Circle. Twarda zasada projektu: testujesz WYŁĄCZNIE na kontach testowych Paweł Wyrozumski i Tomasz Dwa. Nigdy na realnych członkach.

## Baza

- Dev: `befreeclub` (Postgres.app, user `tomasz`).
- Testy: `befreeclub_test` dla testów wymagających bazy. Obecny pytest (`uv run pytest tests/ -q`) chodzi na mockach i bazy nie potrzebuje.
- Reset bazy dev:

```bash
dropdb befreeclub && createdb befreeclub
cd backend && uv run alembic upgrade head
```

Migracje po zmianie modeli:

```bash
cd backend
uv run alembic revision --autogenerate -m "opis"
uv run alembic upgrade head
```
