# Frontend contract + przenosiny `apps/web` i `packages/shared`

Spec na podstawie pełnej lektury źródeł w `/Users/tomasz/repos/befreeclub/admin`:

- `apps/web/src/tools/circle-dm/lib/api.ts` (klient REST Circle DM)
- `apps/web/src/core/lib/auth-api.ts`, `feedback-api.ts`, `ws.ts`
- `apps/web/src/core/hooks/useAuth.ts`, `apps/web/src/tools/circle-dm/lib/{account-context,bulk-queue}.ts`
- `apps/web/{vite.config.ts, package.json, tailwind.config.ts, tsconfig.json, tsconfig.app.json, tsconfig.node.json, index.html, postcss.config.js}`
- `packages/shared/src/**` (wszystkie pliki: index.ts, voice.ts, schemas/{admin-account,api,assistant,compose,draft,feedback,kb,member,message,thread,ws-events}.ts)
- root: `package.json`, `pnpm-workspace.yaml`, `turbo.json`, `tsconfig.base.json`, `packages/shared/{package.json,tsconfig.json}`, fragmenty `Dockerfile` i `apps/server/src/index.ts` (serwowanie SPA)

Cel: backend przepisujemy 1:1 na FastAPI, frontend React zostaje bez zmian funkcjonalnych. Część 1 mówi, co backend MUSI dostarczyć, żeby frontend działał bez modyfikacji. Część 2 mówi, co trzeba zmienić przy kopiowaniu frontendu do nowego monorepo.

---

# CZĘŚĆ 1: KONTRAKT (czego frontend oczekuje od backendu)

## 1.1 Base URL-e i ścieżki

Frontend używa WYŁĄCZNIE ścieżek względnych (same-origin). Żadnych env-owych base URL-i, żadnego CORS w prod (jeden host, jeden port):

| Klient | Stała `BASE` | Plik |
|---|---|---|
| Circle DM API | `/api/circle-dm` | `tools/circle-dm/lib/api.ts` |
| Auth API | `/api/auth` | `core/lib/auth-api.ts` |
| Feedback API | `/api/feedback` | `core/lib/feedback-api.ts` |
| WebSocket | `/ws` (ten sam host, `ws:`/`wss:` wg `window.location.protocol`) | `core/lib/ws.ts` |
| Health | `/health`, `/health/claude` (przez proxy w dev, bezpośrednio w prod) | - |

Dodatkowo backend w prod serwuje SPA (sekcja 1.9).

## 1.2 Wspólny wzorzec klienta HTTP (3 kopie tej samej funkcji `http<T>`)

Wszystkie trzy klienty (`api.ts`, `auth-api.ts`, `feedback-api.ts`) robią:

```
fetch(`${BASE}${path}`, {
  method,
  credentials: 'same-origin',
  headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
  body: body !== undefined ? JSON.stringify(body) : undefined,
})
```

Po stronie odpowiedzi:

1. `const text = await res.text()` - zawsze czyta body jako tekst.
2. Jeśli `!res.ok` (status spoza 2xx):
   - **`api.ts` (circle-dm) i `feedback-api.ts`**: jeśli `res.status === 401` → `window.location.reload()` (twardy reload całej strony; auth-gate w `App.tsx` przeładuje `/api/auth/me` i pokaże LoginPage). Reload NIE przerywa dalszego wykonania - kod i tak rzuci błąd niżej.
   - **`auth-api.ts`**: 401 NIE robi reloadu (LoginPage musi pokazać komunikat błędnego hasła).
   - Budowa komunikatu błędu (identyczna we wszystkich trzech):
     - domyślnie `message = "${res.status} ${res.statusText}"` (np. `"422 Unprocessable Entity"`),
     - próba `JSON.parse(text)`; jeśli sparsowany obiekt ma pole `error` typu string → `message = parsed.error`,
     - jeśli parse się wywali a `text` niepusty → `message = text` (surowy tekst body).
   - Rzucany wyjątek: `api.ts` → `ApiError(status, message)` (`name='ApiError'`, ma publiczne pole `status`), `auth-api.ts` → `AuthError(status, message)`, `feedback-api.ts` → zwykły `Error(message)`.
3. Jeśli `res.ok`: `text ? JSON.parse(text) : undefined` - pusta odpowiedź 2xx jest legalna (zwraca `undefined`), niepusta MUSI być poprawnym JSON-em (inaczej wyjątek z `JSON.parse`).

**Wniosek dla backendu (twardy kontrakt):**
- Format błędu: `{"error": "<string>"}` (opcjonalnie `"details"` - patrz `apiErrorSchema`, ale frontend czyta tylko `error`).
- 401 zwracać WYŁĄCZNIE przy realnym braku/wygaśnięciu sesji. Każde inne użycie 401 = pętla reloadów strony.
- Status sukcesu może być dowolny 2xx (frontend nie sprawdza konkretnego kodu; np. `POST /assistant/turn` wg PROJECT.md zwraca 202 i to działa, bo body jest czytane tak samo).
- Komunikaty błędów z pola `error` lądują 1:1 w toastach UI - mają być po ludzku (po polsku tam, gdzie user je widzi).

## 1.3 Auth (`/api/auth`)

| Metoda i ścieżka | Request body | Response (dokładne pola) |
|---|---|---|
| `GET /api/auth/me` | - | `{ "authenticated": boolean, "email"?: string }` |
| `POST /api/auth/login` | `{ "email": string, "password": string }` | `{ "ok": true, "email": string }` |
| `POST /api/auth/logout` | - (bez body, bez Content-Type) | `{ "ok": true }` |

Sesja: cookie HttpOnly (w obecnym backendzie: Secure + SameSite=Lax, 30 dni, sliding window). Frontend nie dotyka cookie - liczy tylko na `credentials: 'same-origin'`.

Konsumpcja (React Query, `core/hooks/useAuth.ts`):
- `useAuth()` - query key `['auth','me']`, `staleTime: 60_000` ms, `retry: false`. `App.tsx` na `authenticated: false` renderuje LoginPage.
- `useLogin()` - po sukcesie invaliduje `['auth','me']`.
- `useLogout()` - po sukcesie `queryClient.clear()` + invalidate.
- `/api/auth/me` MUSI zwracać 200 z `{authenticated:false}` dla niezalogowanego (nie 401!), inaczej query poleci w error path.

Middleware auth na backendzie: wymagany na całym `/api/*` POZA `/api/auth/*` i `/health*`. W dev (NODE_ENV != production) obecny backend bypassuje auth (user `dev@local`).

## 1.4 Feedback (`/api/feedback`)

| Metoda i ścieżka | Request body | Response |
|---|---|---|
| `GET /api/feedback` | - | `{ "items": FeedbackItem[] }` |
| `GET /api/feedback/count` | - | `{ "openCount": number }` |
| `POST /api/feedback` | `{ "body": string, "scope": string }` | `{ "item": FeedbackItem }` |
| `PATCH /api/feedback/:id/status` | `{ "status": "open" \| "done" }` | `{ "ok": true }` |
| `DELETE /api/feedback/:id` | - | `{ "ok": true }` |

Walidacja wejścia (serwer, `createFeedbackSchema`): `body` min 1 max 4000, `scope` min 1 max 40, default `"general"`.

## 1.5 Circle DM (`/api/circle-dm`) - pełny inwentarz wywołań frontendowych

Wszystkie body request/response w **camelCase**. Typy DTO w sekcji 1.7.

### Accounts
| Wywołanie | Request | Response (typ oczekiwany przez frontend) |
|---|---|---|
| `GET /accounts` | - | `{ "accounts": AdminAccount[] }` |
| `POST /accounts` | `CreateAdminAccount` | `{ "id": number }` |
| `PATCH /accounts/:id` | `UpdateAdminAccount` | `{ "ok": true }` |
| `DELETE /accounts/:id` | - | `{ "ok": true }` |
| `POST /accounts/:id/test-connection` | - (bez body) | `TestConnectionResult` |
| `POST /accounts/:id/sync` | - (bez body) | `{ "ok": boolean, "changedThreadIds": number[], "newUnreadThreadIds": number[] }` |

### Threads
| Wywołanie | Request | Response |
|---|---|---|
| `GET /threads?adminAccountId=&filter=&sort=&limit=` | query: `adminAccountId` (wymagane, liczba jako string), `filter` (enum ThreadFilter, opcjonalny), `sort` (enum ThreadSort, opcjonalny), `limit` (liczba, opcjonalny; frontend pomija gdy falsy) | `{ "threads": DmThread[], "count": number }` |
| `GET /threads/:id` | - | `DmThread` (goły obiekt, bez koperty) |
| `GET /threads/:id/messages` lub `GET /threads/:id/messages?refetch=1` | query `refetch=1` wymusza refetch z Circle | `{ "messages": DmMessage[] }` - serwer zwraca też `hasPrevious:false, hasNext:false`, ale **frontend ich nie czyta** |
| `PATCH /threads/:id/status` | `{ "status": "inbox" \| "done" }` | `{ "ok": true }` |
| `PATCH /threads/:id/flag` | `{ "isFlagged": boolean }` | `{ "ok": true }` |
| `POST /threads/bulk-action` | `{ "adminAccountId": number, "ids": number[], "action": "done" \| "inbox" \| "flag" \| "unflag" }` | `{ "ok": true, "count": number }` |
| `GET /threads/:id/checkups` | - | `{ "checkups": ThreadCheckup[] }` |
| `POST /threads/:id/checkups` | `{ "dueAt": string, "note"?: string \| null }` (dueAt = ISO datetime) | `ThreadCheckup` (goły obiekt) |
| `PATCH /threads/:id/checkups/:checkupId/done` | - | `{ "ok": true }` |
| `DELETE /threads/:id/checkups/:checkupId` | - | `{ "ok": true }` |

### Messages (retry przetwarzania załączników)
| Wywołanie | Request | Response |
|---|---|---|
| `POST /messages/:messageId/transcribe-retry` | - | `{ "ok": true }` |
| `POST /messages/:messageId/image-descriptions/:descId/retry` | - | `{ "ok": true }` |

### Drafts
| Wywołanie | Request | Response |
|---|---|---|
| `GET /drafts/:threadId` | - | `{ "session": DraftSession \| null, "iterations": DraftIteration[] }` |
| `POST /drafts/:threadId/generate` | - (bez body) | `{ "ok": true }` (generowanie async, wynik streamowany po WS) |
| `PATCH /drafts/:threadId` | `{ "draft": string }` | `{ "ok": true }` |
| `DELETE /drafts/:threadId` | - | `{ "ok": true }` |
| `POST /drafts/:threadId/send` | `{ "body": string }` | `{ "ok": boolean, "circleMessageId": number \| null, "error"?: string }` |

### Members
| Wywołanie | Request | Response |
|---|---|---|
| `GET /members?adminAccountId=&q=&limit=&excludeWithThread=1` | query: `adminAccountId` wymagane; `q`, `limit` opcjonalne; `excludeWithThread` wysyłane jako literalnie `"1"` gdy true | `{ "members": CommunityMember[], "count": number }` |
| `GET /members/:id` | - | `CommunityMember` (goły obiekt) |
| `POST /members/sync` | `{ "adminAccountId": number }` | `{ "ok": true, "syncedCount": number }` |

### Compose
| Wywołanie | Request | Response |
|---|---|---|
| `POST /compose/generate` | `{ "adminAccountId": number, "circleCommunityMemberId": number }` | `{ "draft": string }` (`ComposeDraftResult`) |
| `POST /compose/send` | `{ "adminAccountId": number, "circleCommunityMemberId": number, "body": string }` | union: `{ "ok": true, "threadId": number, "circleChatRoomUuid": string }` LUB `{ "ok": false, "error": string }` (`ComposeSendResult`) |

### Format (poprawa tekstu AI)
| Wywołanie | Request | Response |
|---|---|---|
| `POST /format/thread` | `{ "threadId": number, "text": string }` | `{ "text": string }` |
| `POST /format/compose` | `{ "adminAccountId": number, "circleCommunityMemberId": number, "text": string }` | `{ "text": string }` |
| `POST /format/bulk` | `{ "adminAccountId": number, "text": string }` | `{ "text": string }` |

### Bulk send
`POST /bulk/send`, request:
```json
{
  "items": [
    { "kind": "thread", "threadId": 123 },
    { "kind": "member", "adminAccountId": 1, "memberId": 456 }
  ],
  "body": "treść wiadomości"
}
```
Response:
```json
{
  "totalCount": 2,
  "okCount": 1,
  "results": [
    { "kind": "thread", "threadId": 123, "memberId": null, "ok": true, "circleMessageId": 999, "error": undefined },
    { "kind": "member", "threadId": null, "memberId": 456, "ok": false, "circleMessageId": null, "error": "..." }
  ]
}
```
(`error` opcjonalne; `kind` to `"thread"` lub `"member"`.)

### Settings
| Wywołanie | Request | Response |
|---|---|---|
| `GET /settings` | - | `{ "globalMetaPrompt": string, "formatPrompt": string, "draftModel": string \| null, "formatModel": string \| null, "noReplyThresholdDays": number, "silenceThresholdDays": number }` |
| `PUT /settings` | dowolny podzbiór tych samych pól (partial patch) | `{ "ok": true }` |

Uwaga: ten kształt jest zdefiniowany inline w `api.ts`, NIE w `packages/shared`.

### Knowledge Base (kb)
| Wywołanie | Request | Response |
|---|---|---|
| `GET /kb?scope=&accountId=` | query: `scope` = `"global"`\|`"account"` (wymagane), `accountId` (opcjonalne, tylko gdy truthy) | `KbListResponse` = `{ "documents": KbDocument[], "capacity": KbCapacity }` |
| `GET /kb/:id` | - | `KbDocumentDetail` (KbDocument + `bodyText`) |
| `POST /kb` | `{ "scope": KbScope, "adminAccountId"?: number \| null, "title": string, "bodyText": string }` | `{ "id": number }` |
| `POST /kb/upload` | **multipart/form-data**: pole `file` (File), `scope` (string), opcjonalnie `title` (string), opcjonalnie `adminAccountId` (string z liczbą). Bez ręcznego Content-Type (przeglądarka ustawia boundary). `credentials:'same-origin'`. Obsługa błędów i 401→reload identyczna jak w `http()` (zduplikowana ręcznie w `api.ts`) | `{ "id": number, "tokenEstimate": number }` |
| `PATCH /kb/:id` | `{ "title"?: string, "bodyText"?: string, "enabled"?: boolean }` (min 1 pole - serwer waliduje `updateKbSchema`) | `{ "ok": true }` |
| `DELETE /kb/:id` | - | `{ "ok": true }` |
| `GET /kb/:id/original` | nawigacja przeglądarki (link `href` budowany przez `api.kb.originalUrl(id)` = `/api/circle-dm/kb/${id}/original`) | oryginalny plik (download); auth przez cookie, NIE przez fetch klienta |

### Assistant
| Wywołanie | Request | Response |
|---|---|---|
| `GET /assistant/conversations` | - | `{ "conversations": AssistantConversation[] }` |
| `GET /assistant/conversation` lub `GET /assistant/conversation?id=<n>` | bez `id` = bieżąca/lazy-create | `AssistantConversationFull` = `{ "conversation": AssistantConversation, "messages": AssistantMessage[] }` |
| `POST /assistant/new` | - (bez body) | `AssistantConversationFull` |
| `DELETE /assistant/conversation/:id` | - | `{ "ok": boolean }` |
| `POST /assistant/turn` | `{ "conversationId": number, "message": string, "context": AssistantContext }` | `{ "ok": true, "userMessageId": number, "assistantMessageId": number, "hasAction": boolean }` (status może być 202; odpowiedź streamowana po WS) |
| `POST /assistant/messages/:id/apply` | - | `{ "ok": boolean, "message": AssistantMessage \| null, "error"?: string }` |
| `POST /assistant/messages/:id/dismiss` | - | `{ "ok": true }` |
| `POST /assistant/cancel` | `{ "conversationId": number }` | `{ "ok": boolean }` |

Uwaga serwerowa: `assistantTurnRequestSchema` w shared waliduje `message` min 1 max 4000 znaków + `context` wg `assistantContextSchema`.

### Martwy kod w api.ts
Na końcu `api.ts` jest eksport `listThreadsUrl(params)` który zwraca `params` bez zmian (identity). Nie generuje żadnego requestu - ignorować przy porcie.

## 1.6 WebSocket `/ws`

### Klient (`core/lib/ws.ts`) - zachowanie
- Singleton `WsClient`, `connect()` wywołane przy imporcie modułu (czyli od startu appki, również na LoginPage).
- URL: `new URL('/ws', window.location.href)` z protokołem `wss:` gdy strona po `https:`, inaczej `ws:`. Brak query params, brak subprotokołów, brak tokenów w URL - **auth WS opiera się na cookie sesyjnym przesyłanym przy upgrade**.
- **Reconnect**: na `close` (o ile nie `dispose()`): `setTimeout(connect, retryDelay)`; `retryDelay` startuje od `1000` ms, po każdej próbie `retryDelay = min(retryDelay * 1.5, 15000)`; reset do `1000` na `open`. Na `error` klient woła `ws.close()` (co odpala ścieżkę reconnect).
- **Brak ping/pong i brak resubskrypcji** - klient niczego nie wysyła do serwera (kanał wyłącznie server→client). Keepalive musi ogarnąć serwer/proxy.
- Parsowanie ramek: `JSON.parse(evt.data)`; jeśli `data?.type === 'hello'` → ignoruj; inaczej `wsEventSchema.safeParse(data)` i tylko przy sukcesie dispatch do handlerów. **Każda ramka niezgodna ze schematem (zły typ, brakujące pole, zły case nazwy pola) jest CICHO odrzucana.**
- Hooki: `useWsEvent(type, handler)` filtruje po `event.type`; `useWsEvents(handler)` dostaje wszystko.

### Serwer - co musi robić
- Akceptować upgrade na ścieżce dokładnie `/ws`.
- Po połączeniu wysłać ramkę powitalną `{"type":"hello"}` (obecny serwer to robi; klient ją ignoruje, ale zachowaj dla parytetu).
- Broadcastować eventy jako pojedyncze obiekty JSON (jedna ramka tekstowa = jeden event), zgodne 1:1 z `wsEventSchema`.

### Pełny katalog ramek (`wsEventSchema`, discriminated union po `type`)

```
{ "type": "threads:updated",  "adminAccountId": int, "changedThreadIds": int[] }
{ "type": "thread:new_messages", "threadId": int, "newCount": int }
{ "type": "messages:loaded",  "threadId": int, "count": int }
{ "type": "message:transcript_ready", "threadId": int, "messageId": int }
{ "type": "message:image_description_ready", "threadId": int, "messageId": int }
{ "type": "draft:status",     "threadId": int, "status": DraftStatus, "error"?: string }
{ "type": "draft:token",      "threadId": int, "chunk": string, "iterationKind": IterationKind }
{ "type": "draft:complete",   "threadId": int, "iterationKind": IterationKind, "draft": string,
                              "tokensUsed": int|null, "costUsd": number|null }
{ "type": "draft:tool_use",   "threadId": int, "toolName": string }
{ "type": "send:result",      "threadId": int, "ok": boolean, "circleMessageId": int|null, "error"?: string }
{ "type": "assistant:token",    "conversationId": int, "chunk": string }
{ "type": "assistant:complete", "conversationId": int, "messageId": int, "hasAction": boolean }
{ "type": "assistant:error",    "conversationId": int, "error": string }
```

`DraftStatus` = `'idle' | 'generating' | 'has_draft' | 'polishing' | 'ready_to_send' | 'sent' | 'error'`.
`IterationKind` = `'initial' | 'user_feedback' | 'polish'`.

### Co frontend realnie subskrybuje (stan na dziś)
- `InboxPage`: `threads:updated` (odświeża listę po sync/pollingu).
- `ThreadPage`: `messages:loaded`, `message:transcript_ready`, `message:image_description_ready`, `send:result` oraz przez `useWsEvents` cały strumień draftowy: `draft:status` (status `'generating'` czyści bufor i włącza streaming UI, `'sent'` czyści textarea), `draft:token` (akumulacja `chunk` do bufora → live podgląd), `draft:complete` (ustawia `event.draft` jako tekst, status `'has_draft'`, invaliduje query `['draft', threadId]`), `draft:tool_use` (no-op). Filtr: eventy z `threadId !== threadId` strony są ignorowane, WYJĄTEK: `send:result` przechodzi zawsze.
- `AssistantPanel`: `assistant:token`, `assistant:complete` (refetch konwersacji), `assistant:error`.
- `thread:new_messages` jest w schemacie, ale obecnie nikt go nie subskrybuje w UI (emitowany przez serwer, zachować dla parytetu).

## 1.7 Pełny inwentarz schematów `packages/shared` (DTO)

Wszystkie pola camelCase. `z.string().datetime()` = ISO 8601; obecny backend serializuje przez `Date.toISOString()`, czyli format `YYYY-MM-DDTHH:mm:ss.sssZ` (UTC z `Z`). **Zod `.datetime()` bez opcji NIE akceptuje offsetów typu `+02:00`** - emituj zawsze UTC z `Z`.

WAŻNE: frontend NIE waliduje odpowiedzi HTTP Zodem (typy tylko compile-time). Zod runtime działa wyłącznie: (a) na ramkach WS u klienta, (b) na request body po stronie obecnego serwera. Czyli `.default(...)` w schematach (np. w `dmMessageSchema`) NIE uzupełni braków w odpowiedziach HTTP - backend musi zwracać wszystkie pola jawnie.

### admin-account.ts
```
AdminAccount {
  id: int>0, label: string(1..120), email: email,
  hasToken: boolean,                       // czy konto ma zapisany circleAdminToken (token NIGDY nie wraca do klienta)
  communityId: int|null, communityMemberId: int|null,
  systemPrompt: string, isActive: boolean,
  lastSyncedAt: datetime|null, createdAt: datetime, updatedAt: datetime
}
CreateAdminAccount { label: string(1..120), email: email, circleAdminToken?: string(min 8), systemPrompt: string(min 10) }
UpdateAdminAccount = partial(CreateAdminAccount) + { isActive?: boolean }
TestConnectionResult { ok: boolean, communityId: int|null, communityMemberId: int|null, error?: string }
```

### api.ts
```
ApiError { error: string, details?: unknown }
```

### thread.ts
```
ChatRoomKind = 'direct' | 'group_chat'
ThreadStatus = 'inbox' | 'done'
ThreadFilter = 'inbox' | 'unread' | 'no_reply' | 'silent' | 'flagged' | 'checkup' | 'done'
ThreadSort   = 'recent' | 'oldest_no_reply' | 'next_checkup'
ThreadCheckup { id: int>0, threadId: int>0, dueAt: datetime, note: string|null, doneAt: datetime|null, createdAt: datetime }
DmThread {
  id: int>0, adminAccountId: int>0,
  circleChatRoomId: int, circleChatRoomUuid: string,
  chatRoomKind: ChatRoomKind, chatRoomName: string|null,
  otherParticipantEmail: string|null, otherParticipantName: string|null,
  otherParticipantId: int|null, otherParticipantAvatarUrl: string|null,
  unreadMessagesCount: int>=0, pinnedAt: datetime|null,
  status: ThreadStatus, isFlagged: boolean,
  nextCheckupDueAt: datetime|null,      // computed: najbliższy pending check-up
  nextCheckupNote: string|null, pendingCheckupCount: int>=0,
  lastMessageAt: datetime|null, lastMessageSenderId: int|null,
  lastMessageSenderIsMe: boolean, lastMessagePreview: string|null,
  fetchedAt: datetime
}
ThreadListResponse { threads: DmThread[], count: int }
```

### message.ts
```
AttachmentKind = 'image' | 'video' | 'audio' | 'file'
DmAttachment {
  kind: AttachmentKind, url: url-string, thumbnailUrl: url|null, fullUrl: url|null,
  filename: string, contentType: string, byteSize: int|null,
  width: int|null, height: int|null, voiceMessage: boolean
}
VoiceTranscriptStatus  = 'pending' | 'done' | 'error'
ImageDescriptionStatus = 'pending' | 'done' | 'error'
DmImageDescription { id: int>0, attachmentIndex: int>=0, description: string|null,
                     status: ImageDescriptionStatus, error: string|null }
DmMessage {
  id: int>0, threadId: int>0, circleMessageId: int,
  body: string, senderId: int|null, senderName: string|null, senderIsMe: boolean,
  parentMessageId: int|null, chatThreadId: int|null,
  createdAt: datetime, editedAt: datetime|null,
  attachments: DmAttachment[]            (Zod default: []),
  voiceTranscript: string|null           (default null),
  voiceTranscriptStatus: VoiceTranscriptStatus|null (default null),
  voiceTranscriptError: string|null      (default null),
  voiceDurationSec: int|null             (default null),
  imageDescriptions: DmImageDescription[] (default [])
}
MessageListResponse { messages: DmMessage[], hasPrevious: boolean, hasNext: boolean }
```
Uwaga: serwer obecnie zwraca `hasPrevious:false, hasNext:false` na sztywno; frontend czyta tylko `messages`.

### draft.ts
```
DraftStatus   = 'idle'|'generating'|'has_draft'|'polishing'|'ready_to_send'|'sent'|'error'
IterationKind = 'initial'|'user_feedback'|'polish'
DraftIteration { id: int>0, draftSessionId: int>0, iterationKind: IterationKind,
                 userInstruction: string|null, draftText: string,
                 tokensUsed: int|null, costUsd: number|null, createdAt: datetime }
DraftSession { id: int>0, threadId: int>0, claudeSessionId: uuid-string, status: DraftStatus,
               currentDraft: string|null, iterationsCount: int>=0, lastError: string|null,
               createdAt: datetime, updatedAt: datetime }
GenerateDraftRequest {}                       // puste body
FeedbackDraftRequest { feedback: string(min 1) }
UpdateDraftRequest { draft: string }
SendDraftRequest { body: string(min 1) }
```

### member.ts
```
CommunityMember {
  id: int>0, adminAccountId: int>0, circleCommunityMemberId: int>0,
  name: string, email: string|null, avatarUrl: string|null,
  headline: string|null, bio: string|null, location: string|null,
  lastSeenText: string|null, status: string|null,
  isAdmin: boolean, canSendMessage: boolean, fetchedAt: datetime
}
MemberListResponse { members: CommunityMember[], count: int }
```

### compose.ts
```
ComposeDraftResult { draft: string }
ComposeSendResult = { ok: true, threadId: int>0, circleChatRoomUuid: string }
                  | { ok: false, error: string }
```

### feedback.ts
```
FeedbackStatus = 'open' | 'done'
FeedbackItem { id: int>0, authAccountId: int>=0, authorEmail: string|null,
               scope: string, body: string, status: FeedbackStatus,
               doneAt: datetime|null, createdAt: datetime }
createFeedbackSchema { body: string(1..4000), scope: string(1..40) default 'general' }
updateFeedbackStatusSchema { status: FeedbackStatus }
```

### kb.ts
```
KbScope = 'global' | 'account'
KbSourceKind = 'pdf' | 'md' | 'manual'
KbDocument {                       // list item, bez bodyText
  id: int>0, scope: KbScope, adminAccountId: int>0|null, title: string,
  sourceKind: KbSourceKind, originalFilename: string|null, hasOriginal: boolean,
  tokenEstimate: int, enabled: boolean, createdAt: datetime, updatedAt: datetime
}
KbDocumentDetail = KbDocument + { bodyText: string }
KbCapacity { globalTokens: int, accountTokens: int, totalTokens: int,
             budget: int, hardCeiling: int, overBudget: boolean }
KbListResponse { documents: KbDocument[], capacity: KbCapacity }
CreateKbManual { scope: KbScope, adminAccountId?: int>0|null, title: string(1..200), bodyText: string(1..500000) }
UpdateKb { title?: string(1..200), bodyText?: string(max 500000), enabled?: boolean }
          + refine: min jedno pole zdefiniowane, message 'at least one field required'
```

### assistant.ts
`AssistantContext` - discriminated union po `kind`, wysyłany przez frontend w `POST /assistant/turn`:
```
{ kind: 'inbox',   adminAccountId: int>0|null, filter: string, sort: string, query: string }
{ kind: 'thread',  adminAccountId: int>0, threadId: int>0, recipientName: string|null,
                   persona: string, accountLabel: string, draftText: string, historyExcerpt: string }
{ kind: 'compose', adminAccountId: int>0, memberId: int>0, memberName: string,
                   persona: string, accountLabel: string, currentText: string, memberProfile: string }
{ kind: 'settings', metaPrompt: string, formatPrompt: string }
{ kind: 'account',  accountId: int>0, label: string, personaText: string }
{ kind: 'none' }
```

`ActionProposal` - discriminated union po `action` (zwracany w `AssistantMessage.actionProposal`, walidowany przy apply):
```
{ action: 'setDraft',            params: { threadId: int>0, newText: string(min 1) }, preview: string }
{ action: 'setPersona',          params: { accountId: int>0, newText: string(min 10) }, preview: string }
{ action: 'setGlobalMetaPrompt', params: { newText: string }, preview: string }
{ action: 'setFormatPrompt',     params: { newText: string }, preview: string }
{ action: 'setKbDoc',            params: { id: int>0, title?: string(1..200), bodyText?: string(max 500000) }, preview: string }
{ action: 'createKbManual',      params: { scope: 'global'|'account', adminAccountId?: int>0|null,
                                           title: string(1..200), bodyText: string(1..500000) }, preview: string }
```

DTO:
```
AssistantMessageRole = 'user' | 'assistant'
AssistantMessage { id: int>0, conversationId: int>0, role: AssistantMessageRole, content: string,
                   actionProposal: ActionProposal|null, appliedAt: datetime|null,
                   applyError: string|null, createdAt: datetime }
AssistantConversation { id: int>0, title: string|null, lastMessageAt: datetime|null, createdAt: datetime }
AssistantConversationFull { conversation: AssistantConversation, messages: AssistantMessage[] }
AssistantTurnRequest { message: string(1..4000), context: AssistantContext }
```
Sentinel: odrzucenie propozycji = `applyError === 'dismissed'` (UI to rozróżnia od realnego błędu apply).

### voice.ts (helpery współdzielone frontend + serwer - DOSŁOWNA logika)
Te funkcje budują linie kontekstu AI (history excerpt). Serwer Pythonowy MUSI je odtworzyć znak w znak (polskie stringi!), frontend zachowuje wersję TS:

```
formatVoiceDuration(sec):
  null lub <0  → "?"
  <60          → "{sec}s"
  inaczej      → "{m}m{ss}s"   gdzie m=floor(sec/60), ss=sec%60 zero-padded do 2 znaków (np. "3m05s")

formatVoiceForAi(durationSec, status, transcript):
  status 'done' i transcript truthy → `[głosówka {dur}, transkrypt]: "{transcript}"`
  status 'pending'                  → `[głosówka {dur}, transkrypcja jeszcze nie gotowa]`
  status 'error'                    → `[głosówka {dur}, transkrypcja nieudana]`
  inaczej                           → `[głosówka {dur}]`

formatImageForAi(status, description):
  status 'done' i description truthy → `[zdjęcie]: "{description}"`
  status 'pending'                   → `[zdjęcie, opis jeszcze nie gotowy]`
  status 'error'                     → `[zdjęcie, opis nieudany]`
  inaczej                            → `[zdjęcie]`
```

## 1.8 Stan po stronie przeglądarki (nie dotyczy backendu, ale ważne dla zachowania)
- `localStorage["circle-dm:active-account-id"]` - aktywne konto admina (string z int). Zmiana emituje `window` event `circle-dm:account-changed`.
- `sessionStorage["circle-dm:bulk-queue"]` - kolejka bulk: `{ adminAccountId, items: [{kind:'thread', threadId, name, avatarUrl, lastMessagePreview, lastMessageAt} | {kind:'member', memberId, ...}] }`.

## 1.9 Serwowanie SPA i health (prod)

Obecny Hono robi (i FastAPI musi powtórzyć):
- `GET /health` → `{ "ok": true, "version": "0.1.0" }` (bez auth).
- `GET /health/claude` → status zdrowia subprocesu Claude (bez auth).
- Statyczne: `/assets/*` (zbundlowane JS/CSS z hash w nazwie - można dawać długi cache) i `/favicon.svg` z katalogu `WEB_DIST_PATH` (= `apps/web/dist`).
- SPA fallback: KAŻDY niedopasowany `GET` → zawartość `dist/index.html` jako `text/html`, CHYBA ŻE `path.startsWith('/api/')` lub `path === '/ws'` → wtedy 404. (Porównanie `/ws` jest exact, `/api/` po prefiksie.)
- `index.html` ładuje fonty z Google Fonts (`Permanent Marker`, `Inter` 400-800) - host musi mieć dostęp wychodzący z przeglądarki, nic po stronie serwera.

---

# CZĘŚĆ 2: PRZENOSINY `apps/web` + `packages/shared` do nowego monorepo (backend = Python)

## 2.1 Jak to działa dziś (żeby wiedzieć, co się zepsuje)

- Root pnpm workspace: `pnpm-workspace.yaml` z `packages: ["apps/*", "packages/*"]` + `allowBuilds: {'@biomejs/biome': true, esbuild: true}`. Root `package.json`: `packageManager: "pnpm@11.1.0"`, engines node>=20 pnpm>=10, devDeps: `@biomejs/biome ^1.9.4`, `turbo ^2.5.0`, `typescript ^5.7.0`.
- `apps/web/package.json` zależy od `"@admin/shared": "workspace:*"`. Vite (oraz `tsc -b`) rozwiązuje import `@admin/shared` przez pole `exports` paczki shared → `./dist/index.js` + `./dist/index.d.ts`. **Czyli web konsumuje ZBUDOWANY dist shared, nie źródła.**
- `packages/shared` builduje się przez `tsc` (`rootDir: src`, `outDir: dist`, `declaration: true`, `declarationMap`, `sourceMap`). Wewnętrzne importy w shared mają rozszerzenia `.js` (`export * from './schemas/draft.js'`) - to działa, bo TS kompiluje do plików `.js` w dist.
- Turbo (`turbo.json`): taski `dev` (persistent, `dependsOn: ["^build"]`), `build` (`dependsOn: ["^build"]`, outputs `dist/**`), `typecheck` (`dependsOn: ["^build"]`), `lint`. Przez `^build` shared jest budowany ZANIM wystartuje dev/build weba. W trybie dev shared ma własny task `dev` = `tsc --watch` (rebuild dist przy zmianie). Stąd gotcha z CLAUDE.md: po dodaniu plików w shared trzeba zrestartować `pnpm dev`.
- tsconfigi: web `tsconfig.json` to tylko project references na `tsconfig.app.json` (src, jsx react-jsx, `paths: {"@/*": ["src/*"]}`, moduleResolution Bundler, target ES2022, lib +DOM) i `tsconfig.node.json` (sam `vite.config.ts`). Oba `extends: "../../tsconfig.base.json"` (strict, noUncheckedIndexedAccess - app nadpisuje na false, isolatedModules, skipLibCheck itd.). Shared też extenduje base.
- Build weba: `tsc -b && vite build` → `apps/web/dist`. Lint: `biome check .` (root `biome.json`).
- Vite dev server: port `5173` `strictPort`, alias `'@' → <web>/src`, proxy: `/api` → `http://localhost:3000` (changeOrigin), `/ws` → `ws://localhost:3000` (`ws: true`), `/health` → `http://localhost:3000` (changeOrigin).
- Dockerfile (multi-stage): builder kopiuje manifesty workspace, `pnpm install --frozen-lockfile`, potem buduje w kolejności `@admin/shared` → `@admin/server` → `@admin/web`; runtime dostaje `apps/web/dist` pod `WEB_DIST_PATH=/app/web-dist`.

## 2.2 Co skopiować

1. `apps/web/` w całości: `src/`, `public/` (zawiera tylko `favicon.svg`), `index.html`, `vite.config.ts`, `tailwind.config.ts`, `postcss.config.js`, `package.json`, `tsconfig.json`, `tsconfig.app.json`, `tsconfig.node.json`. NIE kopiować `dist/`, `node_modules/`, `.tsbuildinfo.*`.
2. `packages/shared/` w całości (`src/`, `package.json`, `tsconfig.json`) - schematy w runtime potrzebne są TYLKO frontowi (walidacja ramek WS + typy), więc paczka zostaje czysto frontendowa, ale to nadal wygodne źródło prawdy przy pisaniu Pydantic.
3. Z roota: `tsconfig.base.json` (extends w obu paczkach!), `biome.json` (jeśli zostajemy przy biome do lintu), `pnpm-workspace.yaml` + root `package.json` (przyciąć), `pnpm-lock.yaml` NIE kopiować 1:1 (zawiera `@admin/server` i jego deps) - wygenerować świeży `pnpm install` albo `pnpm install --fix-lockfile`.

## 2.3 Lista zmian (checklist)

1. **Workspace**: w nowym monorepo zostaw mini-workspace pnpm wyłącznie dla JS: `pnpm-workspace.yaml` z wpisami wskazującymi nowe ścieżki (np. `frontends/admin-web`, `frontends/packages/shared` - dopasować do struktury repo). Zależność `"@admin/shared": "workspace:*"` w web/package.json zostaje bez zmian o ile shared dalej jest w workspace. Zachowaj `allowBuilds` dla `esbuild` (i `@biomejs/biome` jeśli zostaje) - bez tego pnpm 10+/11 zablokuje postinstall buildy.
   - Alternatywa (mniej ruchomych części): wchłonąć źródła shared do `apps/web/src/shared/` i zamienić importy `@admin/shared` na `@/shared`. UWAGA pułapka: wewnętrzne importy shared mają sufiks `.js` (`'./schemas/draft.js'`) - pod Vite (bundler resolution na źródłach) trzeba te sufiksy USUNĄĆ albo skonfigurować resolve. Przy zostawieniu osobnej paczki z buildem tsc problem nie istnieje.
2. **Root package.json**: wyrzucić skrypty `db:*`, `set-auth-password` (to były filtry na `@admin/server`). Zostawić/uprościć `dev`, `build`, `typecheck`, `lint`, `format`. Turbo można zostawić (taski `dev`/`build`/`typecheck`/`lint` z `dependsOn: ["^build"]` - to one gwarantują build shared przed webem) albo wyrzucić turbo i zastąpić: `pnpm --filter @admin/shared build && pnpm --filter @admin/web build`. Jeśli turbo zostaje: usunąć z `turbo.json` taski `db:generate`/`db:migrate`.
3. **Vite proxy**: zaktualizować targety do portu FastAPI (np. uvicorn `:8000`):
   - `/api` → `http://localhost:8000` (changeOrigin true),
   - `/ws` → `ws://localhost:8000` z `ws: true`,
   - `/health` → `http://localhost:8000`.
   Port weba `5173` `strictPort: true` może zostać. Alias `'@'` bez zmian.
4. **tsconfig.base.json**: skopiować do roota nowego repo (oba `extends: "../../tsconfig.base.json"` zakładają, że web i shared siedzą DWA poziomy pod plikiem base). Jeśli głębokość katalogów się zmieni - poprawić ścieżki `extends`. Ewentualnie wkleić opcje base bezpośrednio do tsconfigów paczek i zlikwidować zależność od roota.
5. **Build prod**: pipeline = `pnpm install` → build shared (tsc) → build web (`tsc -b && vite build`) → artefakt `apps/web/dist`. W nowym Dockerfile (albo na etapie CI) odtworzyć kolejność z obecnego Dockerfile. FastAPI serwuje `dist` wg sekcji 1.9 (StaticFiles na `/assets`, plik `/favicon.svg`, fallback `index.html` z wyjątkiem `/api/*` i `/ws`). Env odpowiednik `WEB_DIST_PATH` zachować jako konfigurację ścieżki.
6. **Lint/format**: `biome check .` wymaga root `biome.json` i devDep `@biomejs/biome`. Albo skopiować, albo zmienić skrypt `lint` weba. Nic w kodzie nie zależy od biome w runtime.
7. **Nic w kodzie frontendu nie wymaga zmian** poza ewentualnymi importami z pkt 1 - base URL-e są względne, auth cookie same-origin, WS po `window.location`. Frontend zadziała z FastAPI bez przebudowy, o ile backend dotrzyma kontraktu z części 1.
8. **pnpm-lock**: wygenerować od zera w nowym repo (`pnpm install`), commitnąć. `packageManager: "pnpm@11.1.0"` + corepack, node >= 20 (Dockerfile używa node:22).

---

# Uwagi dla portu na FastAPI (pułapki)

1. **camelCase wszędzie, bez wyjątków.** Frontend nie waliduje odpowiedzi HTTP, więc literówka/snake_case nie rzuci błędem - po prostu UI dostanie `undefined` i będzie się sypać po cichu. W Pydantic: `alias_generator=to_camel` + `populate_by_name=True` + serializacja `by_alias=True` na WSZYSTKICH modelach response i ramkach WS.
2. **Ramki WS są walidowane Zodem z `safeParse` - niezgodna ramka znika bez śladu.** Brak pola, snake_case, zły typ (`"5"` zamiast `5`) = event cicho odrzucony, zero błędów w konsoli (parse errors są połykane). To najtrudniejszy do debugowania punkt portu. Testować każdą ramkę kontraktowo przeciw `wsEventSchema`.
3. **Format błędu to `{"error": "<string>"}`.** Domyślne FastAPI `{"detail": ...}` NIE zostanie odczytane - frontend pokaże fallback `"422 Unprocessable Entity"`. Trzeba globalnie przemapować `RequestValidationError` i `HTTPException` na `{"error": "..."}`. Pole `error` MUSI być stringiem (frontend sprawdza `typeof parsed.error === 'string'`).
4. **401 = twardy reload strony** (w klientach circle-dm i feedback). Zwracaj 401 tylko przy braku/wygaśnięciu sesji. Błędna walidacja, brak uprawnień do zasobu itp. = 4xx inne niż 401, inaczej user wpada w pętlę reloadów. `GET /api/auth/me` dla niezalogowanego = 200 z `{"authenticated": false}`, nigdy 401.
5. **Datetime: ISO 8601 UTC z `Z`** (`2026-06-10T12:00:00.000Z`). Pythonowe `datetime.isoformat()` daje `+00:00` - Zod `.datetime()` bez `offset: true` to ODRZUCI. Dziś dotyczy to tylko walidacji WS (ramki nie mają dat), ale trzymaj jeden serializer (np. `dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'`) dla całego API - parytet i odporność na przyszłe walidacje.
6. **Defaulty Zod nie chronią HTTP.** `dmMessageSchema` ma `.default([])`/`.default(null)` na `attachments`, `voiceTranscript`, `imageDescriptions` itd., ale działają tylko przy parsowaniu (czyli nigdzie po stronie odpowiedzi HTTP). FastAPI musi zwracać te pola jawnie, z `[]`/`null`, nie pomijać.
7. **WS bez ping/pong od klienta i bez resubskrypcji.** Klient tylko słucha. Za Caddy/proxy idle connection może paść - serwer powinien wysyłać protokołowe pingi (frame ping, nie JSON - JSON-owy "ping" nieznanego typu i tak byłby odrzucony przez schemat). Reconnect klienta: 1s → x1.5 → cap 15s, reset na open. Po reconnect klient NICZEGO nie wysyła - eventy z okresu rozłączenia przepadają (UI nadrabia refetchami React Query), więc nie buduj logiki zakładającej dostarczalność.
8. **Ramka `{"type":"hello"}` po połączeniu** - klient ją jawnie ignoruje, ale wyślij dla parytetu. Auth WS przez cookie sesyjne w handshake'u (klient nie ma jak dodać headera).
9. **Pusta odpowiedź 2xx jest legalna** (klient zwraca `undefined`), ale niepusta MUSI być JSON-em - nie zwracaj plain-textu na 2xx.
10. **`POST /kb/upload` to multipart**, nie JSON: pola `file`, `scope`, opcjonalnie `title`, `adminAccountId` (przychodzi jako string, np. `"3"` - parsować int). Response `{ "id": number, "tokenEstimate": number }`. `GET /kb/:id/original` jest otwierany linkiem przeglądarki (cookie auth, response = plik do pobrania).
11. **Booleany w query stringu są stringami**: `excludeWithThread=1`, `refetch=1` (literalne `"1"`). `adminAccountId`, `limit`, `accountId`, `id` w query też przychodzą jako stringi - parsować int.
12. **Union `ComposeSendResult`**: `ok:true` → `threadId` + `circleChatRoomUuid`; `ok:false` → `error`. Frontend gałęziuje po `ok`, więc kształty muszą być dokładnie takie (nie mieszać pól z obu wariantów).
13. **`GET /threads/:id/messages`** - serwer zwraca też `hasPrevious`/`hasNext` (dziś na sztywno `false`); frontend czyta tylko `messages`. Można zwrócić same `messages`, ale dla parytetu 1:1 zwróć wszystkie trzy pola.
14. **`voice.ts` musi mieć pythonowego bliźniaka** (formatowanie głosówek/zdjęć do kontekstu AI, polskie stringi, format czasu `3m05s`). Każda różnica znaków zmienia prompty wysyłane do modelu.
15. **Sentinel `applyError === 'dismissed'`** w AssistantMessage - UI traktuje to jako "odrzucone przez usera", nie błąd. Zachować dosłownie.
16. **`AdminAccount.hasToken`** - backend nigdy nie zwraca tokenu Circle, tylko boolean czy jest zapisany. Nie dodać przypadkiem pola z tokenem.
17. **Komunikaty błędów lądują 1:1 w toastach** - pisać po polsku, głosem marki BFC (bez długich myślników).
18. **SPA fallback**: 404 tylko dla `path.startswith('/api/')` i `path == '/ws'` (exact). Wszystko inne, łącznie z `/circle-dm/thread/123`, dostaje `index.html`. `/assets/*` i `/favicon.svg` jako statyki.
19. **Eventy WS emitowane nadmiarowo**: `thread:new_messages` i `draft:tool_use` nikt dziś sensownie nie konsumuje (tool_use ma pusty case), ale są w schemacie - emitować dla parytetu, koszt zerowy.
20. **`listThreadsUrl` w api.ts to martwy kod** (identity function) - nie szukać odpowiadającego endpointu.
21. **Drobny mismatch typów klienta vs serwera**: `api.accounts.sync` typuje response jako `{ok, changedThreadIds, newUnreadThreadIds}`, a kształty `settings` i `bulk.send` są zdefiniowane inline w `api.ts`, nie w shared. Przy porcie traktuj `api.ts` (nie shared) jako ostateczne źródło prawdy o response'ach - shared bywa nadzbiorem (np. `MessageListResponse`) albo w ogóle nie pokrywa endpointu.
