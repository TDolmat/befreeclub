# Spec: klient Circle Headless API (circle-dm)

Źródła (TypeScript, port 1:1 na Python/FastAPI):

- `admin/apps/server/src/tools/circle-dm/circle/client.ts`
- `admin/apps/server/src/tools/circle-dm/circle/jwt-manager.ts`
- `admin/apps/server/src/tools/circle-dm/circle/tiptap.ts`
- `admin/apps/server/src/tools/circle-dm/circle/types.ts`

Moduł to niskopoziomowy klient Circle.so Headless Member API używany przez tool Circle DM. Składa się z: generycznego helpera HTTP, 7 funkcji-endpointów, managera JWT z cache w Postgresie i dwóch konwerterów Tiptap (text → envelope i envelope → plain text).

---

## 1. Warstwa HTTP (`client.ts`)

### 1.1. Stałe

```
BASE = 'https://app.circle.so'
```

Logger ma namespace `circle` (`createLogger('circle')`).

### 1.2. Klasa błędu `CircleApiError`

```ts
class CircleApiError extends Error {
  status: number;   // HTTP status odpowiedzi Circle
  body: string;     // PEŁNE surowe body odpowiedzi (text)
  message: string;  // patrz niżej
  name = 'CircleApiError';
}
```

W Pythonie: własny exception z polami `status: int`, `body: str`, `message: str`. Kod wyżej (sync, send-handlery) łapie ten typ i czyta `status` (np. 401 → invalidate JWT) oraz `body` (np. wykrywanie "Messaging is disabled by receiver"). Pola `status` i `body` MUSZĄ być dostępne programowo.

### 1.3. Generyczny `request<T>(method, path, options)`

Parametry:

- `method`: `'GET' | 'POST' | 'PATCH'` (tylko te trzy są używane).
- `path`: jeśli zaczyna się od `http` → użyj jak pełnego URL, w przeciwnym razie `BASE + path`.
- `options.auth`: string wstawiany do `Authorization: Bearer <auth>` (raz admin token, raz member JWT, zależnie od funkcji).
- `options.body`: opcjonalny obiekt; jeśli `undefined` → żądanie BEZ body; inaczej `JSON.stringify(body)`.
- `options.timeoutMs`: domyślnie `30_000` ms (30 s). Realizowane przez `AbortController` + `setTimeout`; po przekroczeniu request jest abortowany (w Pythonie: timeout httpx/aiohttp na 30 s; timeout NIE jest mapowany na `CircleApiError`, leci jako goły wyjątek abort/timeout).

Headery KAŻDEGO żądania (dokładnie te trzy):

```
Authorization: Bearer <auth>
Content-Type: application/json
Accept: application/json
```

Uwaga: `Content-Type: application/json` jest wysyłany też przy GET (fetch tak robi, bo header ustawiony jawnie).

Obsługa odpowiedzi, w tej kolejności:

1. Przeczytaj body jako tekst (`res.text()`), zawsze, niezależnie od statusu.
2. Jeśli status nie jest 2xx (`!res.ok`):
   - log warn: `` `${method} ${path} → ${res.status}` `` + pierwsze 200 znaków body,
   - rzuć `CircleApiError(res.status, text, `Circle API ${res.status}: ${text.slice(0, 200)}`)` - message zawiera status i pierwsze 200 znaków body.
3. Jeśli status OK i body puste (`text === ''`) → zwróć `undefined` (w Pythonie: `None`).
4. Spróbuj `JSON.parse(text)`; jeśli się nie uda → rzuć `CircleApiError(res.status, text, 'Circle returned non-JSON response')` (status jest wtedy statusem 2xx!).
5. Zwróć sparsowany JSON.

**Brak retry, brak backoff, brak rate-limitingu** w tej warstwie. Retry/invalidacja JWT (po 401) dzieje się piętro wyżej, poza tymi plikami. `clearTimeout` zawsze w `finally`.

---

## 2. Funkcje-endpointy

Wszystkie zwracają sparsowany JSON odpowiedzi Circle (kształty w sekcji 5). Wszystkie używają domyślnego timeoutu 30 s. Żadna nie robi paginacji automatycznie - paginacja jest po stronie wywołującego (parametry `page`/`per_page`, w odpowiedziach `has_next_page`).

### 2.1. `exchangeAdminTokenForJWT(adminToken, email)`

Wymiana admin headless tokena + e-maila na ~1h member JWT.

- Metoda/URL: `POST https://app.circle.so/api/v1/headless/auth_token` (UWAGA: jedyny endpoint na ścieżce `/api/v1/headless/...`; cała reszta to `/api/headless/v1/...`).
- Auth: `Bearer <adminToken>` (admin headless token z DB, nie JWT).
- Body żądania (dokładnie): `{ "email": "<email>" }`.
- Odpowiedź: `CircleAuthResponse` (sekcja 5.1).
- Log debug przed wywołaniem: `auth_token exchange for <email>`.

### 2.2. `listThreads(jwt, opts)`

Lista wątków DM zalogowanego membera.

- Metoda/URL: `GET /api/headless/v1/messages?page=<page>&per_page=<perPage>`.
- Auth: member JWT.
- Defaulty: `page = 1` (Circle indeksuje strony od 1), `perPage = 50`.
- Body: brak.
- Odpowiedź: `CirclePaginatedThreads` (sekcja 5.3): `records` + `page`, `per_page`, `has_next_page`, `count`, `page_count`.

### 2.3. `getThreadMessages(jwt, chatRoomUuid, opts)`

Historia jednego chat roomu (po UUID).

- Metoda/URL: `GET /api/headless/v1/messages/<chatRoomUuid>/chat_room_messages?per_page=<perPage>`.
- Auth: member JWT.
- Default: `perPage = 100`. Brak parametru `page` (ten endpoint paginuje kursorem `first_id`/`last_id`, klient go nie używa - bierze jedną stronę).
- Odpowiedź: `CirclePaginatedMessages` (sekcja 5.4): `records`, `first_id`, `last_id`, `total_count`, `has_previous_page`, `has_next_page`.

### 2.4. `sendMessage(jwt, chatRoomUuid, body)`

Wysyłka wiadomości do ISTNIEJĄCEGO chat roomu.

- Metoda/URL: `POST /api/headless/v1/messages/<chatRoomUuid>/chat_room_messages`.
- Auth: member JWT.
- Body żądania (dokładnie):

```json
{
  "body": "<plain text>",
  "rich_text_body": <wynik textToTiptap(body)>
}
```

- KRYTYCZNE (zweryfikowane 2026-05-12): Circle wymaga OBU pól. Samo `body` bez `rich_text_body` lub `rich_text_body` bez wrappera `body` (goły dokument Tiptap zamiast envelope) → POST zwraca 200/creation_uuid, ale treść jest po cichu gubiona (kolejne GET-y zwracają `body: ""` i `rich_text_body: null`).
- Odpowiedź: `CircleSendMessageResponse` (sekcja 5.5) - zwraca `creation_uuid`, NIE numeryczne `id`.

### 2.5. `markChatRoomRead(jwt, chatRoomUuid)`

Oznaczenie chat roomu jako przeczytany po stronie Circle.

- Metoda/URL: `PATCH /api/headless/v1/messages/<chatRoomUuid>`.
- Auth: member JWT.
- Body żądania (dokładnie): `{ "unread_messages_count": 0 }`.
- Odpowiedź ignorowana (funkcja zwraca void). Headless API nie ma dedykowanego `/mark_as_read`; generyczny PATCH przyjmuje nadpisanie `unread_messages_count` i zwraca 200 echując `unread_messages_count: 0` (zweryfikowane 2026-05-12).
- Semantyka: best-effort. Błąd tej operacji NIE może blokować wysyłki wiadomości (wywołujący łapie wyjątek; sama funkcja nie łyka błędów - rzuca `CircleApiError` jak każda inna).

### 2.6. `sendToNewRecipient(jwt, communityMemberIds, body)`

Find-or-create direct chat roomu z danym memberem (lub memberami) + wysyłka pierwszej wiadomości w jednym wywołaniu.

- Metoda/URL: `POST /api/headless/v1/messages`.
- Auth: member JWT.
- Body żądania (dokładnie, zweryfikowane 2026-05-12):

```json
{
  "chat_room": {
    "kind": "direct",
    "community_member_ids": [123, 456]
  },
  "body": "<plain text>",
  "rich_text_body": <wynik textToTiptap(body)>
}
```

- Odpowiedź: `CircleFindOrCreateResponse` = `{ "chat_room": <CircleThreadRecord> }` (pełny obiekt chat roomu z `uuid`, kształt jak w liście wątków).
- Edge case: jeśli odbiorca wyłączył DM-y, Circle zwraca błąd z komunikatem zawierającym `"Messaging is disabled by receiver"` (trafia do `CircleApiError.body`/`message`; warstwa wyżej go po tym rozpoznaje).

### 2.7. `listMembers(jwt, opts)`

Lista członków community (paginowana), zasila picker odbiorców "nowa wiadomość".

- Metoda/URL: `GET /api/headless/v1/community_members?page=<page>&per_page=<perPage>[&query=<query>]`.
- Auth: member JWT.
- Defaulty: `page = 1`, `perPage = 100`. Parametr `query` dodawany TYLKO gdy podany i niepusty (truthy check - pusty string `""` nie jest wysyłany). Wartości URL-encodowane (`URLSearchParams`).
- Odpowiedź: `CirclePaginatedCommunityMembers` (sekcja 5.7). Kontekst: community ma ~172 członków, czyli ~2 strony przy per_page=100.

---

## 3. Manager JWT (`jwt-manager.ts`)

Cache member-JWT per konto admina, persystowany w tabeli Postgres `admin_accounts`. Logger namespace: `circle:jwt`.

### 3.1. Stała

```
REFRESH_LEAD_MS = 5 * 60 * 1000   // 5 minut marginesu przed wygaśnięciem
```

### 3.2. Użyte kolumny tabeli `admin_accounts`

| Kolumna (Postgres) | Typ | Rola |
|---|---|---|
| `id` | bigserial PK | klucz konta |
| `email` | text NOT NULL | e-mail do exchange |
| `circle_admin_token` | text NOT NULL | admin headless token (sekret, długoterminowy) |
| `circle_access_token` | text NULL | cache member JWT |
| `circle_access_token_expires_at` | timestamptz NULL | wygaśnięcie JWT |
| `circle_refresh_token` | text NULL | zapisywany, ale NIGDY nie używany do odświeżania (odświeżamy zawsze przez exchange admin tokena) |
| `community_id` | bigint NULL | z odpowiedzi exchange |
| `community_member_id` | bigint NULL | z odpowiedzi exchange |
| `is_active` | boolean NOT NULL default true | bramka aktywności konta |

### 3.3. Zwracany stan `JwtState`

```ts
{ accessToken: string; expiresAt: Date; communityId: number; communityMemberId: number }
```

### 3.4. `getJwtFor(adminAccountId)` - deduplikacja concurrency

In-memory mapa `inflight: Map<adminAccountId, Promise<JwtState>>` na poziomie modułu (singleton procesu):

1. Jeśli w `inflight` jest promise dla tego `adminAccountId` → zwróć go (równoległe wywołania dla tego samego konta dostają TEN SAM wynik, exchange leci max raz naraz).
2. Inaczej: odpal `resolveJwt(adminAccountId)`, wstaw promise do mapy, po zakończeniu (sukces LUB błąd - `finally`) usuń wpis z mapy.

W Pythonie (asyncio): dict `account_id -> asyncio.Task`/`Future` albo per-account `asyncio.Lock`; kluczowe, żeby wpis był czyszczony także po wyjątku i żeby błąd propagował do wszystkich czekających.

### 3.5. `resolveJwt(adminAccountId)` - logika cache

Kolejność operacji:

1. `SELECT * FROM admin_accounts WHERE id = <adminAccountId> LIMIT 1`.
2. Brak rekordu → rzuć `Error("admin_account <id> not found")`.
3. `is_active = false` → rzuć `Error("admin_account <id> is not active")`.
4. **Cache hit** - zwróć stan z DB bez requestu do Circle, gdy WSZYSTKIE warunki spełnione:
   - `circle_access_token` niepusty,
   - `circle_access_token_expires_at` niepusty,
   - `expires_at - 5 min > teraz` (czyli token ma jeszcze ponad 5 minut życia),
   - `community_id IS NOT NULL` **i** `community_member_id IS NOT NULL`.
5. **Cache miss** (cokolwiek z powyższego nie gra):
   - log info: `Exchanging admin token for fresh JWT (account <id>)`,
   - `exchangeAdminTokenForJWT(circle_admin_token, email)` (sekcja 2.1),
   - `expiresAt = new Date(response.access_token_expires_at)` (parsowanie ISO stringa z odpowiedzi),
   - `UPDATE admin_accounts SET circle_access_token = <access_token>, circle_access_token_expires_at = <expiresAt>, circle_refresh_token = <refresh_token>, community_id = <community_id>, community_member_id = <community_member_id> WHERE id = <adminAccountId>`,
   - zwróć świeży stan.

Uwaga: `updated_at` NIE jest tu dotykany (Drizzle nie ma auto-update na tej kolumnie).

### 3.6. `invalidateJwt(adminAccountId)`

Wywoływane przez warstwę wyżej po otrzymaniu 401 z Circle:

```sql
UPDATE admin_accounts
SET circle_access_token = NULL, circle_access_token_expires_at = NULL
WHERE id = <adminAccountId>;
```

`circle_refresh_token`, `community_id`, `community_member_id` zostają nietknięte. Następne `getJwtFor` zrobi świeży exchange.

---

## 4. Konwertery Tiptap

### 4.1. `textToTiptap(text)` - plain text → envelope `rich_text_body` (w `client.ts`)

Zweryfikowane na realnych wiadomościach Circle (2026-05-12 i 2026-05-13). **Pełna, DOSŁOWNA struktura envelope** (klucze i wartości dokładnie tak):

```json
{
  "body": {
    "type": "doc",
    "content": [ /* węzły doc, patrz algorytm */ ]
  },
  "polls": [],
  "format": "chat",
  "entities": [],
  "attachments": [],
  "group_mentions": [],
  "community_members": [],
  "inline_attachments": [],
  "sgids_to_object_map": {},
  "circle_ios_fallback_text": "<cały znormalizowany plain text>"
}
```

Algorytm budowy `content`, krok po kroku:

1. Normalizacja: `text.replace(/\r\n/g, '\n').trim()` → `trimmed`. `circle_ios_fallback_text` na poziomie envelope = `trimmed` (cały tekst po normalizacji, z zachowanymi `\n`).
2. Podział na akapity: `trimmed.split(/\n{2,}/)` (2+ kolejnych newline'ów = separator akapitów). Jeśli `trimmed` jest pusty → akapitów brak.
3. Jeśli akapitów 0 → `content = [{ "type": "paragraph" }]` (jeden pusty paragraph, bez pola `content`).
4. Dla każdego akapitu zbuduj węzeł paragraph (`buildParagraph`):
   - podziel akapit na linie po `\n`,
   - dla każdej NIEPUSTEJ linii (`line.length > 0`) dodaj węzeł tekstowy:

     ```json
     { "type": "text", "text": "<linia>", "circle_ios_fallback_text": "<linia>" }
     ```

     (UWAGA: `circle_ios_fallback_text` jest też na KAŻDYM węźle text, = treść tej linii),
   - po każdej linii OPRÓCZ ostatniej dodaj `{ "type": "hardBreak" }` (także po linii pustej - pusta linia nie daje węzła text, ale daje hardBreak jeśli nie jest ostatnia),
   - jeśli lista węzłów niepusta → `{ "type": "paragraph", "content": [...] }`; jeśli pusta → `{ "type": "paragraph" }` (bez klucza `content`).
5. **Spacer między akapitami**: po każdym paragraphie OPRÓCZ ostatniego wstaw dodatkowy pusty `{ "type": "paragraph" }`. Powód: Circle Web renderuje sąsiednie paragraphy bez pionowego odstępu; realne wiadomości Circle mają pusty paragraph jako wizualny spacer (zweryfikowane na wiadomości Weroniki Stelmaszak 2026-05-13). Doc dla 2 akapitów wygląda więc tak: `[P1, {"type":"paragraph"}, P2]`.

Przykład: wejście `"Cześć!\nCo słychać?\n\nPozdrawiam"` daje:

```json
{
  "body": {
    "type": "doc",
    "content": [
      {
        "type": "paragraph",
        "content": [
          { "type": "text", "text": "Cześć!", "circle_ios_fallback_text": "Cześć!" },
          { "type": "hardBreak" },
          { "type": "text", "text": "Co słychać?", "circle_ios_fallback_text": "Co słychać?" }
        ]
      },
      { "type": "paragraph" },
      {
        "type": "paragraph",
        "content": [
          { "type": "text", "text": "Pozdrawiam", "circle_ios_fallback_text": "Pozdrawiam" }
        ]
      }
    ]
  },
  "polls": [],
  "format": "chat",
  "entities": [],
  "attachments": [],
  "group_mentions": [],
  "community_members": [],
  "inline_attachments": [],
  "sgids_to_object_map": {},
  "circle_ios_fallback_text": "Cześć!\nCo słychać?\n\nPozdrawiam"
}
```

### 4.2. `tiptapToPlainText(doc)` - envelope/doc → plain text (`tiptap.ts`)

Używany do czytania `rich_text_body` przychodzących wiadomości. Powód istnienia: pole `circle_ios_fallback_text` na poziomie envelope jest sklejone BEZ przerw akapitowych (np. "...na testywięc pytanie..."), a pole `body` (plain) ma tę samą wadę, więc trzeba chodzić po drzewie Tiptap.

Logika:

1. Jeśli wejście nie jest obiektem (None, string itp.) → zwróć `""`.
2. Rozpakowanie envelope: jeśli `doc.body` istnieje i jest obiektem → root = `doc.body`; inaczej root = `doc` (akceptuje goły doc i envelope).
3. Rekurencyjny `visit(node)`:
   - nie-obiekt → `""`,
   - `type == "text"` ze stringowym `text` → zwróć `text`,
   - `type == "hardBreak"` → `"\n"`,
   - `type` ∈ {`paragraph`, `blockquote`, `heading`} → `join('')` dzieci przez visit + `"\n\n"` na końcu,
   - `type == "bulletList"` → dzieci (listItemy) renderowane jako `- <renderListItem(c)>`, łączone `"\n"`, plus `"\n\n"` na końcu,
   - `type == "orderedList"` → start = `attrs.start ?? 1`; itemy jako `"<start+i>. <renderListItem(c)>"`, łączone `"\n"`, plus `"\n\n"` na końcu,
   - `type == "listItem"` → `renderListItem(n)`,
   - każdy inny typ (w tym brak `type`, np. root doc) → `join('')` visitów dzieci.
4. `renderListItem(node)`: `join('')` visitów dzieci, potem zetnij KOŃCOWE newline'y (`replace(/\n+$/, '')`) - bo paragraph w środku itemu dokleja `\n\n`.
5. Na koniec całości: `result.replace(/\n{3,}/g, '\n\n').trim()` - zbij 3+ newline'ów do dwóch i przytnij białe znaki z brzegów.

---

## 5. Typy odpowiedzi Circle (`types.ts`)

Wszystkie pola w snake_case, dokładnie jak zwraca Circle (zweryfikowane PoC 2026-05-12). `?` = pole może nie wystąpić; `| null` = pole występuje ale bywa null.

### 5.1. `CircleAuthResponse` (POST /api/v1/headless/auth_token)

```
access_token: string
refresh_token: string
access_token_expires_at: string        // ISO timestamp, JWT żyje ~1h
refresh_token_expires_at: string
community_id: number
community_member_id: number
```

### 5.2. `CircleParticipantPreview`

```
id: number
community_member_id: number
name: string
email?: string
avatar_url?: string
status?: string
last_seen_text?: string
```

`CircleLastMessage`:

```
id: number
body: string
created_at: string
sender?: CircleParticipantPreview
rich_text_body?: unknown
```

`CircleThreadRecord` (element `records` z listy wątków oraz `chat_room` z find-or-create):

```
id: number
uuid: string
identifier?: string
chat_room_kind: 'direct' | 'group_chat'
chat_room_name: string | null
unread_messages_count: number
pinned_at: string | null
chat_room_participants_count: number
other_participants_preview: CircleParticipantPreview[]
current_participant?: CircleParticipantPreview
last_message: CircleLastMessage | null
```

### 5.3. `CirclePaginatedThreads` (GET /api/headless/v1/messages)

```
records: CircleThreadRecord[]
page: number
per_page: number
has_next_page: boolean
count: number
page_count: number
```

### 5.4. `CirclePaginatedMessages` (GET .../chat_room_messages)

```
records: CircleMessageRecord[]
first_id: number | null
last_id: number | null
total_count: number
has_previous_page: boolean
has_next_page: boolean
```

`CircleMessageRecord`:

```
id: number
body: string
rich_text_body?: unknown
created_at: string
edited_at: string | null
sent_at?: string
parent_message_id: number | null
chat_thread_id: number | null
chat_room_uuid: string
chat_room_participant_id: number
sender: CircleParticipantPreview & { community_member_id: number }   // sender ZAWSZE ma community_member_id
reactions: unknown[]
bookmark_id: number | null
```

### 5.5. `CircleSendMessageResponse` (POST .../chat_room_messages)

```
creation_uuid: string
parent_message_id: number | null
sent_at: string
id?: number     // Circle dziś NIE zwraca numeric id; pole opcjonalne na przyszłość
```

KRYTYCZNE: POST zwraca `creation_uuid`, a NIE numeryczne `id` wiadomości. Pełna wiadomość z numerycznym id pojawia się dopiero w kolejnym GET `/chat_room_messages`.

### 5.6. `CircleFindOrCreateResponse` (POST /api/headless/v1/messages)

```
chat_room: CircleThreadRecord
```

### 5.7. `CirclePaginatedCommunityMembers` (GET /api/headless/v1/community_members)

```
records: CircleCommunityMemberRecord[]
page: number
per_page: number
has_next_page: boolean
count: number
page_count: number
```

`CircleCommunityMemberRecord`:

```
community_member_id: number
contact_id?: number
name: string
email?: string
avatar_url?: string
headline?: string
bio?: string
location?: string
last_seen_text?: string
status?: string
user_id?: number
roles?: { admin?: boolean; moderator?: boolean }
member_tags?: string[] | null
time_zone?: string | null
```

UWAGA: rekord membera NIE ma pola `id` - klucz to `community_member_id` (inaczej niż w `CircleParticipantPreview`, który ma oba: `id` i `community_member_id`).

---

## Uwagi dla portu na FastAPI

1. **Dwa różne prefiksy API**: auth_token to `/api/v1/headless/auth_token`, cała reszta to `/api/headless/v1/...`. Łatwo pomylić przy przepisywaniu - to nie literówka.
2. **`sendMessage`/`sendToNewRecipient` muszą wysyłać i `body` (plain), i `rich_text_body` (PEŁNY envelope z sekcji 4.1, w tym wrapper `body` wewnątrz envelope, `format: "chat"` i wszystkie puste tablice/obiekt)**. Circle nie zwraca błędu przy złym kształcie - przyjmuje POST (creation_uuid) i po cichu gubi treść. To najpodstępniejszy gotcha całego modułu; testuj przez GET po wysyłce.
3. **`circle_ios_fallback_text` w dwóch miejscach**: na poziomie envelope (cały tekst) i na KAŻDYM węźle `{type:"text"}` (treść linii). Pusty paragraph nie ma klucza `content` w ogóle (nie `content: []`).
4. **Spacer-paragraph między akapitami** w `textToTiptap` - bez niego Circle Web skleja akapity wizualnie. Odtwórz dokładnie: `[P1, pusty P, P2, pusty P, P3]`.
5. **Pydantic i casing**: wszystkie pola Circle są snake_case, więc w Pythonie mapują się naturalnie. NIE włączaj żadnych aliasów camelCase. Pola `rich_text_body`, `raw` itp. trzymaj jako `Any`/dict - nie waliduj ich agresywnie, bo Circle zmienia kształt.
6. **Nie waliduj odpowiedzi zbyt restrykcyjnie** - typy w `types.ts` to deklaracje, runtime niczego nie sprawdzał (`JSON.parse` i cast). Jeśli używasz Pydantic, daj `model_config = ConfigDict(extra="ignore")` i pola opcjonalne tam, gdzie spec ma `?`, inaczej drobna zmiana po stronie Circle wywali sync.
7. **Puste body 2xx → `None`**: oryginał zwraca `undefined` przy pustym body (np. teoretycznie PATCH). Port: zwróć `None`, nie próbuj parsować.
8. **Non-JSON przy 2xx → błąd**: `CircleApiError` z message `Circle returned non-JSON response` i statusem 2xx. Zachowaj, bo Circle czasem oddaje HTML (np. Cloudflare).
9. **Brak retry w kliencie** - nie dodawaj automatycznych retry "przy okazji". Logika 401 → `invalidateJwt` + ponowienie żyje w warstwie wyżej; jej spec jest w innym pliku. Klient ma tylko rzucać `CircleApiError` z dostępnym `status` i pełnym `body`.
10. **JWT cache w DB, nie w pamięci**: stan tokena żyje w `admin_accounts` (przeżywa restart procesu). In-memory jest TYLKO deduplikacja inflight. W FastAPI z wieloma workerami (gunicorn/uvicorn workers) deduplikacja per-proces nadal jest poprawna (najwyżej 2 procesy zrobią exchange równolegle - Circle to znosi, ostatni zapis wygrywa), ale nie przenoś dedupe do DB bez potrzeby.
11. **Margines 5 minut**: warunek cache-hit to `expires_at - 300s > now` ORAZ obecność `community_id` i `community_member_id`. Jeśli ktoś ręcznie wyzeruje `community_id` w DB, cache jest traktowany jako miss mimo ważnego tokena - zachowaj ten warunek.
12. **`invalidateJwt` zeruje tylko `circle_access_token` + `circle_access_token_expires_at`**, nie rusza refresh tokena ani community-id. Refresh token jest zapisywany, ale NIGDY nie używany - odświeżanie zawsze przez exchange admin tokena. Nie "ulepszaj" tego o refresh-flow.
13. **Daty**: `access_token_expires_at` przychodzi jako ISO string; kolumna w Postgresie jest timestamptz. W Pythonie parsuj do aware datetime (`datetime.fromisoformat` radzi sobie z offsetem; uważaj na sufiks `Z` - na Pythonie <3.11 wymaga zamiany na `+00:00`).
14. **Timeout 30 s** na każdy request; w TS to abort (wyjątek `AbortError`, NIE `CircleApiError`). W httpx odpowiednikiem jest `httpx.TimeoutException` - nie opakowuj jej w `CircleApiError`, warstwa wyżej rozróżnia te przypadki.
15. **`listMembers`**: `query` doklejany tylko gdy truthy (pusty string pomijany); wartości muszą być URL-encodowane.
16. **`tiptapToPlainText` musi chodzić po drzewie**, nie czytać `circle_ios_fallback_text` z envelope - fallback skleja akapity bez separatora. Funkcja musi przyjmować zarówno envelope (`{body: {...}}`), jak i goły doc, oraz dowolne śmieci (None/string → `""`).
17. **Komunikaty błędów 1:1**: message `CircleApiError` to dokładnie `Circle API <status>: <pierwsze 200 znaków body>`; błędy jwt-managera to `admin_account <id> not found` i `admin_account <id> is not active`. Warstwy wyżej / testy mogą na nich polegać.
