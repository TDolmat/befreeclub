# Lokalny development

Jak odpalić cały stack na Macu. Backend FastAPI na :3000, admin front (Vite) na :5173, Postgres lokalny.

## Wymagania

- **uv** (Python 3.12+): `brew install uv`
- **Node 24** (Active LTS) - wersja przypięta w repo w `.node-version` / `.nvmrc`, ta sama na dev i prod (obrazy Docker też na 24). Nie polegaj na tym, co masz globalnie. Użyj menadżera wersji, który to czyta, i w katalogu repo ustaw właściwą:
  - `fnm` (polecany, `brew install fnm`) -> `fnm use`
  - `nvm` -> `nvm use`
  - `n` -> `n auto`
- **pnpm**: nie instalujesz globalnie. Włącz **corepack** (jest w Node): `corepack enable`. pnpm sam się provisionuje w wersji z `package.json` (`packageManager: pnpm@11.1.0`), identycznej u każdego.
- **Postgres.app**: lokalny Postgres, user systemowy (`tomasz`), peer auth bez hasła
- **Stripe CLI** (do webhooków): `brew install stripe/stripe-cli/stripe`

## Pierwszy setup

Skrót: task VS Code **Setup (pierwszy raz)** robi kroki 1, 3 i 4 jednym kliknięciem. Ręcznie:

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

Taski są w repo (`.vscode/tasks.json`), więc każdy kto sklonuje ma je od razu. Na co dzień klikasz **jeden**. `Cmd+Shift+B` (domyślny) albo `Cmd+Shift+P` -> "Tasks: Run Task":

- **▶ Admin (backend + front)** - to jest TEN task. Backend + admin front równolegle, każdy w swoim panelu. Front nigdy nie chodzi bez backendu, więc jeden klik odpala oba. Backend sam odpala migracje przed serwerem (jak Docker), po `git pull` nie migrujesz ręcznie.
- **Stripe webhooki (gdy testujesz płatności)** - odpalasz DODATKOWO, obok Admina, tylko gdy dłubiesz w płatnościach. Tunel Stripe -> localhost, żeby webhooki dochodziły lokalnie. Poza płatnościami nie potrzebny.
- **Setup (pierwszy raz)** - createdb + uv sync + migracje + pnpm install jednym kliknięciem.

- **DB: reset dev** - wywala bazę dev i stawia od zera (czyste schematy + seedy, zero danych). Gdy chcesz czysty stan.

Klocki "Backend" i "Admin front" są na liście, ale sam ich nie klikasz - używa ich task "▶ Admin". Migracje, testy i lint robisz z terminala (komendy niżej), nie zaśmiecają listy tasków.

Konwencja na przyszłość: gdy dojdzie landing (faza 2.4), dorzuca się analogiczny task **▶ Landing (backend + front)** - jeden serwis = jeden task odpalający backend + jego front równolegle.

Albo ręcznie:

```bash
cd backend && uv run alembic upgrade head && uv run uvicorn app.main:app --port 3000 --reload
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

## Co działa od razu, a co wymaga sandbox kluczy

**Bez żadnych kluczy zewnętrznych stack wstaje i działa.** Maile, Sender i Circle members są zamockowane automatycznie (patrz niżej), a sekrety DOI są pre-wypełnione dev-wartościami w `.env.example`. Czyli `cp .env.example .env` + `createdb` + start = panel admina, ustawienia, newsletter, anulowania chodzą lokalnie.

Realnych (sandbox) kluczy potrzebujesz tylko gdy chcesz testować konkretną integrację:

- **Stripe** (płatności, webhooki): klucz **test mode** `sk_test_...` + `whsec_...` z `stripe listen` (sekcja niżej). Nigdy nie mockowany.
- **OpenAI** (opcjonalnie): głosówki/zdjęcia w Circle DM. Puste = te funkcje wyłączone.
- **Circle DM** (drafty): realny token Circle, ale operujesz WYŁĄCZNIE na kontach testowych.

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

## Testy i lint

```bash
cd backend && uv run pytest tests/ -q       # testy backendu
cd backend && uv run ruff check .           # lint backendu
cd frontends && pnpm typecheck && pnpm lint # front
```
