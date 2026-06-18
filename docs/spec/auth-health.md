# Spec: Auth + Health (panel admina BFC) - port 1:1 na FastAPI

Źródła (stan na 2026-06-10):

- `admin/apps/server/src/core/auth/password.ts`
- `admin/apps/server/src/core/auth/sessions.ts`
- `admin/apps/server/src/core/auth/middleware.ts`
- `admin/apps/server/src/core/auth/routes.ts`
- `admin/apps/server/src/core/auth/rate-limit.ts`
- `admin/apps/server/src/core/health/claude-health.ts`
- `admin/apps/server/src/scripts/set-auth-password.ts`
- kontekst montowania: `admin/apps/server/src/index.ts`, env: `admin/apps/server/src/core/env.ts`, schema: `admin/apps/server/src/core/db/schema.ts`

---

## 1. Hashowanie haseł (KRYTYCZNE - migrujemy istniejące hashe)

### 1.1 Parametry scrypt (dokładne)

```
N      = 65536        (2^16, CPU/memory cost)
r      = 8            (block size)
p      = 1            (parallelization)
keylen = 64           (dkLen, długość klucza pochodnego w bajtach)
salt   = 16 bajtów    losowych (crypto.randomBytes(16))
maxmem = 256 * 1024 * 1024  (268435456 bajtów; Node wymaga podniesienia limitu, bo 128*N*r = 64 MiB > domyślne 32 MiB)
```

Komentarz w oryginale: "OWASP 2024 recommendation for interactive logins", ~150 ms na hash.

### 1.2 Format stringa `password_hash` w bazie (bajt w bajt)

```
scrypt$<N>$<r>$<p>$<saltHex>$<hashHex>
```

Konkretnie dla obecnych parametrów:

```
scrypt$65536$8$1$<32 znaki hex soli, lowercase>$<128 znaków hex klucza, lowercase>
```

- Separator: znak `$`. String ma DOKŁADNIE 6 części po `split('$')`: `['scrypt', N, r, p, saltHex, hashHex]`.
- Hex zawsze lowercase (tak generuje `Buffer.toString('hex')`; Python `bytes.hex()` też daje lowercase - zgodne).
- Format jest samoopisujący: weryfikacja czyta N, r, p i długość hasha ze stringa, NIE z konstant. Dzięki temu można później podbić parametry bez łamania starych hashy. **Port musi tak samo czytać parametry ze stringa przy weryfikacji.**

### 1.3 `hashPassword(password) -> string`

1. Wygeneruj 16 losowych bajtów soli.
2. `scrypt(password_utf8, salt, N=65536, r=8, p=1, dklen=64, maxmem=256MiB)`.
3. Zwróć `scrypt$65536$8$1$` + hex(salt) + `$` + hex(derived).

### 1.4 `verifyPassword(password, stored) -> bool`

Kolejność i edge case'y (każdy fail = `false`, nigdy wyjątek):

1. `stored.split('$')` - jeśli liczba części != 6 albo część `[0] != 'scrypt'` → `false`.
2. Parsuj N, r, p jako int (base 10); jeśli któreś nie jest skończoną liczbą → `false`.
3. Dekoduj `parts[4]` (salt) i `parts[5]` (expected) z hex; błąd dekodowania → `false`.
   (Uwaga: w Node `Buffer.from(x,'hex')` nie rzuca tylko obcina; w Pythonie `bytes.fromhex` rzuca `ValueError` - złap i zwróć `false`, zachowanie efektywnie to samo: złe hashe nie przechodzą.)
4. `derived = scrypt(password, salt, dklen=len(expected), N, r, p, maxmem=256MiB)`; wyjątek scrypta (np. absurdalne parametry) → `false`.
5. Jeśli `len(derived) != len(expected)` → `false`.
6. Porównanie stałoczasowe (`crypto.timingSafeEqual`; w Pythonie `hmac.compare_digest`) → wynik.

**dklen przy weryfikacji = długość zdekodowanego `hashHex`**, nie stała 64. To ważne dla dummy hasha (1.5).

### 1.5 Dummy hash anty-enumeracyjny (w loginie)

Gdy konto o danym emailu nie istnieje, weryfikujemy hasło względem dosłownego stringa:

```
scrypt$65536$8$1$00$00
```

Czyli: salt = 1 bajt `0x00`, expected = 1 bajt `0x00`, dklen = 1. scrypt i tak liczy się ~150 ms (koszt zdominowany przez N), więc timing "brak emaila" ≈ timing "złe hasło". Wynik weryfikacji jest praktycznie zawsze `false` (a nawet gdyby był `true`, kod i tak sprawdza `!account || !passwordOk`).

---

## 2. Sesje

### 2.1 Tabele (Postgres, nazwy kolumn snake_case)

`auth_accounts`:

| kolumna | typ | uwagi |
|---|---|---|
| `id` | bigserial PK | |
| `email` | text NOT NULL UNIQUE | przechowywany lowercase (normalizuje skrypt i login) |
| `password_hash` | text NOT NULL | format z sekcji 1.2 |
| `created_at` | timestamptz NOT NULL DEFAULT now() | |
| `updated_at` | timestamptz NOT NULL DEFAULT now() | |

`auth_sessions`:

| kolumna | typ | uwagi |
|---|---|---|
| `id` | text PK | session id, patrz 2.2 |
| `auth_account_id` | bigint NOT NULL REFERENCES auth_accounts(id) ON DELETE CASCADE | |
| `expires_at` | timestamptz NOT NULL | |
| `last_seen_at` | timestamptz NOT NULL DEFAULT now() | |
| `ip_addr` | text NULL | |
| `user_agent` | text NULL | |
| `created_at` | timestamptz NOT NULL DEFAULT now() | |

Index: `idx_auth_sessions_expires` na `expires_at`.

### 2.2 Session ID

```
randomBytes(32).toString('hex')  →  64 znaki hex lowercase
```

Python: `secrets.token_hex(32)`.

### 2.3 TTL i sliding window

```
SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000   # 30 dni
```

- `createSession(authAccountId, {ipAddr, userAgent})`: insert wiersza z `expires_at = now + 30d`, `last_seen_at = now`, ip/ua albo NULL. Zwraca `{id, expiresAt}`.
- `validateSession(id)`:
  1. SELECT sesji JOIN `auth_accounts` po `id` (limit 1). Brak wiersza → `null`.
  2. Jeśli `expires_at < now` → DELETE tego wiersza (lazy cleanup) i `null`.
  3. **Sliding window: na KAŻDYM udanym walidowaniu** UPDATE `last_seen_at = now`, `expires_at = now + 30d`. Czyli sesja żyje wiecznie dopóki user jest aktywny min. raz na 30 dni; po 30 dniach ciszy wygasa. To jest write do DB na każdy uwierzytelniony request (też na `GET /api/auth/me`).
  4. Zwraca `{authAccountId, email}`.
- `invalidateSession(id)`: DELETE po id.
- `invalidateAllForAccount(authAccountId)`: DELETE wszystkich sesji konta (po zmianie hasła).
- `purgeExpiredSessions()`: DELETE WHERE `expires_at < now`, zwraca liczbę usuniętych. Wołane z timera **co 1h** (interval w `index.ts`, `unref()`, błędy tylko logowane jako warn). Walidacja i tak odrzuca wygasłe; purge tylko pilnuje rozmiaru tabeli.

### 2.4 Cookie sesyjne (dokładne atrybuty)

```
nazwa:    admin_session
wartość:  session id (64 hex)
HttpOnly: tak
Secure:   tylko gdy NODE_ENV === 'production'
SameSite: Lax
Path:     /
Max-Age:  floor(SESSION_TTL_MS / 1000) = 2592000   (30 dni)
Domain:   BRAK (host-only)
Expires:  BRAK (tylko Max-Age)
```

Kasowanie cookie (logout, nieważna sesja w `/me`): hono `deleteCookie(c, 'admin_session', { path: '/' })` → ustawia cookie z pustą wartością i `Max-Age=0`, `Path=/`.

---

## 3. Middleware `requireAuth`

Montowany jako `app.use('/api/*', requireAuth)` ALE PO `app.route('/api/auth', authRoutes)` - w Hono kolejność rejestracji decyduje, więc **`/api/auth/*` jest publiczne**, a `/api/feedback` i `/api/circle-dm` chronione. W FastAPI odwzorować przez dependency na chronionych routerach, nie przez globalny middleware na `/api/*`.

Zachowanie:

1. **Dev bypass**: gdy `NODE_ENV !== 'production'` middleware nic nie sprawdza i ustawia w kontekście fałszywą tożsamość:
   ```ts
   DEV_FAKE_AUTH = { authAccountId: 0, email: 'dev@local' }
   ```
2. W produkcji: czytaj cookie `admin_session`. Brak → `401` body `{"error":"Unauthorized"}`.
3. `validateSession(sid)` (z sliding window). `null` → `401` `{"error":"Unauthorized"}`.
4. OK → kontekst `auth = {authAccountId, email}` dostępny dla handlerów.

---

## 4. Rate limiter logowania

In-memory (jeden proces, bez Redis), `Map<string, Bucket>`. **Stan ginie przy restarcie procesu - to akceptowane zachowanie.**

```
Bucket = { failures: number[] (timestampy ms), lockedUntil: number (ms, 0 = brak locka) }

FAILURE_WINDOW_MS = 15 * 60 * 1000   # 15 min
MAX_FAILURES      = 5
LOCK_DURATION_MS  = 60 * 60 * 1000   # 1 h
```

Klucz bucketa: `` `${emailLowercase}|${ip}` `` (pipe jako separator). Osobny budżet na każdą parę email+IP: atakujący z innego IP nie blokuje prawowitego usera.

- `getBucket(key)`: tworzy bucket jeśli brak; **przy każdym dostępie** filtruje `failures` zostawiając tylko te z ostatnich 15 min.
- `isLocked(key)`: jeśli `lockedUntil > now` → `{locked: true, retryAfterSec: ceil((lockedUntil - now)/1000)}`, inaczej `{locked: false}`.
- `recordFailure(key)`: push `now` do `failures`; jeśli `failures.length >= 5` → `lockedUntil = now + 1h`, `failures = []`, zwraca `{lockedNow: true, retryAfterSec: 3600}`. Inaczej `{lockedNow: false}`.
- `recordSuccess(key)`: `buckets.delete(key)` (pełny reset bucketa).

Czyli: 5. nieudana próba w oknie 15 min blokuje na 1h. Po wygaśnięciu locka (`lockedUntil <= now`) bucket startuje z czystą listą failures (wyczyszczona przy locku). Lock NIE jest sprawdzany w żadnym innym endpoincie niż login.

### IP klienta (funkcja `clientIp`, kolejność nagłówków)

```
1. cf-connecting-ip
2. x-real-ip
3. x-forwarded-for  → pierwszy element po split(','), trim()
4. fallback: literalny string 'unknown'
```

Nie używa adresu socketa. (Prod stoi za Caddy, który ustawia `X-Forwarded-For`.)

---

## 5. Endpointy auth (prefix `/api/auth`, wszystkie publiczne)

### 5.1 `POST /api/auth/login`

Request body (JSON, walidacja zod):

```json
{ "email": "<string, format email, max 320>", "password": "<string, min 1, max 512>" }
```

Walidacja niezaliczona → `400` (zod-validator zwraca obiekt SafeParseError; frontend traktuje to po prostu jako błąd, dokładny kształt body nie jest load-bearing, ale status 400 tak). Walidacja działa też w dev (middleware walidujący odpala się przed handlerem).

Przebieg (kolejność operacji jest istotna):

1. **Dev bypass**: gdy `NODE_ENV !== 'production'` → natychmiast `200` `{"ok":true,"email":"dev@local"}`. **Bez sprawdzania hasła, bez DB, bez cookie** (cookie niepotrzebne, bo requireAuth i `/me` też bypassują).
2. `emailNorm = email.toLowerCase()`; `ip = clientIp(c)`; `ua = nagłówek user-agent ?? null`.
3. `bucketKey = emailNorm + '|' + ip`; `isLocked(bucketKey)`:
   - locked → `429` body:
     ```json
     { "error": "Too many attempts. Try again in ~<X> min." }
     ```
     gdzie `X = ceil(retryAfterSec / 60)` (gdy `retryAfterSec` undefined, traktowane jako 0). DOSŁOWNY template: `` `Too many attempts. Try again in ~${Math.ceil((lock.retryAfterSec ?? 0) / 60)} min.` ``
4. SELECT konta z `auth_accounts` WHERE `email = emailNorm` LIMIT 1. (Match exact - dlatego baza musi trzymać emaile lowercase; skrypt to gwarantuje.)
5. Anty-enumeracja: `verifyPassword(password, account?.password_hash ?? 'scrypt$65536$8$1$00$00')` - hash dummy liczony ZAWSZE gdy konta brak, żeby timing był identyczny.
6. Gdy `!account || !passwordOk`:
   - `recordFailure(bucketKey)`; log warn `failed login <email> from <ip> (locked: <bool>)`.
   - jeśli właśnie nastąpił lock (`lockedNow`) → `429`:
     ```json
     { "error": "Too many attempts. Locked for ~60 min." }
     ```
     (dosłownie: `` `Too many attempts. Locked for ~${Math.ceil((r.retryAfterSec ?? 0) / 60)} min.` ``; przy 3600 s daje 60)
   - inaczej → `401`:
     ```json
     { "error": "Invalid email or password" }
     ```
7. Sukces: `recordSuccess(bucketKey)` → `createSession(account.id, {ipAddr: ip, userAgent: ua})` → ustaw cookie (sekcja 2.4) → log info `login ok <email> from <ip>` → `200`:
   ```json
   { "ok": true, "email": "<account.email z bazy>" }
   ```

### 5.2 `POST /api/auth/logout`

Brak body, brak wymogu auth, **brak dev bypassu** (w dev po prostu zwykle nie ma cookie).

1. Czytaj cookie `admin_session`; jeśli jest → `invalidateSession(sid)` (DELETE z DB) + log info `logout`.
2. ZAWSZE `deleteCookie('admin_session', path='/')`.
3. ZAWSZE `200` `{"ok":true}` (idempotentne, nawet bez cookie / z nieistniejącą sesją).

### 5.3 `GET /api/auth/me`

Zawsze `200`, nigdy 401 (SPA na tej podstawie decyduje: login page czy dashboard).

1. **Dev bypass**: `NODE_ENV !== 'production'` → `200` `{"authenticated":true,"email":"dev@local"}`.
2. Brak cookie → `200` `{"authenticated":false}` (bez pola `email`).
3. `validateSession(sid)` (uwaga: przesuwa sliding window) → `null`: skasuj cookie (`deleteCookie`, path `/`) i `200` `{"authenticated":false}`.
4. Ważna sesja → `200` `{"authenticated":true,"email":"<email>"}`.

---

## 6. Health endpointy (publiczne, bez auth - celowo, pod n8n/Uptime Kuma)

### 6.1 `GET /health` (płytki, liveness)

Zawsze `200`:

```json
{ "ok": true, "version": "0.1.0" }
```

Wersja jest zahardkodowana w `index.ts`.

### 6.2 `GET /health/claude` (+ `?deep=1`)

Status: `200` gdy `health.ok === true`, inaczej `503`. Body = obiekt `ClaudeHealth`:

```json
{
  "ok": true,
  "checks": {
    "binary":      { "ok": true, "detail": "..." },
    "credentials": { "ok": true, "detail": "..." },
    "version":     { "ok": true, "detail": "..." },
    "deepProbe":   { "ok": true, "detail": "...", "cachedFor": 123, "lastRunAt": "2026-06-10T12:00:00.000Z" }
  }
}
```

- `deepProbe` występuje TYLKO gdy query `deep` ma wartość dokładnie `'1'` (`c.req.query('deep') === '1'`; `deep=true` itp. NIE włącza).
- `detail` jest opcjonalny w każdym checku (w praktyce zawsze ustawiany).
- `ok` (top-level) = AND z `binary.ok && credentials.ok && version.ok` (+ `deepProbe.ok` jeśli deep).
- Trzy płytkie checki lecą RÓWNOLEGLE (`Promise.all`).

#### checkBinary

`fs.access(env.CLAUDE_BIN_PATH)` (istnienie pliku):

- istnieje → `{ok: true, detail: <CLAUDE_BIN_PATH>}`
- nie → `{ok: false, detail: "binary not found at <CLAUDE_BIN_PATH>"}`

#### checkCredentials

Ścieżka: `~/.claude/.credentials.json` (homedir usera procesu).

- brak pliku → `{ok: false, detail: "credentials file missing — run `claude login`"}` (UWAGA: detail zawiera długi myślnik `—` i backticki, zachować dosłownie)
- `stat` rzuca → `{ok: false, detail: <err.message>}`
- rozmiar `< 50` bajtów → `{ok: false, detail: "credentials file suspiciously small (<size> bytes)"}`
- OK → `{ok: true, detail: "<size> bytes"}`

#### checkVersion

`spawn(CLAUDE_BIN_PATH, ['--version'])` z timeoutem **5000 ms** (Node `spawn` option `timeout` - po przekroczeniu wysyła SIGTERM; wtedy exit code = null → detail `exit null: ...`). Zbiera stdout+stderr.

- event `error` (np. ENOENT) → `{ok: false, detail: err.message}`
- exit code != 0 → `{ok: false, detail: "exit <code>: <(stderr || stdout).slice(0,120).trim()>"}`
- exit 0 → `{ok: true, detail: stdout.trim().slice(0,60)}`

#### deepProbe (realny round-trip LLM przez Claude CLI)

Cache: in-memory, na poziomie modułu:

```
DEEP_PROBE_CACHE_MS = 60 * 60 * 1000   # 1 h
deepProbeCache: { ok, detail?, at } | null
```

- Probe odpala się tylko gdy cache pusty LUB `now - cache.at > 1h`. **Cache zapisuje też wynik NEGATYWNY** - nieudany probe jest trzymany przez 1h tak samo jak udany (świadomy trade-off: nie palić limitu subskrypcji Max przy health-checku co 10 min).
- Spawn (argumenty DOSŁOWNIE):
  ```
  CLAUDE_BIN_PATH --print --model claude-haiku-4-5 --input-format text
  ```
  timeout **20000 ms** (`20_000`), na stdin zapisywane `ping` i zamknięcie stdin (`child.stdin.end('ping')`).
- event `error` → `{ok: false, detail: err.message}`
- exit != 0 → `{ok: false, detail: "exit <code>: <(stderr || stdout).slice(0,200).trim()>"}`
- exit 0, pusty `stdout.trim()` → `{ok: false, detail: "claude returned empty output"}`
- inaczej → `{ok: true, detail: out.slice(0,60)}`
- Pola dodawane do odpowiedzi (zawsze z cache, nie z bieżącego biegu):
  - `cachedFor` = `round((now - cache.at)/1000)` sekund (0 zaraz po świeżym biegu)
  - `lastRunAt` = `new Date(cache.at).toISOString()` np. `2026-06-10T12:34:56.789Z`

#### Zależności env

- `CLAUDE_BIN_PATH` - wymagany string (min 1 znak), brak defaultu.
- `NODE_ENV` - enum `development|production|test`, default `development` (czyli **domyślnie dev bypass auth jest WŁĄCZONY**).

---

## 7. Skrypt `set-auth-password`

Uruchamianie: `pnpm set-auth-password` (root → `pnpm --filter @admin/server set-auth-password` → `tsx src/scripts/set-auth-password.ts`); prod: `set-auth-password:prod` = `node dist/scripts/set-auth-password.js`. Łączy się z DB przez to samo `DATABASE_URL`.

### Argumenty CLI

```
--email <address>      wymagany
--password <password>  opcjonalny; brak → interaktywny prompt bez echa
-h | --help            wypisuje help i exit 0
<cokolwiek innego>     "Unknown argument: <a>" na stderr + help + exit 2
```

Help (dosłownie):

```
Usage: pnpm set-auth-password --email <email> [--password <password>]

Creates or updates a panel-admin login account in auth_accounts.

If --password is omitted, you'll be prompted to enter it interactively
(no echo to terminal, no shell history leak — recommended).

Examples:
  pnpm set-auth-password --email tomasz@befreeclub.pl
  pnpm set-auth-password --email krystian@befreeclub.pl --password '...'
```

### Przebieg

1. Brak `--email` → stderr `Missing required --email <address>` + help + **exit 2**.
2. `emailNorm = email.trim().toLowerCase()`.
3. Walidacja regexem `^[^\s@]+@[^\s@]+\.[^\s@]+$`; fail → stderr `Email looks invalid: "<emailNorm>"` + **exit 2**.
4. Hasło:
   - z `--password` → bierze jak jest;
   - inaczej prompt `Password for <emailNorm>: ` (tryb raw, bez echa; `\r`/`\n` kończy, `\x7f`/`\b` = backspace usuwa znak, `\x03` Ctrl-C = abort z błędem "aborted" → exit 1 przez catch). Jeśli stdin nie jest TTY (pipe), czyta jedną linię normalnie. Puste hasło → `Empty password — aborting.` + **exit 2**. Potem prompt `Confirm password:    ` (4 spacje wyrównujące); niezgodność → `Passwords do not match — aborting.` + **exit 2**. (Confirm tylko w trybie interaktywnym; z `--password` brak potwierdzenia.)
5. Minimalna długość **12 znaków** (dotyczy też `--password`); za krótkie → `Password too short (<len> chars). Minimum 12.` + **exit 2**.
6. `hash = hashPassword(password)` (sekcja 1.3).
7. Upsert:
   ```sql
   INSERT INTO auth_accounts (email, password_hash) VALUES (<emailNorm>, <hash>)
   ON CONFLICT (email) DO UPDATE SET password_hash = <hash>, updated_at = now()
   ```
   (w oryginale `updatedAt: new Date()` - czas aplikacji, nie DB; różnica pomijalna).
8. **Invalidacja wszystkich sesji konta** (zmiana hasła = wylogowanie wszędzie), surowy SQL:
   ```sql
   DELETE FROM auth_sessions
   WHERE auth_account_id = (SELECT id FROM auth_accounts WHERE email = <emailNorm>)
   ```
9. stdout `✓ Password set for <emailNorm>. Existing sessions invalidated.` + **exit 0**.
10. Każdy nieobsłużony wyjątek → stderr `✗ Failed: <message>` + **exit 1**.

Exit codes: `0` sukces, `1` runtime error / abort, `2` błąd argumentów/walidacji.

---

## 8. Kontekst montowania (z `index.ts`, istotne dla portu)

Kolejność tras (decyduje o tym co jest publiczne):

```
GET  /health                  publiczny
GET  /health/claude           publiczny (?deep=1)
*    /api/auth/*              publiczne (login/logout/me)
use  /api/*  requireAuth      ← dopiero PO zamontowaniu /api/auth
*    /api/feedback/*          chronione
*    /api/circle-dm/*         chronione
```

- CORS: origin `http://localhost:5173` i `http://127.0.0.1:5173`, `credentials: true`, allowHeaders `Content-Type`, metody GET/POST/PATCH/DELETE/PUT/OPTIONS. (Potrzebne tylko w dev; w prod SPA i API z tego samego origina.)
- Global error handler: każdy nieobsłużony wyjątek → `500` `{"error":"<err.message>"}` + log error.
- Purge wygasłych sesji: `setInterval` co 1h (sekcja 2.3).
- W prod Hono serwuje SPA z `WEB_DIST_PATH` z fallbackiem na `index.html` dla wszystkich GET poza `/api/*` i `/ws` (404 dla niedopasowanych `/api/*`).

---

## Uwagi dla portu na FastAPI

1. **scrypt maxmem - najczęstsza pułapka.** `hashlib.scrypt` w Pythonie domyślnie ma limit pamięci ~32 MiB (OpenSSL), a N=65536, r=8 potrzebuje 128*N*r = 64 MiB. Bez `maxmem` dostaniesz `ValueError: memory limit exceeded`. Przekaż `maxmem=268435456` (256 MiB, jak w Node), np.:
   ```python
   hashlib.scrypt(password.encode(), salt=salt, n=65536, r=8, p=1, dklen=64, maxmem=268435456)
   ```
2. **Hashe migrują 1:1.** Nie przehashowuj. Weryfikacja MUSI parsować N/r/p/dklen ze stringa, nie z konstant, i akceptować dowolne (skończone) wartości - łącznie z dummy `scrypt$65536$8$1$00$00` (dklen=1). `bytes.fromhex` rzuca na nieparzystej długości i złych znakach - złap i zwróć False.
3. **`hashlib.scrypt` jest synchroniczne i CPU-bound (~150 ms).** W async FastAPI odpal w threadpoolu (`run_in_executor` / `anyio.to_thread`), inaczej blokujesz event loop. Dotyczy logowania i dummy-hasha.
4. **Dev bypass zależy od `NODE_ENV`, którego w Pythonie nie ma.** Trzeba wprowadzić własny odpowiednik (np. `APP_ENV`) i odwzorować dokładnie: bypass aktywny gdy != 'production' (czyli DOMYŚLNIE bypass włączony - default `development`). Upewnij się, że prod ustawia wartość 'production', bo inaczej panel jest otwarty na świat.
5. **Kolejność: rate-limit lock sprawdzany PRZED zapytaniem do DB**, a dummy-hash liczony zawsze przy braku konta. Zachowaj, bo to świadoma ochrona przed enumeracją emaili (kształt odpowiedzi i timing identyczne dla "brak emaila" i "złe hasło": `401 {"error":"Invalid email or password"}`).
6. **Sliding window robi UPDATE na każdym żądaniu uwierzytelnionym** (też `GET /api/auth/me`). To zamierzone. Nie optymalizuj na "odśwież raz na godzinę" bez decyzji - zmienia semantykę wygasania.
7. **Rate limiter i cache deep probe są in-memory per proces.** Działa, bo apka to 1 proces. Jeśli FastAPI pójdzie pod uvicorn z `workers > 1`, limity i cache się rozjadą (każdy worker swoje). Trzymać 1 worker albo przenieść stan do DB/Redis.
8. **`/me` zawsze zwraca 200**, nigdy 401 - frontend na tym polega. `requireAuth` z kolei zwraca `401 {"error":"Unauthorized"}` (dokładnie ten string, z wielkiej litery).
9. **Cookie**: `Secure` tylko w produkcji (w dev po HTTP by nie działało), `SameSite=Lax`, `Path=/`, `Max-Age=2592000`, bez `Domain`. W FastAPI: `response.set_cookie("admin_session", sid, httponly=True, secure=is_prod, samesite="lax", path="/", max_age=2592000)`.
10. **Login w dev nie ustawia cookie** i akceptuje dowolne (poprawne formatem) dane - ale walidacja body (400 na zły JSON/email) działa też w dev.
11. **Komunikaty błędów zawierają długie myślniki `—`** w dwóch miejscach: detail credentials checku (`credentials file missing — run \`claude login\``) i komunikaty skryptu CLI (`Empty password — aborting.`, `Passwords do not match — aborting.`, help). To wyjątek od brand voice (teksty operatorskie po angielsku); jeśli port ma być 1:1, zostaw dosłownie - monitoring może parsować stringi.
12. **Spawn timeouts**: `--version` 5 s, deep probe 20 s. W Pythonie `asyncio.create_subprocess_exec` + `wait_for` + kill; Node przy timeout wysyła SIGTERM i zwraca `code=null` → detail "exit null: ..."; w Pythonie po SIGTERM returncode = -15, detal będzie inny - akceptowalna różnica, ale `ok=false` musi się zgadzać.
13. **Cache deep probe trzyma też porażki przez 1h.** Nie "naprawiaj" tego - to celowe (oszczędność limitów Claude Max przy monitorze co 10 min). `cachedFor` w sekundach (int, round), `lastRunAt` ISO 8601 z milisekundami i `Z` (format `datetime.isoformat()` różni się - Node daje zawsze `.123Z`; jeśli chcesz identycznie: `dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond//1000:03d}Z'`).
14. **Casing JSON**: wszystkie pola odpowiedzi camelCase (`ok`, `email`, `error`, `authenticated`, `checks`, `binary`, `credentials`, `version`, `deepProbe`, `detail`, `cachedFor`, `lastRunAt`). W Pydantic uważaj na auto-snake_case.
15. **`deep=1` dosłownie**: tylko wartość query `'1'` włącza deep probe.
16. **401 z walidacji loginu** (zły email/hasło) to JSON `{"error": ...}`, a FastAPI domyślnie zwraca `{"detail": ...}` dla HTTPException - zwracaj własny JSONResponse, żeby front nie pękł. To samo dla 429 i 500 (global handler: `{"error": "<message>"}`).
17. **Email w DB lowercase**: login porównuje `email.lower()` exact-match. Jeżeli w migrowanej bazie istnieje konto z wielkimi literami (nie powinno - skrypt normalizuje), login go nie znajdzie. Sprawdzić przy migracji danych.
