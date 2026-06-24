# Katalog ustawień panelu admina (źródło prawdy)

Ten plik to **kontrakt dla wszystkich agentów** budujących centralną sekcję
"Ustawienia" w panelu admina (backend `/api/admin/settings*`, front `frontends/admin`
`/ustawienia`). Wymienia KAŻDY faktycznie kontrolny knob w backendzie, jego
klasyfikację, klucz `admin.settings`, fallback z env, label PL, typ kontrolki,
bezpieczny default, plik-konsument, sekcję panelu i uwagi.

Mechanizm i twarde zasady: patrz `CLAUDE.md` ("DYREKTYWA: ustawienia") oraz
`docs/spec-landing/cleanup-controls.md` (semantyka bramek workerów). Fundament:
migracja `0003_admin_settings.py`, model `app/modules/admin/models.py::Setting`,
serwis `app/modules/admin/services/settings.py`.

> **Dodajesz nowy knob w backendzie?** Dopisz tu wiersz, w tej samej turze
> wystaw go w panelu. Nie czekaj, aż user poprosi.

## 0. Klasyfikacja (legenda)

| Klasa | Gdzie żyje wartość | W panelu |
|---|---|---|
| **TOGGLE** | `admin.settings` (JSONB `{enabled, dryRun?}`), bezpieczny default | przełącznik on/off + (jeśli destrukcyjny) potwierdzenie |
| **TUNABLE** | `admin.settings` (JSONB `{value: ...}`) nadpisuje env | edytowalne pole (number/text/select) |
| **PROMPT** | już w `circle_dm.settings` (tabela jednowierszowa) | NIE duplikuj storage; panel linkuje/surfaceuje istniejące API `/api/circle-dm/settings` |
| **SECRET** | env na VPS, **NIGDY** do bazy | tylko status (`set`/`missing`) + "test połączenia" |
| **INFRA** | env twardo ustawione w compose | NIE w panelu |

Reguła efektywnej wartości (helper `get_effective`): **DB nadpisuje env; brak obu =
bezpieczny default; nic destrukcyjnego nie jest domyślnie włączone.** Brak wiersza
w `admin.settings` ≠ "włączone" - zawsze bezpieczny fallback.

### Konwencja kluczy i kształtu JSONB

- **TOGGLE/worker** (`members.cleanup`, `members.klarna_reconcile`,
  `members.invite_retry`): `{"enabled": bool, "dryRun": bool}`. Czyta `get_setting(key)`
  - brak wiersza/pola = `SAFE_DEFAULTS` (`enabled:false`, `dryRun:true` dla destrukcyjnych).
- **TUNABLE** (skalary: modele, interwały, progi, ID): `{"value": <scalar|null>}`.
  Czyta `get_effective(key, env_fallback=settings.X, safe_default=...)`. Brak wiersza =
  env, brak env = `safe_default`.
- `set_setting(key, value, user_id)` zapisuje cały dict pod kluczem, invaliduje cache 30 s.

---

## 1. Sekcja panelu: Circle DM & AI

Knoby AI/Circle DM. **Prompty i progi już mają własny storage** (`circle_dm.settings`,
serwis `circle_dm/services/app_settings.py`, API `GET|PUT /api/circle-dm/settings`,
UI `frontends/admin/src/tools/circle-dm/pages/SettingsPage.tsx`). Centralny panel ich
**nie duplikuje** - linkuje/surfaceuje istniejące API.

### 1a. PROMPT / TUNABLE już w `circle_dm.settings` (NIE duplikować)

| Knob | Pole circle_dm.settings | env fallback | label PL | kontrolka | safe default | plik-konsument | uwagi |
|---|---|---|---|---|---|---|---|
| Globalny meta-prompt | `global_meta_prompt` | - | "Globalne zasady stylu (meta-prompt)" | prompt-link | `""` | `circle_dm/services/app_settings.py`, draft/compose/format orchestrators | PROMPT; istniejący storage+UI |
| Prompt "Formatuj z AI" | `format_prompt` | `DEFAULT_FORMAT_PROMPT` (seed) | "Prompt Formatuj z AI" | prompt-link | seed z migracji | `format_orchestrator.py` | PROMPT; puste = wbudowany default |
| Draft model | `draft_model` | `DRAFT_MODEL` (`claude-sonnet-4-6`) | "Draft model (auto-generate)" | text/select | env | `draft_orchestrator.py:234`, `compose_orchestrator.py:124`, `assistant_orchestrator.py:472` | TUNABLE; puste pole = NULL = env. Cache 30 s, bez restartu |
| Format model | `format_model` | `POLISH_MODEL` (`claude-opus-4-7`) | "Format model (Formatuj z AI)" | text/select | env | `format_orchestrator.py:145,194,217` | TUNABLE; jw. |
| Próg "brak odpowiedzi" (dni) | `no_reply_threshold_days` | - (kolumna DB, default 3) | "Próg braku odpowiedzi (dni)" | number | 3 | `circle_dm/routes/threads.py:135` | TUNABLE; czytane per request |
| Próg "cisza" (dni) | `silence_threshold_days` | - (kolumna DB, default 14) | "Próg ciszy (dni)" | number | 14 | `circle_dm/routes/threads.py:136` | TUNABLE; jw. |
| Globalna baza wiedzy (KB) | `circle_dm.kb_documents` (scope=global) | - | "Baza wiedzy globalna" | prompt-link | brak | `knowledge_base.py` | zarządzana przez `KnowledgeAttach`; panel linkuje |

### 1b. TUNABLE z env → kandydaci do `admin.settings` (nadpisanie env)

Te są dziś TYLKO w env (stałe per proces). Wystawiamy je w centralnym panelu jako
edytowalne; klucz `admin.settings` kształtu `{"value": ...}`, czytane przez `get_effective`.
**Uwaga - większość czytana raz przy starcie procesu lub przy starcie workera** (semafory,
intervale w pętli `_loop`): zmiana w panelu zadziała w pełni dopiero po restarcie backendu.
Modele i progi KB są czytane per użycie - bez restartu.

| Knob | klucz admin.settings | env fallback | label PL | kontrolka | safe default | plik-konsument | uwagi |
|---|---|---|---|---|---|---|---|
| Max równoległych Claude | `circle_dm.claude_max_concurrent` | `CLAUDE_MAX_CONCURRENT` (2) | "Max równoległych zapytań Claude" | number (1-8) | 2 | `*_orchestrator.py` (`Semaphore` module-level) | TUNABLE; **wymaga restartu** - semafor tworzony przy imporcie. Realny limit = 4×wartość (semafor per orchestrator, quirk) |
| Interwał pollingu Circle (ms) | `circle_dm.polling_interval_ms` | `POLLING_INTERVAL_MS` (30000) | "Interwał pollingu Circle (ms)" | number (≥5000) | 30000 | `polling_worker.py:90,97` | TUNABLE; **wymaga restartu** workera |
| Interwał transkrypcji głosówek (ms) | `circle_dm.voice_transcript_interval_ms` | `VOICE_TRANSCRIPT_INTERVAL_MS` (20000) | "Interwał transkrypcji głosówek (ms)" | number (≥5000) | 20000 | `voice_transcript_worker.py:149,160` | TUNABLE; **wymaga restartu** workera |
| Interwał opisu obrazków (ms) | `circle_dm.image_description_interval_ms` | `IMAGE_DESCRIPTION_INTERVAL_MS` (20000) | "Interwał opisu obrazków (ms)" | number (≥5000) | 20000 | `image_description_worker.py:125,136` | TUNABLE; **wymaga restartu** workera |
| Budżet tokenów KB | `circle_dm.kb_budget_tokens` | `KB_BUDGET_TOKENS` (60000) | "Budżet tokenów bazy wiedzy" | number | 60000 | `knowledge_base.py:164,166` | TUNABLE; czytane per użycie - bez restartu |
| Twardy limit tokenów KB | `circle_dm.kb_hard_ceiling_tokens` | `KB_HARD_CEILING_TOKENS` (90000) | "Twardy limit tokenów bazy wiedzy" | number | 90000 | `knowledge_base.py:100,165` | TUNABLE; jw. |
| Model Whisper (STT) | `circle_dm.openai_whisper_model` | `OPENAI_WHISPER_MODEL` (`whisper-1`) | "Model transkrypcji (Whisper)" | text | env | `openai_stt.py:91` | TUNABLE; czytane per użycie |
| Model vision | `circle_dm.openai_vision_model` | `OPENAI_VISION_MODEL` (`gpt-4o-mini`) | "Model opisu obrazków (vision)" | text | env | `openai_vision.py:52` | TUNABLE; czytane per użycie |

---

## 2. Sekcja panelu: Członkostwo

Bramki destrukcyjnych workerów + ich interwały. **Tu mieszka żelazna zasada:
świeży deploy nikogo nie usuwa.** Seed migracji 0003 wpisuje bezpieczne domyśle.

### 2a. TOGGLE (bramki workerów) - klucze już zaseedowane

| Knob | klucz admin.settings | env fallback | label PL | kontrolka | safe default | plik-konsument | uwagi |
|---|---|---|---|---|---|---|---|
| Cleanup członkostw | `members.cleanup` | - | "Automatyczny cleanup członkostw" | toggle + dryRun + **potwierdzenie** | `{enabled:false, dryRun:true}` | `members/services/cleanup_worker.py`, `cleanup.py` | **DESTRUKCYJNY** (usuwa z Circle). Wyłączenie dryRun = potwierdzenie w UI. Gate `enabled`/`dryRun` do podpięcia w workerze (downstream) |
| Reconcile Klarny | `members.klarna_reconcile` | - | "Automatyczny reconcile Klarny" | toggle | `{enabled:false}` | `billing/services/klarna_reconcile_worker.py` | nadaje dostęp (nie usuwa); domyślnie off |
| Retry zaproszeń Circle | `members.invite_retry` | - | "Automatyczne ponawianie zaproszeń" | toggle | `{enabled:false}` | `members/services/invite_retry_worker.py` | ponawia tylko `invite_failed`; domyślnie off |

> **Bramka podpięta (zadanie [be-membership]).** `_tick` każdego workera czyta
> klucz przed przebiegiem: `enabled=false` (brak wiersza = SAFE_DEFAULTS) →
> tick pominięty (log debug). Cleanup: `enabled=true`+`dryRun=true` =
> **tryb cienia** (cała logika + `_decide` z odczytem Stripe/Circle, log "would
> remove", ale BEZ `circle.remove` i BEZ zmiany statusu/eventów); realne usunięcie
> tylko `enabled=true`+`dryRun=false`. Świeży deploy nikogo nie usuwa. Ręczny
> trigger admina (`POST /api/billing/admin/workers/membership_cleanup/run`) NIE jest
> blokowany bramką `enabled` (świadoma akcja), ale `dryRun` z ustawień go obowiązuje;
> odpowiedź niesie `dryRun` i `wouldRemove`. Ostatni przebieg cleanupu zapisywany
> pod kluczem `members.cleanup.last_run` (`{value: {timestamp, checked, wouldRemove,
> removed, mode}}`) do podglądu w panelu.

### 2b. TUNABLE (interwały workerów)

| Knob | klucz admin.settings | env fallback | label PL | kontrolka | safe default | plik-konsument | uwagi |
|---|---|---|---|---|---|---|---|
| Interwał cleanupu (ms) | `members.cleanup_interval_ms` | `MEMBERSHIP_CLEANUP_INTERVAL_MS` (21600000 = 6 h) | "Interwał cleanupu (ms)" | number (≥5000) | 21600000 | `cleanup_worker.py:58,67` | **wymaga restartu** workera. Harmonogram oryginału nieznany (cron Supabase) |
| Interwał reconcile Klarny (ms) | `members.klarna_reconcile_interval_ms` | `KLARNA_RECONCILE_INTERVAL_MS` (3600000 = 1 h) | "Interwał reconcile Klarny (ms)" | number (≥5000) | 3600000 | `klarna_reconcile_worker.py:217,225` | **wymaga restartu** workera |
| Interwał retry zaproszeń (ms) | `members.invite_retry_interval_ms` | `INVITE_RETRY_INTERVAL_MS` (3600000 = 1 h) | "Interwał retry zaproszeń (ms)" | number (≥5000) | 3600000 | `invite_retry_worker.py:48,55` | **wymaga restartu** workera |

---

## 3. Sekcja panelu: Billing & Newsletter

Parametry niesekretne. Sekrety billingu/newslettera są w sekcji 5 (status-only).

| Knob | klucz admin.settings | env fallback | label PL | kontrolka | safe default | plik-konsument | uwagi |
|---|---|---|---|---|---|---|---|
| URL frontu (linki w mailach/redirecty) | `billing.frontend_url` | `FRONTEND_URL` (`https://befreeclub.pl`) | "Adres frontu (linki w mailach)" | text (URL) | `https://befreeclub.pl` | `cancellation.py:165`, `payment_method.py:200`, `checkout.py:510`, `ebook.py:70`, `newsletter/routes/public.py:157` | TUNABLE; współdzielony przez billing+newsletter. Walidacja URL |
| Baza URL potwierdzenia newslettera | `newsletter.confirm_url_base` | `CONFIRM_URL_BASE` (`https://befreeclub.pl/newsletter/potwierdz`) | "Adres potwierdzenia newslettera" | text (URL) | default w kodzie | `newsletter/routes/public.py:101` | TUNABLE |
| Nadawca maili anulowania/karty | `billing.cancellation_from_email` | `CANCELLATION_FROM_EMAIL` (`DEFAULT_FROM`) | "Nadawca maili (anulowanie/karta)" | text (email/„Nazwa <mail>") | `Be Free Club <noreply@befreeclub.pl>` | `cancellation.py:173`, `payment_method.py:208` | TUNABLE |
| Nadawca maili newslettera | `newsletter.from_email` | `NEWSLETTER_FROM_EMAIL` | "Nadawca maili newslettera" | text | `Be Free Club <krystian@befreeclub.pl>` | `newsletter/routes/public.py:111` | TUNABLE |
| Grupy Sender.net (CSV id) | `newsletter.sender_group_ids` | `SENDER_GROUP_IDS` (`epnLzm,el06vl`) | "Grupy Sender.net (id, po przecinku)" | text (CSV) | `epnLzm,el06vl` | `newsletter/services/sender.py:43` | TUNABLE; czytane per push |
| Ścieżka pliku ebooka (PDF) | `billing.ebook_file_path` | `EBOOK_FILE_PATH` | "Ścieżka pliku ebooka (na dysku VPS)" | text (path) | brak (404 gdy nie ustawione) | `billing/services/ebook.py:420` | TUNABLE; plik na dysku hosta |

---

## 4. Sekcja panelu: Analityka

| Knob | klucz admin.settings | env fallback | label PL | kontrolka | safe default | plik-konsument | uwagi |
|---|---|---|---|---|---|---|---|
| Meta Pixel ID | `analytics.meta_pixel_id` | `META_PIXEL_ID` | "Meta Pixel ID" | text | brak (CAPI wyłączone) | `app/core/meta_capi.py:52,127` | TUNABLE (niesekretne). Bez Pixel ID + tokenu CAPI nie strzela. `CIRCLE_COMMUNITY_ID` → patrz uwaga niżej |

> `CIRCLE_COMMUNITY_ID` (`members/services/circle.py:88`) jest **niesekretne** (id społeczności),
> ale para z sekretnym `CIRCLE_API_TOKEN` decyduje o `circle.is_configured()`. Klasyfikacja:
> TUNABLE - klucz `members.circle_community_id`, fallback `CIRCLE_COMMUNITY_ID`, sekcja
> "Połączenia API" (obok statusu tokenu Circle), kontrolka text. Zmiana bez restartu
> (czytane per request). NIE myl z `META_CAPI_TOKEN` (sekret, sekcja 5).

---

## 5. Sekcja panelu: Połączenia API (status + 4 klucze EDYTOWALNE)

Wartość pełna **NIGDY** nie wychodzi przez GET ani logi. 4 klucze API są edytowalne
w panelu (zaszyfrowane Fernetem, tabela `admin.encrypted_secrets`, migracja 0004),
env jest fallbackiem (ustawienie w panelu nadpisuje env). Reszta (Stripe/Circle/HMAC)
zostaje status-only z env.

**Edytowalne (`editable=true`):** `OPENAI_API_KEY` → `openai.api_key`,
`RESEND_API_KEY` → `resend.api_key`, `SENDER_API_TOKEN` → `sender.api_token`,
`META_CAPI_TOKEN` → `meta.capi_token`. Klucze ustawiamy/czyścimy przez
`PUT/DELETE /api/admin/connections/{key}/secret`, GET statusu daje tylko `masked`
(maska efektywnej wartości), pełną wartość zwraca wyłącznie świadomy
`GET /api/admin/connections/{key}/secret/reveal` za auth. Master key: env
`SECRETS_MASTER_KEY` (brak = bezpieczny fallback na env, set → 400 bez crash).

Status-only (`editable=false`, jak dotąd): Stripe (current/legacy + webhooki),
Circle, HMAC DOI, bootstrap, infra DB. Panel pokazuje status + opcjonalny przycisk
"test połączenia". Wszystkie z env.

| Sekret (env) | label PL | status z | test połączenia | plik-konsument | uwagi |
|---|---|---|---|---|---|
| `STRIPE_SECRET_KEY` | "Stripe (konto current)" | `is_configured(CURRENT)` | lista 1 produktu / `GET /v1/balance` | `core/stripe_client.py:43` | dev blokuje `sk_live_` (guard) |
| `STRIPE_LEGACY_SECRET_KEY` | "Stripe (konto legacy)" | `is_configured(LEGACY)` | jw. | `core/stripe_client.py:44` | |
| `STRIPE_WEBHOOK_SECRET` | "Stripe webhook (current)" | `is not None` | brak (weryfikowany przy odbiorze) | `core/stripe_client.py:50` | |
| `STRIPE_LEGACY_WEBHOOK_SECRET` | "Stripe webhook (legacy)" | `is not None` | brak | `core/stripe_client.py:51` | |
| `CIRCLE_API_TOKEN` | "Circle API" | `circle.is_configured()` (z community_id) | lista członków (1) | `members/services/circle.py:87` | para z `CIRCLE_COMMUNITY_ID` (niesekret, sekcja 4) |
| `OPENAI_API_KEY` | "OpenAI (STT/vision)" | `is not None` | `GET /v1/models` (1) | `openai_stt.py`, `openai_vision.py`, workery | brak = workery STT/vision pomijają tick |
| `RESEND_API_KEY` | "Resend (maile)" | `email.is_configured()` | `GET /domains` | `core/email.py:122,130` | dev może mockować (`MOCK_EMAIL`) |
| `SENDER_API_TOKEN` | "Sender.net (newsletter)" | `sender.is_configured()` | `GET /v2/...` | `newsletter/services/sender.py:28` | dev może mockować (`MOCK_SENDER`) |
| `META_CAPI_TOKEN` | "Meta Conversions API" | `meta_capi.is_configured()` (z Pixel ID) | `POST` test event (opcjonalnie) | `core/meta_capi.py:52,128` | para z `META_PIXEL_ID` (niesekret, sekcja 4) |
| `CANCELLATION_DOI_SECRET` | "HMAC magic linków (billing)" | `is not None` | brak | `cancellation.py:130`, `payment_method.py:148` | sekret HMAC, nie API |
| `NEWSLETTER_DOI_SECRET` | "HMAC double opt-in (newsletter)" | `is not None` | brak | `newsletter/routes/public.py:99,134` | sekret HMAC, nie API |
| `BOOTSTRAP_ADMIN_TOKEN` | "Token bootstrap admina" | `is not None` | brak | bootstrap admina (faza 1) | jednorazowe utworzenie konta admina |
| `DB_PASS` / `DATABASE_URL` | "Połączenie z bazą" | proces żyje = OK | brak | `core/config.py`, `core/db.py` | sekret infra; status pośrednio (zdrowie procesu) |

> Wszystkie powyższe sekrety to nazwy env. Status liczy istniejący helper
> `is_configured()` danego serwisu (nie odsłaniaj wartości). "Test połączenia" robi
> minimalne, taniе read-only zapytanie do dostawcy. Wynik: `{ok, detail?}` bez sekretu.

---

## 6. INFRA (NIE w panelu)

Twardo ustawione w `infra/` (docker compose) lub środowisku procesu. Zmiana =
redeploy, nie panel. Nie wystawiać.

| env | rola | plik-konsument |
|---|---|---|
| `NODE_ENV` | tryb (dev bypass auth, guard Stripe) | `core/config.py`, `auth.py` |
| `PORT` | port uvicorn | `app/main.py:313` |
| `LOG_LEVEL` | poziom logów | `core/logging.py` |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_NAME` | DSN Postgresa | `core/config.py`, `core/db.py` |
| `CLAUDE_BIN_PATH` | ścieżka binarki Claude CLI | `core/claude_cli.py:213`, `claude_health.py` |
| `WEB_DIST_PATH` | katalog statyków frontu | `app/main.py` |
| `BOOTSTRAP_ADMIN_LABEL` / `BOOTSTRAP_ADMIN_EMAIL` | bootstrap konta admina | faza 1 |
| `MOCK_EMAIL` / `MOCK_SENDER` / `MOCK_CIRCLE_MEMBERS` | przełączniki mocków dev | `core/dev_mode.py` |

> `MOCK_*` świadomie INFRA: to przełączniki trybu dev/testów (na produkcji ignorowane,
> brak klucza = twardy błąd). Nie wystawiać w panelu produkcyjnym.

---

## 7. Kontrakt API (wiążący dla backendu i frontu)

Wszystkie odpowiedzi camelCase (`CamelModel`). Błąd: `{"error": "..."}`. Walidacja → 400.
Daty `toISOString` z `Z`. Auth: `require_auth` (dev bypass gdy `NODE_ENV != production`).
Montowane pod `/api/admin/settings*` i `/api/admin/connections` za `require_auth`.

### 7.1 `GET /api/admin/settings`

Zwraca WSZYSTKIE edytowalne ustawienia (TOGGLE + TUNABLE), pogrupowane po sekcjach
panelu, z **efektywnymi** wartościami (DB nadpisuje env). **Bez sekretów.**

```jsonc
{
  "groups": {
    "circleDmAi": {
      "claudeMaxConcurrent": { "value": 2, "source": "env", "envFallback": 2, "requiresRestart": true },
      "pollingIntervalMs":   { "value": 30000, "source": "env", "envFallback": 30000, "requiresRestart": true },
      "voiceTranscriptIntervalMs": { "value": 20000, "source": "env", "envFallback": 20000, "requiresRestart": true },
      "imageDescriptionIntervalMs": { "value": 20000, "source": "env", "envFallback": 20000, "requiresRestart": true },
      "kbBudgetTokens":      { "value": 60000, "source": "env", "envFallback": 60000, "requiresRestart": false },
      "kbHardCeilingTokens": { "value": 90000, "source": "env", "envFallback": 90000, "requiresRestart": false },
      "openaiWhisperModel":  { "value": "whisper-1", "source": "env", "envFallback": "whisper-1", "requiresRestart": false },
      "openaiVisionModel":   { "value": "gpt-4o-mini", "source": "env", "envFallback": "gpt-4o-mini", "requiresRestart": false }
      // prompty/progi/modele draft+format: NIE tu - link do GET /api/circle-dm/settings
    },
    "membership": {
      "cleanup":         { "enabled": false, "dryRun": true, "destructive": true },
      "klarnaReconcile": { "enabled": false },
      "inviteRetry":     { "enabled": false },
      "cleanupIntervalMs":         { "value": 21600000, "source": "env", "envFallback": 21600000, "requiresRestart": true },
      "klarnaReconcileIntervalMs": { "value": 3600000, "source": "env", "envFallback": 3600000, "requiresRestart": true },
      "inviteRetryIntervalMs":     { "value": 3600000, "source": "env", "envFallback": 3600000, "requiresRestart": true }
    },
    "billingNewsletter": {
      "frontendUrl":            { "value": "https://befreeclub.pl", "source": "env", "envFallback": "https://befreeclub.pl", "requiresRestart": false },
      "confirmUrlBase":         { "value": "https://befreeclub.pl/newsletter/potwierdz", "source": "default", "requiresRestart": false },
      "cancellationFromEmail":  { "value": "Be Free Club <noreply@befreeclub.pl>", "source": "default", "requiresRestart": false },
      "newsletterFromEmail":    { "value": "Be Free Club <krystian@befreeclub.pl>", "source": "default", "requiresRestart": false },
      "senderGroupIds":         { "value": "epnLzm,el06vl", "source": "default", "requiresRestart": false },
      "ebookFilePath":          { "value": null, "source": "default", "requiresRestart": false }
    },
    "analytics": {
      "metaPixelId":        { "value": null, "source": "default", "requiresRestart": false },
      "circleCommunityId":  { "value": null, "source": "default", "requiresRestart": false }
    }
  }
}
```

- `source`: `"db"` (nadpisane w panelu) | `"env"` (z env) | `"default"` (safe default, brak obu).
- `requiresRestart`: czy zmiana zadziała dopiero po restarcie procesu/workera (semafory,
  intervale czytane w `_loop`). Kolumna „uwagi" tego katalogu jest źródłem prawdy.
- TOGGLE niosą `enabled`/`dryRun`/`destructive`, nie `value`.
- Bloku `secrets` tu NIE ma - status sekretów osobno (`/api/admin/connections`).

### 7.2 `PUT /api/admin/settings/{group}`

Częściowy patch jednej grupy (`circleDmAi` | `membership` | `billingNewsletter` |
`analytics`). Body = mapa zmienianych kluczy (camelCase). Pomijasz klucz = bez zmian.
Zapisuje `set_setting(key, value, user_id=auth.auth_account_id)` per knob.

```jsonc
// PUT /api/admin/settings/membership
{ "cleanup": { "enabled": true, "dryRun": false } }   // dryRun=false WYMAGA potwierdzenia w UI
// PUT /api/admin/settings/circleDmAi
{ "kbBudgetTokens": { "value": 80000 } }
```

- TUNABLE: `{ "<key>": { "value": <scalar|null> } }`. `value:null` przywraca fallback env
  (usuwa nadpisanie - serwis traktuje brak/`null` jak fallback).
- TOGGLE: `{ "<key>": { "enabled": bool, "dryRun"?: bool } }`.
- Włączenie destrukcyjnego (`cleanup.enabled=true` z `dryRun=false`): backend akceptuje,
  ale UI MUSI wymusić potwierdzenie przed wysłaniem.
- Walidacja typu/zakresu (np. interwał ≥5000, `claudeMaxConcurrent` 1-8) → 400 `{"error":"Invalid request"}`.
- Odpowiedź: stan grupy po zapisie (ten sam kształt co w `GET`).

### 7.3 `GET /api/admin/connections`

Status każdego API + opcjonalny wynik testu połączenia. **NIGDY** wartości sekretów.
Kształt 1:1 z implementacją (`app/modules/admin/services/connections.py::ConnectionResult`)
i schematem frontu (`@bfc/shared` `connectionResultSchema`).

```jsonc
// GET /api/admin/connections        (tani listing, bez test-calli)
// GET /api/admin/connections?test=1 (dodatkowo odpala test-call każdego API z testem)
{
  "connections": [
    { "key": "stripeCurrent", "label": "Stripe (konto current)",  "configured": true,  "status": "ok",           "detail": "GET /v1/balance ok", "source": "env",   "editable": false, "masked": null },
    { "key": "stripeLegacy",  "label": "Stripe (konto legacy)",   "configured": false, "status": "unconfigured", "detail": "brak klucza",        "source": "brak",  "editable": false, "masked": null },
    { "key": "circle",        "label": "Circle API",              "configured": true,  "status": "error",        "detail": "HTTP 401",           "source": "env",   "editable": false, "masked": null },
    { "key": "openai",        "label": "OpenAI (STT/vision)",     "configured": true,  "status": "ok",           "detail": "GET /v1/models ok",  "source": "panel", "editable": true,  "masked": "sk-1…wxyz" },
    { "key": "resend",        "label": "Resend (maile)",          "configured": true,  "status": "ok",           "detail": "GET /domains ok",    "source": "env",   "editable": true,  "masked": "re_a…1234" },
    { "key": "sender",        "label": "Sender.net (newsletter)", "configured": true,  "status": "skipped",      "detail": "brak test-call",     "source": "env",   "editable": true,  "masked": "tok…" },
    { "key": "metaCapi",      "label": "Meta Conversions API",    "configured": false, "status": "unconfigured", "detail": "brak klucza",        "source": "brak",  "editable": true,  "masked": null }
  ]
}
```

- `key`: stabilny identyfikator (camelCase). `label`: PL nazwa do UI.
- `configured` (bool): czy efektywny klucz jest dostępny (panel DB nadpisuje env). Liczone
  helperem `is_configured()` / resolverem - **bez** odsłaniania wartości.
- `source`: `"panel"` (klucz ustawiony w panelu, wiersz w `admin.encrypted_secrets`) >
  `"env"` (brak wiersza, jest fallback w env) > `"brak"` (ani DB, ani env).
- `editable` (bool): `true` dla 4 kluczy API (openai/resend/sender/metaCapi) - mają
  set/clear/reveal. `false` dla Stripe/Circle (status-only).
- `masked`: maska **efektywnej** wartości dla edytowalnych (`v[:4]+'…'+v[-4:]`, krótka → `'••••'`).
  Dla nieedytowalnych `null`. **Nigdy** pełna wartość.
- `status`:
  - `"ok"` - test-call przeszedł,
  - `"error"` - skonfigurowany, ale test-call padł (HTTP/sieć/wyjątek); `detail` BEZ sekretu,
  - `"unconfigured"` - brak klucza w env (i serwis nie ma mocka dev),
  - `"skipped"` - skonfigurowany, ale testu nie zrobiono (brak taniego testu: Sender/Meta;
    albo `?test=1` nie podano),
  - `"mock"` - tylko dev (`NODE_ENV != production`): brak klucza, ale serwis działa na mocku
    (Circle members / Resend / Sender) - nie błąd.
- `detail`: krótki, BEZPIECZNY opis (nigdy sekret ani fragment). Przy błędzie HTTP = `"HTTP <kod>"`,
  przy błędzie sieci/wyjątku = `"<typ>: <skrócony tekst>"` ucinany do 200 znaków.
- Test = minimalne read-only zapytanie do dostawcy (Stripe `GET /v1/balance`, Circle lista 1
  członka, OpenAI `GET /v1/models`, Resend `GET /domains`), timeout 8 s. Sekretów HMAC
  (`*_DOI_SECRET`), webhooków, bootstrap-tokenu i DB **nie** wystawiamy w tym endpoincie
  (są w katalogu sekcja 5 jako status-only; endpoint pokrywa 7 API z test-callem/mockiem).

### 7.4 `POST /api/admin/connections/{key}/test`

Pojedynczy test-call na żądanie (przycisk "test połączenia" w UI). Zwraca
`{ "connection": <ConnectionResult> }` (ten sam kształt co element listy 7.3).
Nieznany `key` → `404 {"error": "Connection not found"}`. Sekret nigdy nie wychodzi.

### 7.4a Edycja klucza (tylko 4 edytowalne: openai/resend/sender/metaCapi)

Wszystkie za `require_auth`. Nieedytowalny/nieznany `key` (stripe/circle/...) →
`404 {"error": "Connection not found"}`.

- `PUT /api/admin/connections/{key}/secret` — body `{ "value": "<klucz>" }`. Pusta
  wartość → `400 {"error": "wartosc nie moze byc pusta"}`. Brak `SECRETS_MASTER_KEY`
  (szyfrowanie niedostępne) → `400 {"error": "szyfrowanie sekretow niedostepne (brak SECRETS_MASTER_KEY)"}`
  (bez crash). Sukces → `{ "connection": <ConnectionResult> }` (z `masked`, BEZ pełnej
  wartości). Log: `secret {key} set by {email}` (bez wartości).
- `DELETE /api/admin/connections/{key}/secret` — czyści wiersz, powrót na env fallback.
  Sukces → `{ "connection": <ConnectionResult> }`. Log bez wartości.
- `GET /api/admin/connections/{key}/secret/reveal` — świadomy odczyt pełnej efektywnej
  wartości (DB decrypt > env > null) → `{ "value": "<pełna wartość>"|null }`. Jedyny
  endpoint zwracający pełną wartość; tylko dla edytowalnych.

### 7.5 `POST /api/billing/admin/workers/membership_cleanup/run` (ręczny przebieg cleanupu)

Świadoma akcja człowieka: **ignoruje** bramkę `enabled`, ale **respektuje** `dryRun`
z `members.cleanup` (brak wiersza = `dryRun=true`, czyli tryb cienia). Schema:
`@bfc/shared` `cleanupRunResultSchema`.

```jsonc
{ "success": true, "checked": 12, "removed": 0, "wouldRemove": 3, "dryRun": true,
  "decisions": [ { "memberId": 1, "email": "a@x.pl", "decision": "subscription_dead", "removed": false } ] }
```

- Niekompletna konfiguracja (brak któregoś klucza Stripe lub Circle) → guard `_require_config`
  rzuca PRZED przetworzeniem kogokolwiek → `500 {"error": "STRIPE_SECRET_KEY not set"}`
  (głośny błąd bez sekretu, nikt nie usunięty). To celowe - cleanup nie rusza bez pełnego configu.

---

## 8. Stan implementacji (na teraz)

- [x] Migracja `0003_admin_settings.py` (tabela `admin.settings`, trigger `updated_at`,
      FK `updated_by_user_id` → `admin.users(id)` ON DELETE SET NULL, seed bezpiecznych
      domyślnych ON CONFLICT DO NOTHING).
- [x] Model `app/modules/admin/models.py::Setting`.
- [x] Serwis `app/modules/admin/services/settings.py`: `get_setting`, `set_setting`,
      `get_effective`, `safe_default_for`, cache 30 s + invalidacja, `SAFE_DEFAULTS`.
- [x] Test `tests/test_admin_settings.py` (czysta logika reguły bezpieczeństwa + precedencji).
- [x] **Routes `/api/admin/settings*` + `/api/admin/connections`** (sekcja 7). Montowane w
      `app/main.py` za `require_auth` (dependency `protected`). Katalog knobów w
      `app/modules/admin/services/settings_catalog.py` (TUNABLE/TOGGLE, walidacja → 400),
      status połączeń w `app/modules/admin/services/connections.py` (status-only, test-call
      read-only, tryb mock dev). Testy: `tests/test_admin_settings_api.py`,
      `tests/test_admin_connections.py` (twardy assert: żaden sekret nie wycieka do JSON).
- [x] **Bramki `enabled`/`dryRun` w `_tick` workerów** (zadanie [be-membership]):
      `cleanup`, `klarna_reconcile`, `invite_retry` czytają swój klucz przed przebiegiem;
      cleanup w trybie cienia przechodzi logikę bez usuwania. Interwały workerów czytane
      przez `get_effective` (env fallback). Last-run cleanupu pod `members.cleanup.last_run`
      (read-only do panelu). Testy w `tests/test_members.py` (świeży deploy nikogo nie usuwa)
      i `tests/test_workers.py`.
- [x] **Front `frontends/admin` `/ustawienia`**: 5 sekcji (`MembershipSection`,
      `CircleDmAiSection`, `BillingNewsletterSection`+`AnalyticsSection`, `ConnectionsSection`).
      Włączenie realnego usuwania (`cleanup` dryRun→false) za potwierdzeniem w dialogu;
      domyślne włączenie idzie w tryb cienia (`dryRun=true`). Prompty/modele Circle DM
      **linkowane** do `/circle-dm/settings`, nie duplikowane. Klient API:
      `src/core/lib/settings-api.ts`.
- [x] **Schematy `frontends/packages/shared`** dla kontraktu z sekcji 7:
      `src/schemas/settings.ts` (grupy, TUNABLE/TOGGLE, `connectionResultSchema`,
      `cleanupRunResultSchema`). Zgodne 1:1 z odpowiedziami backendu (`key`/`configured`/
      `status`/`detail`, nie `id`/`set`/`missing`).
