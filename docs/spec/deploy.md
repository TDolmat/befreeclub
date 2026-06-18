# Spec: deployment panelu admina BFC (Docker + VPS)

Źródła: `admin/Dockerfile`, `admin/docker-compose.yml`, `admin/DEPLOY.md`, `admin/README.md`, `admin/CLAUDE.md`, `admin/.env.production.example`, `admin/apps/server/.env.example`, `admin/biome.json`, `admin/package.json`, pomocniczo `admin/apps/server/package.json`, `admin/apps/server/src/core/db/migrate.ts`, `admin/.dockerignore`, `admin/turbo.json`, `admin/pnpm-workspace.yaml`.

Cel: pełny obraz deploymentu obecnego stacku Node (Hono + Drizzle + Postgres 16 + WS), tak żeby dało się odtworzyć zachowanie 1:1 przy porcie backendu na FastAPI. Frontend React zostaje bez zmian.

---

## 1. Architektura prod w skrócie

```
Internet (https://admin.befreeclub.pro)
        ↓
Hostinger DNS - A record: admin → IP VPS (ten sam co api.befreeclub.pro, 151.80.147.100)
        ↓
VPS OVH, port 443 - Caddy hosta (docker compose w /var/www/caddy/, sieć caddy_network)
        ↓ reverse_proxy bfc-admin:3000 (po nazwie kontenera, przez caddy_network)
docker compose (admin-server + admin-db)
        ↓
Hono :3000
  ├─ GET /health, /health/claude  → publiczne (monitoring, pre-auth)
  ├─ POST /api/auth/login         → publiczne
  ├─ /api/* (reszta)              → wymaga ciasteczka sesji
  └─ /* (frontend SPA)            → publiczne (gate w aplikacji)
```

- Auth prod: email + hasło (scrypt hash w DB) → cookie HttpOnly + Secure + SameSite=Lax, ważne 30 dni ze sliding window (każda aktywność odnawia).
- Lockout: 5 nieudanych prób w 15 min → blokada 1h per (email, IP).
- Dev (`NODE_ENV != production`): auth wyłączony całkowicie, middleware no-op, user `dev@local`.
- Caddy automatycznie: cert Let's Encrypt, WebSocket upgrade (dla `/ws` - streamowane drafty Claude'a), nagłówki X-Forwarded-For/Proto.

UWAGA na rozbieżność: diagram w `DEPLOY.md` mówi "reverse proxy → 127.0.0.1:3000" i "Port 127.0.0.1:3000 jest teraz otwarty na hoście", ale **aktualny `docker-compose.yml` NIE publikuje żadnego portu admin-server na hoście**. Caddy dochodzi do kontenera wyłącznie po sieci dockerowej `caddy_network` po nazwie `bfc-admin:3000`. DEPLOY.md jest w tym punkcie nieaktualny.

---

## 2. Dockerfile - dwa stage'e

Plik: `admin/Dockerfile`, header `# syntax=docker/dockerfile:1.7`.

### Stage 1: `builder` (`node:22-bookworm-slim AS builder`)

1. `corepack enable` (pnpm przez corepack).
2. `WORKDIR /repo`.
3. Najpierw same manifesty workspace dla layer cache (deps rzadko się zmieniają):
   - `package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json tsconfig.base.json` → `./`
   - `apps/server/package.json`, `apps/web/package.json`, `packages/shared/package.json` → odpowiednie podkatalogi.
4. Instalacja z cache mountem:
   ```dockerfile
   RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store \
       pnpm install --frozen-lockfile
   ```
5. Kopiowanie źródeł: tsconfigi + `src` serwera, tsconfigi + `vite.config.ts tailwind.config.ts postcss.config.js index.html` + `src` + `public` weba, tsconfig + `src` shared.
6. Build w kolejności (komentarz z oryginału: server tsc potrzebuje dist+d.ts z shared; node w prod nie załaduje `.ts` z node_modules):
   ```
   pnpm --filter @admin/shared build
   pnpm --filter @admin/server build
   pnpm --filter @admin/web build
   ```
7. Prune dev-depsów do czystego drzewa runtime:
   ```
   pnpm --filter @admin/server --prod deploy --legacy /pruned/server
   ```

### Stage 2: `runtime` (`node:22-bookworm-slim AS runtime`)

1. Pakiety systemowe (narzędzia dla Claude CLI do git/file ops + healthcheck):
   ```
   apt-get install -y --no-install-recommends ca-certificates curl git
   ```
2. Claude Code CLI globalnie z npm (cloud LLM, potrzebuje tylko wychodzącego HTTPS):
   ```
   npm install -g @anthropic-ai/claude-code
   ```
3. ENV wbudowane w obraz:
   ```
   NODE_ENV=production
   PORT=3000
   CLAUDE_BIN_PATH=/usr/local/bin/claude
   WEB_DIST_PATH=/app/web-dist
   ```
4. `WORKDIR /app`. Kopiowanie z buildera (wszystko `--chown=node:node`):
   - `/pruned/server` → `/app` (deps prod + `dist/`)
   - `/repo/apps/server/src/core/db/migrations` → `/app/dist/core/db/migrations` (pliki SQL migracji NIE są kompilowane przez tsc, kopiuje się je ręcznie obok skompilowanego `migrate.js`)
   - `/repo/apps/web/dist` → `/app/web-dist` (SPA serwowane przez backend)
5. Pre-create katalogu domowego Claude CLI z poprawnym właścicielem (named volume dziedziczy uprawnienia z punktu montowania przy pierwszym podpięciu):
   ```
   RUN mkdir -p /home/node/.claude && chown -R node:node /home/node
   ```
6. Shim wygodowy (runtime image celowo NIE ma pnpm, żeby był lekki):
   ```dockerfile
   RUN printf '#!/bin/sh\nexec node /app/dist/scripts/set-auth-password.js "$@"\n' \
       > /usr/local/bin/set-auth-password \
       && chmod +x /usr/local/bin/set-auth-password
   ```
   Użycie: `docker compose exec admin-server set-auth-password --email ...`
7. `USER node` - **kluczowe**: user `node` (uid=1000) jest wbudowany w bazowy obraz `node:22-bookworm-slim`. Kontener działa jako non-root, bo **Claude Code CLI odmawia działania z `--permission-mode=bypassPermissions` gdy jest uruchomiony jako root**. To jedyny powód; bez Claude CLI to "tylko" dobra praktyka.
8. `VOLUME ["/home/node/.claude"]` - persystencja credentiali Claude CLI między restartami.
9. `EXPOSE 3000`.
10. Healthcheck obrazu:
    ```dockerfile
    HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
        CMD curl -fsS http://localhost:${PORT}/health || exit 1
    ```
11. CMD - migracje przy KAŻDYM starcie (idempotentne), potem serwer:
    ```dockerfile
    CMD ["sh", "-c", "node dist/core/db/migrate.js && node dist/index.js"]
    ```
    Jeśli migracja padnie (`process.exit(1)`), serwer nie startuje; `restart: unless-stopped` powoduje ponawianie.

### .dockerignore (istotne dla kontekstu builda)

```
.git
.github
node_modules
**/node_modules
**/dist
**/.turbo
.turbo
.DS_Store
**/*.log
**/.env
**/.env.local
**/.env.production
**/.env.development
*.md
!apps/web/index.html
DEPLOY.md
PROJECT.md
README.md
.dockerignore
Dockerfile
docker-compose.yml
docker-compose.*.yml
```

Czyli: żadne `.env*` (poza example, których wzorce nie łapią... uwaga: `**/.env` łapie tylko dokładnie `.env`, pliki `.env.example` przechodzą, ale i tak nie są COPY-owane), żadne md, żaden lokalny dist/node_modules.

---

## 3. docker-compose.yml - usługi, sieci, wolumeny

Stack stoi na VPS obok `befreeclub-api` i `scopera` (gigscope). Plik compose w katalogu repo `~/repos/befreeclub/admin` na VPS, `.env` obok niego.

### Usługa `admin-db`

```yaml
admin-db:
  image: postgres:16-alpine
  container_name: bfc-admin-db
  restart: unless-stopped
  environment:
    POSTGRES_DB: bfc_admin
    POSTGRES_USER: admin
    POSTGRES_PASSWORD: ${DB_PASS}
    POSTGRES_INITDB_ARGS: "--encoding=UTF8 --locale=C"
  volumes:
    - admin-db-data:/var/lib/postgresql/data
  ports:
    - "127.0.0.1:5433:5432"
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U admin -d bfc_admin"]
    interval: 5s
    timeout: 5s
    retries: 10
  networks:
    - admin-net
```

- Postgres wystawiony **tylko na loopbacku VPS-a na porcie 5433** (host) → 5432 (kontener). Powód portu 5433: gigscope ma swojego Postgresa na 5432. Dostęp z zewnątrz wyłącznie przez tunel SSH (TablePlus itp.). Nigdy publicznie.
- DB jest w sieci `admin-net` (izolowana, NIE w caddy_network).
- Initdb: encoding UTF8, locale C.

### Usługa `admin-server`

```yaml
admin-server:
  build:
    context: .
    dockerfile: Dockerfile
  container_name: bfc-admin
  restart: unless-stopped
  depends_on:
    admin-db:
      condition: service_healthy
  environment:
    NODE_ENV: production
    DATABASE_URL: postgresql://admin:${DB_PASS}@admin-db:5432/bfc_admin
    CLAUDE_BIN_PATH: /usr/local/bin/claude
    DRAFT_MODEL: ${DRAFT_MODEL:-claude-sonnet-4-6}
    POLISH_MODEL: ${POLISH_MODEL:-claude-opus-4-7}
    CLAUDE_MAX_CONCURRENT: ${CLAUDE_MAX_CONCURRENT:-2}
    POLLING_INTERVAL_MS: ${POLLING_INTERVAL_MS:-30000}
    PORT: 3000
    LOG_LEVEL: ${LOG_LEVEL:-info}
    BOOTSTRAP_ADMIN_LABEL: ${BOOTSTRAP_ADMIN_LABEL:-}
    BOOTSTRAP_ADMIN_EMAIL: ${BOOTSTRAP_ADMIN_EMAIL:-}
    BOOTSTRAP_ADMIN_TOKEN: ${BOOTSTRAP_ADMIN_TOKEN:-}
    OPENAI_API_KEY: ${OPENAI_API_KEY:-}
    OPENAI_WHISPER_MODEL: ${OPENAI_WHISPER_MODEL:-whisper-1}
    OPENAI_VISION_MODEL: ${OPENAI_VISION_MODEL:-gpt-4o-mini}
    VOICE_TRANSCRIPT_INTERVAL_MS: ${VOICE_TRANSCRIPT_INTERVAL_MS:-20000}
    IMAGE_DESCRIPTION_INTERVAL_MS: ${IMAGE_DESCRIPTION_INTERVAL_MS:-20000}
  volumes:
    - claude-config:/home/node/.claude
  networks:
    - admin-net
    - caddy_network
```

- **Brak mapowania portów na hosta.** Caddy łączy się przez sieć `caddy_network` po nazwie kontenera `bfc-admin:3000`.
- `depends_on` z `condition: service_healthy` - serwer (a więc i migracje w CMD) startuje dopiero po przejściu healthchecku Postgresa.
- Wolumen `claude-config` montowany w `/home/node/.claude` - tu lądują tokeny OAuth po `claude login` (device-code flow), przeżywają restarty i rebuildy obrazu.

### Wolumeny i sieci

```yaml
volumes:
  admin-db-data:    # dane Postgresa
  claude-config:    # credentiale Claude CLI (~/.claude usera node)

networks:
  admin-net:
    driver: bridge   # prywatna sieć server ↔ db
  caddy_network:
    external: true   # istniejąca sieć stacku Caddy hosta (/var/www/caddy/)
```

---

## 4. Zmienne środowiskowe per plik example

Pliki `.env` BEZ "example" w nazwie istnieją i NIE były czytane (sekrety):
- `admin/apps/server/.env` - istnieje lokalnie (dev).
- Na VPS istnieje `admin/.env` (tworzony z `.env.production.example`). W repo lokalnie root `.env` nie istnieje.

### `admin/.env.production.example` (root, konsumowany przez docker-compose na VPS)

| Zmienna | Wartość example / default | Opis |
|---|---|---|
| `DB_PASS` | `CHANGE_ME_TO_A_LONG_RANDOM_STRING` | Hasło Postgresa, mocne losowe ≥24 znaki (`openssl rand -hex 24`). Używane jednocześnie w `POSTGRES_PASSWORD` i w `DATABASE_URL` składanym w compose. **Wymagane.** |
| `DRAFT_MODEL` | `claude-sonnet-4-6` | Model Claude do draftów + iteracji (tani i szybki). |
| `POLISH_MODEL` | `claude-opus-4-7` | Model Claude do polish pass (najwyższa jakość). |
| `CLAUDE_MAX_CONCURRENT` | `2` | Max równoległych procesów `claude` (ogranicza presję na subskrypcję Max). |
| `POLLING_INTERVAL_MS` | `30000` | Interwał polling workera (ms). |
| `LOG_LEVEL` | `info` | Poziom logów. |
| `OPENAI_API_KEY` | (puste) | Klucz OpenAI dla Whisper (głosówki) + GPT-4o-mini vision (obrazki). **Puste = oba workery wyłączone: logują WARN przy boocie i pomijają ticki.** |
| `OPENAI_WHISPER_MODEL` | `whisper-1` | Model transkrypcji głosówek. |
| `OPENAI_VISION_MODEL` | `gpt-4o-mini` | Model opisów obrazków. |
| `VOICE_TRANSCRIPT_INTERVAL_MS` | `20000` | Interwał workera transkrypcji (ms). |
| `IMAGE_DESCRIPTION_INTERVAL_MS` | `20000` | Interwał workera opisów obrazków (ms). |
| `BOOTSTRAP_ADMIN_LABEL` | `Tomasz` | Opcjonalny bootstrap pierwszego konta admina Circle. |
| `BOOTSTRAP_ADMIN_EMAIL` | `tomasz@befreeclub.pl` | jw. |
| `BOOTSTRAP_ADMIN_TOKEN` | `YOUR_CIRCLE_HEADLESS_ADMIN_TOKEN` | Circle Headless admin token. Komentarz z pliku: "Creates one row in **admin_accounts** on first start if EMAIL+TOKEN are set. Skip if you'd rather add accounts via the UI after first login." |

WAŻNE rozróżnienie: `BOOTSTRAP_ADMIN_*` tworzy wiersz w tabeli **`admin_accounts`** (konto Circle z tokenem Headless, "w czyim imieniu wysyłamy DM-y"). To NIE jest konto logowania do panelu - te żyją w tabeli **`auth_accounts`** i tworzy się je skryptem `set-auth-password` (sekcja 6). Token `BOOTSTRAP_ADMIN_TOKEN` / `circle_admin_token` mintuje JWT dla DOWOLNEGO członka community - traktować env i DB jak sekret, rotacja w panelu Circle.

### `admin/apps/server/.env.example` (dev, lokalny Mac)

| Zmienna | Wartość example | Opis |
|---|---|---|
| `DATABASE_URL` | `postgresql://tomasz@localhost:5432/bfc_admin` | Postgres.app/Homebrew, peer auth bez hasła (user = login user). |
| `CLAUDE_BIN_PATH` | `/Users/tomasz/.local/bin/claude` | Ścieżka do binarki `claude` (sprawdzić `which claude`). |
| `DRAFT_MODEL` | `claude-sonnet-4-6` | jw. |
| `POLISH_MODEL` | `claude-opus-4-7` | jw. |
| `CLAUDE_MAX_CONCURRENT` | `2` | jw. |
| `POLLING_INTERVAL_MS` | `30000` | jw. |
| `PORT` | `3000` | Port HTTP backendu. |
| `LOG_LEVEL` | `info` | jw. |
| `NODE_ENV` | `development` | Dev = auth bypass (`dev@local`). |
| `OPENAI_API_KEY` | (puste) | "Leave empty in dev — worker skips when missing, voice messages just show no transcript. Required in prod for [głosówka] context to reach AI." |
| `OPENAI_WHISPER_MODEL` | `whisper-1` | jw. |
| `OPENAI_VISION_MODEL` | `gpt-4o-mini` | jw. |
| `VOICE_TRANSCRIPT_INTERVAL_MS` | `20000` | jw. |
| `IMAGE_DESCRIPTION_INTERVAL_MS` | `20000` | jw. |
| `BOOTSTRAP_ADMIN_LABEL` | (puste) | Opcjonalny bootstrap admin_account przy pierwszym starcie. |
| `BOOTSTRAP_ADMIN_EMAIL` | (puste) | jw. |
| `BOOTSTRAP_ADMIN_TOKEN` | (puste) | jw. |

Dodatkowo wbudowane w obraz (Dockerfile ENV, niezależne od `.env`): `NODE_ENV=production`, `PORT=3000`, `CLAUDE_BIN_PATH=/usr/local/bin/claude`, `WEB_DIST_PATH=/app/web-dist`. `WEB_DIST_PATH` występuje TYLKO w Dockerfile (nie ma go w żadnym example) - wskazuje backendowi katalog zbudowanego SPA do serwowania.

---

## 5. Co się dzieje przy starcie kontenera

Sekwencja (CMD = `sh -c "node dist/core/db/migrate.js && node dist/index.js"`):

1. **Migracje** (`dist/core/db/migrate.js`, źródło `apps/server/src/core/db/migrate.ts`):
   - Loguje `▶ Running migrations against <DATABASE_URL z zamaskowanym hasłem>` - maskowanie regexem `/:[^:@]+@/` → `:***@`.
   - Folder migracji: `resolve(dirname(import.meta.url), 'migrations')` - czyli obok pliku migrate, w dev `src/core/db/migrations/`, w prod `dist/core/db/migrations/` (skopiowane w Dockerfile).
   - Klient `postgres(env.DATABASE_URL, { max: 1 })` + `drizzle-orm/postgres-js/migrator`.
   - Po migracjach Drizzle wykonuje **dodatkowy idempotentny SQL poza systemem migracji**: instaluje funkcję triggera i triggery `updated_at`:
     ```sql
     CREATE OR REPLACE FUNCTION update_updated_at_column()
     RETURNS TRIGGER AS $$
     BEGIN
       NEW.updated_at = NOW();
       RETURN NEW;
     END;
     $$ language 'plpgsql';
     ```
     a potem dla każdej z tabel: `admin_accounts`, `draft_sessions`, `kb_documents`, `assistant_conversations`, `assistant_messages`, `feedback_items` wzorzec:
     ```sql
     DROP TRIGGER IF EXISTS set_<tabela>_updated_at ON <tabela>;
     CREATE TRIGGER set_<tabela>_updated_at BEFORE UPDATE ON <tabela>
       FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();
     ```
   - Sukces: log `✅ Migrations applied`, `process.exit(0)`. Błąd: `❌ Migration failed:`, `process.exit(1)` → serwer nie startuje, docker restartuje kontener.
2. **Serwer** (`dist/index.js`): HTTP na `PORT` (3000), serwuje SPA z `WEB_DIST_PATH`, startuje polling worker. Oczekiwane logi pierwszego startu (z DEPLOY.md):
   ```
   ▶ Running migrations against postgresql://admin:***@admin-db:5432/bfc_admin
   ✅ Migrations applied
   [server] HTTP server listening on http://localhost:3000
   [server] Serving SPA from /app/web-dist
   [polling] Starting polling worker (interval 30000ms)
   ```
3. Jeśli ustawione `BOOTSTRAP_ADMIN_EMAIL`+`BOOTSTRAP_ADMIN_TOKEN`: przy pierwszym starcie backend tworzy jeden wiersz w `admin_accounts`.
4. Bez `OPENAI_API_KEY`: workery voice/image logują WARN i pomijają ticki.

Healthcheck dockerowy zaczyna pingować `GET /health` po 20s (start-period), co 30s, timeout 5s, 3 retraje.

---

## 6. Procedury operacyjne (VPS)

### First-time setup (skrót z komentarza w compose + DEPLOY.md)

1. DNS w Hostinger: `Type: A  Name: admin  Points to: <IP_VPS>  TTL: 3600`. Weryfikacja: `dig +short admin.befreeclub.pro`.
2. `ssh tomasz@vps`, `cd ~/repos/befreeclub`, `git clone <github URL> admin`, `cd admin`.
3. `cp .env.production.example .env`, uzupełnić `DB_PASS` (`openssl rand -hex 24`), `BOOTSTRAP_ADMIN_LABEL/EMAIL/TOKEN`; modele zostawić domyślne.
4. `docker compose up -d --build`. Weryfikacja: `docker compose ps`, `docker compose logs admin-server | tail -20`.
5. Caddy: w `/var/www/caddy/Caddyfile` dopisać blok (przed sekcją "FUTURE APPS"):
   ```caddy
   admin.befreeclub.pro {
       reverse_proxy bfc-admin:3000
   }
   ```
   Reload: `cd /var/www/caddy && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. Cert Let's Encrypt wystawia się sam (1-2 min). Test: `curl https://admin.befreeclub.pro/health` → `{"ok":true,"version":"0.1.0"}`.
6. `docker compose exec -it admin-server claude login` - device-code flow: CLI wypisze URL typu `https://console.anthropic.com/auth/device?code=ABCD-1234`, otwierasz na lokalnym Macu zalogowany w Claude Max, Authorize, terminal wykryje `✓ Authenticated`. Tokeny lądują w wolumenie `claude-config`. Test: `docker compose exec admin-server claude --print "Powiedz cześć."`.
7. `docker compose exec -it admin-server set-auth-password --email tomasz@befreeclub.pl` - hasło ze stdin (bez echo, bez historii shella), minimum 12 znaków, pyta dwa razy. Skrypt: hash scrypt (**N=2^16, r=8, p=1**), INSERT/UPDATE do `auth_accounts`, **unieważnia wszystkie istniejące sesje konta**.
8. Pierwszy login na `https://admin.befreeclub.pro` (LoginPage email+hasło) → Dashboard.

### Update po zmianach kodu

```bash
ssh vps
cd ~/repos/befreeclub/admin
git pull
docker compose up -d --build admin-server   # admin-db nie ruszamy, bez rebuildu
```

Migracje DB lecą automatycznie przy każdym starcie (idempotentne). Dane DB i credentiale Claude przeżywają (named volumes).

### Zmiana hasła / kolejny użytkownik / usunięcie

- Zmiana hasła: ten sam `set-auth-password --email <email>` jeszcze raz - zastępuje hash i unieważnia wszystkie aktywne sesje konta.
- Kolejny user: `set-auth-password --email nowy@example.com`, bez limitów ilościowych. (DEPLOY.md pokazuje tu `pnpm set-auth-password:prod`, ale runtime image NIE MA pnpm - działa tylko shim `set-auth-password`. Patrz pułapki.)
- Usunięcie konta logowania: `DELETE FROM auth_accounts WHERE email='...';` (np. `docker compose exec admin-db psql -U admin bfc_admin -c "..."`). Sesje kasują się przez CASCADE.

### Monitoring (n8n)

Endpointy publiczne (pre-auth):
- `GET /health` → `{"ok":true,"version":"0.1.0"}`
- `GET /health/claude` → `{"ok":true,"checks":{binary,credentials,version}}`
- `GET /health/claude?deep=1` → dodatkowo `deepProbe` (realny round-trip do Claude, **cache 1h**)

Sugerowany workflow: Schedule co 10 min → HTTP GET `/health/claude` → IF `ok === false` → alert. Drugi co godzinę z `?deep=1` (wykrywanie wygasłych tokenów Claude).

### Rollback

```bash
cd ~/repos/befreeclub/admin && docker compose down
# Caddyfile: usunąć blok admin.befreeclub.pro, caddy reload
# Hostinger: usunąć A record "admin"
```
Reszta domeny (`befreeclub.pro`/`www` → Circle, `api` → befreeclub-api Flask na 151.80.147.100, `gigscope`) nietknięta.

---

## 7. Monorepo i tooling (kontekst builda)

- **pnpm workspaces** (`pnpm-workspace.yaml`): `apps/*`, `packages/*`; `allowBuilds`: `@biomejs/biome`, `esbuild`. `packageManager: pnpm@11.1.0`, engines `node >=20` (prod 22), `pnpm >=10`.
- **Turbo** (`turbo.json`): tasks `dev` (no cache, persistent, `dependsOn ^build`), `build` (`dependsOn ^build`, outputs `dist/**`, `.vite/**`), `lint`, `typecheck` (`dependsOn ^build`), `db:generate`/`db:migrate` (no cache).
- **Root package.json scripts**: `dev|build|lint|typecheck` przez turbo; `db:generate|db:migrate|db:studio|set-auth-password` delegowane do `@admin/server`; `format` = `biome format --write .`, `format:check`.
- **Server package.json scripts** (istotne dla deployu): `build` = `tsc -p tsconfig.build.json`; `start:migrate` = `node dist/core/db/migrate.js && node dist/index.js` (to samo co CMD kontenera); pary dev/prod: `db:migrate` (tsx) / `db:migrate:prod` (node dist), `set-auth-password` / `set-auth-password:prod`, `backfill-voice-transcripts(:prod)`, `backfill-image-descriptions(:prod)`.
- **Zależności runtime serwera** (do zmapowania na Pythona): `@hono/node-server`, `@hono/zod-validator`, `hono`, `drizzle-orm`, `drizzle-zod`, `postgres`, `dotenv`, `unpdf` (parsowanie PDF!), `uuid`, `ws`, `zod`, `@admin/shared` (workspace).
- **Biome** (`biome.json`, lint+format zamiast eslint/prettier): formatter spaces/2, lineWidth 100, LF; JS: single quotes, trailing commas all, semicolons always, arrow parens always; linter recommended + `noExplicitAny: warn`, `noConsoleLog: off`, `useImportType: warn`, `useNodejsImportProtocol: warn`, `noUnusedVariables/noUnusedImports: warn`; organizeImports on; ignores: `node_modules`, `dist`, `.turbo`, `**/drizzle/**`, `apps/server/src/db/migrations/**` (uwaga: ta ścieżka ignore jest nieaktualna, realna to `apps/server/src/core/db/migrations`).

---

## 8. Uwagi dla portu na FastAPI

1. **User non-root z uid=1000 zostaje.** Powód istnienia `USER node` to Claude Code CLI: odmawia `--permission-mode=bypassPermissions` jako root. W obrazie Pythona nie ma usera `node` - trzeba go stworzyć (`useradd -u 1000 -m node` albo dowolna nazwa, byle uid=1000 i istniejący `$HOME`). Claude CLI nadal wymaga Node w runtime → obraz Pythona musi mieć **i Pythona, i Node 20+** (np. `python:3.12-slim` + nodejs z nodesource, albo multi-stage z kopiowaniem node). `npm install -g @anthropic-ai/claude-code` zostaje.
2. **Wolumen `claude-config` musi trafić w `$HOME/.claude` nowego usera** i katalog musi być pre-created z chown przed `VOLUME`/mountem, inaczej named volume zamontuje się jako root i `claude login` nie zapisze credentiali. Przy zmianie home directory (np. `/home/app`) zaktualizować mount w compose: `claude-config:/home/app/.claude`. Jeśli wolumen już istnieje z prodowymi tokenami, pliki w nim mają ownera uid=1000 - stąd twardy wymóg uid=1000 dla nowego usera.
3. **Brak portu na hoście dla serwera.** Nie dodawać `ports:` do admin-server. Caddy łączy się po `caddy_network` z `bfc-admin:3000`. Nazwa kontenera `bfc-admin` jest częścią kontraktu z Caddyfile - nie zmieniać, albo zmienić też Caddyfile. DEPLOY.md w dwóch miejscach (diagram, krok 2) błędnie sugeruje publikację 127.0.0.1:3000.
4. **Migracje przy każdym starcie kontenera, przed serwerem, fail = brak startu.** Odpowiednik w FastAPI: np. `CMD ["sh", "-c", "alembic upgrade head && uvicorn ..."]`. Migrator robi więcej niż schema: po migracjach instaluje **funkcję `update_updated_at_column()` i triggery `set_<tabela>_updated_at`** dla `admin_accounts`, `draft_sessions`, `kb_documents`, `assistant_conversations`, `assistant_messages`, `feedback_items` - poza plikami migracji, idempotentnie przy każdym starcie. W porcie: albo przenieść do migracji Alembica, albo odtworzyć krok post-migrate. Bez tego `updated_at` przestanie się aktualizować. Log migracji maskuje hasło w URL (`:[^:@]+@` → `:***@`).
5. **Pliki migracji nie przechodzą przez kompilator** - w Dockerfile kopiowane osobno do `dist/core/db/migrations`. W Pythonie analogicznie pilnować, żeby katalog wersji Alembica trafił do obrazu.
6. **`depends_on: condition: service_healthy`** na admin-db jest konieczny - bez niego migracje wystartują przed gotowym Postgresem. Healthcheck DB: `pg_isready -U admin -d bfc_admin`, 5s/5s/10 prób.
7. **Healthcheck serwera przez curl** - `curl` musi być w obrazie (w slim Pythonie często go nie ma). Parametry do zachowania: interval 30s, timeout 5s, start-period 20s, retries 3, `GET http://localhost:${PORT}/health`. Endpointy `/health` i `/health/claude` muszą zostać publiczne (pre-auth) i zachować shape JSON (`{"ok":true,"version":"0.1.0"}`), bo pinguje je n8n.
8. **Shim `set-auth-password`** w `/usr/local/bin` to część kontraktu operacyjnego (DEPLOY.md każe `docker compose exec admin-server set-auth-password ...`). W porcie zrobić analogiczny shim wołający skrypt Pythona. Zachować zachowanie skryptu: prompt o hasło 2x ze stdin bez echa, min 12 znaków, scrypt N=2^16 r=8 p=1, INSERT/UPDATE `auth_accounts`, unieważnienie wszystkich sesji konta. Uwaga: komendy `pnpm set-auth-password:prod` z DEPLOY.md (krok 5 i "Dodanie kolejnego użytkownika") są martwe - runtime image nie ma pnpm, działa tylko shim. Nie przenosić tego błędu do nowej dokumentacji.
9. **Dwie tabele kont, łatwo pomylić**: `admin_accounts` (konta Circle z tokenem Headless; bootstrap przez `BOOTSTRAP_ADMIN_*` przy pierwszym starcie) vs `auth_accounts` (logowanie do panelu; `set-auth-password`). `DELETE FROM auth_accounts` kasuje sesje przez CASCADE - zachować FK z ON DELETE CASCADE.
10. **Wszystkie defaulty env są zdublowane w compose** (`${VAR:-default}`): brak zmiennej w `.env` nie wywala startu. Aplikacja po stronie Pythona powinna mieć te same defaulty w walidacji env (pydantic-settings), żeby dev bez pełnego `.env` zachowywał się tak samo: `DRAFT_MODEL=claude-sonnet-4-6`, `POLISH_MODEL=claude-opus-4-7`, `CLAUDE_MAX_CONCURRENT=2`, `POLLING_INTERVAL_MS=30000`, `LOG_LEVEL=info`, `OPENAI_WHISPER_MODEL=whisper-1`, `OPENAI_VISION_MODEL=gpt-4o-mini`, `VOICE_TRANSCRIPT_INTERVAL_MS=20000`, `IMAGE_DESCRIPTION_INTERVAL_MS=20000`. Puste `OPENAI_API_KEY` ≠ błąd: workery voice/image mają być wyłączone z WARN-em przy boocie.
11. **`NODE_ENV` steruje auth bypass** (`!= production` → no-op middleware, user `dev@local`). W FastAPI potrzebny odpowiednik (np. `APP_ENV`); pamiętać, że Dockerfile i compose ustawiają `NODE_ENV=production` w dwóch miejscach.
12. **Backend serwuje SPA** z `WEB_DIST_PATH=/app/web-dist` (build weba kopiowany do obrazu serwera). Frontend zostaje Reactowy, więc w obrazie FastAPI nadal potrzebny stage budujący `apps/web` (Node) i StaticFiles z fallbackiem na `index.html` dla SPA routingu. `/ws` (WebSocket, streamowane drafty) przechodzi przez Caddy bez dodatkowej konfiguracji.
13. **Postgres na host-porcie 5433** (loopback only) to świadoma decyzja (kolizja z gigscope na 5432) i kanał dostępu przez tunel SSH - zachować.
14. **`POSTGRES_INITDB_ARGS: "--encoding=UTF8 --locale=C"`** - locale C wpływa na sortowanie tekstu (ORDER BY po polskich znakach inne niż w pl_PL). Przy migracji danych do nowej bazy zachować te same initdb args, inaczej zmieni się collation.
15. Nazwy wolumenów (`admin-db-data`, `claude-config`) i kontenerów (`bfc-admin`, `bfc-admin-db`) zachować przy podmianie compose na VPS - inaczej docker utworzy nowe, puste wolumeny (utrata danych DB i tokenów Claude). Compose prefiksuje nazwy wolumenów nazwą projektu (katalogu `admin`) - nowy stack musi stać w tym samym katalogu albo używać `external: true` / jawnego `name:`.
