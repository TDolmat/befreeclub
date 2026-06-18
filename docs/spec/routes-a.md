# Spec routes-a: Circle DM (accounts, threads, messages, members, settings) + core feedback

Źródła (stan na 2026-06-10):

- `admin/apps/server/src/tools/circle-dm/routes/index.ts`
- `admin/apps/server/src/tools/circle-dm/routes/accounts.ts`
- `admin/apps/server/src/tools/circle-dm/routes/threads.ts`
- `admin/apps/server/src/tools/circle-dm/routes/messages.ts`
- `admin/apps/server/src/tools/circle-dm/routes/members.ts`
- `admin/apps/server/src/tools/circle-dm/routes/settings.ts`
- `admin/apps/server/src/core/feedback/routes.ts`
- pomocniczo: `packages/shared/src/schemas/*.ts`, `services/{thread-state,thread-sync,app-settings,members-sync}.ts`, `circle/{client,attachments,types}.ts`, `core/auth/middleware.ts`, `src/index.ts`

Wszystkie pola JSON w request/response są **camelCase**, chyba że wyraźnie zaznaczono inaczej. Daty zawsze jako ISO 8601 string z `.toISOString()` (format `YYYY-MM-DDTHH:mm:ss.sssZ`, zawsze UTC z `Z` i milisekundami).

---

## 1. Montowanie i warstwa wspólna

### 1.1 Prefiksy ścieżek (z `src/index.ts`)

```
.route('/api/auth', authRoutes)          // publiczne (poza tym spec)
.use('/api/*', requireAuth)              // WSZYSTKO poniżej za auth
.route('/api/feedback', feedbackRoutes)
.route('/api/circle-dm', dmApp)
```

`dmApp` (z `routes/index.ts`) montuje pod `/api/circle-dm`:

```
/accounts   → accountsRoute   (ten spec)
/threads    → threadsRoute    (ten spec)
/messages   → messagesRoute   (ten spec)
/drafts     → draftsRoute     (inny spec)
/members    → membersRoute    (ten spec)
/compose    → composeRoute    (inny spec)
/format     → formatRoute     (inny spec)
/bulk       → bulkRoute       (inny spec)
/settings   → settingsRoute   (ten spec)
/kb         → kbRoute         (inny spec)
/assistant  → assistantRoute  (inny spec)
```

### 1.2 Auth (`requireAuth`)

- `NODE_ENV !== 'production'`: middleware no-op, do kontekstu wkłada `auth = { authAccountId: 0, email: 'dev@local' }` (stała `DEV_FAKE_AUTH`). Brak jakiegokolwiek sprawdzania cookie.
- Produkcja: czyta cookie sesji (`SESSION_COOKIE_NAME`), waliduje w DB. Brak/nieprawidłowa/wygasła sesja → **401** body `{"error":"Unauthorized"}`. Po sukcesie `auth = { authAccountId, email }` z sesji.
- Z route'ów opisanych tutaj kontekstu `auth` używa TYLKO feedback (`POST /api/feedback`). Route'y circle-dm nie czytają auth (autoryzacja = sam fakt zalogowania, brak per-account ACL).

### 1.3 Walidacja (zValidator, `@hono/zod-validator` ^0.4.1)

Każdy `zValidator('param'|'query'|'json', schema)` przy niepowodzeniu walidacji zwraca **400** z domyślnym body = zserializowany wynik zod safeParse:

```json
{"success":false,"error":{"issues":[{"code":"...","path":["..."],"message":"..."}],"name":"ZodError"}}
```

Dotyczy to też nienumerycznych `:id` w ścieżce (400, NIE 404) i błędnego JSON-a w body. Parametry path/query są stringami, więc schematy używają `z.coerce.number()` tam, gdzie trzeba (zaznaczone niżej). W body JSON `z.number()` bez coerce = string `"5"` odrzucony.

Wspólny schemat parametru ścieżki (w kilku plikach identyczny):

```ts
const idParam = z.object({ id: z.coerce.number().int().positive() });
```

### 1.4 Globalny error handler

Nieobsłużony wyjątek w handlerze → **500** body `{"error": "<err.message>"}` (oraz log `unhandled error`).

---

## 2. Accounts: `/api/circle-dm/accounts`

Tabela: `admin_accounts` (konta adminów Circle, z których wysyłamy DM-y).

### GET `/api/circle-dm/accounts`

- Brak parametrów.
- Zwraca **200**:

```json
{
  "accounts": [
    {
      "id": 1,
      "label": "Tomek",
      "email": "x@y.z",
      "hasToken": true,
      "communityId": 123,
      "communityMemberId": 456,
      "systemPrompt": "...",
      "isActive": true,
      "lastSyncedAt": "2026-06-10T10:00:00.000Z",
      "createdAt": "2026-05-12T...",
      "updatedAt": "2026-05-12T..."
    }
  ]
}
```

- `hasToken` = `circleAdminToken.length > 0` (samego tokena NIGDY nie zwracamy).
- `communityId`, `communityMemberId` mogą być `null` (przed pierwszym test-connection/sync).
- `lastSyncedAt`: ISO string albo `null`.
- Brak filtrowania, brak limitu, brak sortowania (kolejność z DB).

### POST `/api/circle-dm/accounts`

Body (`createAdminAccountSchema`):

| pole | typ | walidacja |
|---|---|---|
| `label` | string | min 1, max 120, wymagane |
| `email` | string | `z.string().email()`, wymagane |
| `circleAdminToken` | string | min 8, **opcjonalne** |
| `systemPrompt` | string | min 10, wymagane |

Logika:

1. `token = body.circleAdminToken ?? env.BOOTSTRAP_ADMIN_TOKEN`.
2. Jeśli brak obu → **400** body dokładnie: `{"error":"circleAdminToken missing and BOOTSTRAP_ADMIN_TOKEN is not set in env"}`.
3. INSERT do `admin_accounts` (label, email, circleAdminToken=token, systemPrompt; reszta kolumn defaultowa, m.in. `isActive=true`).
4. Log info: `created account <id> (<email>)`.
5. **Side-effect fire-and-forget** (nie blokuje odpowiedzi, błąd tylko logowany jako warn): `syncThreadsForAccount(id)` - pierwsze zaciągnięcie skrzynki (patrz 2.1).
6. Odpowiedź **201**: `{"id": <number>}`.

### PATCH `/api/circle-dm/accounts/:id`

- Param: `id` (coerce int > 0).
- Body (`updateAdminAccountSchema` = partial create + `isActive`): wszystkie pola opcjonalne: `label`, `email`, `circleAdminToken` (min 8), `systemPrompt` (min 10), `isActive` (boolean). **Puste body `{}` jest poprawne** (zaktualizuje tylko `updatedAt`).
- Aktualizowane są tylko pola przekazane (`!== undefined`). Zawsze ustawiane `updatedAt = now()`.
- **Side-effect**: jeśli zmieniono `circleAdminToken`, dodatkowo `circleAccessToken = null` i `circleAccessTokenExpiresAt = null` (wymusza re-auth do Circle przy następnym wywołaniu).
- **Brak sprawdzenia istnienia** - PATCH nieistniejącego id również zwraca sukces.
- Odpowiedź **200**: `{"ok": true}`.

### DELETE `/api/circle-dm/accounts/:id`

- Param: `id` (coerce int > 0).
- `DELETE FROM admin_accounts WHERE id = :id`. Brak sprawdzenia istnienia.
- Odpowiedź **200**: `{"ok": true}`.

### POST `/api/circle-dm/accounts/:id/test-connection`

- Param: `id`. Brak body.
- Jeśli konto nie istnieje → **404** `{"error":"account not found"}`.
- Wywołuje `exchangeAdminTokenForJWT(account.circleAdminToken, account.email)` = `POST https://<circle>/api/v1/headless/auth_token` z body `{ "email": "<email>" }` i tokenem admina w auth. Odpowiedź Circle (snake_case!):

```ts
interface CircleAuthResponse {
  access_token: string;
  refresh_token: string;
  access_token_expires_at: string;
  refresh_token_expires_at: string;
  community_id: number;
  community_member_id: number;
}
```

- Sukces → **mutacja DB**: zapis na koncie `circleAccessToken`, `circleAccessTokenExpiresAt` (= `new Date(access_token_expires_at)`), `circleRefreshToken`, `communityId`, `communityMemberId`. Odpowiedź **200**:

```json
{"ok": true, "communityId": 123, "communityMemberId": 456}
```

- Błąd (wyjątek z Circle) → **400**:

```json
{"ok": false, "communityId": null, "communityMemberId": null, "error": "<message>"}
```

### POST `/api/circle-dm/accounts/:id/sync`

- Param: `id`. Brak body. Ręczny sync wątków.
- Wywołuje `syncThreadsForAccount(id)` (patrz 2.1). Następnie dla KAŻDEGO id z `staleMessageThreadIds` odpala **fire-and-forget** `syncMessagesForThread(threadId)` (błędy tylko warn-log) - dociąga historię wiadomości dla wątków, którym poprzedni tick zaktualizował metadane, ale nie wiadomości.
- Sukces **200**:

```json
{
  "ok": true,
  "changedThreadIds": [1, 2],
  "newUnreadThreadIds": [2],
  "staleMessageThreadIds": [1, 2, 7]
}
```

- Wyjątek → **500** `{"ok": false, "error": "<message>"}` (uwaga: NIE globalny handler, własny catch - kształt z `ok:false`).

### 2.1 Side-effecty serwisu `syncThreadsForAccount(adminAccountId)`

(istotne, bo route'y POST `/accounts` i `/accounts/:id/sync` go wywołują)

1. Pobiera JWT konta (cache w DB + refresh; 401 z Circle → `invalidateJwt` i rethrow).
2. `GET /api/headless/v1/messages?page=1&per_page=50` - **tylko jedna strona, 50 wątków**.
3. Dla każdego rekordu upsert do `dm_threads` po `(adminAccountId, circleChatRoomUuid)`. Kluczowy kawałek: `localUnread = lastSenderIsMe ? 0 : record.unread_messages_count` (jeśli ostatnią wiadomość wysłaliśmy my, lokalny unread = 0 niezależnie od Circle). Zapisuje też preview `last_message.body.slice(0, 240)`, `rawPayload` (cały rekord), `fetchedAt = now()`.
4. Klasyfikacja zwracanych id:
   - `changed`: unread się zmienił LUB `lastMessageAt` się zmienił (porównanie po `getTime()`, null traktowany jak 0); nowo wstawiony wątek zawsze `changed`.
   - `newUnread`: `localUnread > poprzedni unread` (nowy wątek: `localUnread > 0`).
   - `staleMessages`: `lastMessageAt != null` i (`messagesFetchedAt == null` lub `messagesFetchedAt < lastMessageAt`); nowy wątek: `lastMessageAt != null`.
5. Na koniec `admin_accounts.lastSyncedAt = now()`.
6. Zwraca `{ changedThreadIds, newUnreadThreadIds, staleMessageThreadIds }`. Ta funkcja sama NIE broadcastuje WS (robi to polling-worker, poza tym spec'em).

### 2.2 Side-effecty serwisu `syncMessagesForThread(threadId)`

(wywoływany przez `/accounts/:id/sync` fire-and-forget oraz przez GET `/threads/:id/messages`)

1. Jeśli wątek nie istnieje → `throw Error("thread <id> not found")`.
2. `GET /api/headless/v1/messages/<chatRoomUuid>/chat_room_messages?per_page=100` (jedna strona, 100 wiadomości). 401 → invalidate JWT + rethrow.
3. Upsert każdej wiadomości do `dm_messages` po `(threadId, circleMessageId)`. Treść plain: preferuje `tiptapToPlainText(rich_text_body)`, fallback `record.body`. Na konflikcie nadpisuje `body`, `richTextBody`, `editedAt`. Detekcja insert vs update: `xmax = 0`. Jeśli wiadomość ma załącznik audio z `voiceMessage=true` → przy INSERT `voiceTranscriptStatus = 'pending'` (na update nie zmienia statusu).
4. Dla każdego załącznika `kind === 'image'` insert do `message_image_descriptions` (`messageId`, `attachmentIndex`, `attachmentUrl = fullUrl ?? url`), `ON CONFLICT DO NOTHING` (unikalność `(message_id, attachment_index)`), status default `'pending'`.
5. Czyści syntetyczne placeholdery (nasze lokalne kopie wysłanych wiadomości, `circleMessageId < 0`, `senderIsMe = true`), których body po normalizacji whitespace (`replace(/\s+/g,' ').trim()`) pokrywa się z prawdziwą wiadomością od nas zwróconą przez Circle - DELETE.
6. `dm_threads.messagesFetchedAt = now()`.
7. **Auto-revival**: jeśli wstawiono >0 nowych wiadomości i jakakolwiek z odpowiedzi Circle pochodzi nie od nas → `reviveIfDone(threadId)`: `UPDATE dm_threads SET status='inbox' WHERE id=:id AND status='done'`.
8. **WS broadcast** (jeśli wstawiono >0): `{"type":"messages:loaded","threadId":<id>,"count":<inserted>}`.
9. Zwraca liczbę wstawionych wiadomości (int).

---

## 3. Threads: `/api/circle-dm/threads`

Tabele: `dm_threads`, `dm_messages`, `thread_checkups`, `message_image_descriptions`.

### Schematy zod (verbatim z `threads.ts`)

```ts
const filterEnum = z.enum(['inbox','unread','no_reply','silent','flagged','checkup','done']);

const listQuery = z.object({
  adminAccountId: z.coerce.number().int().positive(),
  filter: filterEnum.default('inbox'),
  sort: z.enum(['recent','oldest_no_reply','next_checkup']).default('recent'),
  limit: z.coerce.number().int().min(1).max(200).default(100),
});

const threadIdParam = z.object({ id: z.coerce.number().int().positive() });
const checkupIdParam = z.object({
  id: z.coerce.number().int().positive(),
  checkupId: z.coerce.number().int().positive(),
});

const statusBody = z.object({ status: z.enum(['inbox','done']) });
const flagBody = z.object({ isFlagged: z.boolean() });
const bulkActionBody = z.object({
  adminAccountId: z.number().int().positive(),
  ids: z.array(z.number().int().positive()).min(1).max(500),
  action: z.enum(['done','inbox','flag','unflag']),
});
const createCheckupBody = z.object({
  dueAt: z.string().datetime(),
  note: z.string().max(500).nullable().optional(),
});
```

Surowe SQL EXISTS (verbatim, używane w filtrach):

```sql
-- futurePendingCheckupExists: wątek "zaparkowany" na PRZYSZŁY check-up
EXISTS (
  SELECT 1 FROM thread_checkups
  WHERE thread_checkups.thread_id = dm_threads.id
    AND thread_checkups.done_at IS NULL
    AND thread_checkups.due_at > now()
)

-- anyPendingCheckupExists: jakikolwiek niezamknięty check-up
EXISTS (
  SELECT 1 FROM thread_checkups
  WHERE thread_checkups.thread_id = dm_threads.id
    AND thread_checkups.done_at IS NULL
)
```

Check-up z `due_at <= now()` (już "DUE") celowo NIE chowa wątku z Inbox.

### Serializacja wątku (`serializeThread`) - kształt JSON

```json
{
  "id": 1,
  "adminAccountId": 1,
  "circleChatRoomId": 999,
  "circleChatRoomUuid": "uuid-string",
  "chatRoomKind": "direct",
  "chatRoomName": null,
  "otherParticipantEmail": "a@b.c",
  "otherParticipantName": "Jan",
  "otherParticipantId": 42,
  "otherParticipantAvatarUrl": "https://...",
  "unreadMessagesCount": 0,
  "pinnedAt": null,
  "status": "inbox",
  "isFlagged": false,
  "nextCheckupDueAt": null,
  "nextCheckupNote": null,
  "pendingCheckupCount": 0,
  "lastMessageAt": "2026-06-01T10:00:00.000Z",
  "lastMessageSenderId": 42,
  "lastMessageSenderIsMe": false,
  "lastMessagePreview": "tekst max 240 znaków",
  "fetchedAt": "2026-06-10T09:00:00.000Z"
}
```

- `chatRoomKind`: `'direct' | 'group_chat'`. `status`: `'inbox' | 'done'`.
- Nullable: `chatRoomName`, `otherParticipant*`, `pinnedAt`, `nextCheckupDueAt`, `nextCheckupNote`, `lastMessageAt`, `lastMessageSenderId`, `lastMessagePreview`.
- Pola `nextCheckupDueAt`/`nextCheckupNote`/`pendingCheckupCount` są WYLICZANE: dla podanych threadIds ładowane są pending check-upy (`done_at IS NULL`) posortowane `due_at ASC`; pierwszy rekord per wątek daje `nextDueAt`/`nextNote`, `count` = liczba pending. Brak pending → `null`/`null`/`0`.

### GET `/api/circle-dm/threads`

Query: `adminAccountId` (wymagany, coerce int>0), `filter` (default `inbox`), `sort` (default `recent`), `limit` (coerce int 1-200, default 100).

Warunki WHERE (zawsze `adminAccountId = :x` plus per filter):

| filter | warunki dodatkowe |
|---|---|
| `inbox` | `status='inbox'` AND `NOT (futurePendingCheckupExists)` |
| `unread` | `status='inbox'` AND `lastMessageSenderIsMe = false` |
| `no_reply` | `status='inbox'` AND `lastMessageSenderIsMe = true` AND `lastMessageAt < now() - noReplyThresholdDays` |
| `silent` | `status='inbox'` AND `lastMessageAt < now() - silenceThresholdDays` AND `NOT (futurePendingCheckupExists)` |
| `flagged` | `isFlagged = true` (UWAGA: bez warunku status - łapie też done) |
| `checkup` | `anyPendingCheckupExists` (bez warunku status - łapie też done) |
| `done` | `status='done'` |

- Progi dni z ustawień (`getSettings()`, patrz sekcja 6): `noReplyThresholdDays` default 3, `silenceThresholdDays` default 14. Przeliczane na ms: `days * 24*60*60*1000`, porównanie `lastMessageAt < new Date(Date.now() - ms)` (strictly less-than).
- ORDER BY: dla `sort=oldest_no_reply`: `pinnedAt DESC, lastMessageAt ASC`; dla `recent` i `next_checkup`: `pinnedAt DESC, lastMessageAt DESC`. LIMIT `limit`.
- `sort=next_checkup`: po pobraniu z DB (czyli **po** zastosowaniu limitu na sortowaniu "recent"!) sortowanie w pamięci rosnąco po `nextCheckupDueAt`; wątki bez check-upu na końcu (`Infinity`).
- Odpowiedź **200**: `{"threads": [<serializeThread>...], "count": <threads.length>}` (`count` = długość zwróconej listy, NIE total w DB).

### GET `/api/circle-dm/threads/:id`

- Param `id`. Nie znaleziono → **404** `{"error":"thread not found"}`.
- **200**: pojedynczy obiekt `serializeThread` **bez koperty** (nie `{thread: ...}`).

### PATCH `/api/circle-dm/threads/:id/status`

- Body: `{"status": "inbox" | "done"}`.
- `UPDATE dm_threads SET status=:status WHERE id=:id` (bez sprawdzenia istnienia).
- **200**: `{"ok": true}`.

### PATCH `/api/circle-dm/threads/:id/flag`

- Body: `{"isFlagged": true|false}` (ścisły boolean).
- `UPDATE dm_threads SET is_flagged=:v WHERE id=:id` (bez sprawdzenia istnienia).
- **200**: `{"ok": true}`.

### POST `/api/circle-dm/threads/bulk-action`

- Body: `{"adminAccountId": <int>, "ids": [<int>...], "action": "done"|"inbox"|"flag"|"unflag"}`. UWAGA: tu `adminAccountId` i `ids` to `z.number()` BEZ coerce (stringi odrzucane). `ids` min 1, max 500 elementów.
- Logika (`bulkSetStatus` / `bulkSetFlagged`): `UPDATE dm_threads SET ... WHERE admin_account_id = :acc AND id IN (:ids) RETURNING id`. Scoping po koncie = id z cudzego konta jest po cichu ignorowane. Zwracana liczba = faktycznie zmienione wiersze.
  - `done` → status='done', `inbox` → status='inbox', `flag` → isFlagged=true, `unflag` → isFlagged=false.
- **200**: `{"ok": true, "count": <int>}`.

### GET `/api/circle-dm/threads/:id/checkups`

- **200**: `{"checkups": [<CheckupRow>...]}` - **wszystkie** check-upy wątku (pending i done), `ORDER BY due_at ASC`.
- CheckupRow:

```json
{
  "id": 1,
  "threadId": 5,
  "dueAt": "2026-06-15T08:00:00.000Z",
  "note": "dopytać o projekt",
  "doneAt": null,
  "createdAt": "2026-06-10T09:00:00.000Z"
}
```

### POST `/api/circle-dm/threads/:id/checkups`

- Body: `{"dueAt": "<ISO datetime>", "note": "<string max 500>" | null}` (`note` opcjonalne, default zapisuje `null`). `dueAt` przez `z.string().datetime()` - zod akceptuje TYLKO format UTC z `Z` (np. `2026-06-15T08:00:00Z` lub z ms), **odrzuca offsety typu `+02:00`**.
- INSERT do `thread_checkups` (`threadId` z param, `dueAt = new Date(dueAt)`, `note`). Brak walidacji, że wątek istnieje (FK w DB rzuci → 500).
- **200** (nie 201!): pojedynczy CheckupRow bez koperty (świeży: `doneAt: null`).

### PATCH `/api/circle-dm/threads/:id/checkups/:checkupId/done`

- Param: `id` i `checkupId` (oba walidowane, ale `id` NIE jest używane do sprawdzenia własności - update idzie tylko po `checkupId`).
- `UPDATE thread_checkups SET done_at = now() WHERE id = :checkupId`. Bez 404.
- **200**: `{"ok": true}`.

### DELETE `/api/circle-dm/threads/:id/checkups/:checkupId`

- Jak wyżej: delete tylko po `checkupId`, `id` ignorowane. Bez 404.
- **200**: `{"ok": true}`.

### GET `/api/circle-dm/threads/:id/messages`

- Param `id`. Query: `refetch` - czytany RĘCZNIE (`c.req.query('refetch') === '1'`), bez zod; każda inna wartość = false.
- Kroki:
  1. SELECT wątku (tylko `id`, `messagesFetchedAt`). Brak → **404** `{"error":"thread not found"}`.
  2. Jeśli `refetch === true` LUB `messagesFetchedAt` jest null → `await syncMessagesForThread(id)` (pełne side-effecty z sekcji 2.2, w tym WS `messages:loaded` i auto-revival). Wyjątek → **502** `{"error":"Circle fetch failed: <message>"}`.
  3. SELECT wszystkich wiadomości wątku `ORDER BY created_at ASC` (bez limitu/paginacji).
  4. SELECT opisów obrazków (`message_image_descriptions`) dla wszystkich id wiadomości jednym `IN`.
- **200**:

```json
{
  "messages": [
    {
      "id": 10,
      "threadId": 5,
      "circleMessageId": 88123,
      "body": "tekst plain",
      "senderId": 42,
      "senderName": "Jan",
      "senderIsMe": false,
      "parentMessageId": null,
      "chatThreadId": null,
      "createdAt": "2026-06-01T10:00:00.000Z",
      "editedAt": null,
      "attachments": [
        {
          "kind": "image",
          "url": "https://...",
          "thumbnailUrl": "https://...",
          "fullUrl": "https://...",
          "filename": "foto.png",
          "contentType": "image/png",
          "byteSize": 12345,
          "width": 800,
          "height": 600,
          "voiceMessage": false
        }
      ],
      "voiceTranscript": null,
      "voiceTranscriptStatus": null,
      "voiceTranscriptError": null,
      "voiceDurationSec": null,
      "imageDescriptions": [
        {"id": 3, "attachmentIndex": 0, "description": "...", "status": "done", "error": null}
      ]
    }
  ],
  "hasPrevious": false,
  "hasNext": false
}
```

- `hasPrevious`/`hasNext` są **zawsze hardcoded `false`** (paginacja niezaimplementowana, ale pola muszą być).
- `voiceTranscriptStatus` i `imageDescriptions[].status`: `'pending' | 'done' | 'error'`; `voiceTranscriptStatus` może być też `null` (wiadomość bez głosówki).
- `circleMessageId` może być **ujemny** (syntetyczny placeholder lokalnie wysłanej wiadomości, zanim Circle ją zwróci).
- `imageDescriptions` sortowane rosnąco po `attachmentIndex`.
- `attachments` NIE są w DB jako kolumna - liczone w locie z `richTextBody` (jsonb) przez `extractAttachments` (sekcja 3.1).

### 3.1 `extractAttachments(richTextBody)` - normalizacja załączników

Wejście: jsonb `rich_text_body` z Circle. Jeśli null/nie-obiekt → `[]`. Zbiera elementy z `richTextBody.attachments` oraz `richTextBody.inline_attachments` (w tej kolejności, konkatenacja). Każdy surowy element (snake_case z Circle: `url`, `filename`, `content_type`, `byte_size`, `metadata{width,height,voice_message}`, `image_variants{thumbnail,small,medium,large,original}`) normalizowany:

- brak/nie-string `url` → element pomijany;
- `contentType` default `"application/octet-stream"`, `filename` default `"plik"`, `byteSize`/`width`/`height` → `null` gdy nie-number;
- `voiceMessage = metadata.voice_message === true`;
- `kind`: `voiceMessage → 'audio'`; inaczej prefiks `content_type`: `image/ → 'image'`, `video/ → 'video'`, `audio/ → 'audio'`, reszta `'file'`;
- dla `kind='image'`: `thumbnailUrl = image_variants.medium ?? small ?? thumbnail ?? url`, `fullUrl = image_variants.original ?? url`; dla pozostałych kindów oba `null` (wariant brany tylko gdy niepusty string).

---

## 4. Messages: `/api/circle-dm/messages`

Plik `messages.ts` (nieudokumentowany w PROJECT.md). Dwa endpointy retry dla workerów asynchronicznych (transkrypcja głosówek Whisperem i opisy obrazków). Workery (poza tym spec'em) co tick zbierają rekordy `status='pending'` z `attempts < limit` i po wyniku broadcastują WS `message:transcript_ready` / `message:image_description_ready`.

### POST `/api/circle-dm/messages/:id/transcribe-retry`

- Param: `id` (coerce int>0) = id wiadomości w `dm_messages`.
- Kroki:
  1. SELECT wiadomości (id, `voiceTranscriptStatus`). Brak → **404** `{"error":"message not found"}`.
  2. `voiceTranscriptStatus === null` (wiadomość nie ma głosówki) → **400** `{"error":"message has no voice attachment"}`.
  3. `retryTranscript(id)`: `UPDATE dm_messages SET voice_transcript_status='pending', voice_transcript_error=NULL, voice_transcript_attempts=0 WHERE id=:id`. Reset attempts daje workerowi pełny budżet 3 prób od nowa. Działa niezależnie od aktualnego statusu (`pending`/`done`/`error` - każdy można zresetować).
- **200**: `{"ok": true}`. Sam endpoint nie transkrybuje - tylko kolejkuje; worker podniesie rekord w swoim ticku.

### POST `/api/circle-dm/messages/:id/image-descriptions/:descId/retry`

- Param: `id` (id wiadomości) i `descId` (id w `message_image_descriptions`), oba coerce int>0.
- Kroki:
  1. SELECT z `message_image_descriptions` WHERE `id = :descId AND message_id = :id` (tu, w odróżnieniu od check-upów, własność JEST sprawdzana). Brak → **404** `{"error":"image description not found"}`.
  2. `retryImageDescription(descId)`: `UPDATE message_image_descriptions SET status='pending', error=NULL, attempts=0 WHERE id=:descId`.
- **200**: `{"ok": true}`.

---

## 5. Members: `/api/circle-dm/members`

Tabela: `community_members` (lokalny cache członków społeczności Circle per konto admina).

### GET `/api/circle-dm/members`

Query (`listQuery`):

| pole | typ | walidacja / default |
|---|---|---|
| `adminAccountId` | coerce int > 0 | wymagane |
| `q` | string | opcjonalne (fraza szukania) |
| `limit` | coerce int | min 1, max 500, default 200 |
| `excludeWithThread` | string | opcjonalne; działa TYLKO wartość dokładnie `'1'` |

Kroki:

1. **Side-effect**: `ensureMembersCached(adminAccountId)` - jeśli cache dla konta jest PUSTY (count == 0), wykonuje pełny `syncMembersForAccount` synchronicznie (pierwsze wywołanie może trwać; brak TTL - niepusty cache nigdy nie odświeża się sam, tylko przez POST /sync).
2. WHERE: zawsze `admin_account_id = :acc`; przy `excludeWithThread === '1'` dodatkowo:

```sql
NOT EXISTS (
  SELECT 1 FROM dm_threads
  WHERE dm_threads.admin_account_id = :acc
    AND dm_threads.other_participant_id = community_members.circle_community_member_id
)
```

3. Przy niepustym `q` (po `trim()`): `ILIKE '%<q.trim()>%'` na `name` OR `email` OR `headline`. (Wartość q nie jest escapowana pod `%`/`_` - znaki wildcard od usera działają jak wildcard.)
4. `ORDER BY LOWER(name) ASC`, `LIMIT :limit`.

**200**:

```json
{
  "members": [
    {
      "id": 1,
      "adminAccountId": 1,
      "circleCommunityMemberId": 42,
      "name": "Jan Kowalski",
      "email": "jan@x.pl",
      "avatarUrl": null,
      "headline": null,
      "bio": null,
      "location": null,
      "lastSeenText": null,
      "status": null,
      "isAdmin": false,
      "canSendMessage": true,
      "fetchedAt": "2026-06-10T09:00:00.000Z"
    }
  ],
  "count": 1
}
```

Nullable: `email`, `avatarUrl`, `headline`, `bio`, `location`, `lastSeenText`, `status`. `count` = długość listy.

### GET `/api/circle-dm/members/:id`

- Param `id` = lokalny id wiersza (NIE circleCommunityMemberId). Brak → **404** `{"error":"member not found"}`.
- **200**: pojedynczy obiekt jak wyżej, bez koperty. (Bez `ensureMembersCached`.)

### POST `/api/circle-dm/members/sync`

- Body: `{"adminAccountId": <int>}` - tu akurat `z.coerce.number().int().positive()`, więc string `"1"` też przejdzie.
- `syncMembersForAccount`: pętla po stronach Circle `GET /api/headless/v1/community_members?page=N&per_page=100` (page 1-indexed), max **30 stron** (cap 3000 członków), przerwanie gdy `has_next_page == false`. 401 z Circle → invalidate JWT + rethrow (efektywnie 500 z globalnego handlera: `{"error": "<msg>"}`).
- **Pomija własne konto** (`community_member_id === jwt.communityMemberId`).
- Upsert po `(adminAccountId, circleCommunityMemberId)`, mapowanie pól Circle (snake_case) → DB: `name`, `email`, `avatar_url→avatarUrl`, `headline`, `bio`, `location`, `last_seen_text→lastSeenText`, `status`, `isAdmin = roles?.admin ?? false`, `canSendMessage = true` (hardcoded), `rawPayload` = cały rekord, `fetchedAt = now()`.
- `syncedCount` = liczba PRZETWORZONYCH członków (każdy upsert z `.returning` zwraca wiersz, więc to total, nie "nowi").
- **200**: `{"ok": true, "syncedCount": <int>}`.

---

## 6. Settings: `/api/circle-dm/settings`

Tabela: `app_settings`, singleton wiersz `id = 1`, tworzony lazy przez upsert. Serwer trzyma in-process cache snapshotu z TTL **30 000 ms**; każdy setter inwalidiuje cache (ustawia `cached = null`).

### GET `/api/circle-dm/settings`

- **200**, body = snapshot ustawień. UWAGA: obiekt cache zawiera też techniczne pole `cachedAt` (epoch ms, number) i ono **wycieka do odpowiedzi JSON** (TypeScript nie wycina pól w runtime):

```json
{
  "globalMetaPrompt": "",
  "formatPrompt": "",
  "draftModel": null,
  "formatModel": null,
  "noReplyThresholdDays": 3,
  "silenceThresholdDays": 14,
  "cachedAt": 1760000000000
}
```

- Defaulty przy braku wiersza w DB: `globalMetaPrompt: ""`, `formatPrompt: ""`, `draftModel: null`, `formatModel: null`, `noReplyThresholdDays: 3`, `silenceThresholdDays: 14`.

### PUT `/api/circle-dm/settings`

Body (verbatim):

```ts
z.object({
  globalMetaPrompt: z.string().max(10_000).optional(),
  formatPrompt: z.string().max(20_000).optional(),
  draftModel: z.string().max(120).nullable().optional(),
  formatModel: z.string().max(120).nullable().optional(),
  noReplyThresholdDays: z.number().int().min(1).max(90).optional(),
  silenceThresholdDays: z.number().int().min(1).max(365).optional(),
}).refine((v) => Object.values(v).some((x) => x !== undefined), {
  message: 'at least one field required',
})
```

- Puste body `{}` → 400 (refine `at least one field required`, w domyślnym kształcie błędu zod).
- Każde przekazane pole zapisywane OSOBNYM upsertem (`INSERT ... id=1 ON CONFLICT (id) DO UPDATE SET <pole>, updated_at=now()`), sekwencyjnie, w kolejności: globalMetaPrompt, formatPrompt, draftModel, formatModel, noReplyThresholdDays, silenceThresholdDays. Nie-atomowe.
- Normalizacja: `draftModel`/`formatModel` przekazane jako `null` LUB `""` → zapis `NULL` (pusty string nadpisywałby fallback z env). Dla `globalMetaPrompt`/`formatPrompt`: `value ?? ''`.
- Po każdym setterze inwalidacja 30-sekundowego cache.
- **200**: `{"ok": true}`.

---

## 7. Feedback: `/api/feedback` (core, cross-tool)

Tabele: `feedback_items` + LEFT JOIN `auth_accounts` (po `auth_account_id`) dla maila autora. Wspólny backlog pomysłów obu adminów - brak multi-tenancy, każdy zalogowany widzi i edytuje wszystko.

Kształt `FeedbackItem` (serializacja):

```json
{
  "id": 1,
  "authAccountId": 0,
  "authorEmail": "dev@local",
  "scope": "general",
  "body": "treść",
  "status": "open",
  "doneAt": null,
  "createdAt": "2026-06-10T09:00:00.000Z"
}
```

- `status`: `'open' | 'done'`. `authorEmail` może być `null` (brak wiersza w auth_accounts). `authAccountId` może być `0` (dev).

### GET `/api/feedback`

- **200**: `{"items": [<FeedbackItem>...]}`.
- Sortowanie: najpierw otwarte, potem done, w obrębie grupy najnowsze pierwsze. Verbatim ORDER BY:

```sql
CASE feedback_items.status WHEN 'open' THEN 0 ELSE 1 END,
created_at DESC
```

### GET `/api/feedback/count`

- Lekki endpoint pollowany przez badge w UI.
- **200**: `{"openCount": <int>}` = `count(*)` z `status='open'` (0 gdy brak).

### POST `/api/feedback`

Body (`createFeedbackSchema`):

```ts
z.object({
  body: z.string().min(1).max(4000),
  scope: z.string().min(1).max(40).default('general'),
})
```

- `scope` opcjonalne z defaultem `'general'`.
- Autor = `auth` z kontekstu (`authAccountId`, `email`).
- **Side-effect dev-only** (`ensureDevAuthAccount`): gdy `NODE_ENV !== 'production'` i `authAccountId === 0`, najpierw raw SQL (verbatim):

```sql
INSERT INTO auth_accounts (id, email, password_hash)
VALUES (0, 'dev@local', '')
ON CONFLICT (id) DO NOTHING
```

(inaczej FK na feedback_items by się wywalił przy fake-userze dev).
- INSERT `{authAccountId, body, scope}` (status default `'open'` z DB). Log info: `feedback <id> from <email> (<scope>)`.
- **201**: `{"item": <FeedbackItem>}` z `authorEmail` = email zalogowanego (nie z joina).

### PATCH `/api/feedback/:id/status`

- Param `id` (coerce int>0). Body: `{"status": "open" | "done"}`.
- UPDATE: `status = :status`, `doneAt = (status === 'done' ? now() : NULL)`, `updatedAt = now()`. Bez sprawdzenia istnienia.
- **200**: `{"ok": true}`.

### DELETE `/api/feedback/:id`

- Twardy DELETE, bez 404.
- **200**: `{"ok": true}`.

---

## 8. Uwagi dla portu na FastAPI

1. **Casing pól**: cały kontrakt JSON jest camelCase (`adminAccountId`, `isFlagged`, `lastMessageAt`...). Pydantic domyślnie wypluwa snake_case z nazw pól - trzeba ustawić `alias_generator=to_camel` + `populate_by_name=True` (i serializować `by_alias=True`), inaczej frontend React się rozjedzie. Wyjątek: payloady DO/Z Circle API są snake_case - nie mieszać tych dwóch światów.
2. **Kody błędów walidacji**: Hono+zValidator zwraca **400** z kształtem `{"success":false,"error":{"issues":[...],"name":"ZodError"}}`. FastAPI domyślnie daje **422** z `{"detail":[...]}`. Frontend dziś raczej sprawdza tylko status !== 2xx, ale dla 1:1 dodaj exception handler mapujący `RequestValidationError` → 400. Pamiętaj: nienumeryczne `:id` w ścieżce = 400 (nie 404).
3. **Niespójne envelope'y**: GET listy są opakowane (`{accounts:[]}`, `{threads:[],count}`, `{members:[],count}`, `{checkups:[]}`, `{items:[]}`), ale GET pojedynczego zasobu (`/threads/:id`, `/members/:id`) i POST checkupa zwracają **goły obiekt bez koperty**. POST feedback zwraca `{item: ...}`. Nie ujednolicać - frontend zostaje.
4. **Statusy sukcesu**: 201 tylko dla POST `/accounts` (`{id}`) i POST `/feedback` (`{item}`). POST checkupa i wszystkie inne POST/PATCH/PUT/DELETE → 200.
5. **Brak 404 na mutacjach**: PATCH/DELETE account, PATCH status/flag wątku, done/delete checkupa, PATCH/DELETE feedback wykonują UPDATE/DELETE bez sprawdzenia istnienia i zawsze zwracają `{"ok":true}`. Nie dodawaj 404 "bo tak ładniej".
6. **Sprawdzanie własności**: checkup done/delete ignoruje `:id` wątku (operuje tylko po `checkupId`); za to image-description retry WYMAGA zgodności `message_id`. Odtworzyć dokładnie.
7. **GET /settings wycieka `cachedAt`** (epoch ms) - artefakt cache. Frontend tego nie używa, ale dla 1:1 albo dodaj pole, albo świadomie utnij (i zanotuj różnicę).
8. **Cache ustawień 30 s in-process**: w FastAPI z wieloma workerami (uvicorn/gunicorn) każdy proces ma własny cache, a PUT inwalidiuje tylko swój. Oryginał to jeden proces Node - bezpiecznie odtworzysz tylko przy 1 workerze albo cache w Redis/zerowym TTL.
9. **Fire-and-forget**: POST `/accounts` (pierwszy sync skrzynki) i POST `/accounts/:id/sync` (per-thread message sync dla `staleMessageThreadIds`) odpalają taski w tle, których błędy są tylko logowane. W FastAPI: `asyncio.create_task` lub `BackgroundTasks` - odpowiedź NIE czeka.
10. **502 vs 500**: GET `/threads/:id/messages` przy padzie Circle zwraca **502** z prefiksem komunikatu dokładnie `Circle fetch failed: `. POST `/accounts/:id/sync` przy padzie zwraca **500** z `{"ok":false,"error":...}` (inny kształt niż globalny `{"error":...}`!). Test-connection przy padzie zwraca **400** z `{"ok":false,"communityId":null,"communityMemberId":null,"error":...}`.
11. **`z.string().datetime()` dla `dueAt`** akceptuje tylko ISO UTC z `Z` (bez offsetów `+02:00`). Pydantic `datetime` jest dużo luźniejszy - dla 1:1 waliduj regexem/własnym typem albo zaakceptuj poszerzenie kontraktu świadomie.
12. **Coerce niekonsekwentne**: `bulk-action` body wymaga prawdziwych JSON-owych liczb (bez coerce), ale `members/sync` body przyjmie też `"1"` (coerce). Query params zawsze coerce. Flagi `refetch` i `excludeWithThread` to porównanie stringa z `'1'`, nie bool.
13. **Sort `next_checkup` po LIMIT**: DB sortuje jak `recent` (pinned DESC, lastMessageAt DESC), tnie do `limit`, dopiero potem sort w pamięci po najbliższym checkupie (null = na końcu). Przy >limit wątków wynik różni się od "posortuj wszystko po checkupie" - to celowe zachowanie oryginału, odtworzyć.
14. **`count` w listach** = długość zwróconej strony, nie total. `hasPrevious`/`hasNext` w messages zawsze `false`.
15. **WS eventy**: jedyny broadcast wywoływany pośrednio z tych route'ów to `{"type":"messages:loaded","threadId":N,"count":N}` (z `syncMessagesForThread`, gdy wstawiono nowe wiadomości - czyli przy GET `/threads/:id/messages` z refetch/pierwszym fetchu i przy manualnym account sync). Pełna unia eventów w `packages/shared/src/schemas/ws-events.ts` (pola eventów też camelCase).
16. **Auto-revival i side-effecty syncu**: GET wiadomości potrafi ZMIENIĆ status wątku done→inbox (gdy przyszło coś nowego od drugiej strony), skasować syntetyczne placeholdery (`circleMessageId < 0`) i zakolejkować transkrypcje/opisy obrazków. GET nie jest "czysty" - nie czyść tego przy porcie.
17. **`ensureMembersCached`** synchronizuje TYLKO przy pustym cache (count==0) i robi to synchronicznie w request GET `/members` - pierwszy request po wdrożeniu może trwać długo (do 30 stron po 100 członków z Circle). Brak TTL: odświeżenie tylko ręcznym POST `/members/sync`.
18. **`syncedCount`** z POST `/members/sync` to liczba przetworzonych (upsertowanych) członków łącznie, nie nowych. Nazwa myli - zachowanie zostawić.
19. **ILIKE bez escapowania**: `q` w members trafia do `%...%` bez escapowania `%`/`_`. Dla 1:1 nie escapować (user-wildcardy działają), w SQLAlchemy `ilike(f"%{q}%")` bez `escape=`.
20. **Auth kontekst**: feedback POST używa `auth.authAccountId`/`auth.email`; dev bypass (`NODE_ENV != production`) = `{authAccountId: 0, email: 'dev@local'}` + lazy INSERT wiersza `auth_accounts(id=0)`. 401 zawsze `{"error":"Unauthorized"}`.
21. **Sekrety**: GET `/accounts` nie zwraca `circleAdminToken` ani tokenów JWT - tylko `hasToken`. Pilnować przy modelach Pydantic (nie zwracać ORM-a wprost).
