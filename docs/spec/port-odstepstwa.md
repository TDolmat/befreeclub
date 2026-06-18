# Odstępstwa portu FastAPI od starego admina (Hono/TS)

Finalna lista różnic nowego backendu względem `admin/apps/server`. Trzy sekcje:
naprawione quirki oryginału, zachowane quirki, odstępstwa techniczne bez wpływu
na front. Każda naprawa z sekcji (a) była sprawdzona przeciw
`frontend-contract.md`: front jej nie zauważy albo zauważy wyłącznie pozytywnie.

Poza tym audyt portu znalazł 4 błędy samego portu (rozjazdy z oryginałem).
Zostały naprawione, więc NIE są już różnicami. Dla porządku: walidacja emaila
w loginie wróciła do regexa zod (luźny pattern przepuszczał np. `a..b@x.com`
i robił z tego 401 zamiast 400), klient Circle podąża za redirectami jak fetch
w Node, JWT manager nie trzyma już sesji DB przez cały request HTTP do Circle,
a schemat propozycji akcji asystenta używa StrictInt jak zod (string "42"
i true nie przechodzą jako id).

## (a) Naprawione quirki oryginału

1. **GET /settings nie wystawia `cachedAt`.** Oryginał wyciekał techniczne
   pole (epoch ms ze snapshotu cache 30 s). Front go nigdzie nie czyta
   (SettingsPage bierze tylko prompty i modele), typ w `api.ts` też go nie ma.
   Wpływ na Ciebie: zero, czystszy kontrakt.

2. **Asystent bierze model z ustawień.** Oryginał ignorował
   `app_settings.draft_model` i brał zawsze env `DRAFT_MODEL`. Teraz asystent
   słucha tego samego pola "Draft model" co generowanie draftów i compose
   (fallback na env, gdy w ustawieniach null). Wpływ: zmiana modelu w
   SettingsPage działa też na asystenta. Tak miało być od początku.

3. **Checkup done/delete scoped do wątku.** Oryginał ignorował `:id` wątku
   z URL i operował samym `checkupId`. Dało się odhaczyć lub skasować checkup
   cudzego wątku spreparowanym URL-em. Teraz WHERE ma też `thread_id`.
   Front zawsze woła parę z tego samego wątku, więc w normalnym użyciu nic
   się nie zmienia. Odpowiedź celowo zostaje `{ok:true}` nawet przy 0
   dopasowań, patrz sekcja (b).

4. **PATCH /accounts/:id zwraca 404 dla nieistniejącego konta.** Oryginał
   odpowiadał fałszywym `{ok:true}`. EditAccountDialog ma toast błędu, więc
   w wyścigu (konto skasowane w innej karcie) dostaniesz uczciwe "Nie udało
   się zapisać" zamiast fałszywego "Konto zaktualizowane".

5. **PATCH /threads/:id/status i /flag zwracają 404 dla nieistniejącego
   wątku.** Jak wyżej: oryginał `{ok:true}`. Oba mutationy w ThreadPage mają
   onError, a strona i tak nie wyrenderuje toolbara bez wątku (GET już
   404-uje). Edge case dostaje uczciwy toast zamiast cichego sukcesu.

6. **sort=next_checkup sortowany w SQL przed LIMIT.** Oryginał ciął listę
   limitem na sortowaniu "recent", a dopiero potem sortował w JS po dacie
   checkupa. Powyżej limitu (front woła 200) wątki z najbliższym checkupem
   wypadały z listy. Teraz correlated subquery `MIN(due_at)` pending
   checkupów, NULL-e na końcu, tiebreak jak "recent". Przy ~150 członkach
   wynik dziś identyczny bajt w bajt, powyżej 200 wątków poprawny.

## (b) Zachowane quirki (celowo 1:1)

- **`{ok:true}` przy 0 dopasowań** dla: PATCH checkup done, DELETE checkup,
  DELETE account, PATCH/DELETE feedbacku, DELETE dokumentu KB. Front nie ma
  tam onError, a refetch z onSuccess samoleczy nieaktualne UI. 404 byłoby
  cichą porażką bez odświeżenia listy.
- **Semafory per orchestrator** (draft, compose, format, assistant), więc
  realny limit równoległych Claude to 4 × `CLAUDE_MAX_CONCURRENT`.
- **WS `thread:new_messages` zawsze `newCount: 1`** (hardcoded w oryginale).
- **Brak auth na `/ws`.** Jak w oryginale, WS tylko broadcastuje eventy.
- **Brak timeoutu w `run_claude`.** Proces może działać dowolnie długo,
  jedyny kill to cancel od Ciebie.
- **Deep-probe health Claude'a cache'uje też porażki przez 1 h.**
- **`idx_checkups_pending_due` to zwykły btree** (nie partial).
- **`can_send_message` nadpisywane na true** przy każdym pełnym sync members.
- **Bulk-send ściśle sekwencyjny**, bez przerwania na błędzie (rate-friendly
  dla Circle).
- **Auto-revival "szeroki"**: każda nowa wiadomość przywraca done do inbox.
- **Polling**: top 50 wątków, 100 wiadomości na wątek.
- **GET /threads/:id/messages ma side-effecty**: sync z Circle, auto-revival,
  kasowanie placeholderów, kolejkowanie transkrypcji i opisów obrazków,
  WS `messages:loaded`.
- **Members bez TTL cache**: sync tylko przy pustym cache, pierwszy GET po
  wdrożeniu może chwilę trwać. `q` idzie do ILIKE bez escapowania `%`/`_`.
  `syncedCount` to liczba upsertów, nie nowych rekordów.
- **POST /assistant/turn zwraca placeholdery** (`assistantMessageId: 0`,
  `hasAction: false`), prawdziwe wartości lecą po WS `assistant:complete`.
  Dismiss nie sprawdza, czy wiadomość w ogóle ma akcję.
- **POST /drafts/:id/generate to fire-and-forget**: 200 od razu, błędy tła
  bez śladu w HTTP, wszystko po WS.
- **Upload KB multipart**: tytuł bez limitu 200 znaków (JSON-owy POST limit
  ma), komunikaty błędów dosłowne z oryginału.
- **PUT /settings zapisuje pola osobnymi upsertami** (nie-atomowo), w stałej
  kolejności.

## (c) Odstępstwa techniczne (nie-load-bearing)

- **Body błędu walidacji**: 400 z `{"error": "Invalid request"}` zamiast
  zodowego dumpa issues. Status 400 i klucz `error` zachowane, front czyta
  tylko je. Nienumeryczne `:id` w ścieżce daje 400 jak w oryginale.
- **Timeout klienta Circle**: httpx rzuca własny wyjątek timeout, w TS to był
  AbortError. Inna klasa, ten sam efekt (błąd 5xx z komunikatem).
- **OpenAI STT/Vision mają jawne timeouty httpx** (Node fetch nie miał
  żadnego). Timeout lub błąd sieci to SttFetchError/VisionFetchError,
  nie-fatalne, worker retry'uje.
- **Exit code zabitego procesu Claude**: Node dawał null, Python daje ujemny
  returncode mapowany na None. Logika cancel identyczna.
- **Logi**: format odtwarza styl TS (timestamp ISO, scope), ale to Python
  logging. Treści pojedynczych linii mogą się różnić, komunikaty błędów
  widoczne w HTTP/WS są kopiowane 1:1.
- **DB ma nowe nazwy kolumn** (`account_id`, `user_id`), JSON trzyma stare
  (`adminAccountId`, `authAccountId`). Mapują DTO przez jawne aliasy.
- **Serializacja**: `numeric` z PG idzie w JSON jako string (jak postgres-js),
  daty jako `toISOString` z `Z`. Wyjątek 1:1 z oryginałem: `costUsd` w WS
  `draft:complete` to number.
- **Serwowanie SPA**: kod jest (parytet), ale w deployu `WEB_DIST_PATH` pusty,
  front serwuje osobny nginx (decyzja z `ARCHITEKTURA.md`).
- **Dev mode**: fake auth (`dev@local`, id 0) i lazy INSERT konta id=0 przy
  feedbacku/asystencie, żeby FK nie wybuchał. Tylko `NODE_ENV != production`.
