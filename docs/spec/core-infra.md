# Spec: core-infra (entrypoint, env, WS broker, logger, semaphore)

Źródła (wszystkie względem `/Users/tomasz/repos/befreeclub/admin/apps/server/src/`):

- `index.ts` - entrypoint serwera
- `core/env.ts` - walidacja zmiennych środowiskowych
- `core/ws/broker.ts` - broker WebSocket
- `core/util/logger.ts` - logger
- `core/util/semaphore.ts` - semafor
- pomocniczo (bo entrypoint je woła): `core/health/claude-health.ts`, `core/auth/middleware.ts` (`requireAuth`), `core/auth/sessions.ts` (`purgeExpiredSessions`), workery z `tools/circle-dm/services/*`, typy WS z `packages/shared/src/schemas/ws-events.ts` i `draft.ts`

Cel: odtworzenie 1:1 w FastAPI. Frontend React zostaje bez zmian, więc nazwy pól JSON, casing, statusy HTTP i protokół WS muszą być identyczne.

---

## 1. Entrypoint (`index.ts`)

### 1.1. Kolejność startu (funkcja `main()`)

1. `bootstrapAdminIfRequested()` - PRZED otwarciem portu (await).
2. Start serwera HTTP na porcie `env.PORT` (Node `@hono/node-server`). Log: `HTTP server listening on http://localhost:{port}`.
3. `attachWsServer(server)` - podpięcie WebSocketServer do tego samego serwera HTTP, path `/ws` (upgrade na tym samym porcie).
4. `startPolling()` - worker synchronizacji DM-ów Circle.
5. `startVoiceTranscriptWorker()` - worker transkrypcji głosówek (Whisper).
6. `startImageDescriptionWorker()` - worker opisów obrazków (vision).
7. Timer czyszczenia sesji: `setInterval` co `60 * 60 * 1000` ms (1h), timer `unref()` (nie blokuje exitu). Każdy tick woła `purgeExpiredSessions()`:
   - `DELETE FROM auth_sessions WHERE expires_at < now()` zwracając liczbę usuniętych wierszy,
   - jeśli `n > 0` log info: `purged {n} expired session(s)`,
   - błąd → log warn: `session purge failed: {message}` (NIE wywala procesu).
   - UWAGA: pierwszy tick dopiero PO godzinie, nie przy bootcie.
8. Handlery `SIGINT` i `SIGTERM` → `shutdown(signal)`.

Błąd na starcie (`main().catch`): log error `fatal startup error` + obiekt błędu, `process.exit(1)`.

### 1.2. Graceful shutdown (`shutdown(signal)`)

Kolejność:
1. log info: `Received {signal}, shutting down...`
2. `stopPolling()` (clearInterval)
3. `stopVoiceTranscriptWorker()` (clearInterval)
4. `stopImageDescriptionWorker()` (clearInterval)
5. `closeWs()` - zamyka wszystkie sockety klientów + serwer WS
6. `server.close()` → po zamknięciu `process.exit(0)`
7. fallback: `setTimeout(5000).unref()` → `process.exit(1)` jeśli close nie zdąży w 5 s

### 1.3. Bootstrap konta admina (`bootstrapAdminIfRequested`)

Wykonuje się TYLKO gdy ustawione są OBA: `BOOTSTRAP_ADMIN_EMAIL` i `BOOTSTRAP_ADMIN_TOKEN` (inaczej natychmiastowy return).

1. `SELECT id FROM admin_accounts WHERE email = {BOOTSTRAP_ADMIN_EMAIL} LIMIT 1`.
2. Jeśli istnieje: log info `Bootstrap admin already exists ({email}), skipping`, return.
3. Jeśli nie: INSERT do `admin_accounts` z polami:
   - `label` = `BOOTSTRAP_ADMIN_LABEL` jeśli ustawiony, inaczej `BOOTSTRAP_ADMIN_EMAIL`
   - `email` = `BOOTSTRAP_ADMIN_EMAIL`
   - `circleAdminToken` (kolumna `circle_admin_token`) = `BOOTSTRAP_ADMIN_TOKEN`
   - `systemPrompt` (kolumna `system_prompt`) = `DEFAULT_PERSONA` (dosłowna treść poniżej)
4. Log info: `Bootstrapped admin account for {email}`.

`DEFAULT_PERSONA` - DOSŁOWNIE (zachowaj treść co do znaku, łącznie z myślnikami listy i cudzysłowami):

```
Jesteś sprawnie piszącym współzałożycielem klubu Be Free Club.
- Piszesz po polsku, mówionym tonem, w pierwszej osobie.
- Krótko i naturalnie. Nie korpomowa, nie chatbotowy "rozumiem, że...".
- Bez pompatycznego "z chęcią", bez emoji w UI, bez wykrzykników na siłę.
- Pomagasz, ale stawiasz granice. Pisze człowiek do człowieka.
```

### 1.4. Kolejność middleware i routingu (KRYTYCZNA)

Rejestracja w tej kolejności:

1. **Request logger** (`hono/logger`) na `*` - loguje każdy request przez `log.debug` (scope `server`). Format hono loggera: linia wejścia `<-- {METHOD} {path}` i wyjścia `--> {METHOD} {path} {status} {elapsed}`. W porcie wystarczy access-log na poziomie debug.
2. **CORS** na `*`:
   - `origin`: `['http://localhost:5173', 'http://127.0.0.1:5173']` (tylko dev Vite; w prod SPA jest same-origin, CORS praktycznie nieużywany)
   - `allowHeaders`: `['Content-Type']`
   - `allowMethods`: `['GET', 'POST', 'PATCH', 'DELETE', 'PUT', 'OPTIONS']`
   - `credentials: true` (cookies przechodzą cross-origin)
3. **Publiczne endpointy (bez auth)**:
   - `GET /health` → `200`, body: `{"ok":true,"version":"0.1.0"}`
   - `GET /health/claude` → patrz 1.5
   - `route('/api/auth', authRoutes)` - login/logout/me. Celowo PRZED `requireAuth`: login musi działać bez sesji, `/api/auth/me` zwraca `{authenticated:false}` zamiast 401.
4. **`use('/api/*', requireAuth)`** - dotyczy WYŁĄCZNIE route'ów zarejestrowanych PO tej linii (w Hono middleware nie działa wstecz). Czyli `/api/auth/*` pozostaje publiczne, a chronione są:
5. `route('/api/feedback', feedbackRoutes)`
6. `route('/api/circle-dm', dmApp)`
7. **`onError`**: każdy nieobsłużony wyjątek → log error `unhandled error` + message, odpowiedź `500` z body `{"error": "<err.message>"}`.

Zachowanie `requireAuth` (szczegóły w spec auth, tu skrót bo wpływa na entrypoint):
- `NODE_ENV !== 'production'` → no-op, podstawia fake tożsamość `{ authAccountId: 0, email: 'dev@local' }`.
- W prod: cookie sesji → walidacja w DB; brak/nieważna → `401` body `{"error":"Unauthorized"}`.

### 1.5. `GET /health/claude`

Publiczny (monitoring zewnętrzny: n8n, Uptime Kuma). Query param: `deep` - deep probe wykonywany tylko gdy `deep === '1'` (dokładnie string `1`).

Status HTTP: `200` gdy `ok: true`, `503` gdy `ok: false`. Body (`ClaudeHealth`):

```json
{
  "ok": true,
  "checks": {
    "binary":      { "ok": true, "detail": "..." },
    "credentials": { "ok": true, "detail": "..." },
    "version":     { "ok": true, "detail": "..." },
    "deepProbe":   { "ok": true, "detail": "...", "cachedFor": 123, "lastRunAt": "ISO8601" }
  }
}
```

`deepProbe` obecny TYLKO przy `deep=1`. Pole `detail` opcjonalne (pomijane gdy undefined). Trzy podstawowe checki lecą równolegle (`Promise.all`):

- **binary**: czy plik `env.CLAUDE_BIN_PATH` istnieje. OK → `detail` = ścieżka. Fail → `detail` = `binary not found at {path}`.
- **credentials**: czy istnieje `~/.claude/.credentials.json`. Brak → `{ok:false, detail:"credentials file missing — run `claude login`"}`. Istnieje, ale rozmiar `< 50` bajtów → `{ok:false, detail:"credentials file suspiciously small ({size} bytes)"}`. OK → `detail` = `"{size} bytes"`.
- **version**: spawn `{CLAUDE_BIN_PATH} --version`, timeout 5000 ms. Exit != 0 → `{ok:false, detail:"exit {code}: {(stderr||stdout).slice(0,120).trim()}"}`. Exit 0 → `detail` = `stdout.trim().slice(0,60)`. Błąd spawna → `{ok:false, detail: err.message}`.
- **deepProbe** (realny round-trip LLM, cache 1h = `DEEP_PROBE_CACHE_MS = 60*60*1000`, cache in-memory per proces):
  - spawn `{CLAUDE_BIN_PATH} --print --model claude-haiku-4-5 --input-format text`, timeout 20000 ms, na stdin wysyłane `ping` i zamknięcie stdin.
  - Exit != 0 → `{ok:false, detail:"exit {code}: {(stderr||stdout).slice(0,200).trim()}"}`. Pusty output → `{ok:false, detail:"claude returned empty output"}`. OK → `detail` = `output.slice(0,60)`.
  - Cache: jeśli ostatni probe < 1h temu, NIE odpala nowego. W odpowiedzi zawsze: `cachedFor` = sekundy od ostatniego realnego probe (`Math.round((now - at)/1000)`), `lastRunAt` = ISO timestamp ostatniego probe.
  - `result.ok` końcowe = AND wszystkich checków łącznie z deepProbe (gdy deep=1).

### 1.6. Serwowanie SPA (tylko gdy `WEB_DIST_PATH` ustawione)

Rejestrowane PO `onError`, przed `main()`:

1. Log info: `Serving SPA from {root}`.
2. `GET /assets/*` → pliki statyczne z `{WEB_DIST_PATH}/assets/...` (ścieżka requestu mapowana 1:1 względem roota).
3. `GET /favicon.svg` → zawsze plik `{WEB_DIST_PATH}/favicon.svg`.
4. Catch-all `GET *` (SPA fallback):
   - jeśli `path.startsWith('/api/')` lub `path === '/ws'` → `404` (domyślny Hono notFound: text `404 Not Found`),
   - inaczej: czyta `{WEB_DIST_PATH}/index.html` (utf8, czytany przy KAŻDYM requeście, bez cache) i zwraca jako `200 text/html`.

W dev (`WEB_DIST_PATH` puste) tego bloku nie ma - SPA serwuje Vite na :5173.

---

## 2. Zmienne środowiskowe (`core/env.ts`)

Ładowane przez `dotenv/config` (plik `.env` w cwd), walidacja Zod przy imporcie modułu. Walidacja niepoprawna → `console.error('❌ Invalid environment variables:\n', fieldErrors)` i `process.exit(1)` - proces NIE startuje.

Konwencja "opcjonalny string": pusta wartość (`""`) jest traktowana jak brak (transform `v.length > 0 ? v : undefined`). W Pydantic trzeba to odtworzyć validatorem (pusty string → None).

| Zmienna | Typ | Default | Walidacja | Wymagana? |
|---|---|---|---|---|
| `DATABASE_URL` | string | - | musi być poprawnym URL | TAK (zawsze) |
| `CLAUDE_BIN_PATH` | string | - | min. 1 znak | TAK (zawsze) |
| `DRAFT_MODEL` | string | `claude-sonnet-4-6` | - | nie |
| `POLISH_MODEL` | string | `claude-opus-4-7` | - | nie |
| `CLAUDE_MAX_CONCURRENT` | int (coerce ze stringa) | `2` | int, min 1, max 8 | nie |
| `POLLING_INTERVAL_MS` | int (coerce) | `30000` | int, min 5000 | nie |
| `PORT` | int (coerce) | `3000` | int, 1-65535 | nie |
| `LOG_LEVEL` | enum | `info` | `debug\|info\|warn\|error` | nie |
| `NODE_ENV` | enum | `development` | `development\|production\|test` | nie |
| `BOOTSTRAP_ADMIN_LABEL` | string? | undefined | pusty → undefined | nie |
| `BOOTSTRAP_ADMIN_EMAIL` | string? | undefined | pusty → undefined; jeśli podany: poprawny email | nie |
| `BOOTSTRAP_ADMIN_TOKEN` | string? | undefined | pusty → undefined | nie |
| `WEB_DIST_PATH` | string? | undefined | pusty → undefined | nie (prod: ustawiana na `apps/web/dist`) |
| `OPENAI_API_KEY` | string? | undefined | pusty → undefined | nie (bez niej workery voice/image NIE startują) |
| `OPENAI_WHISPER_MODEL` | string | `whisper-1` | - | nie |
| `OPENAI_VISION_MODEL` | string | `gpt-4o-mini` | - | nie |
| `VOICE_TRANSCRIPT_INTERVAL_MS` | int (coerce) | `20000` | int, min 5000 | nie |
| `IMAGE_DESCRIPTION_INTERVAL_MS` | int (coerce) | `20000` | int, min 5000 | nie |

Uwaga: "wymagane w prod" nie jest egzekwowane osobno - `DATABASE_URL` i `CLAUDE_BIN_PATH` są wymagane zawsze (też w dev), reszta ma defaulty albo jest opcjonalna. `OPENAI_API_KEY` jest de facto wymagany w prod, żeby działały transkrypcje/opisy, ale walidacja go nie wymusza (worker tylko loguje warn i się nie uruchamia).

---

## 3. Broker WebSocket (`core/ws/broker.ts`)

Biblioteka `ws`, `WebSocketServer` podpięty do istniejącego serwera HTTP z opcją `path: '/ws'` - upgrade obsługiwany tylko na dokładnie tej ścieżce, ten sam port co HTTP.

### Protokół

- **Brak autoryzacji** na `/ws`. Żadnej weryfikacji cookie ani tokena przy upgrade. Każdy, kto się połączy, dostaje wszystkie eventy. (W prod chroni to tylko Caddy/sieć. Świadoma decyzja, port ma zachować to samo - albo patrz Uwagi.)
- **Brak heartbeat/ping-pong** na poziomie aplikacji. Serwer nie wysyła pingów, nie ubija martwych połączeń. Klient też nie pinguje.
- **Jednokierunkowy**: server → client. Serwer NIE obsługuje wiadomości przychodzących (brak handlera `message`; przychodzące ramki są ignorowane).
- **Brak adresowania**: wszystkie eventy idą broadcastem do WSZYSTKICH podłączonych klientów. Frontend sam filtruje po `adminAccountId` / `threadId` / `conversationId`.

### Cykl życia połączenia

1. Klient łączy się na `ws://host/ws` (prod: `wss://admin.befreeclub.pro/ws` przez Caddy).
2. Serwer dodaje socket do setu `clients`, loguje `client connected ({n} total) from {remoteAddress}`.
3. Serwer natychmiast wysyła ramkę powitalną: `{"type":"hello"}` (JSON, text frame).
4. `close` → usunięcie z setu, log `client disconnected ({n} total)`.
5. `error` na sockecie → log warn `client socket error` + message (socket NIE jest ręcznie zamykany).

### `broadcast(event)`

- Jeśli `clients` puste → return (bez serializacji).
- `JSON.stringify(event)` raz, wysyłka do każdego klienta z `readyState === OPEN` (inne stany pomijane bez błędu).

### `closeWs()`

- `close()` na każdym kliencie, wyczyszczenie setu, `wss.close()`, `wss = null`.

### Format ramek (eventy server→client)

Wszystkie ramki to JSON text z dyskryminatorem `type`. Pola w **camelCase** - frontend na tym polega, zachować dokładnie. Pełna unia (z `packages/shared/src/schemas/ws-events.ts`):

| `type` | Pola |
|---|---|
| `hello` | (tylko `type`; wysyłane raz po połączeniu, spoza schematu WsEvent) |
| `threads:updated` | `adminAccountId` int, `changedThreadIds` int[] |
| `thread:new_messages` | `threadId` int, `newCount` int |
| `messages:loaded` | `threadId` int, `count` int |
| `message:transcript_ready` | `threadId` int, `messageId` int |
| `message:image_description_ready` | `threadId` int, `messageId` int |
| `draft:status` | `threadId` int, `status` DraftStatus, `error?` string |
| `draft:token` | `threadId` int, `chunk` string, `iterationKind` IterationKind |
| `draft:complete` | `threadId` int, `iterationKind` IterationKind, `draft` string, `tokensUsed` int\|null, `costUsd` number\|null |
| `draft:tool_use` | `threadId` int, `toolName` string |
| `send:result` | `threadId` int, `ok` bool, `circleMessageId` int\|null, `error?` string |
| `assistant:token` | `conversationId` int, `chunk` string |
| `assistant:complete` | `conversationId` int, `messageId` int, `hasAction` bool |
| `assistant:error` | `conversationId` int, `error` string |

Enumy:
- `DraftStatus` = `'idle' | 'generating' | 'has_draft' | 'polishing' | 'ready_to_send' | 'sent' | 'error'`
- `IterationKind` = `'initial' | 'user_feedback' | 'polish'`

Pola opcjonalne (`error?`) przy braku wartości są POMIJANE w JSON (nie `null`). Pola nullable (`tokensUsed`, `costUsd`, `circleMessageId`) są obecne z wartością `null`.

---

## 4. Logger (`core/util/logger.ts`)

Prosty logger na `console`, bez zewnętrznych bibliotek. Poziomy i progi: `debug=10, info=20, warn=30, error=40`; threshold z `env.LOG_LEVEL` (ustalany raz przy imporcie). Komunikat logowany gdy `poziom >= threshold`.

Format linii (dokładny):

```
[{ISO8601 timestamp}] {LEVEL padEnd(5)} {scope padEnd(16)} {msg}
```

- timestamp: `new Date().toISOString()` (UTC, np. `2026-06-10T12:34:56.789Z`)
- LEVEL: uppercase, dopełniony spacjami do 5 znaków (`DEBUG`, `INFO `, `WARN `, `ERROR`)
- scope: nazwa modułu (np. `server`, `ws`, `polling`), dopełniona spacjami do 16 znaków
- opcjonalny `extra`: doklejany po spacji; string jak jest, inaczej `JSON.stringify(extra)`; jeśli serializacja rzuci - extra pomijane

Wyjście: `debug`/`info` → stdout (`console.log`), `warn` → `console.warn` (stderr), `error` → `console.error` (stderr).

API: `createLogger(scope)` zwraca obiekt `{debug, info, warn, error}`, każda metoda `(msg: string, extra?: unknown)`.

---

## 5. Semaphore (`core/util/semaphore.ts`)

Semafor in-process do limitowania równoległych spawnów `claude` CLI (limit subskrypcji Max). Tworzony z `capacity` (w praktyce `env.CLAUDE_MAX_CONCURRENT`).

Semantyka:
- `acquire(): Promise<() => void>` - jeśli są wolne sloty, zmniejsza licznik i od razu zwraca funkcję release. Jeśli nie - czeka w kolejce **FIFO** (`waiters.shift()`), promise rozwiązuje się dopiero gdy ktoś zrobi release.
- `release()` (prywatne, zwracane jako closure z acquire): jeśli ktoś czeka, budzi PIERWSZEGO z kolejki (slot przechodzi bezpośrednio, licznik `available` się nie zmienia); jeśli nikt nie czeka, `available += 1`.
- Brak timeoutu, brak anulowania, brak ochrony przed podwójnym release (podwójne wywołanie release zwiększy capacity - bug, którego nie należy odtwarzać celowo, po prostu wołać release raz, np. w `finally`).
- `queued` (getter) - liczba czekających.

Odpowiednik w Pythonie: `asyncio.Semaphore(capacity)` ma identyczną semantykę FIFO; wystarczy `async with`. Getter `queued` można odtworzyć przez `len(sem._waiters)` lub własną obudowę, jeśli gdzieś jest raportowany.

---

## 6. Workery startowane przy boot (parametry, bez logiki wewnętrznej - ta jest w specach circle-dm)

Wspólny wzorzec wszystkich trzech: idempotentny start (drugi `start*()` to no-op gdy interval już istnieje), **natychmiastowy pierwszy tick przy starcie** (`void tick()` przed `setInterval`), potem tick co interval. Ticki nie nakładają się (wewnętrzna flaga `running`). `stop*()` = clearInterval.

| Worker | Funkcja | Interval | Warunek startu |
|---|---|---|---|
| Polling DM (sync Circle) | `startPolling()` | `POLLING_INTERVAL_MS` (default 30000) | zawsze; log `Starting polling worker (interval {ms}ms)` |
| Transkrypcje głosówek | `startVoiceTranscriptWorker()` | `VOICE_TRANSCRIPT_INTERVAL_MS` (default 20000) | tylko gdy `OPENAI_API_KEY` ustawione; inaczej warn `OPENAI_API_KEY not set — voice transcripts disabled` i return |
| Opisy obrazków | `startImageDescriptionWorker()` | `IMAGE_DESCRIPTION_INTERVAL_MS` (default 20000) | jw., warn `OPENAI_API_KEY not set — image descriptions disabled` |

Dodatkowo polling-worker eksportuje `syncNow(adminAccountId?)` - ręczny trigger (np. po wysłaniu DM), używany przez routery.

---

## Uwagi dla portu na FastAPI

1. **Kolejność middleware vs Hono**: w Hono `use('/api/*', requireAuth)` działa tylko na route'y zarejestrowane PO nim, dlatego `/api/auth/*` jest publiczne mimo prefiksu `/api`. W FastAPI globalny middleware na `/api/*` objąłby też `/api/auth` - nie rób tego. Zamiast middleware użyj dependency (`Depends(require_auth)`) na routerach `feedback` i `circle-dm`, a router `auth` zostaw bez.
2. **WS bez autoryzacji**: `/ws` nie sprawdza sesji. Jeśli port ma być 1:1, zostaw tak samo (FastAPI: `await websocket.accept()` bez sprawdzania cookie). Świadomie zdecyduj, czy przy okazji nie dodać walidacji cookie sesji - to odstępstwo od 1:1, ale tania poprawka bezpieczeństwa.
3. **WS jest czysto jednokierunkowy i broadcastowy**: nie buduj routingu per-klient. Frontend filtruje eventy sam. Pamiętaj o ramce powitalnej `{"type":"hello"}` zaraz po accept - frontend może na niej polegać jako sygnale "połączono".
4. **Casing pól**: cały JSON (REST i WS) jest w camelCase (`adminAccountId`, `tokensUsed`, `circleMessageId`...). Pydantic domyślnie serializuje snake_case - ustaw `alias_generator=to_camel` + `populate_by_name=True` albo pisz aliasy ręcznie. Pola `Optional` z `undefined` w TS muszą być pomijane (`exclude_none` NIE wystarczy, bo `tokensUsed: null` musi zostać w JSON!). Rozróżnij: `error?` (pomijane) vs `tokensUsed: int|null` (obecne jako null). W Pydantic: `exclude_unset` lub osobne modele.
5. **Puste stringi w env = brak wartości**: `BOOTSTRAP_ADMIN_*`, `WEB_DIST_PATH`, `OPENAI_API_KEY` traktują `""` jak unset. W Pydantic Settings dodaj validator zamieniający pusty string na `None`, inaczej `WEB_DIST_PATH=""` w docker-compose włączy serwowanie SPA z pustego roota.
6. **Walidacja env przy imporcie**: oryginał robi exit(1) z czytelnym błędem zanim cokolwiek wstanie. W FastAPI ładuj settings na poziomie modułu (przed utworzeniem appki), nie lazy.
7. **Pierwszy tick workerów natychmiast przy boot**, nie po pierwszym interwale. Purge sesji odwrotnie: pierwszy raz dopiero po 1h. Nie pomyl tych dwóch wzorców.
8. **Timery `unref()`**: w Pythonie nie ma odpowiednika - jeśli użyjesz `asyncio.create_task` z pętlą `sleep`, pamiętaj o cancel w shutdown (lifespan FastAPI). Shutdown ma twardy deadline 5 s, po którym proces ubija się z kodem 1.
9. **SPA fallback czyta index.html z dysku przy każdym requeście** (świadomie, żeby podmiana builda działała bez restartu). Catch-all musi zwracać 404 dla `/api/*` i `/ws`, inaczej frontend dostanie HTML zamiast JSON-owego 404. W FastAPI: `StaticFiles` dla `/assets`, osobny route na `/favicon.svg`, catch-all GET z wykluczeniem prefiksów.
10. **`onError` zwraca surowy `err.message` w polu `error` z kodem 500** - frontend pokazuje to userowi. Zachowaj kształt `{"error": "..."}` (w FastAPI: globalny exception handler, nie domyślne `{"detail": ...}`!). To samo dotyczy 401: `{"error":"Unauthorized"}`, nie `{"detail":"Unauthorized"}`.
11. **Dev bypass auth**: `NODE_ENV != production` → wszystkie chronione endpointy działają bez logowania jako `{authAccountId: 0, email: 'dev@local'}`. Bez tego lokalny dev workflow się sypie.
12. **Deep probe `/health/claude?deep=1`**: cache 1h trzymany w pamięci procesu. Przy wielu workerach uvicorn/gunicorn każdy proces miałby własny cache i własne zużycie kwoty - uruchamiaj 1 worker (i tak wymaga tego stan WS clients oraz semafor in-process). To samo dotyczy brokera WS i Semaphore: **cała architektura zakłada JEDEN proces**.
13. `GET /health` zwraca zahardcodowaną wersję `0.1.0` - nie czyta package.json.
14. CORS allowlist tylko dla dev Vite (5173); prod działa same-origin. Nie dodawaj prod-domeny do allowlist bez potrzeby.
15. Health endpointy są publiczne celowo (monitoring zewnętrzny) i `/health/claude` spawnuje proces CLI przy każdym hicie (3 checki, w tym `claude --version` z timeoutem 5 s) - endpoint potrafi odpowiadać wolno; nie wrzucaj go pod auth ani nie cache'uj checków podstawowych (tylko deep probe ma cache).
