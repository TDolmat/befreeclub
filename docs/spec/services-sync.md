# Spec: serwisy sync / send / state / settings (Circle DM)

Źródła (TypeScript, stan na 2026-06-10):

- `admin/apps/server/src/tools/circle-dm/services/polling-worker.ts`
- `admin/apps/server/src/tools/circle-dm/services/thread-sync.ts`
- `admin/apps/server/src/tools/circle-dm/services/members-sync.ts`
- `admin/apps/server/src/tools/circle-dm/services/thread-state.ts`
- `admin/apps/server/src/tools/circle-dm/services/send.ts`
- `admin/apps/server/src/tools/circle-dm/services/bulk-send.ts`
- `admin/apps/server/src/tools/circle-dm/services/app-settings.ts`

Cel: odtworzenie zachowania 1:1 w FastAPI. Spec opisuje logikę krok po kroku, z dokładnymi nazwami pól, wartościami domyślnymi, payloadami WS i komunikatami błędów. Wszystkie nazwy kolumn DB podane w nawiasach to fizyczne nazwy w Postgres (snake_case), nazwy pól JSON w eventach WS i odpowiedziach to dokładnie te z kodu (camelCase po stronie naszego API/WS, snake_case po stronie Circle).

---

## 0. Wspólne zależności (kontrakty, na których opierają się serwisy)

### 0.1 JWT manager (`circle/jwt-manager.ts`)

- `getJwtFor(adminAccountId)` zwraca obiekt `{ accessToken, expiresAt, communityId, communityMemberId }`.
  - Czyta wiersz `admin_accounts`; brak → `Error("admin_account {id} not found")`; `is_active=false` → `Error("admin_account {id} is not active")`.
  - Cache w DB: jeśli `circle_access_token` istnieje i `circle_access_token_expires_at - 5min > now` i `community_id`/`community_member_id` nie są null → zwraca z DB bez requestu.
  - Inaczej wymienia `circle_admin_token` + `email` przez `POST https://app.circle.so/api/v1/headless/auth_token` body `{"email": "..."}`, zapisuje do DB (`circle_access_token`, `circle_access_token_expires_at`, `circle_refresh_token`, `community_id`, `community_member_id`) i zwraca.
  - `REFRESH_LEAD_MS = 5 * 60 * 1000` (odświeża 5 min przed wygaśnięciem).
  - Dedupe równoległych wywołań: in-memory mapa `inflight` per `adminAccountId` (dwa równoległe `getJwtFor` dla tego samego konta = jeden request).
- `invalidateJwt(adminAccountId)`: `UPDATE admin_accounts SET circle_access_token=NULL, circle_access_token_expires_at=NULL WHERE id=...`. Wołane po każdym 401 z Circle (potem błąd jest re-throw, caller decyduje co dalej).

### 0.2 Klient Circle (`circle/client.ts`) - fragmenty używane przez te serwisy

Wszystkie requesty: nagłówki `Authorization: Bearer {token}`, `Content-Type: application/json`, `Accept: application/json`. **Timeout 30_000 ms** (AbortController). Odpowiedź nie-2xx → `CircleApiError(status, body, message)` gdzie `message = "Circle API {status}: {pierwsze 200 znaków body}"`. Puste body przy 2xx → zwraca `undefined`. 2xx z body nie-JSON → `CircleApiError(status, text, "Circle returned non-JSON response")`.

- `listThreads(jwt, {page=1, perPage=50})` → `GET /api/headless/v1/messages?page={page}&per_page={perPage}`. Zwraca `{ records, page, per_page, has_next_page, count, page_count }`.
- `getThreadMessages(jwt, chatRoomUuid, {perPage=100})` → `GET /api/headless/v1/messages/{uuid}/chat_room_messages?per_page={perPage}`. Zwraca `{ records, first_id, last_id, total_count, has_previous_page, has_next_page }`.
- `sendMessage(jwt, chatRoomUuid, body)` → `POST /api/headless/v1/messages/{uuid}/chat_room_messages` z body JSON:
  ```json
  { "body": "<plain text>", "rich_text_body": <envelope z textToTiptap(body)> }
  ```
  Odpowiedź: `{ "creation_uuid": "...", "parent_message_id": null, "sent_at": "..." }`; pole `id` (numeryczne) jest **opcjonalne** i w praktyce nie przychodzi. UWAGA: wysłanie samego Tiptap doc bez envelope `body:{type:'doc',...}` jest przyjmowane przez Circle (zwraca creation_uuid), ale treść jest **cicho dropowana**.
- `markChatRoomRead(jwt, chatRoomUuid)` → `PATCH /api/headless/v1/messages/{uuid}` z body:
  ```json
  { "unread_messages_count": 0 }
  ```
  Zweryfikowane 2026-05-12: zwraca 200 i echo `unread_messages_count: 0`. To generyczny PATCH (headless API nie ma dedykowanego /mark_as_read).
- `listMembers(jwt, {page=1, perPage=100, query?})` → `GET /api/headless/v1/community_members?page={page}&per_page={perPage}` (+ `&query=...` jeśli podano). Zwraca `{ records, page, per_page, has_next_page, count, page_count }`.

`textToTiptap(text)` - krytyczne dla send, DOSŁOWNA logika:

1. `trimmed = text.replace(/\r\n/g, '\n').trim()`; `fallback = trimmed`.
2. Podział na akapity: `trimmed.split(/\n{2,}/)` (2+ newline'y = nowy akapit). Pusty `trimmed` → `paragraphs = []`.
3. Każdy akapit budowany jako `{type:'paragraph', content:[...]}`, gdzie content to per linia (`split('\n')`): jeśli linia niepusta `{type:'text', text: line, circle_ios_fallback_text: line}`, a między liniami (idx < lines.length-1) `{type:'hardBreak'}`. Akapit bez contentu → `{type:'paragraph'}` (bez klucza content).
4. **Między akapitami wstawiany jest pusty `{type:'paragraph'}` jako wizualny spacer** (Circle Web renderuje sąsiednie paragraphy bez odstępu). Czyli doc = [P1, {paragraph}, P2, {paragraph}, P3]. Brak akapitów → docContent = `[{type:'paragraph'}]`.
5. Wynikowy envelope:
   ```json
   {
     "body": { "type": "doc", "content": [/* docContent */] },
     "polls": [],
     "format": "chat",
     "entities": [],
     "attachments": [],
     "group_mentions": [],
     "community_members": [],
     "inline_attachments": [],
     "sgids_to_object_map": {},
     "circle_ios_fallback_text": "<pełny trimmed plain text>"
   }
   ```

### 0.3 WS broker (`core/ws/broker.ts`)

- Endpoint WS na path `/ws` na tym samym serwerze HTTP. Przy połączeniu serwer wysyła `{"type":"hello"}`.
- `broadcast(event)`: serializuje JSON i wysyła do **wszystkich** podłączonych klientów (readyState OPEN). Brak klientów → no-op. Brak rozróżnienia per user/konto - każdy klient dostaje wszystko.

Eventy WS emitowane przez serwisy z tego speca (dokładne payloady, klucze camelCase):

| Event | Payload |
|---|---|
| `threads:updated` | `{"type":"threads:updated","adminAccountId":<int>,"changedThreadIds":[<int>...]}` |
| `thread:new_messages` | `{"type":"thread:new_messages","threadId":<int>,"newCount":<int>}` |
| `messages:loaded` | `{"type":"messages:loaded","threadId":<int>,"count":<int>}` |
| `draft:status` | `{"type":"draft:status","threadId":<int>,"status":"sent"}` (`error` opcjonalne, tu nieużywane) |
| `send:result` | `{"type":"send:result","threadId":<int>,"ok":<bool>,"circleMessageId":<int\|null>}` + opcjonalnie `"error":"<string>"` przy ok=false |

### 0.4 Tabele DB, których dotykają serwisy

(pełny schemat w osobnym specu; tu tylko pola istotne dla logiki)

- `admin_accounts`: `id`, `label`, `email`, `circle_admin_token`, `circle_refresh_token`, `circle_access_token`, `circle_access_token_expires_at`, `community_id`, `community_member_id`, `system_prompt`, `is_active` (bool, default true), `last_synced_at`, `created_at`, `updated_at`.
- `dm_threads`: `id`, `admin_account_id` (FK cascade), `circle_chat_room_id`, `circle_chat_room_uuid` (uuid), `chat_room_kind` (enum 'direct'|'group_chat'), `chat_room_name`, `other_participant_email|name|id|avatar_url`, `unread_messages_count` (int, default 0), `pinned_at`, `status` (enum 'inbox'|'done', default 'inbox'), `is_flagged` (bool, default false), `last_message_at`, `last_message_sender_id`, `last_message_sender_is_me` (bool, default false), `last_message_preview`, `raw_payload` (jsonb), `messages_fetched_at`, `fetched_at`. UNIQUE `(admin_account_id, circle_chat_room_uuid)`.
- `dm_messages`: `id`, `thread_id` (FK cascade), `circle_message_id` (bigint, NOT NULL - syntetyczne są **ujemne**), `body` (NOT NULL), `rich_text_body` (jsonb), `sender_id`, `sender_name`, `sender_is_me` (bool NOT NULL), `parent_message_id`, `chat_thread_id`, `created_at` (NOT NULL), `edited_at`, `fetched_at`, `voice_transcript`, `voice_transcript_status` (enum 'pending'|'done'|'error', nullable), `voice_transcript_error`, `voice_transcript_attempts` (default 0), `voice_duration_sec`, `voice_transcribed_at`. UNIQUE `(thread_id, circle_message_id)`.
- `message_image_descriptions`: `id`, `message_id` (FK cascade), `attachment_index` (int), `attachment_url` (NOT NULL), `description`, `status` (enum 'pending'|'done'|'error', default 'pending'), `error`, `attempts` (default 0), `created_at`, `described_at`. UNIQUE `(message_id, attachment_index)`.
- `thread_checkups`: `id`, `thread_id` (FK cascade), `due_at` (NOT NULL), `note`, `done_at`, `created_at`.
- `sent_messages`: `id`, `thread_id` (FK cascade), `body` (NOT NULL), `circle_message_id` (nullable), `circle_creation_uuid` (uuid nullable), `sent_at` (default now), `draft_session_id` (FK set null), `error`.
- `community_members`: `id`, `admin_account_id` (FK cascade), `circle_community_member_id`, `name` (NOT NULL), `email`, `avatar_url`, `headline`, `bio`, `location`, `last_seen_text`, `status`, `is_admin` (default false), `can_send_message` (default true), `raw_payload` (jsonb), `fetched_at`. UNIQUE `(admin_account_id, circle_community_member_id)`.
- `app_settings`: `id` (int PK, default 1, jedyny wiersz id=1), `global_meta_prompt` (text NOT NULL default ''), `format_prompt` (text NOT NULL default ''), `draft_model` (nullable), `format_model` (nullable), `no_reply_threshold_days` (int NOT NULL default 3), `silence_threshold_days` (int NOT NULL default 14), `updated_at`.
- `draft_sessions` (dotykane przez `markSent`): `thread_id` UNIQUE, `status` (enum draft_status, m.in. 'sent'), `current_draft`.

---

## 1. polling-worker.ts

Stały worker pollingu wątków Circle. **Brak auto-generacji draftów** - PROJECT.md w sekcji "Sync / polling" nadal wymienia "Auto-generate: fire-and-forget top 5 unread per tick", ale to NIEAKTUALNE: auto-draft wyłączono 2026-05-13 decyzją usera (PROJECT.md linia 85) i w kodzie nie ma żadnego śladu tej logiki. W porcie NIE implementować.

### Konfiguracja

- Interwał: env `POLLING_INTERVAL_MS`, walidacja: int, **min 5000**, **default 30000**.

### `startPolling()`

1. Jeśli interval już ustawiony → return (idempotentne).
2. Log info: `Starting polling worker (interval {POLLING_INTERVAL_MS}ms)`.
3. **Natychmiast odpala jeden `tick()`** (fire-and-forget), potem `setInterval(tick, POLLING_INTERVAL_MS)`.

### `stopPolling()`

Czyści interval (graceful shutdown).

### `tick()` - krok po kroku

1. **Guard reentrancy**: moduł trzyma flagę `running`. Jeśli poprzedni tick wciąż trwa → log debug `previous tick still running, skipping` i return. Flaga zwalniana w `finally`.
2. `SELECT id FROM admin_accounts WHERE is_active = true`.
3. Dla każdego konta **sekwencyjnie** (pętla `for`, nie równolegle):
   a. `syncThreadsForAccount(account.id)` → `{ changedThreadIds, newUnreadThreadIds, staleMessageThreadIds }` (sekcja 2.1).
   b. Jeśli `changedThreadIds.length > 0` → broadcast WS:
      ```json
      {"type":"threads:updated","adminAccountId":<id>,"changedThreadIds":[...]}
      ```
   c. Dla **każdego** id w `newUnreadThreadIds` osobny broadcast:
      ```json
      {"type":"thread:new_messages","threadId":<id>,"newCount":1}
      ```
      `newCount` jest **zawsze 1** (hardcoded), niezależnie od realnej liczby nowych wiadomości.
   d. Dla każdego id w `staleMessageThreadIds`: **fire-and-forget** `syncMessagesForThread(threadId)` (bez await; błąd łapany i logowany jako warn `message sync failed for thread {id}: {message}`). Pokrywa: odpowiedzi od członków, sendy zrobione poza naszą apką (Circle web), nasze własne sendy (reconciliacja syntetycznego placeholdera) oraz wątki "stuck" z wcześniejszych ticków, które zaktualizowały metadane ale pominęły messages.
   e. Błąd całego synca konta (w tym 401 po invalidateJwt) → log error `sync failed for account {id}` + message, pętla **kontynuuje** następne konto.

### `syncNow(adminAccountId?)`

Trigger manualny (np. po sendzie / przycisk "Synchronizuj"):
- z argumentem → `await syncThreadsForAccount(adminAccountId)` (bez WS eventów i bez stale-messages follow-upu!),
- bez argumentu → `await tick()` (pełny przebieg z eventami).

---

## 2. thread-sync.ts

### 2.1 `syncThreadsForAccount(adminAccountId)` → `{changedThreadIds, newUnreadThreadIds, staleMessageThreadIds}`

1. `jwt = await getJwtFor(adminAccountId)`.
2. `listThreads(jwt.accessToken, { perPage: 50 })` - **tylko pierwsza strona, 50 wątków** (page=1). Starsze wątki spoza top50 nie są odświeżane przez polling.
3. Catch: jeśli `CircleApiError` ze `status === 401` → `await invalidateJwt(adminAccountId)`, potem **re-throw** (zawsze re-throw, nie tylko przy 401).
4. Dla każdego `record` z `response.records` sekwencyjnie: `upsertThread(adminAccountId, jwt.communityMemberId, record)` (2.2). Zbiera trzy listy id wg flag wyniku.
5. `UPDATE admin_accounts SET last_synced_at = now() WHERE id = adminAccountId`.
6. Zwraca trzy listy (lokalnych `dm_threads.id`).

### 2.2 `upsertThread(adminAccountId, communityMemberId, record)` → `{threadId, changed, newUnread, staleMessages}`

Wejście `record` = element `records` z Circle (`CircleThreadRecord`): pola `id`, `uuid`, `chat_room_kind`, `chat_room_name`, `unread_messages_count`, `pinned_at`, `other_participants_preview` (lista), `last_message` (lub null, w nim `id`, `body`, `created_at`, `sender.community_member_id`).

1. `other = record.other_participants_preview[0]` (może nie istnieć → pola null).
2. `lastMsg = record.last_message`; `lastMsgAt = lastMsg ? parse(lastMsg.created_at) : null`.
3. `lastSenderIsMe = lastMsg?.sender?.community_member_id === communityMemberId` (strict equality; brak sender → false).
4. `SELECT id, unread_messages_count, last_message_at, messages_fetched_at FROM dm_threads WHERE admin_account_id=? AND circle_chat_room_uuid=? LIMIT 1`.
5. **Semantyka lokalnego unread**: `localUnread = lastSenderIsMe ? 0 : record.unread_messages_count`. Czyli: jeśli MY napisaliśmy ostatnią wiadomość, wątek lokalnie ma 0 unread niezależnie od badge'a Circle (Circle potrafi trzymać >0 np. przez legacy notif na innym urządzeniu).
6. Wartości upsertu (UPDATE nadpisuje WSZYSTKIE te pola; NIE dotyka `status`, `is_flagged`, `messages_fetched_at`):
   - `admin_account_id`, `circle_chat_room_id = record.id`, `circle_chat_room_uuid = record.uuid`, `chat_room_kind = record.chat_room_kind`, `chat_room_name = record.chat_room_name`
   - `other_participant_email = other?.email ?? null`, `other_participant_name = other?.name ?? null`, `other_participant_id = other?.community_member_id ?? null`, `other_participant_avatar_url = other?.avatar_url ?? null`
   - `unread_messages_count = localUnread`
   - `pinned_at = record.pinned_at ? parse(record.pinned_at) : null`
   - `last_message_at = lastMsgAt`, `last_message_sender_id = lastMsg?.sender?.community_member_id ?? null`, `last_message_sender_is_me = lastSenderIsMe`
   - `last_message_preview = lastMsg?.body?.slice(0, 240) ?? null` (pierwsze 240 znaków plain body z list endpointu)
   - `raw_payload = record` (cały JSON), `fetched_at = now()`
7. **Jeśli wątek istnieje** (UPDATE ... WHERE id=existing.id):
   - `changed = (existing.unread !== localUnread) || (epoch(existing.last_message_at) ?? 0) !== (epoch(lastMsgAt) ?? 0)` - porównanie timestampów w ms, null traktowany jak 0.
   - `newUnread = localUnread > existing.unread`.
   - `staleMessages = lastMsgAt !== null && (existing.messages_fetched_at === null || existing.messages_fetched_at < lastMsgAt)` - tzn. ostatnia wiadomość w wątku jest nowsza niż moment ostatniego pełnego pobrania historii.
8. **Jeśli nie istnieje** (INSERT ... RETURNING id):
   - `changed = true`, `newUnread = localUnread > 0`, `staleMessages = lastMsgAt !== null`.

### 2.3 `syncMessagesForThread(threadId)` → liczba nowo wstawionych wiadomości

1. `SELECT id, admin_account_id, circle_chat_room_uuid FROM dm_threads WHERE id=? LIMIT 1`; brak → `throw Error("thread {id} not found")`.
2. `jwt = getJwtFor(thread.adminAccountId)`; `getThreadMessages(jwt.accessToken, thread.uuid, { perPage: 100 })` - **jedna strona, 100 ostatnich wiadomości**, bez paginacji wstecz. Catch 401 → `invalidateJwt` + re-throw.
3. Dla każdego rekordu sekwencyjnie `upsertMessage(thread.id, jwt.communityMemberId, record)` (2.4); zlicza `inserted` (tylko realne INSERTy).
4. **Reconciliacja syntetycznych placeholderów** (wiadomości wstawionych lokalnie po sendzie, sekcja 5, mają ujemne `circle_message_id`):
   a. `realBodiesFromMe` = zbiór znormalizowanych treści z `response.records`, gdzie `sender.community_member_id === jwt.communityMemberId`; treść przez `pickPlainText` (2.5), normalizacja przez `normalizeForMatch` (2.6); puste stringi odfiltrowane.
   b. Jeśli zbiór niepusty: `SELECT id, body FROM dm_messages WHERE thread_id=? AND circle_message_id < 0 AND sender_is_me = true`.
   c. Usuń (`DELETE WHERE id IN (...)`) te syntetyczne wiersze, których `normalizeForMatch(body)` występuje w `realBodiesFromMe`. Log debug `Thread {id}: removed {n} synthetic placeholder(s)`. Rationale: Circle może odesłać treść w lekko innym kształcie (trailing space, ASCII vs non-breaking space), porównanie po normalizacji whitespace zapobiega zdublowanym dymkom w UI.
5. `UPDATE dm_threads SET messages_fetched_at = now() WHERE id = threadId`.
6. **Auto-revival**: jeśli `inserted > 0` ORAZ w `response.records` jest **jakakolwiek** wiadomość, której `sender.community_member_id !== jwt.communityMemberId` → `reviveIfDone(threadId)` (sekcja 4). UWAGA na semantykę 1:1: warunek `incomingFresh` sprawdza CAŁĄ pobraną stronę historii, nie tylko nowo wstawione wiadomości. W praktyce: insert czegokolwiek (nawet naszej własnej wiadomości) + obecność dowolnej cudzej wiadomości w ostatnich 100 → revival wątku ze statusu 'done' do 'inbox'.
7. Log debug `Thread {id}: {records.length} messages, {inserted} new`.
8. Jeśli `inserted > 0` → broadcast `{"type":"messages:loaded","threadId":<id>,"count":<inserted>}`.
9. Zwraca `inserted`.

### 2.4 `upsertMessage(threadId, communityMemberId, record)` → bool (czy INSERT)

Wejście `record` = `CircleMessageRecord`: `id`, `body`, `rich_text_body`, `created_at`, `edited_at`, `parent_message_id`, `chat_thread_id`, `sender.community_member_id`, `sender.name`.

1. `senderIsMe = record.sender?.community_member_id === communityMemberId`.
2. `plain = pickPlainText(record)` (2.5). Preferujemy `rich_text_body` nad plain `body` Circle, bo plain skleja akapity bez newline'ów (zweryfikowane 2026-05-13).
3. Detekcja voice message: `atts = extractAttachments(record.rich_text_body)`; `hasVoice = atts.some(a => a.kind === 'audio' && a.voiceMessage)`.
4. Upsert:
   ```sql
   INSERT INTO dm_messages (thread_id, circle_message_id, body, rich_text_body,
     sender_id, sender_name, sender_is_me, parent_message_id, chat_thread_id,
     created_at, edited_at, voice_transcript_status)
   VALUES (..., hasVoice ? 'pending' : NULL)
   ON CONFLICT (thread_id, circle_message_id) DO UPDATE
     SET body = EXCLUDED.body,            -- plain
         rich_text_body = EXCLUDED.rich_text_body,
         edited_at = <parsed edited_at or NULL>
   RETURNING id, (xmax = 0) AS is_insert
   ```
   - `voice_transcript_status='pending'` ustawiane **tylko przy INSERT** - przy konflikcie status zostaje (może być już 'done'); worker transkrypcji podbiera wiersze 'pending'.
   - Update body/rich_text_body przy konflikcie celowy: back-fill starych wiadomości po ulepszeniach `tiptapToPlainText`.
   - Trik `(xmax = 0)` (Postgres): true tylko gdy wiersz świeżo wstawiony - tak liczony jest `inserted`.
5. **Kolejkowanie opisów obrazków** (zarówno przy INSERT jak i UPDATE): dla każdego załącznika z `atts` o `kind === 'image'` wstaw do `message_image_descriptions`:
   - `message_id` = id z RETURNING, `attachment_index` = **indeks w pełnej liście `atts`** (liczony PRZED filtrowaniem do obrazków, czyli to pozycja wśród wszystkich załączników, nie wśród samych image'ów), `attachment_url = a.fullUrl ?? a.url`.
   - `ON CONFLICT DO NOTHING` (UNIQUE `(message_id, attachment_index)` daje idempotencję). Status default 'pending'; worker vision podbiera.
6. Zwraca `is_insert === true`.

### 2.5 `pickPlainText(record)`

```
fromRich = record.rich_text_body ? tiptapToPlainText(record.rich_text_body) : ''
if fromRich.length > 0: return fromRich
return typeof record.body === 'string' ? record.body : ''
```

### 2.6 `normalizeForMatch(text)`

```
text.replace(/\s+/g, ' ').trim()
```
(JS `\s` obejmuje też ` ` NBSP; w Pythonie `re.sub(r'\s+', ' ', text).strip()` przy str daje to samo, bo Unicode whitespace domyślnie.)

### 2.7 `refetchThreads(threadIds)`

Bulk "wymuś re-sync" (używane po sendzie/bulku): `UPDATE dm_threads SET messages_fetched_at = NULL WHERE id IN (...)`. Pusta lista → no-op. Skutek: następny tick pollingu zobaczy `staleMessages=true` (o ile wątek ma `last_message_at`) i dociągnie historię.

---

## 3. members-sync.ts

### `syncMembersForAccount(adminAccountId)` → liczba "zsynchronizowanych" rekordów

1. `jwt = getJwtFor(adminAccountId)`.
2. Pętla po stronach od `page = 1`, max `MAX_PAGES = 30` (30 * 100 = 3000 członków, safety cap), `perPage = 100`:
   a. `listMembers(jwt.accessToken, { page, perPage: 100 })`; catch 401 → `invalidateJwt` + re-throw.
   b. Dla każdego `m` z `response.records`:
      - **Skip samego siebie**: `if (m.community_member_id === jwt.communityMemberId) continue`.
      - Upsert do `community_members` z `ON CONFLICT (admin_account_id, circle_community_member_id) DO UPDATE SET <wszystkie pola>` i RETURNING id. Wartości:
        - `admin_account_id`, `circle_community_member_id = m.community_member_id`, `name = m.name`, `email = m.email ?? null`, `avatar_url = m.avatar_url ?? null`, `headline = m.headline ?? null`, `bio = m.bio ?? null`, `location = m.location ?? null`, `last_seen_text = m.last_seen_text ?? null`, `status = m.status ?? null`, `is_admin = m.roles?.admin ?? false`, `can_send_message = true` (zawsze nadpisywane na true!), `raw_payload = m`, `fetched_at = now()`.
      - Licznik `totalInserted++` gdy RETURNING zwróci wiersz - czyli **liczy też UPDATE'y**, nazwa myląca; to "liczba przetworzonych członków".
   c. `if (!response.has_next_page) break`; inaczej `page += 1`.
3. Log info `Synced {totalInserted} members for account {id}`. Zwraca licznik.

### `getCachedMembersCount(adminAccountId)`

Zwraca liczbę wierszy `community_members` dla konta. (Oryginał robi `SELECT id ...` i liczy długość listy w pamięci; w porcie po prostu `SELECT COUNT(*)` - wynik identyczny.)

### `ensureMembersCached(adminAccountId)`

`if count == 0: syncMembersForAccount(adminAccountId)`. Czyli pełny sync tylko przy pustym cache; odświeżanie niezerowego cache odbywa się innym wywołaniem (route ręcznego sync).

### `getMemberByCircleId(adminAccountId, circleMemberId)`

`SELECT * FROM community_members WHERE admin_account_id=? AND circle_community_member_id=? LIMIT 1`; zwraca wiersz lub null.

---

## 4. thread-state.ts

Stan aplikacyjny wątków (niezależny od Circle) + check-upy (follow-up reminders).

### Statusy / flagi

- `setThreadStatus(threadId, status)`: `UPDATE dm_threads SET status=? WHERE id=?`. `status` ∈ {`'inbox'`, `'done'`}. Brak walidacji istnienia wątku (update 0 wierszy = cicho OK).
- `setThreadFlagged(threadId, isFlagged)`: analogicznie `SET is_flagged=?`.
- `bulkSetStatus(adminAccountId, ids, status)` → liczba zmienionych: `UPDATE dm_threads SET status=? WHERE admin_account_id=? AND id IN (ids) RETURNING id`. **Scope do konta** - id z cudzego konta nie zostanie ruszone. Pusta lista → return 0 bez SQL. Zwraca `len(returning)`.
- `bulkSetFlagged(adminAccountId, ids, isFlagged)` → identyczny wzorzec dla `is_flagged`.

### Check-upy

Serializacja wiersza do API (`CheckupRow`, dokładne klucze JSON):
```json
{
  "id": <int>,
  "threadId": <int>,
  "dueAt": "<ISO 8601 z tzn., toISOString>",
  "note": <string|null>,
  "doneAt": <"ISO"|null>,
  "createdAt": "<ISO>"
}
```
(JS `toISOString()` daje format `YYYY-MM-DDTHH:mm:ss.sssZ` - UTC z milisekundami i sufiksem `Z`; w Pythonie odwzorować dokładnie ten format.)

- `listCheckups(threadId)`: wszystkie check-upy wątku, `ORDER BY due_at ASC` (done i pending razem).
- `createCheckup(threadId, dueAt, note)`: INSERT `{thread_id, due_at, note}` (note może być null), RETURNING cały wiersz → serializacja.
- `markCheckupDone(checkupId)`: `UPDATE thread_checkups SET done_at = now() WHERE id=?`.
- `deleteCheckup(checkupId)`: `DELETE FROM thread_checkups WHERE id=?`.
- `clearPendingCheckupsOnSend(threadId)` (auto-rule, wołane z send.ts po udanym POST do Circle): `UPDATE thread_checkups SET done_at = now() WHERE thread_id=? AND done_at IS NULL RETURNING id`. Semantyka: user odpisał → wszystkie wiszące follow-upy uznajemy za załatwione. Log debug gdy >0.
- `getLatestPendingCheckup(threadId)`: `WHERE thread_id=? AND done_at IS NULL ORDER BY created_at DESC LIMIT 1` → CheckupRow lub null.

### Auto-revival

- `reviveIfDone(threadId)` → bool: `UPDATE dm_threads SET status='inbox' WHERE id=? AND status='done' RETURNING id`. True jeśli coś zmieniono (log info `thread {id} auto-revived from done → inbox (new incoming)`). Wołane z `syncMessagesForThread` (sekcja 2.3 krok 6). Semantyka: nowa wiadomość przychodząca nie może utknąć w archiwum.

---

## 5. send.ts

### `sendDraft(threadId, body)` → `{ok: bool, circleMessageId: int|null, error?: string}`

Kolejność operacji jest istotna - odtworzyć dokładnie:

1. `SELECT * FROM dm_threads WHERE id=? LIMIT 1`; brak → **throw** `Error("thread {id} not found")` (to wyjątek, nie `{ok:false}`).
2. **Audyt PRZED próbą wysyłki**: `INSERT INTO sent_messages (thread_id, body) VALUES (...) RETURNING id`. Wiersz powstaje zawsze, nawet jeśli send się wywali.
3. `try`:
   a. `jwt = getJwtFor(thread.admin_account_id)`.
   b. `result = sendMessage(jwt.accessToken, thread.circle_chat_room_uuid, body)` - POST z `{body, rich_text_body: textToTiptap(body)}` (sekcja 0.2). Log info `Circle send response` + JSON.slice(0,400).
   c. Update audytu: `UPDATE sent_messages SET circle_message_id = (result.id jeśli jest liczbą, inaczej NULL), circle_creation_uuid = result.creation_uuid ?? NULL WHERE id = <audit id>`. (Circle zwraca `{creation_uuid, parent_message_id, sent_at}`; numeryczne `id` zwykle nie przychodzi.)
   d. `markSent(threadId)` (z draft-orchestratora): `UPDATE draft_sessions SET status='sent', current_draft=NULL WHERE thread_id=?` + broadcast `{"type":"draft:status","threadId":<id>,"status":"sent"}`. **Broadcast leci nawet gdy sesji draftu nie ma** (update 0 wierszy nie blokuje).
   e. `clearPendingCheckupsOnSend(threadId)` (sekcja 4) - wiszące check-upy auto-done.
   f. **Syntetyczny placeholder wiadomości** (eventual consistency Circle: GET zaraz po POST może nie zawierać naszej wiadomości):
      - `SELECT label, community_member_id FROM admin_accounts WHERE id = thread.admin_account_id LIMIT 1`.
      - INSERT do `dm_messages` z `ON CONFLICT DO NOTHING`:
        - `thread_id = threadId`
        - `circle_message_id = -Date.now()` - **ujemny aktualny epoch w milisekundach** (Python: `-int(time.time() * 1000)`). Ujemność = marker syntetyczności, sprzątany przez reconciliację (2.3 krok 4).
        - `body = body`, `rich_text_body = NULL`
        - `sender_id = account?.community_member_id ?? NULL`, `sender_name = account?.label ?? NULL`, `sender_is_me = true`
        - `parent_message_id = NULL`, `chat_thread_id = NULL`
        - `created_at = result.sent_at ? parse(result.sent_at) : now()`, `edited_at = NULL`
   g. **Bump metadanych wątku** (żeby preview w inboxie natychmiast pokazywał send):
      ```sql
      UPDATE dm_threads SET
        last_message_at = <sent_at lub now>,
        last_message_preview = <body[:240]>,
        last_message_sender_id = <account.community_member_id|NULL>,
        last_message_sender_is_me = true,
        unread_messages_count = 0
      WHERE id = threadId
      ```
      Zerowanie unread = semantyka aplikacji "odpisaliśmy → wątek przeczytany".
   h. **Mark-as-read po stronie Circle, fire-and-forget**: `markChatRoomRead(jwt.accessToken, thread.circle_chat_room_uuid)` → `PATCH /api/headless/v1/messages/{uuid}` body `{"unread_messages_count": 0}`. **Bez await** - błąd tylko log warn `mark-as-read failed for thread {id}: {message}`, nie blokuje odpowiedzi. Cel: zgaszenie natywnego badge'a/pusha na realnym koncie Circle admina.
   i. `await syncMessagesForThread(threadId)` - z catch→log warn `post-send message sync failed: {message}` (błąd nie psuje wyniku). Re-sync zaciąga realną wiadomość i sprząta placeholder z (f), jeśli Circle już nadąża.
   j. `await syncThreadsForAccount(thread.admin_account_id)` - z catch→log warn `post-send thread sync failed: {message}`.
   k. `auditMessageId = result.id ?? null` i broadcast:
      ```json
      {"type":"send:result","threadId":<id>,"ok":true,"circleMessageId":<auditMessageId>}
      ```
   l. Log info `sent message to thread {id} (creation_uuid={uuid|'n/a'})`. Return `{ok: true, circleMessageId: auditMessageId}` (w praktyce prawie zawsze `circleMessageId: null`).
4. `catch (err)`:
   a. `message = err.message`; log error `send failed for thread {id}` + message.
   b. Jeśli `CircleApiError` ze status 401 → `invalidateJwt(thread.admin_account_id)` (bez re-throw - błąd idzie dalej jako wynik).
   c. `UPDATE sent_messages SET error = message WHERE id = <audit id>`.
   d. Broadcast `{"type":"send:result","threadId":<id>,"ok":false,"circleMessageId":null,"error":<message>}`.
   e. Return `{ok: false, circleMessageId: null, error: message}`.

Format `message` dla błędów Circle: `Circle API {status}: {pierwsze 200 znaków response body}` (patrz 0.2) - ten string trafia do `sent_messages.error`, do WS i do odpowiedzi HTTP. Zachować.

---

## 6. bulk-send.ts

### Typy

```ts
BulkSendItem =
  | { kind: 'thread'; threadId: number }
  | { kind: 'member'; adminAccountId: number; memberId: number }

BulkSendResult = {
  kind: 'thread' | 'member',
  threadId: number | null,
  memberId: number | null,
  ok: boolean,
  circleMessageId: number | null,
  error?: string            // klucz nieobecny gdy brak błędu
}
```

### `sendToMultiple(items, body)` → `BulkSendResult[]`

Ta sama treść `body` do mieszanej listy istniejących wątków i nowych odbiorców. Log info `bulk send to {n} recipients`. **Pętla ściśle sekwencyjna** (rate-friendly wobec Circle) - każdy item czeka na poprzedni, wynik dopisywany w kolejności items. Brak przerwania na błędzie - przetwarzane są wszystkie itemy.

Dla `kind === 'thread'`:
1. `SELECT id FROM dm_threads WHERE id=? LIMIT 1`. Brak → wynik `{kind:'thread', threadId: item.threadId, memberId: null, ok: false, circleMessageId: null, error: 'Thread not found'}` (dosłowny string) i `continue`.
2. `r = sendDraft(item.threadId, body)` → wynik `{kind:'thread', threadId, memberId: null, ok: r.ok, circleMessageId: r.circleMessageId, error: r.error}` (klucz `error` = undefined przy sukcesie).
3. Throw z `sendDraft` (np. wątek skasowany między SELECT a środkiem) → `{kind:'thread', threadId, memberId: null, ok: false, circleMessageId: null, error: err.message}`.

Dla `kind === 'member'` (nowy odbiorca, find-or-create przez compose-orchestrator):
1. `r = sendComposeDraft(item.adminAccountId, item.memberId, body)` - kontrakt: zwraca `{ok: true, threadId, circleChatRoomUuid}` albo `{ok: false, error}` (szczegóły w specu compose-orchestratora; w środku find-or-create chat roomu + wysyłka jako operacja atomowa).
2. `r.ok` → `{kind:'member', threadId: r.threadId, memberId: item.memberId, ok: true, circleMessageId: null}` (zawsze null - find-or-create nie zwraca message id).
3. `!r.ok` → `{kind:'member', threadId: null, memberId: item.memberId, ok: false, circleMessageId: null, error: r.error}`.
4. Throw → jak (3) z `error: err.message`.

---

## 7. app-settings.ts

Globalne ustawienia w jednowierszowej tabeli `app_settings` (zawsze `id = 1`).

### Snapshot i defaulty

`AppSettingsSnapshot` (pola i defaulty gdy wiersza brak / pole NULL):

| Pole snapshotu | Kolumna | Default |
|---|---|---|
| `globalMetaPrompt` | `global_meta_prompt` | `''` |
| `formatPrompt` | `format_prompt` | `''` |
| `draftModel` | `draft_model` | `null` (null/brak = fallback na env `DRAFT_MODEL`, default `claude-sonnet-4-6`) |
| `formatModel` | `format_model` | `null` (fallback env `POLISH_MODEL`, default `claude-opus-4-7`) |
| `noReplyThresholdDays` | `no_reply_threshold_days` | `3` |
| `silenceThresholdDays` | `silence_threshold_days` | `14` |

### Cache

- In-memory, na poziomie modułu, jeden wpis: snapshot + `cachedAt` (epoch ms).
- **TTL: `CACHE_MS = 30_000` ms.** `getSettings()`: jeśli cache istnieje i `now - cachedAt < 30000` → zwróć cache; inaczej `SELECT * FROM app_settings WHERE id=1 LIMIT 1`, zbuduj snapshot z defaultami j.w., zapisz do cache.
- Gettery pochodne: `getGlobalMetaPrompt()`, `getFormatPrompt()`, `getDraftModel()`, `getFormatModel()` - każdy to `(await getSettings()).<pole>`.

### Zapis (settery)

Wspólny wzorzec upsert (string i int):
```sql
INSERT INTO app_settings (id, <kolumna>, updated_at) VALUES (1, <wartość>, now())
ON CONFLICT (id) DO UPDATE SET <kolumna> = <wartość>, updated_at = now()
```
Po **każdym** zapisie: `cached = null` (twarda invalidacja - następny odczyt idzie do DB).

Normalizacja stringów (`upsertStringField`):
- dla `draftModel` / `formatModel`: `null` LUB `''` → zapis `NULL` (pusty string nadpisywałby fallback na env, dlatego koercja do NULL),
- dla `globalMetaPrompt` / `formatPrompt`: `value ?? ''` (null → pusty string; kolumny są NOT NULL).

Settery: `setGlobalMetaPrompt(string)`, `setFormatPrompt(string)`, `setDraftModel(string|null)`, `setFormatModel(string|null)`, `setNoReplyThresholdDays(number)`, `setSilenceThresholdDays(number)`.

### Komponowanie promptów systemowych (DOSŁOWNE szablony)

`composeSystemPrompt(persona, metaPrompt)` - dla generowania draftów (auto + "Wygeneruj od nowa"; używane przez draft-orchestrator i compose-orchestrator):
- `metaPrompt.trim()` puste → zwraca samą `persona`.
- Inaczej (dokładny string, `\n` literalnie):
  ```
  [GLOBALNE ZASADY STYLU — stosuj zawsze]
  {metaPrompt.trim()}

  ---

  {persona}
  ```
  czyli `"[GLOBALNE ZASADY STYLU — stosuj zawsze]\n" + metaPrompt.trim() + "\n\n---\n\n" + persona`. (Uwaga: w nagłówku jest długi myślnik `—` - to treść promptu, zostawić 1:1.)

`composeFormatSystemPrompt(persona, metaPrompt, formatPrompt)` - dla "Formatuj z AI" (format-orchestrator):
- `base = composeSystemPrompt(persona, metaPrompt)`.
- `formatPrompt.trim()` puste → zwraca `base`.
- Inaczej: `base + "\n\n---\n\n[INSTRUKCJA FORMATOWANIA]\n" + formatPrompt.trim()`.

---

## Uwagi dla portu na FastAPI

1. **Brak auto-generate top5.** PROJECT.md (sekcja "Sync / polling") wciąż opisuje "Auto-generate: fire-and-forget top 5 unread per tick" - to martwy zapis. Auto-draft wyłączony 2026-05-13 (ta sama dokumentacja, linia 85) i kod go nie ma. Nie portować.
2. **Trik `(xmax = 0)`** w `upsertMessage` to Postgres-only sposób odróżnienia INSERT od UPDATE przy `ON CONFLICT DO UPDATE`. W SQLAlchemy/asyncpg dodać do RETURNING surowe wyrażenie `(xmax = 0) AS is_insert`. Licznik `inserted` (a od niego WS `messages:loaded`, auto-revival i reconciliacja) zależy od poprawności tego rozróżnienia.
3. **Ujemne `circle_message_id`**: placeholder to `-Date.now()` = ujemny epoch ms (`-int(time.time()*1000)`). Kolumna bigint NOT NULL. Reconciliacja szuka `circle_message_id < 0 AND sender_is_me = true` i porównuje body po `normalizeForMatch`. Dwa sendy o identycznej treści w tym samym wątku: drugi placeholder ma inny (mniejszy) id, więc unique constraint nie koliduje; reconciliacja skasuje oba gdy Circle odda choć jedną realną wiadomość o tej treści - to zachowanie oryginału, zachować.
4. **Fire-and-forget**: trzy miejsca - (a) `syncMessagesForThread` per stale thread w ticku, (b) `markChatRoomRead` po sendzie, (c) nic więcej. W FastAPI użyć `asyncio.create_task` z odłowieniem wyjątku (log warn); trzymać referencję do taska (inaczej GC może go ubić). Natomiast post-send `syncMessagesForThread` i `syncThreadsForAccount` w `sendDraft` są **awaitowane** (z połkniętym błędem) - odpowiedź HTTP na send czeka na nie; nie zamieniać na background, bo frontend liczy na świeże dane zaraz po sendzie.
5. **Guard reentrancy ticka**: zwykła flaga boolowska wystarcza w jednym event loopie; nie używać blokującego locka (tick ma być pomijany, nie kolejkowany). Pierwszy tick odpala się natychmiast przy starcie aplikacji (lifespan startup), potem co `POLLING_INTERVAL_MS` (default 30 s, min 5 s).
6. **`thread:new_messages` ma zawsze `newCount: 1`** - hardcoded, niezależnie od liczby nowych wiadomości. Frontend traktuje to jako sygnał, nie licznik. Nie "naprawiać".
7. **`attachment_index` to indeks w pełnej liście załączników** (po `extractAttachments`), nie indeks wśród samych obrazków - wiadomość [audio, image] da obrazkowi index 1, nie 0. Unikalność `(message_id, attachment_index)` na tym polega; zmiana indeksowania zdubluje kolejkę vision.
8. **Semantyka unread**: dwa niezależne mechanizmy zerowania - (a) w upsercie wątku `localUnread = 0` gdy `lastSenderIsMe`, (b) po sendzie twardy `unread_messages_count = 0` + PATCH do Circle. Oba zachować; bez (a) badge wraca przy następnym pollingu.
9. **Auto-revival jest "szeroki"**: warunek `incomingFresh` patrzy na całą pobraną stronę 100 wiadomości, nie na nowo wstawione. Wątek 'done', w którym wstawi się COKOLWIEK nowego (nawet nasza własna wiadomość wysłana z Circle web) przy obecności jakiejkolwiek starej cudzej wiadomości w historii → wraca do 'inbox'. Odtworzyć 1:1, nie "ulepszać" do sprawdzania tylko świeżych incoming.
10. **Polling pokrywa tylko top 50 wątków** (jedna strona `listThreads`) i **tylko 100 ostatnich wiadomości** wątku (jedna strona `getThreadMessages`). To celowe ograniczenia - nie dodawać paginacji.
11. **Cache app-settings jest per proces.** TTL 30 s + invalidacja przez `cached = None` działają tylko w obrębie jednego procesu. Przy uvicorn/gunicorn z >1 workerem zapis w workerze A nie invaliduje cache w workerze B - rozjazd do 30 s. Oryginał działa w jednym procesie Node; najbezpieczniej trzymać 1 workera albo zaakceptować TTL-owe opóźnienie.
12. **JWT manager**: dedupe `inflight` to mapa promise'ów per konto - w Pythonie mapa `asyncio.Task`/`Future` z `finally: del`. Po 401 zawsze `invalidateJwt` + re-throw; retry NIE jest robiony w tych serwisach (następny tick / wywołanie pobierze świeży JWT).
13. **Kolejność w `sendDraft` jest kontraktem**: audyt przed wysyłką; po sukcesie najpierw update audytu, potem markSent (czyszczenie draftu + WS `draft:status`), potem check-upy, placeholder, bump wątku, fire-and-forget PATCH, dwa awaitowane synci, na końcu WS `send:result` i return. Przy błędzie: audyt dostaje `error`, WS `send:result` z `ok:false`, funkcja **zwraca** błąd (nie rzuca) - wyjątkiem jest brak wątku (krok 1), który rzuca.
14. **`markSent` broadcastuje `draft:status` nawet bez istniejącej sesji draftu** (UPDATE 0 wierszy). Zachować - frontend i tak filtruje po threadId.
15. **Stringi błędów są API**: `'Thread not found'` (bulk), `"thread {id} not found"` (throw), `"Circle API {status}: {body[:200]}"`, `"Circle returned non-JSON response"`, `"admin_account {id} not found"`, `"admin_account {id} is not active"`. Trafiają do DB (`sent_messages.error`), WS i odpowiedzi HTTP - nie tłumaczyć, nie przeformułowywać.
16. **`slice(0, 240)`** dla preview działa w JS na jednostkach UTF-16; Python `body[:240]` tnie po code pointach. Różnica widoczna tylko przy emoji/surrogate pairs na granicy - akceptowalna, ale świadoma.
17. **`toISOString()`**: serializacja dat check-upów to dokładnie `YYYY-MM-DDTHH:mm:ss.sssZ` (UTC, milisekundy, sufiks `Z`). Python `datetime.isoformat()` daje `+00:00` i mikrosekundy - sformatować ręcznie (`strftime('%Y-%m-%dT%H:%M:%S.') + f'{ms:03d}Z'` po konwersji do UTC), bo frontend może parsować/porównywać stringi.
18. **`textToTiptap` musi być bajt-w-bajt zgodny** (envelope + spacer-paragraphy + hardBreak per linia + `circle_ios_fallback_text` na obu poziomach). Wysyłka bez envelope = treść cicho znika po stronie Circle. Testować na kontach Paweł Wyrozumski / Tomasz Dwa, nigdy na realnych członkach.
19. **Sekwencyjność bulk-send** jest celowa (rate-friendly). Nie zrównoleglać przez `asyncio.gather`.
20. **`syncNow(adminAccountId)`** z argumentem NIE emituje WS eventów i NIE dociąga stale messages (woła samo `syncThreadsForAccount`); bez argumentu robi pełny tick. Route'y sync mogą zależeć od tej różnicy - sprawdzić w specu routes.
21. **`can_send_message` w members-sync jest zawsze nadpisywane na `true`** przy każdym upsercie - jeśli gdzieś indziej w aplikacji to pole jest ustawiane na false (np. po błędzie "Messaging is disabled by receiver"), pełny re-sync członków to wyzeruje. Tak działa oryginał.
22. **Timeout HTTP do Circle: 30 s na request** (nie globalny na tick). Tick może trwać długo przy wielu stale threads - guard reentrancy to przewiduje.
