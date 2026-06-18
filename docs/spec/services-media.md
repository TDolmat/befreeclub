# Serwisy media Circle DM: transkrypcja głosówek + opisy zdjęć

Specyfikacja techniczna funkcji nieudokumentowanych w `PROJECT.md`, na podstawie kodu źródłowego
admina (stan: 2026-06). Cel: odtworzenie zachowania 1:1 w Pythonie (FastAPI).

Pliki źródłowe (TypeScript, `admin/apps/server/src/` o ile nie podano inaczej):

| Plik | Rola |
|---|---|
| `tools/circle-dm/services/voice-transcript-worker.ts` | Worker transkrypcji głosówek |
| `tools/circle-dm/services/image-description-worker.ts` | Worker opisów zdjęć |
| `tools/circle-dm/services/openai-stt.ts` | Klient OpenAI Whisper (STT) |
| `tools/circle-dm/services/openai-vision.ts` | Klient OpenAI Vision (opis obrazka) |
| `tools/circle-dm/circle/attachments.ts` | `extractAttachments` - normalizacja załączników Circle |
| `scripts/backfill-voice-transcripts.ts` | Backfill: oznaczanie starych głosówek jako pending |
| `scripts/backfill-image-descriptions.ts` | Backfill: dosypanie wierszy opisów dla starych zdjęć |
| `admin/packages/shared/src/voice.ts` | `formatVoiceForAi` / `formatImageForAi` / `formatVoiceDuration` |

Powiązane (kontekst, nie częścią tej specyfikacji, ale kluczowe dla zachowania):
`tools/circle-dm/services/thread-sync.ts` (enqueue podczas synca), `tools/circle-dm/routes/messages.ts`
(endpointy retry), `core/env.ts` (zmienne środowiskowe), `core/db/schema.ts` (schemat DB),
`core/ws/broker.ts` (`broadcast` po WebSocket), `index.ts` (start workerów przy bootcie serwera).

---

## 1. Przegląd architektury

Dwa niezależne workery in-process (bez zewnętrznej kolejki, bez crona systemowego). Kolejką jest
sama baza Postgres: wiersze ze statusem `pending`. Workery odpalają się przy starcie serwera HTTP
i co N ms pollują DB. Przetwarzanie jest **szeregowe** (celowo - wolumen mały, serial trzyma
footprint rate-limitów OpenAI na zerze).

Przepływ:

1. **Enqueue**: sync wątków (`thread-sync.ts`) przy insercie wiadomości wykrywa głosówkę
   (`voice_transcript_status = 'pending'`) i zdjęcia (insert wierszy do
   `message_image_descriptions`, default status `pending`).
2. **Worker**: co tick bierze batch max 5 wierszy `pending` z `attempts < 3`, przetwarza po kolei,
   zapisuje wynik/błąd, broadcastuje event WS do frontu.
3. **Konsumpcja**: transkrypty/opisy są wstrzykiwane do kontekstu AI (drafty) przez
   `formatVoiceForAi` / `formatImageForAi` oraz pokazywane w UI.
4. **Retry**: ręczne przez REST endpoint albo skrypt backfill (`--force-errors`).

## 2. Zmienne środowiskowe (`core/env.ts`, walidacja Zod)

| Zmienna | Typ / walidacja | Default | Uwagi |
|---|---|---|---|
| `OPENAI_API_KEY` | string, opcjonalna; pusty string traktowany jak brak (`undefined`) | brak | Bez klucza oba workery są wyłączone (warn w logu przy starcie, ciche pomijanie ticków). |
| `OPENAI_WHISPER_MODEL` | string | `whisper-1` | Model STT. |
| `OPENAI_VISION_MODEL` | string | `gpt-4o-mini` | Model vision. |
| `VOICE_TRANSCRIPT_INTERVAL_MS` | int, coerce z env, min 5000 | `20000` | Interwał ticka workera głosówek. |
| `IMAGE_DESCRIPTION_INTERVAL_MS` | int, coerce z env, min 5000 | `20000` | Interwał ticka workera opisów. |

## 3. Schemat DB

### 3.1. Enumy

```sql
CREATE TYPE voice_transcript_status AS ENUM ('pending', 'done', 'error');
CREATE TYPE image_description_status AS ENUM ('pending', 'done', 'error');
```

### 3.2. Kolumny voice w `dm_messages`

| Kolumna | Typ | Default | Opis |
|---|---|---|---|
| `voice_transcript` | `text` NULL | - | Tekst transkryptu (po `trim()`). |
| `voice_transcript_status` | `voice_transcript_status` NULL | NULL | NULL = wiadomość bez głosówki. `pending`/`done`/`error` tylko dla wiadomości z głosówką. |
| `voice_transcript_error` | `text` NULL | - | Ostatni błąd, max 500 znaków (`message.slice(0, 500)`). Czyszczony (NULL) przy sukcesie i przy retry. |
| `voice_transcript_attempts` | `integer` NOT NULL | `0` | Licznik prób. Inkrementowany **także przy sukcesie** (po sukcesie = prevAttempts+1). |
| `voice_duration_sec` | `integer` NULL | - | `Math.round(duration)` z odpowiedzi Whisper (`verbose_json`), NULL gdy brak. |
| `voice_transcribed_at` | `timestamptz` NULL | - | Czas zakończenia udanej transkrypcji (`now()` po stronie aplikacji). |

Index: `idx_messages_voice_status` na `(voice_transcript_status)`.

Uwaga: pole `language` z odpowiedzi Whisper **nie jest zapisywane do DB**, tylko logowane.

### 3.3. Tabela `message_image_descriptions`

```sql
CREATE TABLE message_image_descriptions (
  id               bigserial PRIMARY KEY,
  message_id       bigint NOT NULL REFERENCES dm_messages(id) ON DELETE CASCADE,
  attachment_index integer NOT NULL,
  attachment_url   text NOT NULL,
  description      text,
  status           image_description_status NOT NULL DEFAULT 'pending',
  error            text,
  attempts         integer NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now(),
  described_at     timestamptz,
  CONSTRAINT uniq_msg_image_idx UNIQUE (message_id, attachment_index)
);
CREATE INDEX idx_image_desc_status ON message_image_descriptions(status);
```

- `attachment_index` = indeks załącznika w **połączonej** liście zwracanej przez
  `extractAttachments` (najpierw `attachments`, potem `inline_attachments`), liczonej po
  wszystkich rodzajach (nie tylko obrazkach!). Czyli jeśli wiadomość ma [pdf, image], obrazek
  ma index 1.
- `attachment_url` = `fullUrl ?? url` z `NormalizedAttachment` (czyli `image_variants.original`
  jeśli istnieje, inaczej bazowy `url`).
- UNIQUE `(message_id, attachment_index)` daje idempotencję enqueue (insert z
  `ON CONFLICT DO NOTHING`).

## 4. Enqueue podczas synca (`thread-sync.ts`, funkcja upsertująca wiadomość)

Przy zapisie wiadomości z Circle:

1. `atts = extractAttachments(record.rich_text_body)`.
2. **Głosówka**: `hasVoice = atts.some(a => a.kind === 'audio' && a.voiceMessage)`.
   - Na INSERT: `voice_transcript_status = hasVoice ? 'pending' : NULL`.
   - Na CONFLICT (wiadomość już była): status **nie jest ruszany** (UPDATE ustawia tylko
     `body`, `rich_text_body`, `edited_at`). Konsekwencja: wiadomość, do której głosówka
     "doszła" przez edycję, nigdy nie dostanie `pending` z synca (quirk; w praktyce w Circle
     nie występuje).
3. **Zdjęcia**: dla każdego załącznika `kind === 'image'` insert wiersza
   `{ messageId, attachmentIndex: idx, attachmentUrl: a.fullUrl ?? a.url }` do
   `message_image_descriptions` z `ON CONFLICT DO NOTHING`. To leci **i przy insercie, i przy
   konflikcie** (re-sync wiadomości dosypie wiersze dla obrazków dodanych edycją; duplikaty
   odbija UNIQUE).

## 5. Worker transkrypcji głosówek (`voice-transcript-worker.ts`)

### 5.1. Stałe i stan

- `MAX_ATTEMPTS = 3`
- `BATCH_SIZE = 5`
- Stan modułu: `interval` (handle setInterval), `running` (mutex bool, chroni przed nakładaniem
  się ticków gdy przetwarzanie trwa dłużej niż interwał).

### 5.2. Cykl życia

- `startVoiceTranscriptWorker()` wywoływany raz przy bootcie serwera (`index.ts`):
  - jeśli `interval` już ustawiony: no-op (guard przed podwójnym startem),
  - jeśli brak `OPENAI_API_KEY`: log warn `OPENAI_API_KEY not set — voice transcripts disabled`,
    return (worker w ogóle nie startuje),
  - inaczej: log info, **natychmiastowy pierwszy tick** (fire-and-forget), potem
    `setInterval(tick, VOICE_TRANSCRIPT_INTERVAL_MS)`.
- `stopVoiceTranscriptWorker()`: clearInterval + zerowanie handle (graceful shutdown).

### 5.3. Tick

1. Jeśli `running === true`: return (tick w toku).
2. Jeśli brak `OPENAI_API_KEY`: return po cichu (przypadek dev).
3. `running = true`; w `finally` zawsze `running = false`.
4. SELECT:
   ```sql
   SELECT id, thread_id, rich_text_body, voice_transcript_attempts
   FROM dm_messages
   WHERE voice_transcript_status = 'pending'
     AND voice_transcript_attempts < 3
   ORDER BY id ASC
   LIMIT 5;
   ```
5. Dla każdego wiersza po kolei (szeregowo, await): `transcribeOne(...)`.
6. Błąd całego ticka (np. DB down): log error, bez crasha procesu.

### 5.4. `transcribeOne(messageId, threadId, richTextBody, prevAttempts)`

1. Re-ekstrakcja załączników z `rich_text_body` (świeżo, nie ufa wcześniejszemu enqueue):
   `voice = atts.find(a => a.kind === 'audio' && a.voiceMessage)` (pierwszy pasujący;
   wiadomość ma w praktyce max 1 głosówkę).
2. Brak głosówki po re-checku → terminalny error, żeby nie loopować:
   ```
   voice_transcript_status = 'error'
   voice_transcript_error  = 'no voice attachment found in rich_text_body'
   voice_transcript_attempts = prevAttempts + 1
   ```
   return.
3. Wywołanie `transcribeAudioFromUrl(voice.url, { filename: voice.filename })`
   (uwaga: dla audio przekazywany jest bazowy `url`, nie warianty - audio ich nie ma).
4. **Sukces**:
   ```
   voice_transcript          = text
   voice_transcript_status   = 'done'
   voice_transcript_error    = NULL
   voice_transcript_attempts = prevAttempts + 1
   voice_duration_sec        = durationSec  (int|NULL)
   voice_transcribed_at      = now()
   ```
   Log info + broadcast WS: `{ "type": "message:transcript_ready", "threadId": <int>, "messageId": <int> }`.
5. **Błąd** - klasyfikacja:
   - `SttConfigError` (brak klucza): log warn, **return bez żadnego UPDATE** - nie zużywa
     budżetu prób, wiersz zostaje `pending` z niezmienionym `attempts`.
   - `fatal` = `SttApiError` z `400 <= status < 500` i `status != 429` (np. 400 bad request,
     401, 404; 429 rate-limit NIE jest fatalny).
   - UPDATE:
     ```
     attempts = prevAttempts + 1
     voice_transcript_status   = (attempts >= 3 OR fatal) ? 'error' : 'pending'
     voice_transcript_error    = message.slice(0, 500)
     voice_transcript_attempts = attempts
     ```
   - Log warn `msg {id} attempt {n}/3 failed: {msg}` (+ ` (fatal, no retry)` gdy fatal).
   - Dodatkowy log gdy `SttFetchError` na ostatniej próbie: `signed URL likely expired`
     (podpisane URL-e Active Storage Circle wygasają).

Ważne niuanse retry:
- "Retry" to po prostu pozostawienie statusu `pending` - wiersz wróci w następnym ticku
  (brak backoffu, odstęp = interwał ticka, ~20 s).
- Pusty transkrypt z Whispera rzuca `SttApiError(..., 200)` - status 200 NIE spełnia warunku
  fatal, więc jest retry'owany do wyczerpania 3 prób.
- `MAX_ATTEMPTS` egzekwowany podwójnie: w WHERE selecta i w wyliczeniu `isLast`.

### 5.5. `retryTranscript(messageId)` (eksport, używany przez endpoint REST)

```
voice_transcript_status   = 'pending'
voice_transcript_error    = NULL
voice_transcript_attempts = 0
```
Pełny reset budżetu 3 prób. Nie czyści `voice_transcript` ani `voice_duration_sec`
(zostaną nadpisane przy sukcesie).

## 6. Worker opisów zdjęć (`image-description-worker.ts`)

Strukturalnie lustrzany do voice workera. Różnice:

- Te same stałe: `MAX_ATTEMPTS = 3`, `BATCH_SIZE = 5`; interwał `IMAGE_DESCRIPTION_INTERVAL_MS`.
- Tick SELECT (JOIN po `thread_id` potrzebny do broadcastu):
  ```sql
  SELECT d.id, d.message_id, d.attachment_url, d.attempts, m.thread_id
  FROM message_image_descriptions d
  INNER JOIN dm_messages m ON m.id = d.message_id
  WHERE d.status = 'pending' AND d.attempts < 3
  ORDER BY d.id ASC
  LIMIT 5;
  ```
- `describeOne`: bez re-ekstrakcji załączników - URL jest już utrwalony w `attachment_url`.
  Wywołuje `describeImageFromUrl(url)`.
- **Sukces**:
  ```
  description  = <opis>
  status       = 'done'
  error        = NULL
  attempts     = prevAttempts + 1
  described_at = now()
  ```
  Broadcast WS: `{ "type": "message:image_description_ready", "threadId": <int>, "messageId": <int> }`.
  Pole `tokensUsed` z odpowiedzi NIE jest zapisywane (tylko debug log w kliencie vision).
- **Błąd**: identyczna logika jak voice (`VisionConfigError` → return bez UPDATE;
  fatal = `VisionApiError` 4xx bez 429; `status = isLast||fatal ? 'error' : 'pending'`;
  `error = message.slice(0, 500)`; `attempts++`).
- Start/stop: `startImageDescriptionWorker()` / `stopImageDescriptionWorker()`, identyczny
  wzorzec (guard, warn bez klucza, natychmiastowy tick + setInterval).
- `retryImageDescription(descId)`: `status='pending', error=NULL, attempts=0` dla jednego
  wiersza opisu (po `id` wiersza opisu, nie wiadomości).

## 7. Klient STT (`openai-stt.ts`)

### 7.1. Stałe

- `MAX_BYTES = 25 * 1024 * 1024` (25 MB - twardy limit Whispera; głosówki Circle to AAC/mp4,
  parę minut ≈ 1-2 MB, więc limit to bezpiecznik).
- Endpoint: `POST https://api.openai.com/v1/audio/transcriptions`.

### 7.2. Klasy błędów

| Klasa | Kiedy |
|---|---|
| `SttConfigError` | brak `OPENAI_API_KEY` |
| `SttFetchError` | błąd sieci przy pobieraniu audio, HTTP != 2xx przy pobieraniu, puste body (0 bajtów), plik > 25 MB |
| `SttApiError(message, status)` | OpenAI zwrócił != 2xx (status z odpowiedzi) **lub** puste `text` w odpowiedzi (wtedy sztuczny `status = 200`) |

### 7.3. `transcribeAudioFromUrl(audioUrl, opts?)` krok po kroku

Sygnatura opts: `{ filename?: string; language?: string | null }`.
Zwraca `{ text: string; durationSec: number | null; language: string | null }`.

1. Brak klucza → `SttConfigError('OPENAI_API_KEY not set')`.
2. `fetch(audioUrl)` **bez żadnych nagłówków auth** - URL-e Active Storage Circle mają
   signed_id w sobie i są publicznie pobieralne.
   - wyjątek sieciowy → `SttFetchError('fetch {url}: {msg}')`
   - `!res.ok` → `SttFetchError('fetch {url}: HTTP {status}')`
3. Całe body do bufora w pamięci.
   - 0 bajtów → `SttFetchError('fetch {url}: empty body')`
   - `> MAX_BYTES` → `SttFetchError('audio too large for Whisper ({n} B > {max} B)')`
4. `filename = opts.filename ?? guessFilename(content-type odpowiedzi)`. Mapowanie
   `guessFilename(ct)` (substring match, kolejność istotna):
   - `ct` null → `voice.m4a`
   - zawiera `mp4` lub `m4a` → `voice.m4a`
   - zawiera `mpeg` → `voice.mp3`
   - zawiera `webm` → `voice.webm`
   - zawiera `ogg` → `voice.ogg`
   - zawiera `wav` → `voice.wav`
   - inaczej → `voice.m4a`
5. `contentType = nagłówek content-type odpowiedzi ?? 'application/octet-stream'`.
6. Request **multipart/form-data** do OpenAI:
   - nagłówek: `Authorization: Bearer {OPENAI_API_KEY}` (boundary ustawia biblioteka HTTP),
   - pola formularza:
     - `file`: bajty audio, z podanym `filename` i `contentType` części,
     - `model`: `OPENAI_WHISPER_MODEL` (default `whisper-1`),
     - `response_format`: `verbose_json`,
     - `language`: logika - jeśli `opts.language === undefined` → `'pl'` (twardy hint, bo
       prawie wszystkie głosówki BFC są po polsku, a autodetekcja na krótkich klipach
       halucynuje języki); jeśli caller poda `null` → pole **pominięte** (autodetekcja);
       jeśli poda string → ten string. Worker nie podaje `language`, więc zawsze `'pl'`.
7. **Timeout: brak jawnego** - używany jest natywny `fetch` Node bez AbortController
   (efektywnie ~300 s headers timeout undici). Patrz uwagi do portu.
8. `!res.ok` → `SttApiError('whisper {status}: {body.slice(0,400)}', status)`
   (body czytane defensywnie, fallback pusty string).
9. Parsowanie JSON (`verbose_json`):
   - `text`: musi być stringiem; `trim()`; pusty → `SttApiError('whisper returned empty text', 200)`,
   - `durationSec`: `Math.round(json.duration)` jeśli number, inaczej `null`,
   - `language`: string albo `null`.
10. Debug log: `transcribed {filename} ({n}B, ~{durationSec}s)`.

## 8. Klient Vision (`openai-vision.ts`)

### 8.1. Stałe

- Endpoint: `POST https://api.openai.com/v1/chat/completions` (Chat Completions, JSON).

### 8.2. Klasy błędów

| Klasa | Kiedy |
|---|---|
| `VisionConfigError` | brak `OPENAI_API_KEY` |
| `VisionFetchError` | wyjątek sieciowy przy fetchu do OpenAI (`'fetch openai: {msg}'`) |
| `VisionApiError(message, status)` | != 2xx (`'vision {status}: {body.slice(0,400)}'`) lub pusta treść odpowiedzi (sztuczny `status = 200`) |

### 8.3. System prompt (DOSŁOWNIE, port 1:1)

```
Opisujesz zdjęcia załączone w wiadomościach DM Be Free Club (community freelancerów AI).
Twoim zadaniem jest dać krótki opis (2-3 zdania) który pozwoli asystentowi AI zrozumieć co użytkownik wysłał.

Zasady:
- Pisz po polsku, krótko i konkretnie
- Jeśli zdjęcie ma tekst (screen czatu, faktura, mockup, screen z aplikacji) zacytuj go w cudzysłowach
- Jeśli to screen rozmowy zaznacz kto napisał co (np. "klient: 'cena?'", "autor: '599 zł'")
- Jeśli to memik/zdjęcie poglądowe opisz krótko temat
- Bez bełkotu typu "to zdjęcie przedstawia" - od razu do treści
- Bez emoji
- Bez myślników długich, używaj kropek
```

### 8.4. `describeImageFromUrl(imageUrl)` - request

Zwraca `{ description: string; tokensUsed: number | null }`.

Body JSON (dokładny kształt):

```json
{
  "model": "<OPENAI_VISION_MODEL, default gpt-4o-mini>",
  "max_tokens": 300,
  "messages": [
    { "role": "system", "content": "<SYSTEM_PROMPT j.w.>" },
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "Opisz to zdjęcie:" },
        { "type": "image_url", "image_url": { "url": "<imageUrl>", "detail": "low" } }
      ]
    }
  ]
}
```

Kluczowe decyzje:
- **Obrazek przekazywany jako URL**, nie base64 - OpenAI sam pobiera podpisany URL Circle.
  (Konsekwencja: wygasły signed URL = błąd po stronie OpenAI, zwykle 400 → fatal, bez retry.)
- `detail: 'low'` - 85 tokenów na obraz; high jest 2-3x droższy i niepotrzebny.
- `max_tokens: 300`.
- Nagłówki: `Authorization: Bearer {key}`, `Content-Type: application/json`.
- Timeout: brak jawnego (jak w STT).

Odpowiedź:
- `description = choices[0].message.content` (musi być string), `trim()`; pusta →
  `VisionApiError('vision returned empty description', 200)` (retryable, bo 200 nie jest fatal).
- `tokensUsed = usage.total_tokens` jeśli number, inaczej `null`; tylko do debug logu,
  nie trafia do DB.

## 9. `extractAttachments` (`circle/attachments.ts`)

Normalizuje `rich_text_body.attachments` + `rich_text_body.inline_attachments` Circle do
płaskiego kształtu. Kształt surowego załącznika Circle (zaobserwowany w realnej DB):

```json
{
  "url": "...", "filename": "...", "content_type": "...", "byte_size": 12345,
  "metadata": { "width": 1170, "height": 2532, "voice_message": true },
  "image_variants": { "thumbnail": "...", "small": "...", "medium": "...", "large": "...", "original": "..." },
  "type": "file", "is_downloadable": true
}
```

### 9.1. Wyjście: `NormalizedAttachment`

| Pole | Typ | Reguła |
|---|---|---|
| `kind` | `'image' \| 'video' \| 'audio' \| 'file'` | patrz 9.3 |
| `url` | `string` | surowe `url`; **załącznik bez stringowego `url` jest odrzucany** (cały element pomijany) |
| `thumbnailUrl` | `string \| null` | tylko dla `kind === 'image'`: `image_variants.medium ?? small ?? thumbnail ?? url`; dla pozostałych kindów zawsze `null` |
| `fullUrl` | `string \| null` | tylko dla `kind === 'image'`: `image_variants.original ?? url`; dla pozostałych `null` |
| `filename` | `string` | surowe `filename` jeśli string, inaczej fallback `'plik'` |
| `contentType` | `string` | surowe `content_type` jeśli string, inaczej `'application/octet-stream'` |
| `byteSize` | `number \| null` | `byte_size` jeśli number |
| `width` | `number \| null` | `metadata.width` jeśli number |
| `height` | `number \| null` | `metadata.height` jeśli number |
| `voiceMessage` | `boolean` | `metadata.voice_message === true` (ścisłe porównanie, wszystko inne = false) |

### 9.2. Reguły wariantów obrazka

`pickImageVariant(variants, key)`: zwraca `variants[key]` tylko jeśli to **niepusty string**,
inaczej `null`. Brak `image_variants` w ogóle → oba fallbacki lecą do `url`.

### 9.3. `kindOf(contentType, voiceMessage)` - kolejność sprawdzeń istotna

1. `voiceMessage === true` → `'audio'` (nadpisuje content_type; głosówki Circle mają
   `content_type` typu `audio/mp4` ale to flaga `voice_message` jest rozstrzygająca),
2. `contentType.startsWith('image/')` → `'image'`,
3. `contentType.startsWith('video/')` → `'video'`,
4. `contentType.startsWith('audio/')` → `'audio'`,
5. inaczej → `'file'`.

Uwaga: "głosówka" w całym systemie = `kind === 'audio' && voiceMessage === true`. Zwykły plik
audio (bez flagi) NIE jest transkrybowany.

### 9.4. `extractAttachments(richTextBody)` - envelope

1. `richTextBody` nie jest obiektem (null, string, liczba) → `[]`.
2. Zbiera w kolejności: najpierw `attachments`, potem `inline_attachments`. Każda lista
   ignorowana jeśli nie jest tablicą. Elementy nie-obiektowe lub bez `url` pomijane.
3. Zwracana płaska tablica - **indeks w tej tablicy = `attachment_index` w DB**.

## 10. Formatowanie do kontekstu AI (`packages/shared/src/voice.ts`)

Te stringi trafiają do historii wysyłanej do Claude przy generowaniu draftów. Port 1:1,
co do znaku (łącznie z polskimi znakami i cudzysłowami prostymi `"`).

### 10.1. `formatVoiceDuration(sec)`

- `sec === null` lub `sec < 0` → `"?"`
- `sec < 60` → `"{sec}s"` (np. `45s`)
- inaczej → `"{m}m{ss}s"`, gdzie `m = floor(sec/60)`, `ss = (sec % 60)` dopełnione zerem do
  2 cyfr (np. 75 → `1m15s`, 120 → `2m00s`).

### 10.2. `formatVoiceForAi(durationSec, status, transcript)`

`dur = formatVoiceDuration(durationSec)`. Kolejność warunków:

| Warunek | Wynik (dosłownie) |
|---|---|
| `status === 'done'` **i** transcript truthy (niepusty) | `[głosówka {dur}, transkrypt]: "{transcript}"` |
| `status === 'pending'` | `[głosówka {dur}, transkrypcja jeszcze nie gotowa]` |
| `status === 'error'` | `[głosówka {dur}, transkrypcja nieudana]` |
| pozostałe (status null, albo done z pustym transkryptem) | `[głosówka {dur}]` |

### 10.3. `formatImageForAi(status, description)`

| Warunek | Wynik (dosłownie) |
|---|---|
| `status === 'done'` **i** description truthy | `[zdjęcie]: "{description}"` |
| `status === 'pending'` | `[zdjęcie, opis jeszcze nie gotowy]` |
| `status === 'error'` | `[zdjęcie, opis nieudany]` |
| pozostałe | `[zdjęcie]` |

## 11. Endpointy REST retry (`routes/messages.ts`)

Montowane pod prefiksem messages w toolu circle-dm: `/api/circle-dm/messages`
(w `index.ts` tool jest pod `.route('/api/circle-dm', dmApp)`, a w `routes/index.ts`
`.route('/messages', messagesRoute)`).

### 11.1. `POST /:id/transcribe-retry`

- Param `id`: int > 0 (coerce).
- 404 `{ "error": "message not found" }` gdy brak wiadomości.
- 400 `{ "error": "message has no voice attachment" }` gdy `voice_transcript_status IS NULL`
  (czyli wiadomość nigdy nie była głosówką).
- Inaczej `retryTranscript(id)` (sekcja 5.5) i `{ "ok": true }`.

### 11.2. `POST /:id/image-descriptions/:descId/retry`

- Param `id` (messageId) i `descId`: int > 0.
- Walidacja: wiersz opisu o `id = descId` **i** `message_id = id` musi istnieć, inaczej 404
  `{ "error": "image description not found" }`.
- Inaczej `retryImageDescription(descId)` (sekcja 6) i `{ "ok": true }`.

## 12. Eventy WebSocket

Broadcast do wszystkich podłączonych klientów panelu (`core/ws/broker.ts`):

```json
{ "type": "message:transcript_ready",        "threadId": 123, "messageId": 456 }
{ "type": "message:image_description_ready", "threadId": 123, "messageId": 456 }
```

Front po otrzymaniu odświeża wątek (transkrypt/opis pojawia się bez reloadu).

## 13. Skrypty backfill

Oba: CLI one-shot (`pnpm tsx src/scripts/<plik>.ts`), **domyślnie dry-run**, flagi `--apply`
(zapis) i `--apply --force-errors` (dodatkowo retry wierszy w statusie error). Po zapisie nie
robią nic więcej - worker zbierze pending na następnym ticku. Exit code 0 przy sukcesie,
1 + stack trace przy błędzie.

### 13.1. `backfill-voice-transcripts.ts`

1. Filtr selecta:
   - bez `--force-errors`: `voice_transcript_status IS NULL`,
   - z `--force-errors`: `voice_transcript_status IS NULL OR voice_transcript_status = 'error'`.
   (Idempotencja: `pending`/`done` nigdy nie są ruszane.)
2. Dla każdego wiersza `extractAttachments(rich_text_body)`; kwalifikacja:
   `atts.some(a => a.kind === 'audio' && a.voiceMessage)`.
3. Log: `Scanned {n} rows, found {m} with voice attachments.` Dry-run wypisuje też
   `Sample ids:` (pierwsze 10).
4. `--apply`: UPDATE w batchach po 500 id (unikanie wielkich IN-list):
   `voice_transcript_status='pending', voice_transcript_error=NULL, voice_transcript_attempts=0`.

### 13.2. `backfill-image-descriptions.ts`

1. SELECT **wszystkich** `dm_messages` (id + rich_text_body), dla każdego
   `extractAttachments`; kandydaci = każdy załącznik `kind === 'image'` jako
   `{ messageId, attachmentIndex: idx, attachmentUrl: fullUrl ?? url }` (idx z połączonej
   listy, jak w sekcji 9.4).
2. SELECT wszystkich istniejących wierszy `message_image_descriptions`
   (messageId, attachmentIndex, status); budowa setów po kluczu `"{messageId}:{attachmentIndex}"`:
   - `seen` = wszystkie istniejące,
   - `errorKeys` = te ze statusem `error`.
3. `toInsert` = kandydaci spoza `seen`. `toRetry` = (tylko z `--force-errors`) kandydaci,
   których klucz jest w `errorKeys`.
4. Raport na stdout, dry-run kończy bez zapisu.
5. `--apply`:
   - insert `toInsert` w batchach po 500 z `ON CONFLICT DO NOTHING` (UNIQUE z sekcji 3.3
     daje pełną idempotencję, status leci z defaultu `pending`),
   - retry: SELECT id wierszy opisów `WHERE message_id IN (messageIds z toRetry)` i UPDATE
     w batchach po 500: `status='pending', error=NULL, attempts=0`.
   - **Quirk (świadomie odtworzyć albo świadomie naprawić)**: reset retry idzie po
     `message_id`, nie po parze `(message_id, attachment_index)`. Jeśli wiadomość ma 2 obrazki
     i tylko jeden w statusie error, reset cofnie do `pending` **oba** wiersze (także `done`,
     który wtedy zostanie ponownie opisany i nadpisany). Przy wolumenie BFC bez znaczenia
     kosztowego, ale to odchył od intencji.

## 14. Koszty / wolumeny (kontekst decyzji)

- STT: whisper-1, $0.006/min; głosówki BFC to zwykle < 2 min.
- Vision: gpt-4o-mini, `detail: low` = 85 tokenów obrazu + ~300 output; grosze na obraz.
- Batch 5 co 20 s = max 15 jednostek/min na worker; przy ~150 członkach to z ogromnym zapasem.

---

## Uwagi dla portu na FastAPI

1. **Workery jako asyncio tasks, nie cron.** Najbliższy odpowiednik: w lifespan FastAPI
   `asyncio.create_task(worker_loop())` z `while True: await tick(); await asyncio.sleep(interval)`.
   Mutex `running` jest wtedy zbędny (pętla z natury szeregowa) - ale jeśli zrobisz
   scheduler/cron, odtwórz guard, bo tick z 5 wywołaniami Whispera potrafi trwać dłużej niż
   20 s. Pamiętaj o natychmiastowym pierwszym ticku przed pierwszym sleepem i o anulowaniu
   taska przy shutdownie (odpowiednik `stop*Worker`).
2. **Kolejka = DB, zostaw tak.** Nie wprowadzaj Celery/RQ. Cała idempotencja siedzi w:
   statusach + `attempts < 3` w WHERE, UNIQUE `(message_id, attachment_index)`,
   `ON CONFLICT DO NOTHING`. Jeśli kiedyś będzie >1 instancja procesu, dodaj
   `FOR UPDATE SKIP LOCKED` do selecta ticka; w TS tego nie ma, bo proces jest jeden.
3. **Klasyfikacja błędów to rdzeń zachowania.** Odtwórz trzy klasy wyjątków per klient
   (Config/Fetch/Api z `status`) i regułę: fatal = `400 <= status < 500 and status != 429`;
   ConfigError = return bez UPDATE (nie pali prób); sztuczny `status=200` dla pustej
   odpowiedzi (retryable). `error` w DB zawsze ucinany do 500 znaków, body błędu OpenAI do
   400 znaków w message.
4. **Timeouty: w TS ich nie ma, w Pythonie ustaw jawnie.** `httpx.AsyncClient` ma default
   5 s - to ZA MAŁO (Whisper na 2-minutowym audio potrafi mielić kilkanaście sekund,
   pobranie audio też trwa). Sensownie: ~120 s na request OpenAI, ~60 s na pobranie audio.
   Timeout zmapuj na odpowiednik FetchError/ApiError (będzie retry'owany jako nie-fatal),
   nie na crash ticka.
5. **Multipart w httpx**: `files={'file': (filename, bytes, content_type)}`,
   `data={'model': ..., 'response_format': 'verbose_json', 'language': 'pl'}`. Pamiętaj o
   trójstanowej semantyce `language` (undefined→'pl', None→pomiń pole, string→przekaż).
   Nie używaj oficjalnego SDK OpenAI, jeśli chcesz 1:1 kontrolę nad błędami - goły httpx
   wystarcza (2 endpointy).
6. **Vision zostaje na Chat Completions** (`/v1/chat/completions`, content jako lista
   `text` + `image_url` z `detail: 'low'`). Nie przepisuj na Responses API bez decyzji -
   zmienia format requestu i odpowiedzi.
7. **`extractAttachments` portuj defensywnie jak w TS**: każde pole walidowane typem
   (`isinstance(x, str)` itd.), bo `rich_text_body` to surowy JSONB z Circle bez gwarancji
   kształtu. `voice_message === true` to ścisłe porównanie z `True` (nie truthiness).
   Zachowaj kolejność `attachments` przed `inline_attachments` - od niej zależy
   `attachment_index` w istniejących danych! Złamanie kolejności = rozjazd z wierszami
   już zapisanymi w `message_image_descriptions`.
8. **Stringi `formatVoiceForAi`/`formatImageForAi` przenieś znak w znak** (z `[głosówka`,
   `[zdjęcie`, cudzysłowami prostymi). Modele/prompty draftów mogły się już "nauczyć" tego
   formatu w promptach systemowych draftera; zmiana formatu = cicha regresja jakości draftów.
   System prompt vision (sekcja 8.3) też kopiuj dosłownie.
9. **Wygasające signed URL-e Circle.** `attachment_url` obrazka utrwalony przy syncu może
   wygasnąć zanim worker go przetworzy (szczególnie po backfillu starych wiadomości) - wtedy
   OpenAI zwróci 400 (fatal, status error). Dla głosówek worker re-ekstrahuje URL z
   `rich_text_body` przy każdej próbie, ale ten JSONB też trzyma stary podpisany URL, więc
   jedyny ratunek to re-sync wątku z Circle. Zachowanie TS: po prostu kończy w `error`
   z możliwością ręcznego retry. Odtwórz to samo, nie kombinuj z auto-refreshem.
10. **Broadcast WS po sukcesie jest częścią kontraktu z frontem** - typy
    `message:transcript_ready` / `message:image_description_ready` z `threadId` + `messageId`
    (int). Jeśli nowy backend ma inny mechanizm push, zachowaj nazwy i payload.
11. **Enum w Postgresie**: `voice_transcript_status` na `dm_messages` jest NULLable i NULL
    znosi informację "to nie głosówka" - endpoint retry używa tego do 400. Nie zamieniaj na
    NOT NULL z defaultem `'none'` bez przejrzenia wszystkich konsumentów (formatery traktują
    NULL jako "bez statusu" → goły `[głosówka {dur}]`).
12. **Skrypty backfill** odtwórz jako komendy CLI (np. `python -m scripts.backfill_voice
    --apply --force-errors` albo typer). Dry-run jako default to feature, nie przypadek -
    skrypty operują na produkcyjnych DM-ach realnych członków. Zdecyduj świadomie, czy
    przenosisz quirk z 13.2 (reset per messageId) czy poprawiasz na parę
    `(message_id, attachment_index)`; jeśli poprawiasz, odnotuj różnicę.
13. **`attempts` rośnie też przy sukcesie** (done z attempts=1 to norma). Jeśli będziesz
    liczył metryki "ile prób kosztował transkrypt", pamiętaj o tej semantyce.
