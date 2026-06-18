# Spec: schemat bazy danych landinga befreeclub.pl (Supabase)

Stan końcowy zbudowany z 10 migracji w `befreeclub/supabase/migrations/*.sql` (od 2026-03-08 do 2026-05-28) + `supabase/config.toml`. Projekt Supabase: `fshkdkvoyysphfrfvmni`. Spec ma wystarczyć do odtworzenia schematu w Postgresie (FastAPI monorepo) bez czytania oryginału.

Kluczowa obserwacja architektoniczna: **baza jest tu drugorzędna**. Źródłem prawdy o subskrypcjach jest Stripe (2 konta: bieżące + legacy), o członkostwie Circle, o newsletterze Sender.net. Tabele w Supabase to logi/leady/tokeny. Landing w ogóle nie używa Supabase Auth (zero tabel `profiles`, zero `auth.users` w grze). Frontend dotyka bazy bezpośrednio (anon key) w DOKŁADNIE jednym miejscu: insert do `contact_messages`.

---

## 1. Tabele (schema `public`) - stan końcowy

### 1.1 `contact_messages`

Wiadomości z formularza kontaktowego (`/kontakt`, `src/pages/Contact.tsx`).

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `name` | text | NOT NULL | - | |
| `email` | text | NOT NULL | - | bez walidacji w DB |
| `message` | text | NOT NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |

Indeksy: tylko PK. FK: brak.

RLS (włączone):
- **`Anyone can submit contact form`** - INSERT, role `anon, authenticated`, `WITH CHECK (true)`. **PUBLICZNY INSERT** - każdy z anon key może pisać.
- **`No public read access`** - SELECT, role `anon, authenticated`, `USING (false)` - odczyt zablokowany.
- UPDATE/DELETE: brak polityk = deny by default.
- Brak jawnej polityki service_role (service_role i tak ma BYPASSRLS).

Flow zapisu: frontend robi insert anon key + równolegle best-effort `supabase.functions.invoke("send-contact-email")` (Resend). Jak mail padnie, wiadomość i tak jest w DB. **Nikt programowo nie czyta tej tabeli** - odczyt tylko ręcznie przez dashboard Supabase.

### 1.2 `circle_members`

Log zaproszeń do społeczności Circle po opłaceniu. Najbardziej "żywa" tabela - pisze do niej 9 funkcji. To NIE jest źródło prawdy o członkostwie (tym jest Circle + Stripe), tylko rejestr "kogo zaprosiliśmy i czy się udało".

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `email` | text | NOT NULL | - | **UNIQUE** (klucz biznesowy, upserty po nim) |
| `circle_member_id` | text | NULL | - | id członka w Circle, uzupełniane przez `sync-circle-ids` |
| `invited_at` | timestamptz | NOT NULL | `now()` | |
| `active` | boolean | NOT NULL | `true` | false = usunięty z Circle (cleanup) albo invite nieudany |
| `stripe_source` | text | NOT NULL | `'current'` | `'current'` / `'legacy'` - z którego konta Stripe pochodzi sub (2 konta Stripe!) |
| `purchase_type` | text | NOT NULL | `'subscription'` | typ zakupu; dodane razem z `expires_at` (zakupy jednorazowe/Klarna z datą ważności) |
| `expires_at` | timestamptz | NULL | - | data wygaśnięcia dostępu dla zakupów nieodnawialnych |

Indeksy:
- UNIQUE index na `email` (z constraintu UNIQUE)
- `idx_circle_members_purchase_type` na `(purchase_type)`
- `idx_circle_members_expires_at` na `(expires_at)` **partial: `WHERE expires_at IS NOT NULL`**

RLS (włączone): jedna polityka **`Service role only`** FOR ALL TO `service_role` (PERMISSIVE). Zero dostępu anon/authenticated. Tabela w pełni prywatna.

### 1.3 `cancellation_tokens`

Kody weryfikacyjne do zamrażania subskrypcji. **UWAGA: tabela jest pół-martwa, patrz Prowizorki #1.**

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `email` | text | NOT NULL | - | |
| `token` | text | NOT NULL | - | 6-cyfrowy kod (wg UI w `Cancel.tsx`), BEZ unique |
| `expires_at` | timestamptz | NOT NULL | - | **ignorowane przez kod** (patrz Prowizorki #6) |
| `used` | boolean | NOT NULL | `false` | przy walidacji też częściowo ignorowane |
| `created_at` | timestamptz | NOT NULL | `now()` | faktyczna podstawa ważności (okno 60 min w kodzie) |

Indeksy: tylko PK (brak indeksu pod query `WHERE email=.. AND token=.. AND created_at >= ..`).

RLS (włączone): **`Service role full access`** FOR ALL TO `service_role`, PERMISSIVE. Historia: pierwotnie polityka była `AS RESTRICTIVE` bez żadnej PERMISSIVE (czyli formalnie nikt nie miał dostępu; działało tylko dzięki BYPASSRLS service_role), migracja 20260528 dropnęła i założyła PERMISSIVE.

### 1.4 `cancellation_reasons`

Log powodów rezygnacji/zamrożeń. Czytany przez panel admina (`admin-list-cancellations`).

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `email` | text | NOT NULL | - | |
| `reason` | text | NOT NULL | - | wolny tekst / wartości z UI (np. `no-time`) |
| `action` | text | NOT NULL | `'cancelled'` | wartości w kodzie: `'cancelled'`, `'frozen'`; bez CHECK |
| `freeze_days` | integer | NULL | - | tylko dla action='frozen' |
| `created_at` | timestamptz | NOT NULL | `now()` | |

Indeksy: tylko PK. RLS: identyczna saga jak `cancellation_tokens` - finalnie **`Service role full access`** PERMISSIVE FOR ALL TO `service_role`.

### 1.5 `ebook_orders`

Zamówienia ebooka (Stripe Checkout lub PaymentIntent/Blik).

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `email` | text | NOT NULL | - | |
| `stripe_session_id` | text | NULL | - | **UNIQUE** (nullable - flow PaymentIntent nie ma sesji) |
| `stripe_payment_intent_id` | text | NULL | - | |
| `amount_paid` | integer | NULL | - | grosze |
| `currency` | text | **NULL** | `'pln'` | nullable z defaultem |
| `status` | text | NOT NULL | `'pending'` | wartości w kodzie: `'pending'`, `'paid'`; bez CHECK |
| `wants_invoice` | boolean | **NULL** | `false` | nullable z defaultem |
| `nip` | text | NULL | - | do faktury |
| `invoice_name` | text | NULL | - | do faktury |
| `email_sent_at` | timestamptz | NULL | - | kiedy poszedł mail z linkiem do pobrania |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `paid_at` | timestamptz | NULL | - | |

Indeksy:
- `idx_ebook_orders_email` na `(email)`
- `idx_ebook_orders_session` na `(stripe_session_id)` (redundantny wobec UNIQUE indeksu z constraintu)

RLS: **`Service role only`** FOR ALL TO `service_role`.

### 1.6 `ebook_download_tokens`

Tokeny do pobrania PDF. **Jedyny FK w całej bazie.**

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `order_id` | uuid | NOT NULL | - | **FK → `ebook_orders(id)` ON DELETE CASCADE** |
| `token` | text | NOT NULL | - | **UNIQUE** |
| `email` | text | NOT NULL | - | denormalizacja z zamówienia |
| `expires_at` | timestamptz | NOT NULL | - | |
| `download_count` | integer | NOT NULL | `0` | |
| `max_downloads` | integer | NOT NULL | `10` | limit pobrań |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `last_downloaded_at` | timestamptz | NULL | - | |

Indeksy: `idx_ebook_tokens_token` na `(token)` - **redundantny** (token ma już UNIQUE index z constraintu). RLS: **`Service role only`** FOR ALL TO `service_role`.

### 1.7 `newsletter_subscribers`

**TABELA MARTWA** - patrz Prowizorki #2. Zero referencji w kodzie (frontend i edge functions). Zapisy newslettera żyją w Sender.net (grupy `SENDER_GROUP_IDS`), double opt-in robiony stateless HMAC-em (`NEWSLETTER_DOI_SECRET`), nie przez DB.

| Kolumna | Typ | Null | Default | Uwagi |
|---|---|---|---|---|
| `id` | uuid | NOT NULL | `gen_random_uuid()` | PK |
| `email` | text | NOT NULL | - | UNIQUE |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `source` | text | NULL | - | |

RLS (włączone):
- **`Anyone can subscribe`** - INSERT, `anon, authenticated`, `WITH CHECK (true)` - **PUBLICZNY INSERT** (otwarty mimo że nikt nie konsumuje).
- **`No public read`** - SELECT, `USING (false)`.

---

## 2. Storage

Bucket **`ebooks`**, `public = false` (prywatny). Polityka na `storage.objects`: `Service role manages ebooks bucket` FOR ALL TO `service_role` z `bucket_id = 'ebooks'`. Zero publicznych polityk.

- Upload PDF-ów: **brak kodu uploadu w repo** - pliki wgrywane ręcznie przez dashboard Supabase.
- Odczyt: `download-ebook` robi `createSignedUrl` (signed URL z limitem czasu) po walidacji tokenu z `ebook_download_tokens`.

W FastAPI: zastąpić katalogiem na dysku/S3 + endpoint streamujący po walidacji tokenu, bez signed URLs.

---

## 3. Rozszerzenia, funkcje SQL, triggery, enumy, pg_cron

- Rozszerzenia (migracja 20260308122724): `pg_cron` (schema `pg_catalog`), `pg_net` (schema `extensions`).
- **Funkcje SQL (PL/pgSQL): BRAK.** Triggery: BRAK (nawet `updated_at`). Enumy: BRAK - wszystkie statusy/typy to wolny `text` bez CHECK constraints (`stripe_source`, `purchase_type`, `status`, `action`).
- **pg_cron: ZERO jobów w migracjach.** Rozszerzenia włączono, ale żadnego `cron.schedule(...)` nie ma w repo. Jeśli joby istnieją, zostały założone ręcznie w SQL editorze dashboardu i żyją tylko w produkcyjnej bazie. **Przed migracją odpytać żywą bazę: `SELECT * FROM cron.job;`** Kandydaci na cron (funkcje bez żadnego callera w repo): `circle-cleanup`, `reconcile-klarna-checkouts`, `retry-circle-invites`, `sync-circle-ids`. Harmonogramy NIEZNANE z repo.

---

## 4. config.toml (verify_jwt)

`project_id = "fshkdkvoyysphfrfvmni"`. 17 funkcji z `verify_jwt = false`:
`create-checkout`, `confirm-subscription`, `send-contact-email`, `circle-cleanup`, `request-cancellation`, `confirm-cancellation`, `sync-circle-ids`, `pause-subscription`, `stripe-webhook`, `update-payment-method`, `admin-pause-subscription`, `admin-list-cancellations`, `admin-extend-subscription`, `admin-reinvite-circle`, `reconcile-klarna-checkouts`, `newsletter-subscribe`, `newsletter-confirm`.

W katalogu `supabase/functions/` jest **26** funkcji. 9 NIE ma wpisu w config (czyli domyślne `verify_jwt = true`): `admin-stripe-legacy-audit`, `confirm-ebook-purchase`, `confirm-klarna-checkout`, `create-ebook-checkout`, `create-ebook-payment-intent`, `create-klarna-checkout`, `download-ebook`, `retry-circle-invites`, `validate-promo`. Działa, bo frontend woła je przez `supabase.functions.invoke()`, które dokleja anon key jako Bearer JWT. Praktyczny wniosek do przepisania: **auth tych endpointów to de facto "znasz publiczny anon key"** - czyli wszystkie 26 funkcji jest publicznie wywoływalnych. Realna autoryzacja: `ADMIN_TOKEN` (funkcje admin-*), podpis Stripe (`STRIPE_WEBHOOK_SECRET` w stripe-webhook), HMAC tokeny (cancel/newsletter DOI), tokeny w DB (download-ebook).

---

## 5. Mapa: edge function ↔ tabele

Z grep po `supabase/functions/**/*.ts` (`.from("tabela")` + insert/update/upsert):

| Edge function | Czyta | Pisze | Caller |
|---|---|---|---|
| `stripe-webhook` | circle_members | circle_members (update, upsert) | Stripe (webhook) |
| `confirm-subscription` | - | circle_members (upsert) | frontend Success |
| `confirm-klarna-checkout` | circle_members | circle_members (update, upsert) | frontend `Success.tsx` |
| `reconcile-klarna-checkouts` | circle_members | circle_members (update, upsert) | **brak w repo** (cron/ręcznie) |
| `circle-cleanup` | circle_members | circle_members (update active=false) | **brak w repo** (cron/ręcznie) |
| `retry-circle-invites` | circle_members | circle_members (update) | **brak w repo** (cron/ręcznie) |
| `sync-circle-ids` | circle_members | circle_members (update circle_member_id) | **brak w repo** (cron/ręcznie) |
| `admin-pause-subscription` | circle_members | circle_members (update), cancellation_reasons (insert) | `Admin.tsx` (fetch + ADMIN_TOKEN) |
| `admin-reinvite-circle` | circle_members | circle_members (update/insert) | `Admin.tsx` |
| `admin-list-cancellations` | cancellation_reasons | - | `Admin.tsx` |
| `admin-extend-subscription` | - (tylko Stripe) | - | **brak w repo** (ręcznie/curl) |
| `admin-stripe-legacy-audit` | - (tylko Stripe legacy) | - | **brak w repo** (ręcznie/curl) |
| `request-cancellation` | - (Stripe lookup, Resend, HMAC) | - | frontend `Cancel.tsx` |
| `confirm-cancellation` | - (weryfikacja HMAC, Stripe) | cancellation_reasons (insert, action='cancelled') | frontend `CancelConfirm.tsx` (goły fetch) |
| `pause-subscription` | **cancellation_tokens** | cancellation_tokens (update used=true), cancellation_reasons (insert, action='frozen') | frontend `Cancel.tsx` |
| `create-ebook-checkout` | - | ebook_orders (insert pending) | frontend |
| `create-ebook-payment-intent` | - | ebook_orders (insert pending) | frontend |
| `confirm-ebook-purchase` | ebook_orders, ebook_download_tokens | ebook_orders (insert/update→paid, email_sent_at), ebook_download_tokens (insert) | frontend |
| `download-ebook` | ebook_download_tokens, storage `ebooks` (signed URL) | ebook_download_tokens (update download_count, last_downloaded_at) | frontend |
| `create-checkout` | - (tylko Stripe) | - | frontend (4 miejsca) |
| `create-klarna-checkout` | - (tylko Stripe) | - | frontend |
| `validate-promo` | - (tylko Stripe) | - | frontend (2 miejsca) |
| `update-payment-method` | - (tylko Stripe) | - | frontend |
| `send-contact-email` | - (tylko Resend) | - | frontend `Contact.tsx` (best-effort) |
| `newsletter-subscribe` | - (Resend + HMAC) | - | frontend (goły fetch, hardcoded URL) |
| `newsletter-confirm` | - (Sender.net API) | - | frontend `NewsletterConfirm.tsx` (goły fetch) |

Dostęp frontend → DB bezpośrednio (anon key): **tylko** `Contact.tsx` → INSERT `contact_messages`.

Tabele bez żadnego writera w repo: `newsletter_subscribers` (martwa), `cancellation_tokens` (brak INSERTU - patrz niżej).

---

## 6. Sekrety (nazwy env w edge functions)

Wbudowane Supabase: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.

Biznesowe: `STRIPE_SECRET_KEY` (bieżące konto), `STRIPE_LEGACY_SECRET_KEY` (stare konto - cancel/pause/cleanup/webhook szukają subów w OBU), `STRIPE_WEBHOOK_SECRET`, `CIRCLE_API_TOKEN`, `CIRCLE_COMMUNITY_ID`, `RESEND_API_KEY`, `SENDER_API_TOKEN`, `SENDER_GROUP_IDS`, `ADMIN_TOKEN` (autoryzacja funkcji admin-*), `CANCELLATION_DOI_SECRET` (HMAC linków anulowania), `NEWSLETTER_DOI_SECRET` (HMAC double opt-in), `CANCELLATION_FROM_EMAIL`, `NEWSLETTER_FROM_EMAIL`, `FRONTEND_URL`, `CONFIRM_URL_BASE`.

---

## 7. Prowizorki i długi

1. **`cancellation_tokens` bez producenta = flow zamrażania prawdopodobnie martwy.** `Cancel.tsx` każe userowi wpisać 6-cyfrowy kod i wysyła go do `pause-subscription`, które waliduje kod przeciwko tabeli `cancellation_tokens`. Ale ŻADEN kod w repo nie INSERT-uje do tej tabeli i nic nie wysyła userowi 6-cyfrowego kodu - `request-cancellation` wysyła magic link HMAC (stateless, bez DB). Albo flow freeze jest zepsuty na produkcji, albo na Supabase wisi STARSZA wersja `request-cancellation` (sprzed przejścia na HMAC), która jeszcze generuje kody. Zweryfikować w żywej bazie (`SELECT * FROM cancellation_tokens ORDER BY created_at DESC LIMIT 20`) zanim się to przepisze.
2. **`newsletter_subscribers` to martwa tabela.** Zero referencji w całym kodzie (poza wygenerowanymi typami). Newsletter żyje w Sender.net. A polityka publicznego INSERT-u jest otwarta - darmowy spam vector bez konsumenta. Przy przepisywaniu: wyrzucić albo świadomie wskrzesić jako lokalna kopia listy.
3. **pg_cron włączony, joby poza repo.** Harmonogramy (jeśli istnieją) żyją tylko w produkcyjnej bazie, nieodtwarzalne z kodu. 4 funkcje-sieroty (`circle-cleanup`, `reconcile-klarna-checkouts`, `retry-circle-invites`, `sync-circle-ids`) nie mają callera. Przed migracją: zrzucić `cron.job` z prod.
4. **Walidacja kodu w `pause-subscription` ignoruje `expires_at` i `used`.** Ważność = okno 60 min liczone w kodzie od `created_at`, z komentarzem że akceptuje kod nawet jeśli wcześniejsza nieudana próba oznaczyła go jako `used`. Kolumny `expires_at`/`used` są dekoracją.
5. **Zero enumów i CHECK constraints.** `stripe_source` ('current'/'legacy'), `purchase_type` ('subscription'/...), `status` ('pending'/'paid'), `action` ('cancelled'/'frozen') to goły text. Literówka w kodzie = cichy zepsuty stan. W nowym schemacie: enumy/CHECK.
6. **Jeden FK na całą bazę** (`ebook_download_tokens.order_id`). Cała reszta relacji jest po `email` (text), a normalizacja lowercase robiona jest tylko w kodzie funkcji - duplikaty z różną wielkością liter blokuje jedynie UNIQUE na `circle_members.email` / `newsletter_subscribers.email`; `cancellation_*` i `ebook_orders` mogą mieć dowolny zapis.
7. **Saga RESTRICTIVE policies.** `cancellation_tokens` i `cancellation_reasons` miały początkowo polityki `AS RESTRICTIVE` bez żadnej PERMISSIVE - formalnie zero dostępu dla kogokolwiek podlegającego RLS; działało wyłącznie dzięki BYPASSRLS roli service_role. Migracja 20260528 naprawiła na PERMISSIVE. Wniosek: RLS pisany na ślepo pod linter Lovable.
8. **9 z 26 funkcji nie ma wpisu w config.toml** (verify_jwt domyślnie true) - w tym cały flow ebooka i Klarny. Działa przypadkiem, bo `functions.invoke` dokleja anon JWT. Faktyczne "zabezpieczenie" wszystkich publicznych endpointów = znajomość publicznego anon key, czyli brak.
9. **Redundantne indeksy**: `idx_ebook_orders_session` i `idx_ebook_tokens_token` dublują UNIQUE indeksy z constraintów. Brak za to indeksu pod realne query `cancellation_tokens(email, token, created_at)` (tabela mała, nieistotne wydajnościowo, ale pokazuje że indeksy szły od szablonu, nie od zapytań).
10. **`wants_invoice` i `currency` w `ebook_orders` są nullable z defaultem** - możliwe trzy stany (true/false/NULL) tam, gdzie powinny być dwa.
11. **Hardcoded URL projektu Supabase** (`fshkdkvoyysphfrfvmni.supabase.co`) w 4 plikach frontu (NewsletterCTA, Newsletter, NewsletterConfirm, CancelConfirm) zamiast env - przy przepisywaniu wyłapać wszystkie miejsca.
12. **`contact_messages` nikt nie czyta programowo** - skrzynka pisz-i-zapomnij; jedyny realny kanał to równoległy mail z Resend (best-effort, błąd połykany). W nowym systemie: wiadomości do panelu admina.

---

## 8. Wskazówki do odtworzenia w FastAPI/Postgres

- Schemat do przeniesienia 1:1 to realnie 6 tabel (`newsletter_subscribers` skip), z czego `cancellation_tokens` tylko jeśli flow freeze ma zostać (lepiej: przepisać freeze na HMAC jak cancel i tabelę skasować).
- RLS znika - zastępuje je warstwa aplikacji. Trzeba zachować dwie publiczne ścieżki zapisu: formularz kontaktowy (i ewentualnie newsletter) jako zwykłe endpointy z rate-limitem, bo dziś chroni je wyłącznie "WITH CHECK (true)" + obscurity.
- Upserty po `email` (`circle_members`) wymagają `ON CONFLICT (email) DO UPDATE` + normalizacji lowercase NA POZIOMIE DB (np. `citext` albo `lower(email)` unique index), nie w kodzie jak teraz.
- pg_cron → scheduler w monolicie (APScheduler/cron kontenera); harmonogramy ustalić ze zrzutu `cron.job` z prod.
- Storage bucket `ebooks` → plik na dysku/S3, endpoint pobrania waliduje token (count < max_downloads, expires_at) i streamuje; signed URLs Supabase nie mają odpowiednika i nie są potrzebne.
