# Spec: lifecycle subskrybenta po zakupie (pauzy, anulowania, przedłużenia, Circle provisioning/deprovisioning)

Źródło: `befreeclub/supabase/functions/{admin-extend-subscription, admin-list-cancellations, admin-pause-subscription, admin-reinvite-circle, admin-stripe-legacy-audit, pause-subscription, request-cancellation, confirm-cancellation, circle-cleanup, retry-circle-invites, sync-circle-ids}/index.ts` + frontend `src/pages/{Cancel,CancelConfirm,Admin}.tsx` + migracje Supabase.

Zakres: wszystko co dzieje się z subskrybentem PO zakupie. Sam zakup (checkout, stripe-webhook, Klarna) jest w osobnym specu, tutaj tylko odwołania.

---

## 1. Kontekst i pojęcia

### Dwa konta Stripe

Wszędzie w lifecycle występują DWA konta Stripe:

| Konto | Klucz (env) | Co tam jest |
|---|---|---|
| `current` | `STRIPE_SECRET_KEY` | nowe subskrypcje (sprzedaż przez landing) |
| `legacy` | `STRIPE_LEGACY_SECRET_KEY` | stare subskrypcje sprzed migracji konta |

Każda operacja "znajdź subskrypcję po emailu" musi przeszukać OBA konta. Członek mógł migrować legacy → current (lub odwrotnie), więc usunięcie dostępu wolno zrobić tylko gdy ŻADNE konto nie ma aktywnej/opłaconej subskrypcji.

### Tabele DB (Supabase Postgres, schema public)

**`circle_members`** - rejestr członków Circle dodanych przez API (źródło prawdy dla cleanupu):

| Kolumna | Typ | Uwagi |
|---|---|---|
| `id` | uuid PK | `gen_random_uuid()` |
| `email` | text NOT NULL UNIQUE | zawsze lowercase (z konwencji, nie z constraintu!) |
| `circle_member_id` | text NULL | ID członka w Circle; NULL = invite się nie powiódł albo niezsynchronizowany legacy |
| `invited_at` | timestamptz NOT NULL default now() | |
| `active` | boolean NOT NULL default true | false = usunięty z Circle / nieudany invite |
| `stripe_source` | text NOT NULL default 'current' | `'current'` / `'legacy'` / `'manual'` (admin-reinvite) |
| `purchase_type` | text NOT NULL default 'subscription' | `'subscription'` / `'one_time'` (Klarna, roczny one-time) |
| `expires_at` | timestamptz NULL | tylko dla `one_time`: do kiedy dostęp |

Indeksy: `idx_circle_members_purchase_type`, `idx_circle_members_expires_at (WHERE expires_at IS NOT NULL)`.
RLS: polityka "Service role only" (FOR ALL TO service_role). Edge functions używają `SUPABASE_SERVICE_ROLE_KEY`.

Kto tworzy wpisy (provisioning, szczegóły w specu checkoutu): `confirm-subscription`, `stripe-webhook`, `confirm-klarna-checkout`, `reconcile-klarna-checkouts` (one_time, stripe_source='current'), `admin-reinvite-circle` (stripe_source='manual').

**`cancellation_reasons`** - audyt log akcji retencyjnych:

| Kolumna | Typ | Uwagi |
|---|---|---|
| `id` | uuid PK | |
| `email` | text NOT NULL | |
| `reason` | text NOT NULL | wartości z UI: `expensive`, `no-time`, `not-meeting-expectations`, `other`; plus `admin-pause` (sztuczny powód z admin-pause-subscription) |
| `action` | text NOT NULL default 'cancelled' | `'cancelled'` albo `'frozen'` |
| `freeze_days` | integer NULL | tylko przy `frozen` |
| `created_at` | timestamptz NOT NULL default now() | |

RLS: service_role only.

**`cancellation_tokens`** - kody weryfikacyjne 6-cyfrowe (MARTWA TABELA, patrz Prowizorki):

| Kolumna | Typ | Uwagi |
|---|---|---|
| `id` | uuid PK | |
| `email` | text NOT NULL | |
| `token` | text NOT NULL | 6-cyfrowy kod |
| `expires_at` | timestamptz NOT NULL | NIEUŻYWANE przez kod (sprawdza created_at + 60 min) |
| `used` | boolean NOT NULL default false | celowo IGNOROWANE przy weryfikacji (komentarz w kodzie: akceptuj mimo wcześniejszego nieudanego użycia) |
| `created_at` | timestamptz NOT NULL default now() | |

NIC obecnie nie INSERT-uje do tej tabeli (funkcja wysyłająca kody została usunięta). Czyta ją tylko `pause-subscription`.

### Sekrety / env (same nazwy)

`STRIPE_SECRET_KEY`, `STRIPE_LEGACY_SECRET_KEY`, `CIRCLE_API_TOKEN`, `CIRCLE_COMMUNITY_ID`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `RESEND_API_KEY`, `CANCELLATION_DOI_SECRET` (sekret HMAC magic linków), `ADMIN_TOKEN` (auth funkcji admin-*), `FRONTEND_URL` (default `https://befreeclub.pl`), `CANCELLATION_FROM_EMAIL` (default `Be Free Club <noreply@befreeclub.pl>`). Frontend: `VITE_SUPABASE_PROJECT_ID`.

### Circle API - używane endpointy

Baza: `https://app.circle.so`, auth `Authorization: Bearer ${CIRCLE_API_TOKEN}`.

- Invite: `POST /api/admin/v2/community_members` body `{ community_id: int, email, skip_invitation: bool }`. ID członka w odpowiedzi: `data.id` LUB `data.community_member.id` (kod sprawdza oba, niespójność API Circle).
- Remove (cleanup): `DELETE /api/admin/v2/community_members/{memberId}?community_id={id}`.
- Remove (admin-pause - INNY endpoint!): `DELETE /api/v2/community_members/{memberId}?community_id={id}` (wersja nie-admin; działa, ale niespójne z resztą; 404 traktowane jako sukces).
- List (sync): `GET /api/admin/v2/community_members?community_id={id}&per_page=50&page={n}`, paginacja po `has_next_page`, odpowiedź w `records[]`.

---

## 2. Jak funkcje są wyzwalane + auth (tabela zbiorcza)

`verify_jwt` z `supabase/config.toml`. Funkcje BEZ wpisu w config.toml mają default `verify_jwt = true` (wymagany nagłówek `Authorization: Bearer <anon key>`).

| Funkcja | verify_jwt | Dodatkowy auth | Wyzwalanie |
|---|---|---|---|
| `request-cancellation` | false | brak | UI `/anuluj` (Cancel.tsx), przycisk "Mimo wszystko chcę anulować" |
| `confirm-cancellation` | false | token HMAC w body | UI `/anuluj/potwierdz` (CancelConfirm.tsx), automatycznie w useEffect po wejściu z maila |
| `pause-subscription` | false | 6-cyfrowy kod z `cancellation_tokens` (martwy) | UI `/anuluj` krok "freeze" (Cancel.tsx) |
| `circle-cleanup` | false | **BRAK AUTH - publiczny!** | pg_cron + pg_net (extensions włączone migracją 2026-03-08; sam `cron.schedule` NIE jest w repo - skonfigurowany ręcznie w bazie przez Lovable/dashboard; harmonogram do odczytania z prod: `SELECT * FROM cron.job;`) |
| `sync-circle-ids` | false | **BRAK AUTH - publiczny!** | ręcznie (curl), one-off narzędzie migracyjne |
| `retry-circle-invites` | true (brak w config) | tylko anon key | ręcznie (curl) |
| `admin-pause-subscription` | false | header `x-admin-token` == env `ADMIN_TOKEN` | UI `/admin` (Admin.tsx) |
| `admin-list-cancellations` | false | `x-admin-token` | UI `/admin`, autoload historii |
| `admin-reinvite-circle` | false | `x-admin-token` | UI `/admin` |
| `admin-extend-subscription` | false | `x-admin-token` | tylko ręcznie (curl) - NIE ma UI |
| `admin-stripe-legacy-audit` | true (brak w config) | `x-admin-token` porównywany z **TOKENEM ZAHARDKODOWANYM W KODZIE** (index.ts:10), nie z env | tylko ręcznie (curl) |

Auth UI `/admin`: route publiczny, `window.prompt("Podaj token admina:")`, token w `sessionStorage` (klucz `bfc_admin_token`), wysyłany jako `x-admin-token`. Walidacja wyłącznie server-side (401 → czyści sessionStorage). Żadnych kont, ról, sesji.

Wzorzec odpowiedzi funkcji publicznych: błędy biznesowe zwracane ze statusem **200** i `{ error: "..." }` w body (bo `supabase.functions.invoke` przy non-2xx nie daje dostępu do body). Wyjątki (`catch`) → 500. Funkcje admin-*: 401/400/500 normalnie, bo wołane fetchem.

CORS wszędzie: `Access-Control-Allow-Origin: *`, obsługa OPTIONS preflight. Funkcje admin dopuszczają nagłówek `x-admin-token` w `Access-Control-Allow-Headers`.

---

## 3. Flow anulowania (double opt-in przez maila)

### Krok 1: UI `/anuluj` (Cancel.tsx)

Kroki UI: `email` → `reason` (retencja) → opcjonalnie `freeze` → `sent` / `frozen`.

1. User podaje email. **Walidacja tylko formatu po stronie UI** - żaden request nie leci (komentarz w kodzie: mail dopiero po "Mimo wszystko chcę anulować"). UI kłamie: "Wyślemy Ci kod weryfikacyjny" - nic nie jest wysyłane na tym etapie.
2. Ekran retencji: lista benefitów które straci, wybór powodu (radio): `expensive` (tip: "zachowasz cenę po powrocie"), `no-time` (pokazuje ofertę zamrożenia), `not-meeting-expectations` (tip: napisz na kontakt@befreeclub.pl), `other`. Duży przycisk "Zostaję w klubie!", mały szary link "Mimo wszystko chcę anulować".
3. Klik "Mimo wszystko..." → `POST request-cancellation { email, reason }`.

### Krok 2: `request-cancellation`

Wejście: `{ email: string, reason?: string }`.

1. Walidacja: brak emaila → 200 `{ error: "Email jest wymagany" }`. Email → trim + lowercase.
2. Wymagane env: `STRIPE_SECRET_KEY`, `STRIPE_LEGACY_SECRET_KEY`, `RESEND_API_KEY`, `CANCELLATION_DOI_SECRET` (brak któregoś → throw → 500).
3. Szuka aktywnej subskrypcji: najpierw current, potem legacy. Algorytm `findActiveSubscription`: `customers.list({email, limit:100})`, dla każdego customera `subscriptions.list({status:"all", limit:100})`, pierwsza sub o statusie ze zbioru CANCELLABLE = `{active, trialing, past_due, unpaid}`.
4. Brak → 200 `{ error: "Nie znaleziono aktywnej subskrypcji dla tego adresu email." }`. (Uwaga: enumeracja emaili - odpowiedź zdradza kto ma subskrypcję.)
5. Buduje token magic linka: payload `{ email, exp: Date.now()+60*60*1000 }`; jeśli `reason` jest stringiem 1..60 znaków, dokleja `reason` do payloadu (dłuższy powód jest po cichu gubiony). Token = `b64url(JSON) + "." + b64url(HMAC-SHA256(CANCELLATION_DOI_SECRET, b64url(JSON)))`. **Token nie jest zapisywany w DB - czysto stateless, brak one-time-use.**
6. Link: `${FRONTEND_URL}/anuluj/potwierdz?token=<urlencoded>`.
7. Mail przez Resend API (`POST https://api.resend.com/emails`, `Authorization: Bearer ${RESEND_API_KEY}`): from = `CANCELLATION_FROM_EMAIL` (default `Be Free Club <noreply@befreeclub.pl>`), to = [email], subject `"Potwierdź anulowanie subskrypcji Be Free Club"`, `reply_to: "kontakt@befreeclub.pl"`. HTML: ciemna karta BFC (tło #1a1b1f, karta #2c2d31, akcent #ECE183), przycisk "Potwierdzam anulowanie", informacja że link wygasa za 60 minut, dostęp do końca opłaconego okresu, fallback z gołym linkiem, stopka z linkiem do befreeclub.pl i IG @krystianbefree. URL escapowany HTML-owo.
8. Resend nie-OK → throw "Nie udało się wysłać emaila potwierdzającego" → 500. OK → 200 `{ success: true }`.

UI po sukcesie: ekran "Sprawdź skrzynkę" (link ważny 60 min, zerknij do spamu).

### Krok 3: UI `/anuluj/potwierdz` (CancelConfirm.tsx)

- Czyta `?token=` z URL. Brak → błąd.
- **Automatycznie w useEffect** robi `POST https://fshkdkvoyysphfrfvmni.supabase.co/functions/v1/confirm-cancellation` (URL z project-id ZAHARDKODOWANY w pliku, stała `CANCEL_FN_BASE`) body `{ token }`. Bez kliknięcia użytkownika - samo wejście na stronę anuluje. (Edge case: skaner antywirusowy/prefetcher wykonujący JS może anulować subskrypcję za użytkownika.)
- Sukces → redirect `/anuluj/anulowano?access_until=<ISO>` (CancelSuccess.tsx). Błąd → komunikat + przycisk "Zacznij od nowa" → `/anuluj`.

### Krok 4: `confirm-cancellation`

Wejście: `{ token: string }`.

1. Weryfikacja tokenu: split po `.` (dokładnie 2 części), HMAC-SHA256 z `CANCELLATION_DOI_SECRET`, porównanie constant-time, parse payloadu, wymaga `email` i `exp`, `Date.now() > exp` → odrzuć. Błąd → 200 `{ error: "Link wygasł lub jest nieprawidłowy. Wróć na stronę anulowania i wyślij nowy." }`.
2. Dla OBU kont Stripe `findAndCancelSubscriptions`: customers.list limit 100 → dla każdego subs.list status all limit 100 → dla KAŻDEJ sub w statusie CANCELLABLE (`active|trialing|past_due|unpaid`): jeśli nie ma jeszcze `cancel_at_period_end`, robi `subscriptions.update(id, { cancel_at_period_end: true })`. **Nie kasuje od razu - dostęp do końca okresu.** Anuluje WSZYSTKIE pasujące suby (user może mieć kilka).
3. Data końca dostępu (`endDate`): pierwsza znaleziona, źródło wg priorytetu: `sub.items.data[0].current_period_end` (Stripe API 2025-08-27.basil przeniósł pole na item) → legacy `sub.current_period_end` → `sub.cancel_at`. Konwersja na ISO.
4. 0 anulowanych → 200 `{ error: "Nie znaleziono aktywnej subskrypcji do anulowania." }` (token zużyty na nic, ale i tak był ważny 60 min wielokrotnego użytku - idempotentne).
5. Jeśli payload zawierał `reason` → INSERT do `cancellation_reasons { email, reason, action: 'cancelled' }`. Bez reason - brak wpisu audytowego!
6. Odpowiedź: `{ success: true, cancelled: <liczba>, access_until: <ISO|null> }`.

**Brak maila potwierdzającego po faktycznym anulowaniu.** Jedyny mail w całym flow to magic link. Po anulowaniu nie ma też żadnego usunięcia z Circle tu i teraz - tym zajmie się `circle-cleanup` po wygaśnięciu opłaconego okresu (patrz sekcja 6).

---

## 4. Flow zamrożenia (pauza self-service) - OBECNIE MARTWY

### UI

Na ekranie powodów, przy `no-time` pojawia się oferta "Zamroź subskrypcję" → krok `freeze`: pole na **6-cyfrowy kod weryfikacyjny** ("który wysłaliśmy na {email}" - kłamstwo, nic nie zostało wysłane) + wybór 14/30/60 dni → `POST pause-subscription { email, code, freeze_days }`.

### `pause-subscription`

Wejście: `{ email, code, freeze_days }`.

1. Walidacje (wszystko 200 + error po polsku): brak pól → "Email, kod i liczba dni sa wymagane"; `freeze_days` musi być z `[14, 30, 60]` → inaczej "Nieprawidlowa liczba dni".
2. Weryfikacja kodu: SELECT z `cancellation_tokens` gdzie `email` = znormalizowany, `token` = `code.trim()`, `created_at >= now() - 60 min`, sort desc, limit 1. Flagi `used` i kolumny `expires_at` NIE sprawdza (celowo - komentarz: poprzednia nieudana próba mogła oznaczyć kod jako użyty). Brak → "Nieprawidlowy lub wygasly kod weryfikacyjny."
3. **Ponieważ NIC nie wstawia wierszy do `cancellation_tokens`, flow zawsze kończy się tym błędem. Funkcja wysyłająca kody została usunięta z projektu, UI i backend zostały.**
4. Gdyby kod przeszedł: dla OBU kont `findAndPauseSubscription`: customers limit 100 → subs status all limit 100 → pierwsza sub w statusie PAUSABLE = `{active, trialing, past_due, unpaid}` → `subscriptions.update(id, { pause_collection: { behavior: "void", resumes_at: now + freeze_days*86400 } })`. Pauzuje pierwszą pasującą sub NA KAŻDYM koncie (możliwa podwójna pauza current+legacy).
5. Nic nie spauzowane → "Nie znaleziono aktywnej subskrypcji."
6. Sukces: UPDATE `cancellation_tokens.used = true` dla użytego wiersza, INSERT `cancellation_reasons { email, reason: 'no-time', action: 'frozen', freeze_days }`, odpowiedź `{ success: true, resumes_at: <ISO> }`.

Semantyka Stripe `pause_collection: behavior "void"`: subskrypcja zostaje w statusie `active`, faktury w okresie pauzy są void-owane (nie pobiera płatności), po `resumes_at` wznawia naliczanie automatycznie. Ważne: dla `circle-cleanup` spauzowany user wygląda jak aktywny (status `active` + `paused` w KEEP_STATUSES) → zachowuje Circle przez całą pauzę.

**Brak maila po zamrożeniu.** UI pokazuje datę wznowienia.

---

## 5. Funkcje admin-* (panel `/admin` + curl)

### `admin-pause-subscription` (UI: karta "Zamroź subskrypcję")

Wejście: `{ email, freeze_days, remove_from_circle? }`. Auth `x-admin-token` vs env `ADMIN_TOKEN`.

1. Walidacje (400): email wymagany; `freeze_days` liczba 1-365 (dowolna, nie tylko 14/30/60 jak self-service).
2. Pauzuje na OBU kontach Stripe, ale algorytm SŁABSZY niż self-service: `customers.list({limit: 1})` (tylko pierwszy customer o tym emailu) i `subscriptions.list({status: "active", limit: 1})` (tylko status `active` - pomija trialing/past_due/unpaid). `pause_collection: { behavior: "void", resumes_at: now + freeze_days*86400 }`.
3. `remove_from_circle` (default **false** w funkcji, ale UI admina wysyła default **true** - checkbox zaznaczony): jeśli true i cokolwiek spauzowano → `removeFromCircle`: szuka w `circle_members` po emailu z `active=true`; brak → "Member not found in DB"; jest, ale bez `circle_member_id` → tylko `active=false` w DB; jest z ID → `DELETE https://app.circle.so/api/v2/community_members/{id}?community_id=...` (endpoint NIE-admin v2, inny niż w cleanup), OK lub 404 → `active=false` w DB. Etykieta w UI obiecuje "przywrócenie automatyczne po wznowieniu Stripe" - dzieje się to pośrednio: po wznowieniu Stripe pobierze płatność → `stripe-webhook` (invoice.payment_succeeded) ponownie zaprosi (patrz spec checkoutu); nie ma dedykowanego mechanizmu.
4. Audyt: INSERT `cancellation_reasons { email, reason: 'admin-pause', action: 'frozen', freeze_days }`.
5. Odpowiedź 200: `{ success, email, freeze_days, customer_found, stripe: { current: PauseResult, legacy: PauseResult }, circle: {removed, reason} | null }` gdzie PauseResult = `{ account, found, paused, subscriptionId, customerId, resumesAt, error? }`. UI pokazuje surowy JSON w `<pre>`.

### `admin-extend-subscription` (TYLKO curl, brak UI)

Cel: przedłużyć komuś subskrypcję za darmo / przestawić pauzę. Wejście:

```json
{
  "subscription_id": "sub_... (opcjonalne)",
  "email": "... (fallback gdy brak sub_id)",
  "account": "current" | "legacy" (default "current"),
  "resumes_at": <unix ts> | null,
  "trial_end": <unix ts> | null,
  "add_months": <int>,
  "clear_pause": <bool>
}
```

1. Wybiera klucz Stripe wg `account` (jedno konto, nie oba).
2. Bez `subscription_id`: szuka po emailu - customers limit 10, subs status all limit 10, pierwsza sub o statusie != `canceled|incomplete_expired`. Brak → throw.
3. `add_months`: pobiera sub, baza = `items[0].current_period_end ?? trial_end`; jeśli baza w przeszłości, baza = teraz; dodaje N miesięcy przez `Date.setMonth` → wynik staje się `effectiveTrialEnd`.
4. **Mechanizm przedłużenia = trial**: jeśli jest `trial_end`/`add_months` → dwustopniowo: (a) `subscriptions.update({ proration_behavior: "none", pause_collection: "" /* hack: pusty string zdejmuje pauzę, rzutowany as unknown as null */, trial_end: effectiveTrialEnd })` - sub wchodzi w `trialing` do tej daty (czyli darmowy okres); (b) jeśli podano też `resumes_at` → ponownie nakłada pauzę `{ behavior: "void", resumes_at }`. Kolejność wymuszona: Stripe nie pozwala ustawić trial_end na spauzowanej sub.
5. Gdy tylko pauza: `clear_pause` → `pause_collection: ""`; albo `resumes_at` → nałóż pauzę. Nic nie podano → throw "Nothing to update".
6. Odpowiedź: `{ success, subscription_id, status, pause_collection, trial_end, trial_end_iso, current_period_end }`.

Brak wpisu audytowego do DB. Brak maila.

### `admin-reinvite-circle` (UI: karta "Dodaj z powrotem do Circle")

Use case z UI: "ktoś został wyrzucony, ale ma aktywną subskrypcję (np. zmienił plan na roczny)". Wejście: `{ email, skip_invitation? }` (UI default skip=true - "już ma konto w Circle", invite bez maila).

1. Circle invite: `POST /api/admin/v2/community_members { community_id, email, skip_invitation }`.
2. Sukces → upsert w `circle_members`: istnieje wiersz po emailu → UPDATE `{ active: true, circle_member_id, invited_at: now }`; brak → INSERT `{ email, circle_member_id, active: true, stripe_source: 'manual', purchase_type: 'subscription' }`. **Uwaga: nie ustawia expires_at, a stripe_source='manual' i tak przechodzi ścieżkę subskrypcyjną w cleanup - jeśli osoba nie ma żadnej sub w Stripe, cleanup ją wywali przy następnym przebiegu!** (chyba że jest na liście PROTECTED_EMAILS).
3. Odpowiedź: `{ success, email, circle_member_id, message }`.

### `admin-list-cancellations` (UI: karta "Historia")

POST bez body. Zwraca `{ rows: [...] }` - ostatnie 200 wierszy `cancellation_reasons` po `created_at` desc. UI: tabela Data/Email/Akcja/Powód/Dni, autoload po wpisaniu tokenu.

### `admin-stripe-legacy-audit` (TYLKO curl)

Jednorazowe narzędzie audytu konta legacy przed/w trakcie migracji. **Token admina ZAHARDKODOWANY w źródle (index.ts linia 10), nie z env - znany dług.** Dodatkowo funkcja nie ma wpisu w config.toml → `verify_jwt=true` → wymaga też anon key.

Działanie: na koncie legacy (`STRIPE_LEGACY_SECRET_KEY`) paginuje WSZYSTKIE subskrypcje w statusach `active, past_due, unpaid, trialing, paused` (po 100, expand customer + default_payment_method). Dla każdej buduje wiersz: id, status, customer_email, current_period_end (najpierw `items[0].current_period_end` - basil API, fallback stare pole), dane karty (brand/last4/exp), `card_expires_before_renewal` (koniec miesiąca ważności karty < data odnowienia), collection_method, cancel_at_period_end, amount_pln (`unit_amount/100`), interval.

Agregaty w odpowiedzi: `total_subscriptions`, `by_status`, `by_renewal_month` (klucz `YYYY-MM` z current_period_end), `estimated_mrr_pln` (year/12, month/1, inaczej /3 - zakłada kwartał), `risks: { no_default_payment_method (tylko active+charge_automatically), card_expires_before_renewal (active|past_due), send_invoice_method }`, `webhook_endpoints` (lista endpointów legacy; pole `recent_failures` zawsze 0 - niedokończone, kod pobiera events ale ich nie analizuje), `problem_rows: { no_default_pm, expiring_cards }` (pełne wiersze).

Inny pin wersji Stripe SDK niż reszta: `stripe@17.6.0?target=deno` (reszta `18.5.0`), apiVersion wszędzie `2025-08-27.basil`.

---

## 6. `circle-cleanup` - automatyczne wywalanie z Circle (deprovisioning)

**Jedyny automat w całym lifecycle.** Wyzwalany przez pg_cron + pg_net (extensions włączone migracją; definicja joba NIE w repo - siedzi w prod DB, harmonogram trzeba odczytać z `cron.job` przed migracją). Endpoint publiczny bez auth (verify_jwt=false, zero tokenów) - każdy może go odpalić; "bezpieczne" o tyle, że logika jest deterministyczna, ale to dług.

### Kogo sprawdza

TYLKO wiersze `circle_members` z `active = true` (czyli członków dodanych przez API i nieusuniętych). Członkowie dodani ręcznie w Circle, nieobecni w tej tabeli, są niewidzialni dla cleanupu - nigdy nie zostaną wywaleni automatycznie.

### Algorytm per członek

1. Brak emaila w wierszu → skip.
2. **Lista ochronna ZAHARDKODOWANA w kodzie** (`PROTECTED_EMAILS`): `sebastiangreczan@gmail.com`, `jakubkokoszczynski@gmail.com` - nigdy nie usuwani, niezależnie od Stripe (komentarz: legacy migracje / ręczny onboarding, gdzie detekcja subskrypcji zawodzi). Przy przepisaniu: przenieść do DB (flaga `protected` na członku).
3. Gałąź `purchase_type === 'one_time'` (Klarna / roczne jednorazowe): w ogóle NIE patrzy w Stripe. Brak `expires_at` → skip (zostaje na zawsze - dług). `expires_at <= now` → usuń. Inaczej zostaje.
4. Gałąź subskrypcyjna (wszystko inne, w tym `manual`): równolegle (`Promise.all`) sprawdza OBA konta Stripe `hasActiveSubscription(email)`. Usuwa tylko gdy NA ŻADNYM koncie nie ma aktywnej/opłaconej.

`hasActiveSubscription`: `customers.list({email, limit: 1})` (**tylko pierwszy customer - jeśli email ma kilku customerów, reszta niewidoczna; ryzyko fałszywego usunięcia**), `subscriptions.list({status: 'all', limit: 20})`. KEEP gdy jakakolwiek sub ma status z `{active, trialing, past_due, unpaid, incomplete, paused}` (zasada: stany odzyskiwalne nie wywalają - past_due/unpaid może się jeszcze opłacić). Dla `canceled`: jeśli `current_period_end` (items[0] lub legacy pole) w przyszłości → KEEP (opłacony okres po anulowaniu = dostęp do końca okresu). `incomplete_expired` i canceled po okresie → nie blokują usunięcia. Zero subów / zero customerów → false.

5. Usunięcie: tylko gdy `shouldRemove && circle_member_id` istnieje. `DELETE /api/admin/v2/community_members/{circle_member_id}?community_id=...`. Sukces → UPDATE `circle_members.active = false` (po **emailu**, nie po id). Niepowodzenie → loguje, wiersz zostaje active (retry przy następnym przebiegu). **Jeśli `shouldRemove` ale brak `circle_member_id` → nic się nie dzieje, wiersz wisi jako active w nieskończoność (zombie; po to powstał sync-circle-ids).**

To domyka anulowanie: `confirm-cancellation` ustawia `cancel_at_period_end`, user korzysta do końca okresu, po końcu okresu sub przechodzi w `canceled` z `current_period_end` w przeszłości → najbliższy przebieg cleanupu wywala z Circle. Analogicznie nieopłacone faktury: dopiero gdy Stripe ostatecznie skasuje sub (po wyczerpaniu ponowień), nie przy pierwszym past_due.

Odpowiedź: `{ success: true, checked: N, removed: M }`. Przetwarzanie sekwencyjne member-po-memberze (2 zapytania Stripe + ewentualny DELETE na głowę) - przy ~150 członkach kilkadziesiąt sekund; limit czasu edge function może być ryzykiem przy wzroście.

---

## 7. `retry-circle-invites` - ponawianie nieudanych zaproszeń

Ręczne narzędzie (curl; verify_jwt=true → wymaga anon key, poza tym brak auth).

1. SELECT `circle_members` gdzie `active = false` **OR** `circle_member_id IS NULL`.
2. Dla każdego: `POST /api/admin/v2/community_members { community_id, email, skip_invitation: false }` (z mailem zaproszeniowym). Sukces → UPDATE `{ circle_member_id, active: true }` po emailu. Porażka → zapis w wynikach, leci dalej.
3. Odpowiedź: `{ results: [{email, success, circleMemberId? , error?}] }`.

**NIEBEZPIECZNE narzędzie**: selekcja łapie nie tylko nieudane invite'y, ale też WSZYSTKICH celowo zdezaktywowanych (wywalonych przez cleanup, anulowane subskrypcje, adminowe usunięcia). Odpalenie go po cleanupie ponownie zaprasza byłych członków i ustawia im active=true. W przepisanej wersji musi być rozdzielony stan "invite failed" od "removed".

---

## 8. `sync-circle-ids` - uzupełnianie brakujących circle_member_id

Ręczne narzędzie one-off dla legacy (curl; verify_jwt=false, **brak auth - publiczny**).

1. Pobiera WSZYSTKICH członków z Circle (paginacja per_page=50, 200 ms sleep między stronami), buduje mapę lowercase(email) → id.
2. SELECT `circle_members` gdzie `stripe_source='legacy'` AND `active=true` AND `circle_member_id IS NULL` (czyli wpisy z migracji legacy zaimportowane bez ID).
3. Match po emailu → UPDATE `circle_member_id`. Brak w Circle → tylko log + lista w odpowiedzi.
4. Odpowiedź: `{ success, circleTotal, found, notFound, notFoundEmails: [...] }`.

Sens istnienia: bez `circle_member_id` cleanup nie umie nikogo usunąć (sekcja 6 pkt 5). Po pełnej migracji do nowego systemu funkcja może zostać zastąpiona jednorazowym skryptem.

---

## 9. Maile w całym lifecycle - podsumowanie

| Zdarzenie | Mail? | Kto wysyła |
|---|---|---|
| Prośba o anulowanie | TAK - magic link DOI (Resend, template inline w kodzie funkcji) | `request-cancellation` |
| Faktyczne anulowanie | NIE | - |
| Zamrożenie (self-service / admin) | NIE (Stripe może wysłać własne powiadomienia billingowe) | - |
| Przedłużenie (admin-extend) | NIE | - |
| Wywalenie z Circle | NIE od nas (Circle może coś wysłać po swojej stronie) | - |
| Re-invite do Circle | mail zaproszeniowy Circle, chyba że `skip_invitation: true` | Circle |

---

## 10. Prowizorki i długi

1. **Zahardkodowany ADMIN_TOKEN w `admin-stripe-legacy-audit/index.ts:10`** - 64-znakowy hex wpisany literalnie w źródle (publiczne repo Lovable!), zamiast `Deno.env.get("ADMIN_TOKEN")` jak w pozostałych admin-*. Znany dług. Przy przepisaniu: jeden mechanizm auth admina (sesje panelu admin na VPS), token unieważnić.
2. **Martwy flow zamrożenia self-service**: UI `/anuluj` (krok freeze) żąda 6-cyfrowego kodu "wysłanego mailem", ale funkcja generująca i wysyłająca kody do `cancellation_tokens` została usunięta - NIC nie robi INSERT do tej tabeli. `pause-subscription` zawsze zwróci "Nieprawidlowy lub wygasly kod weryfikacyjny". Każdy user wybierający "Zamroź" trafia w ślepy zaułek. Działa tylko pauza przez admina. Przy przepisaniu: albo magic-link HMAC jak przy anulowaniu, albo wyciąć krok kodu.
3. **UI kłamie na kroku email**: "Wyślemy Ci kod weryfikacyjny, aby potwierdzić Twoją tożsamość" - żaden kod nie jest wysyłany; tożsamość weryfikuje dopiero magic link przy anulowaniu.
4. **`circle-cleanup` i `sync-circle-ids` są publiczne bez żadnego auth** (verify_jwt=false, zero tokenów). Każdy zna URL → może triggerować masowe operacje.
5. **Harmonogram crona poza repo**: migracja włącza pg_cron+pg_net, ale `cron.schedule` skonfigurowano ręcznie w prod DB. Przed wyłączeniem Supabase odczytać `SELECT * FROM cron.job` (częstotliwość i ewentualne inne joby).
6. **`PROTECTED_EMAILS` zahardkodowane w `circle-cleanup`** (2 realne adresy członków). Powinno być flagą w DB.
7. **`retry-circle-invites` re-invituje celowo usuniętych**: warunek `active=false OR circle_member_id IS NULL` nie odróżnia "invite failed" od "removed by cleanup/cancellation". Odpalenie = przywrócenie byłych członków. Model danych przy przepisaniu musi mieć osobny status (np. enum: invited / invite_failed / active / removed).
8. **`admin-reinvite-circle` vs cleanup**: wpis `stripe_source='manual'` bez sub w Stripe zostanie wywalony przy następnym cleanupie - admin "dodaje z powrotem", cron usuwa. Działa tylko dla ludzi z realną subskrypcją lub z listy ochronnej.
9. **Dwa różne endpointy Circle do usuwania**: cleanup używa `/api/admin/v2/...`, admin-pause `/api/v2/...`. Niespójność; admin-pause traktuje 404 jako sukces, cleanup nie.
10. **Niespójne wyszukiwanie subskrypcji**: admin-pause patrzy tylko na pierwszego customera (limit 1) i tylko status `active`; self-service pause i cancel przeszukują 100 customerów i 4 statusy; cleanup pierwszego customera (limit 1, ryzyko fałszywego usunięcia przy duplikatach customerów); admin-extend 10 customerów. Przy przepisaniu: jedna wspólna funkcja "znajdź subskrypcje po emailu na obu kontach".
11. **`cancellation_tokens.expires_at` i `used` martwe**: weryfikacja używa `created_at + 60 min` i celowo ignoruje `used` (obejście bugu z oznaczaniem kodu przy nieudanej próbie). Schema niezgodna z logiką.
12. **`confirm-cancellation` bez reason nie zostawia śladu w DB** - anulowania z magic linka bez powodu (payload bez reason) nie trafiają do `cancellation_reasons`; historia w panelu admina jest niekompletna. Brak też tabeli z samymi anulowaniami (audyt tylko przez Stripe).
13. **Token magic linka wielokrotnego użytku** (stateless HMAC, brak rejestru zużycia) - skutki idempotentne, ale 60 min okna i auto-confirm przy wejściu na stronę (useEffect bez kliknięcia) = skanery linków w poczcie mogą anulować subskrypcję.
14. **Zahardkodowany URL projektu Supabase** w `CancelConfirm.tsx` (`CANCEL_FN_BASE` z project-id `fshkdkvoyysphfrfvmni`), reszta frontu używa `VITE_SUPABASE_PROJECT_ID`.
15. **Panel `/admin` na window.prompt + sessionStorage** - bez kont, bez ról, token jednym stringiem; brak UI dla `admin-extend-subscription` (tylko curl), brak rate-limitów na bruteforce tokenu.
16. **`admin-stripe-legacy-audit`: `recent_failures` zawsze 0** - kod pobiera events, ale ich nie analizuje (niedokończone); MRR liczy `/3` dla każdego interwału innego niż month/year (założenie kwartału).
17. **one_time bez `expires_at` nigdy nie wygasa** (cleanup skipuje) - zależy od poprawności zapisu przy zakupie.
18. **email UNIQUE w `circle_members` bez normalizacji w DB** - lowercase tylko konwencją w kodzie; wielkość liter w danych historycznych może rozjechać match (cleanup robi `eq("email", email)` wartością z DB, sync robi `toLowerCase()` przy porównaniu, ale UPDATE po oryginalnym emailu).
19. **Mieszane wersje Stripe SDK** (17.6.0 vs 18.5.0) i obejścia basil API (`current_period_end` raz z items[0], raz z suba, `pause_collection: "" as unknown as null`) - przy przepisaniu na oficjalny SDK Pythona ujednolicić i pilnować apiVersion `2025-08-27.basil`.
20. **Błędy biznesowe ze statusem 200** w funkcjach publicznych (obejście ograniczenia supabase-js). W FastAPI wrócić do normalnych kodów HTTP.

---

## 11. Wskazówki do przepisania (mapowanie na monolit FastAPI)

- Stan członkostwa przenieść do jednej tabeli `members` z jawnym statusem (active / cancelled_pending_period_end / frozen / removed / invite_failed / protected) zamiast wnioskowania ze Stripe przy każdym cleanupie - webhooki Stripe (customer.subscription.updated/deleted) mogą aktualizować status na bieżąco, cleanup staje się tanim sprzątaczem rozjazdów.
- Cron: APScheduler/cron w kontenerze albo systemd timer na VPS; endpoint cleanup tylko wewnętrzny (auth service-to-service).
- Magic link DOI: ten sam schemat HMAC da się przenieść 1:1 (payload {email, exp, reason}, sekret `CANCELLATION_DOI_SECRET`), ale dodać rejestr zużytych tokenów i potwierdzenie kliknięciem (przycisk na stronie, nie auto-POST z useEffect).
- Maile: szablon HTML magic linka wyciągnąć z kodu do plików szablonów; dodać mail potwierdzający anulowanie i zamrożenie (dziś brak).
- Panel admina (admin.befreeclub.pro) przejmuje: zamrażanie (z pełnym wyszukiwaniem subów jak self-service), re-invite, przedłużanie (dziś tylko curl), historię `cancellation_reasons`, listę ochronną, podgląd/ręczne uruchomienie cleanupu i retry (z rozdzielonymi statusami).
