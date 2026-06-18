# Architektura monorepo `befreeclub`

Docelowe monorepo całego ekosystemu Be Free Club na VPS. Faza 1: przepisanie panelu admina (`admin/`, Hono+Drizzle+TS) na FastAPI, działanie 1:1. Kolejne fazy: landing (befreeclub.pl), toole AI.

## Struktura katalogów

```
befreeclub/
  backend/                     # JEDEN modularny monolit FastAPI (Python 3.12)
    pyproject.toml             # uv + ruff
    alembic.ini
    alembic/                   # migracje DB (wszystkie schematy)
      env.py
      versions/
    app/
      main.py                  # create_app: montaż modułów, lifespan (workery), SPA static
      core/                    # infra wspólna dla wszystkich modułów (pisana RAZ)
        config.py              # pydantic-settings, wszystkie env vars
        db.py                  # async engine + sessionmaker + Base
        logging.py
        security.py            # scrypt (kompatybilny z hash'ami z Node!)
        semaphore.py
        ws.py                  # broker WebSocket
        claude_cli.py          # spawn Claude Code CLI + parser stream-json
      modules/
        admin/                 # rdzeń panelu admina: auth, feedback, health
          models.py            # tabele schematu PG "admin"
          schemas.py           # DTO pydantic (camelCase przez aliasy)
          routes/              # /api/auth, /api/feedback, /health*
          services/
        circle_dm/             # tool Circle DM (pierwszy tool admina)
          models.py            # tabele schematu PG "circle_dm"
          schemas.py
          routes/              # /api/circle-dm/*
          services/
          circle/              # klient Circle Headless API, JWT, tiptap, załączniki
    scripts/
      set_auth_password.py     # odpowiednik set-auth-password (scrypt, upsert, invalidacja sesji)
  frontends/                   # pnpm workspace (TS żyje tylko tutaj)
    pnpm-workspace.yaml
    package.json
    admin/                     # React SPA panelu (kopia apps/web, te same API)
    packages/
      shared/                  # @bfc/shared - dawne @admin/shared (Zod schemas + DTO)
  infra/
    docker-compose.yml         # db + backend + frontend-admin (caddy zostaje na hoście)
    backend.Dockerfile         # python 3.12 + node (tylko Claude CLI), uv sync, bez frontu
    frontend-admin.Dockerfile  # multi-stage: build Vite (pnpm) -> nginx ze statykami
    nginx-admin.conf           # SPA: try_files fallback, cache assets, bez proxy /api
    README.md                  # build, pierwszy setup VPS, blok Caddyfile, update
  scripts/
    data-migration/            # migracja danych stary admin DB -> nowa baza
      migrate_data.py
      MIGRACJA_DANYCH.md
  docs/
    ARCHITEKTURA.md            # ten plik
    spec/                      # specyfikacje 1:1 wyciągnięte ze starego kodu (kontrakt portu)
```

Zasada: **moduł = schemat Postgresa = prefiks URL**. Nowy tool/moduł dostaje katalog w `app/modules/`, własny schemat w bazie i prefiks `/api/<modul>`. Wspólny kod (klienci zewnętrznych API, auth, WS) tylko w `core/` lub jako moduł-właściciel udostępniający serwis.

## Baza danych - konwencje nazw (długoterminowe)

Jeden Postgres, baza `befreeclub`. Separacja per moduł przez **schematy PG**, nie prefiksy w nazwach tabel. Żelazna zasada: tabelę ZAPISUJE tylko moduł-właściciel (właściciel schematu), inni czytają.

Schematy (fazy przyszłe w nawiasach): `admin`, `circle_dm`, (`landing`, `billing`, `members`, `newsletter`).

Enumy PG żyją w schemacie swojego modułu. Nazwy tabel bez prefiksów modułu (schemat już mówi czyje to): `circle_dm.threads`, nie `circle_dm.dm_threads`.

### Mapa migracji nazw (stara baza `bfc_admin`, schemat public -> nowa `befreeclub`)

| Stara tabela | Nowa tabela | Uwagi |
|---|---|---|
| `auth_accounts` | `admin.users` | konta logowania do panelu |
| `auth_sessions` | `admin.sessions` | |
| `feedback_items` | `admin.feedback_items` | cross-tool feedback |
| `admin_accounts` | `circle_dm.accounts` | konta Circle.so do wysyłki DM |
| `community_members` | `circle_dm.members` | cache członków społeczności |
| `dm_threads` | `circle_dm.threads` | |
| `dm_messages` | `circle_dm.messages` | |
| `message_image_descriptions` | `circle_dm.message_image_descriptions` | |
| `draft_sessions` | `circle_dm.draft_sessions` | |
| `draft_iterations` | `circle_dm.draft_iterations` | |
| `sent_messages` | `circle_dm.sent_messages` | |
| `thread_checkups` | `circle_dm.checkups` | |
| `app_settings` | `circle_dm.settings` | singleton id=1; nazwa "app_settings" była zbyt globalna |
| `kb_documents` | `circle_dm.kb_documents` | |
| `assistant_conversations` | `circle_dm.assistant_conversations` | |
| `assistant_messages` | `circle_dm.assistant_messages` | |

### Zmiany nazw kolumn (tylko dwie, systematyczne)

| Stara kolumna | Nowa | Gdzie |
|---|---|---|
| `auth_account_id` | `user_id` | `admin.sessions`, `admin.feedback_items`, `circle_dm.assistant_conversations` |
| `admin_account_id` | `account_id` | wszystkie tabele `circle_dm.*` które ją mają |

Reszta kolumn 1:1 bez zmian. Enumy: te same wartości, przeniesione do schematów modułów.

**WAŻNE: API JSON się NIE zmienia.** Frontend dalej dostaje/wysyła `adminAccountId` itd. - mapowanie nazw robi warstwa DTO (pydantic aliasy), nie baza. Kontrakt HTTP/WS jest zamrożony 1:1 ze starym backendem (specyfikacje w `docs/spec/`).

## Stack backendu

- **FastAPI + uvicorn**, Python 3.12, zarządzanie zależnościami **uv**, lint/format **ruff**.
- **SQLAlchemy 2 (async) + asyncpg**, migracje **Alembic** (jedna historia migracji dla wszystkich schematów).
- **pydantic v2**: DTO z `alias_generator=to_camel` + `populate_by_name` - JSON w camelCase jak ze starego Hono.
- **httpx** (Circle Headless API, OpenAI), **python-multipart** (upload KB), **pypdf** (ekstrakcja PDF, następca unpdf).
- WebSocket: natywny FastAPI + własny broker (port `core/ws/broker.ts`).
- Workery (polling Circle, image-description, voice-transcript): asyncio taski startowane w lifespan.
- Claude Code CLI: `asyncio.create_subprocess_exec`, prompt przez stdin, stream-json na stdout (port `core/claude/spawn.ts`).
- Auth: scrypt przez `hashlib.scrypt` z parametrami IDENTYCZNYMI jak `crypto.scrypt` w Node - istniejące hashe muszą działać po migracji danych.

## Kontrakt zgodności (faza 1)

Cel (doprecyzowany przez Tomka 2026-06-10): ten sam wygląd, to samo działanie z perspektywy użytkownika, Claude Code CLI pod spodem. NIE kopiujemy bugów dla zasady.

**Twarde (warunek działania nietkniętego frontendu):**
1. Te same ścieżki: `/api/auth/*`, `/api/feedback*`, `/api/circle-dm/*`, `/ws`, `/health`, `/health/claude`.
2. Te same kształty JSON i eventów WS w zakresie, który frontend konsumuje (pola, camelCase, daty w formacie toISOString z `Z`, `/me` zawsze 200, 401 tylko dla wygasłej sesji) - źródło: `docs/spec/frontend-contract.md` + routes-a/b.
3. Te same zachowania workerów, orchestratorów i integracji Circle (semantyka, prompty) - `docs/spec/services-*.md`.
4. Cookie sesji, scrypt, lockout - identyczne (`docs/spec/auth-health.md`), hashe haseł migrują bez resetu.
5. Frontend = kopia starego `apps/web` (zmiany tylko build/workspace).

**Luzowane (quirki oryginału niewidoczne dla frontu - naprawione):** finalna lista wszystkich różnic (naprawione quirki, zachowane quirki, odstępstwa techniczne) jest w `docs/spec/port-odstepstwa.md`. Każda poprawka została potwierdzona przeciw `frontend-contract.md`, że front jej nie zauważy.

## Deploy (faza 1)

Jak stary admin: jeden kontener aplikacji (FastAPI serwuje też zbudowane SPA), osobny kontener Postgres 16, sieć `caddy_network`, Caddy hosta przepina `admin.befreeclub.pro` na nowy kontener. Kontener jako user uid=1000 (wymóg Claude CLI bypass-permissions), wolumen `claude-config` na tokeny `claude login`. Szczegóły w `infra/` i `docs/spec/deploy.md`.

Zmiana decyzji względem powyższego: frontend NIE jest serwowany przez FastAPI. Każdy frontend dostaje własny kontener nginx (faza 1: `bfc-admin-front`), backend (`bfc-backend`) to czyste API + WS. Caddy hosta robi split routing per domena: `/api/*`, `/ws` i `/health*` idą do backendu, reszta do nginx. Powód: deploye frontów niezależne od backendu i gotowość pod wiele frontendów w fazie 2+. Kod serwowania SPA w backendzie zostaje (parytet 1:1 ze starym kodem), w deployu `WEB_DIST_PATH` jest pusty, więc nieaktywny. Reszta bez zmian: uid=1000, wolumen `claude-config`, `caddy_network`.

## Fazy

- **Faza 1 (teraz)**: backend FastAPI 1:1 + kopia frontu + migracja danych ze starej bazy admina. Stary admin zostaje na VPS aż nowy przejdzie weryfikację, przepięcie = podmiana bloku domeny w Caddyfile.
- **Faza 2**: landing befreeclub.pl (moduły landing/billing/members/newsletter, migracja z Supabase).
- **Faza 3**: toole AI (ai-sales-coach, ai-offer-builder, ai-lead-finder, skill-spark) schodzą z Lovable.
