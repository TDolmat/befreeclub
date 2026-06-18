# Spec: Ebook, Newsletter, Formularz kontaktowy (landing befreeclub.pl)

Zakres: edge functions `create-ebook-checkout`, `create-ebook-payment-intent`, `confirm-ebook-purchase`, `download-ebook`, `newsletter-subscribe`, `newsletter-confirm`, `send-contact-email` plus frontend, który je woła. Spec wystarcza do przepisania na FastAPI bez czytania oryginału.

Źródła (oryginał):
- `befreeclub/supabase/functions/{create-ebook-checkout,create-ebook-payment-intent,confirm-ebook-purchase,download-ebook,newsletter-subscribe,newsletter-confirm,send-contact-email}/index.ts`
- Frontend: `src/pages/{Ebook,EbookSuccess,EbookDownload,Newsletter,NewsletterConfirm,NewsletterWelcome,Contact}.tsx`, `src/components/landing/NewsletterCTA.tsx`, `src/components/ebook/EbookCheckoutModal.tsx` (martwy)
- Migracje: `supabase/migrations/20260505191654_*.sql` (ebook), `20260507042823_*.sql` (newsletter_subscribers), `20260308082120_*.sql` (contact_messages)

---

## 1. EBOOK "Na swoich zasadach jako freelancer"

### 1.1 Produkt i ceny

- Jeden produkt: ebook PDF, cena **249.00 PLN = 24900 groszy**, waluta `pln`, jednorazowa płatność.
- Cena jest **zahardkodowana w dwóch miejscach niezależnie**:
  - `create-ebook-payment-intent`: stała `AMOUNT = 24900` (to jest źródło prawdy dla aktywnego flow),
  - `create-ebook-checkout`: Stripe Price ID `price_1TToiWDlsrz5Z08F1DBx2KTQ` (stała `EBOOK_PRICE_ID`) + osobno hardcode `amount_paid: 24900` przy insercie do DB.
- Frontend też ma hardcode "249 zł" w copy (Ebook.tsx, EbookCheckoutModal.tsx).
- Stripe: klucz `STRIPE_SECRET_KEY` (konto "current", to samo co subskrypcje klubu), apiVersion `2025-08-27.basil`. Frontend ma zahardkodowany publishable key `pk_live_51SsMPe...` w `Ebook.tsx` (nie env).

### 1.2 Dwa równoległe flow zakupu (jeden martwy!)

**Flow A - AKTYWNY: embedded PaymentIntent na stronie `/ebook`** (Stripe Payment Element + Express Checkout).

**Flow B - MARTWY: Stripe Checkout Session przez modal** (`EbookCheckoutModal.tsx` + `create-ebook-checkout`). Modal **nie jest nigdzie importowany** w aplikacji. Funkcja `create-ebook-checkout` jest wciąż wdrożona i działa, ale nic jej nie woła. Przy przepisywaniu: flow B można pominąć albo świadomie zostawić jako alternatywę; opisuję oba, bo `confirm-ebook-purchase` obsługuje obie ścieżki.

#### Flow A krok po kroku (aktywny)

1. Wejście na `/ebook` (Ebook.tsx): `useEffect` od razu woła `create-ebook-payment-intent` (przez `supabase.functions.invoke`, body `{}`).
2. `create-ebook-payment-intent`:
   - Tworzy w Stripe PaymentIntent: `amount: 24900`, `currency: "pln"`, `payment_method_types: ["card", "blik"]`, `description: "Ebook: Na swoich zasadach jako freelancer"`, `metadata: { product: "ebook" }`.
   - Insertuje do `ebook_orders` wiersz pending z **placeholderowym emailem** `pending+{pi_id}@befreeclub.pl` (email jest NOT NULL, a na tym etapie go nie znamy), `stripe_payment_intent_id`, `status: "pending"`, `amount_paid: 24900`.
   - Zwraca `{ clientSecret, paymentIntentId }`.
3. Frontend renderuje `<Elements>` z `clientSecret` + dark theme appearance. Dwa warianty płatności:
   - `ExpressCheckoutElement` (Apple Pay / Google Pay, `emailRequired: true`), w zwykłym `PaymentElement` wallety wyłączone (`wallets: { applePay: "never", googlePay: "never" }`),
   - formularz: email (required), checkbox "Chcę fakturę VAT (firma)" -> pola Nazwa firmy + NIP (walidacja klient: 10 cyfr po zdjęciu spacji/myślników, nazwa wymagana).
4. `stripe.confirmPayment` z `redirect: "if_required"`, `receipt_email`, `payment_method_data.billing_details.email`. `return_url`:
   `{origin}/ebook/sukces?payment_intent_id={pi}&email={email}[&invoice=1&nip={nip}&name={invoiceName}]`
   - **Dane fakturowe (NIP, nazwa firmy) i email lecą w query stringu URL-a** - to jedyny kanał, którym trafiają do backendu w flow A.
   - BLIK/przekierowania: Stripe sam dokleja `payment_intent=` do return_url, więc EbookSuccess czyta `payment_intent_id` LUB `payment_intent`.
   - Przy płatności bez redirectu (karta) frontend nawiguję ręcznie na ten sam URL (`goToSuccess()`).
   - Przy błędzie płatności: toast + `onRefreshNeeded()` = **tworzy NOWY PaymentIntent** (i nowy pending wiersz w DB).
5. `/ebook/sukces` (EbookSuccess.tsx): woła `confirm-ebook-purchase` z body:
   ```json
   {
     "sessionId": "<cs_... lub undefined>",
     "paymentIntentId": "<pi_... lub undefined>",
     "email": "<z query param>",
     "wantInvoice": true/false,
     "nip": "<z query param>",
     "invoiceName": "<z query param>"
   }
   ```
   Retry: do **8 prób co 2 sekundy** przy błędzie (status "pending" w UI: "Potwierdzamy płatność..."). Sukces gdy w odpowiedzi jest `downloadUrl` -> pokazuje przycisk "Pobierz ebooka teraz" + info, że mail poszedł na adres.

#### Flow B krok po kroku (martwy, ale funkcja istnieje)

1. `EbookCheckoutModal`: email + opcjonalna faktura (NIP/nazwa). Woła `create-ebook-checkout` body `{ email, wantInvoice, nip, invoiceName }`.
2. `create-ebook-checkout`:
   - Walidacja: email regex `/^[^\s@]+@[^\s@]+\.[^\s@]+$/` (400 "Nieprawidłowy email"), normalizacja trim+lowercase; jeśli `wantInvoice` to NIP musi być 10 cyfr po zdjęciu `[\s-]` (400 "Nieprawidłowy NIP (10 cyfr)").
   - Tworzy Checkout Session: `mode: "payment"`, `payment_method_types: ["card","blik"]`, `line_items: [{price: EBOOK_PRICE_ID, quantity: 1}]`, `customer_email`, `success_url: {origin}/ebook/sukces?session_id={CHECKOUT_SESSION_ID}`, `cancel_url: {origin}/ebook`, `locale: "pl"`, metadata sesji `{product:"ebook", wants_invoice, nip, invoice_name}` oraz metadata payment_intenta `{product:"ebook", email}`. Origin z nagłówka `origin`, fallback `https://befreeclub.pl`.
   - Insert do `ebook_orders`: email, `stripe_session_id`, status pending, dane fakturowe, `amount_paid: 24900` (hardcode, nie z price'a).
   - Zwraca `{ url, sessionId }`, frontend robi `window.location.href = url`.
3. Powrót na `/ebook/sukces?session_id=...` -> ten sam `confirm-ebook-purchase`, ścieżka sessionId.

### 1.3 `confirm-ebook-purchase` - potwierdzenie i fulfillment

Wejście: `sessionId` LUB `paymentIntentId` (400 gdy brak obu). Dodatkowo opcjonalnie `email`, `wantInvoice`, `nip`, `invoiceName` od klienta.

Logika:
1. **Ścieżka sessionId**: `stripe.checkout.sessions.retrieve(sessionId)`. Jeśli `payment_status !== "paid"` -> 409 `{error: "Payment status: ...", status}` (frontend wtedy retry'uje). Email z `session.customer_email || session.customer_details?.email` (trim+lowercase), amount z `session.amount_total`, payment_intent_id z sesji.
2. **Ścieżka paymentIntentId**: `stripe.paymentIntents.retrieve`. Jeśli `status !== "succeeded"` -> 409. Email: **`emailFromClient || pi.receipt_email`** - czyli email z query stringu klienta ma pierwszeństwo. Amount z `pi.amount`.
3. Brak emaila -> throw "Missing email" (500).
4. Szuka orderu w `ebook_orders` po `stripe_session_id`, potem po `stripe_payment_intent_id` (`maybeSingle`).
5. Jeśli order nie istnieje -> insert nowego od razu ze statusem `paid` (z danymi fakturowymi od klienta). Jeśli istnieje i (`status !== "paid"` lub email się różni - czyli też nadpisanie placeholdera `pending+pi_...@befreeclub.pl`) -> update: `status: "paid"`, `email`, `paid_at: now`, `stripe_payment_intent_id`, ew. `stripe_session_id`, ew. dane fakturowe (NIP czyszczony z `[\s-]`).
6. **Token pobrania**: szuka w `ebook_download_tokens` istniejącego ważnego tokenu (`order_id`, `expires_at > now`, najnowszy). Jak nie ma: generuje 32 losowe bajty -> hex (64 znaki), `expires_at = now + 30 dni`, insert `{order_id, token, email, expires_at}` (download_count 0, max_downloads 10 z defaultów DB).
7. `downloadUrl = {FRONTEND_URL||https://befreeclub.pl}/ebook/pobierz?token={token}`.
8. **Mail tylko raz**: jeśli `order.email_sent_at IS NULL`, wysyła przez **Resend API** (`POST https://api.resend.com/emails`, Bearer `RESEND_API_KEY`):
   - from: `Be Free Club <noreply@befreeclub.pl>` (hardcode),
   - subject: `Twój ebook jest gotowy do pobrania 📘`,
   - HTML inline (jasny motyw, przycisk "Pobierz ebooka (PDF)" -> downloadUrl, info "link aktywny 30 dni, do 10 pobrań", kontakt awaryjny `krystian@befreeclub.pl`).
   - Po sukcesie update `email_sent_at = now`. Jak Resend padnie: tylko log, response i tak success (mail się nie ponowi automatycznie... a właściwie ponowi przy następnym wywołaniu confirm, bo `email_sent_at` zostało puste).
9. Response 200: `{ success: true, email, downloadUrl, token }` - **token i link zwracane też wprost do przeglądarki**, mail jest tylko kopią.

Idempotencja: wielokrotne wywołanie z tym samym sessionId/PI jest bezpieczne (reuse tokenu, mail raz). Brak weryfikacji, że wołający to kupujący - wystarczy znać `pi_...`/`cs_...` id.

### 1.4 `download-ebook` - pobranie pliku

Plik: **prywatny bucket Supabase Storage `ebooks`**, jeden obiekt o stałej ścieżce `na-swoich-zasadach.pdf` (stała `EBOOK_FILE_PATH`). Wgrywany ręcznie do bucketa, w repo nie ma kodu uploadu. RLS na buckecie: tylko service_role.

Flow strony `/ebook/pobierz?token=...` (EbookDownload.tsx): przycisk "Pobierz PDF" -> invoke `download-ebook` body `{token}`:
1. Brak/nie-string token -> 400 "Brak tokenu".
2. Lookup w `ebook_download_tokens` po `token` -> brak: 404 "Nieprawidłowy link".
3. `expires_at < now` -> 410 "Link wygasł. Napisz na krystian@befreeclub.pl po nowy."
4. `download_count >= max_downloads` -> 429 "Limit pobrań wyczerpany. Napisz na krystian@befreeclub.pl."
5. `createSignedUrl("na-swoich-zasadach.pdf", 300, {download: "Na-swoich-zasadach-jako-freelancer.pdf"})` - **signed URL ważny 5 minut**, z wymuszoną nazwą pliku do pobrania. Błąd podpisu -> 500 "Plik tymczasowo niedostępny."
6. Update tokenu: `download_count + 1`, `last_downloaded_at = now` (odczyt + zapis, **nieatomowe**).
7. Response: `{ url: signedUrl, remainingDownloads: max - count - 1 }`. Frontend robi `window.location.href = url`.

Ochrona pliku w skrócie: prywatny bucket + token 64 hex (30 dni, 10 pobrań) + krótki signed URL. Limit liczony per wygenerowanie signed URL-a, nie per faktyczne pobranie.

### 1.5 Tabele DB (ebook)

```sql
ebook_orders (
  id UUID PK DEFAULT gen_random_uuid(),
  email TEXT NOT NULL,                 -- placeholder pending+{pi}@befreeclub.pl do czasu confirm (flow A)
  stripe_session_id TEXT UNIQUE,       -- flow B
  stripe_payment_intent_id TEXT,       -- bez UNIQUE!
  amount_paid INTEGER,                 -- grosze
  currency TEXT DEFAULT 'pln',
  status TEXT NOT NULL DEFAULT 'pending',  -- wartości w praktyce: 'pending' | 'paid'
  wants_invoice BOOLEAN DEFAULT false,
  nip TEXT,
  invoice_name TEXT,
  email_sent_at TIMESTAMPTZ,           -- guard "mail tylko raz"
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  paid_at TIMESTAMPTZ
)
-- indeksy: email, stripe_session_id. RLS: tylko service_role.

ebook_download_tokens (
  id UUID PK,
  order_id UUID NOT NULL REFERENCES ebook_orders ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,          -- 64 znaki hex
  email TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,     -- +30 dni od utworzenia
  download_count INTEGER NOT NULL DEFAULT 0,
  max_downloads INTEGER NOT NULL DEFAULT 10,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_downloaded_at TIMESTAMPTZ
)
-- indeks: token. RLS: tylko service_role.
```

### 1.6 Faktury VAT

Nie ma żadnej automatyzacji. `wants_invoice`, `nip`, `invoice_name` tylko lądują w `ebook_orders` - faktury wystawiane ręcznie (zapewne ktoś czyta tabelę). W nowym systemie: minimum to widok w adminie z filtrem `wants_invoice = true`.

---

## 2. NEWSLETTER

### 2.1 Architektura w jednym zdaniu

**Stateless double opt-in**: zapis nie zapisuje NIC w bazie. Mail potwierdzający (Resend) niesie podpisany HMAC token z danymi; dopiero kliknięcie potwierdzenia tworzy subskrybenta - i to **nie w Supabase, tylko w Sender.net** (zewnętrzny ESP). Lista mailingowa żyje w całości w Sender.net.

### 2.2 Wejścia frontendowe

- `/newsletter` (Newsletter.tsx) - dedykowana strona: imię + email, walidacja zod (imię 1-80, email max 255). Po sukcesie ekran "sprawdź skrzynkę" z przyciskiem **"Otwórz mój mail w skrzynce"** (mapa ~30 domen -> webmail z prefiltrowanym wyszukiwaniem nadawcy `krystian@befreeclub.pl`; fallback Gmail) oraz przyciskiem "wyślij ponownie" (po prostu drugi POST, generuje nowy token).
- `NewsletterCTA.tsx` - identyczny formularz osadzony na stronie głównej.
- Oba wołają funkcję **gołym `fetch`-em** (nie `supabase.functions.invoke`) na zahardkodowany URL `https://fshkdkvoyysphfrfvmni.supabase.co/functions/v1/newsletter-subscribe` - dlatego w `config.toml` te dwie funkcje mają `verify_jwt = false`.

### 2.3 `newsletter-subscribe`

Body: `{ name, email }`.
1. Walidacja: name trim, 1-80 znaków (400 "Niepoprawne imię"); email regex + max 255, lowercase (400 "Niepoprawny email").
2. **Token DOI**: payload `{ email, name, exp: Date.now() + 14 dni (ms) }` -> JSON -> base64url -> podpis HMAC-SHA256 sekretem `NEWSLETTER_DOI_SECRET` -> token = `{payloadB64url}.{sigB64url}`. (Własny format, nie JWT.)
3. `confirmUrl = {CONFIRM_URL_BASE||https://befreeclub.pl/newsletter/potwierdz}?token={urlencoded token}`.
4. Wysyłka przez Resend: from `NEWSLETTER_FROM_EMAIL` (default `Be Free Club <krystian@befreeclub.pl>`), `reply_to: krystian@befreeclub.pl`, subject `"{imię} potwierdź swój zapis - nowy link {data+czas pl-PL Europe/Warsaw}"` (timestamp w temacie + nagłówek `X-Entity-Ref-ID: {uuid}` - celowo, żeby Gmail nie sklejał kolejnych prób w wątek i nie chował nowego linku). HTML: ciemna karta BFC (#1a1b1f/#2c2d31, akcent #ECE183), przycisk "Potwierdzam zapis", fallback z gołym linkiem, dopisek "Link wygasa za 14 dni", spora sekcja CSS na dark mode klientów pocztowych (`[data-ogsc]`, prefers-color-scheme).
5. Błąd Resend -> 502 "Nie udało się wysłać maila potwierdzającego". Sukces -> `{ ok: true }`.

Brak jakiegokolwiek zapisu do DB, brak rate-limitu, brak deduplikacji (każdy POST = nowy mail).

### 2.4 `newsletter-confirm`

Body: `{ token }`. Strona `/newsletter/potwierdz?token=...` (NewsletterConfirm.tsx) woła automatycznie po wejściu (też goły fetch), po sukcesie redirect na `/newsletter/witaj?name={name}` (strona powitalna).

1. Weryfikacja tokenu: split po `.`, HMAC-SHA256 z `NEWSLETTER_DOI_SECRET`, porównanie **constant-time**, potem parse payloadu i check `exp` (epoch ms). Złe/wygasłe -> 400 "Link wygasł lub jest nieprawidłowy".
2. Push do **Sender.net API v2** (`https://api.sender.net/v2`, Bearer `SENDER_API_TOKEN`; token jest defensywnie trimowany i zdejmowany z ewentualnego prefiksu "Bearer "):
   - `POST /subscribers` body `{ email, firstname, groups: SENDER_GROUP_IDS, trigger_automation: true }`.
   - Gdy non-OK (np. subskrybent już istnieje): fallback `PATCH /subscribers/{email}` z tym samym body bez emaila.
   - `SENDER_GROUP_IDS` z env, **default zahardkodowany w kodzie: `"epnLzm,el06vl"`** (CSV id grup w Sender.net).
3. Oba nieudane -> 502 "Nie udało się dokończyć zapisu. Spróbuj za chwilę." Sukces -> `{ ok: true, name }`.

Token można kliknąć wielokrotnie przez 14 dni - każde kliknięcie to kolejny upsert do Sender z `trigger_automation: true` (potencjalne ponowne odpalenie automatyzacji powitalnej po stronie Sender, zależnie od ich deduplikacji).

### 2.5 Tabela `newsletter_subscribers` - MARTWA

```sql
newsletter_subscribers (id uuid PK, email text UNIQUE NOT NULL, created_at timestamptz, source text)
-- RLS: anon INSERT allowed, SELECT zabroniony
```
Istnieje w DB i w wygenerowanych typach, ale **żadna funkcja ani żaden komponent z niej nie korzysta** (relikt wcześniejszej wersji sprzed Sender.net). Przy migracji: sprawdzić czy są w niej historyczne rekordy warte eksportu, poza tym do skasowania.

---

## 3. FORMULARZ KONTAKTOWY

Strona `/kontakt` (Contact.tsx), pola: name (max 100), email (max 255 + regex), message (max 5000). Flow dwustopniowy:

1. **Insert wprost z przeglądarki** do tabeli `contact_messages` przez klienta Supabase (RLS: anon może INSERT, nikt nie może SELECT/UPDATE/DELETE przez API). To jest kanał podstawowy - wiadomość zawsze ląduje w DB.
2. **Best-effort mail**: invoke `send-contact-email` w try/catch z pustym catchem - błąd maila nie psuje UX ("message is already saved in DB").

```sql
contact_messages (
  id UUID PK, name TEXT NOT NULL, email TEXT NOT NULL,
  message TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

`send-contact-email` (`verify_jwt = false` w config.toml):
1. **Jeśli `RESEND_API_KEY` nie ustawiony: zwraca 200 "Email sending not configured"** - cichy sukces bez maila.
2. Walidacja name/email/message jak wyżej -> 400 "Invalid input".
3. Resend: from `Be Free Club <noreply@befreeclub.pl>` (hardcode), **to: `krystian@befreeclub.pl`** (hardcode), `reply_to: {email nadawcy}`, subject `Nowa wiadomość od {name escaped, max 80}`, HTML: imię/email/wiadomość (escapowane, `\n` -> `<br>`).
4. Błąd Resend -> 500 "Failed to send email" (ale wiadomość i tak jest już w DB).

Nikt nie czyta `contact_messages` z poziomu aplikacji - odbiorcą jest mail do Krystiana, tabela to backup. Brak captcha/rate-limitu.

---

## 4. Sekrety i konfiguracja (NAZWY zmiennych)

| Zmienna | Używana w | Uwagi |
|---|---|---|
| `STRIPE_SECRET_KEY` | create-ebook-checkout, create-ebook-payment-intent, confirm-ebook-purchase | konto Stripe "current" |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | wszystkie ebookowe | service role omija RLS |
| `RESEND_API_KEY` | confirm-ebook-purchase, newsletter-subscribe, send-contact-email | jeden klucz dla wszystkich maili |
| `FRONTEND_URL` | confirm-ebook-purchase | baza linku pobrania, fallback `https://befreeclub.pl` |
| `NEWSLETTER_DOI_SECRET` | newsletter-subscribe, newsletter-confirm | klucz HMAC tokenu DOI |
| `CONFIRM_URL_BASE` | newsletter-subscribe | fallback `https://befreeclub.pl/newsletter/potwierdz` |
| `NEWSLETTER_FROM_EMAIL` | newsletter-subscribe | fallback `Be Free Club <krystian@befreeclub.pl>` |
| `SENDER_API_TOKEN` | newsletter-confirm | API Sender.net |
| `SENDER_GROUP_IDS` | newsletter-confirm | CSV, fallback hardcode `epnLzm,el06vl` |

Hardcody nie-sekretne, ale konfiguracyjne (kandydaci do panelu admina): cena 24900, `EBOOK_PRICE_ID`, ścieżka pliku `na-swoich-zasadach.pdf` i nazwa pliku do pobrania, limity 30 dni / 10 pobrań / 5 min signed URL / 14 dni DOI, adresy `noreply@befreeclub.pl`, `krystian@befreeclub.pl`, publishable key Stripe w Ebook.tsx, URL projektu Supabase w Newsletter*.tsx.

CORS: wszystkie funkcje `Access-Control-Allow-Origin: *`. JWT: funkcje ebookowe NIE mają wpisu `verify_jwt=false` w config.toml (działają, bo frontend woła je przez `supabase.functions.invoke` z anon key); `newsletter-subscribe`, `newsletter-confirm`, `send-contact-email` mają `verify_jwt=false` (newsletter wołany gołym fetchem bez nagłówków auth).

Routing frontowy do odtworzenia: `/ebook`, `/ebook/sukces`, `/ebook/pobierz`, `/newsletter`, `/newsletter/potwierdz`, `/newsletter/witaj`, `/kontakt`.

---

## 5. Edge case'y i zachowania do zachowania (lub świadomej zmiany)

- **Confirm jest retry'owany przez frontend** (8 x 2s) - backend musi być w pełni idempotentny: reuse tokenu, mail raz (guard `email_sent_at`), update orderu tylko gdy coś się zmienia.
- BLIK i wallety wracają redirectem - parametr na sukcesie to `payment_intent` (doklejony przez Stripe) lub `payment_intent_id` (ręczna nawigacja); obsłużyć oba.
- Email w flow A pochodzi **od klienta z URL-a** (fallback `pi.receipt_email`); przy Express Checkout email bierze się z `billingDetails.email` walleta.
- Nieudana płatność w flow A = nowy PaymentIntent = nowy osierocony wiersz pending z placeholderowym emailem. Nikt tego nie sprząta.
- `confirm-ebook-purchase` zwraca token/downloadUrl każdemu, kto zna id sesji/PI opłaconej transakcji (brak dodatkowego uwierzytelnienia).
- Newsletter "wyślij ponownie" = nowy token; stare tokeny pozostają ważne do swojego `exp` (stateless, nie da się ich unieważnić bez zmiany sekretu).
- Mail DOI ma timestamp w temacie i `X-Entity-Ref-ID`, żeby Gmail nie zwijał ponownych wysyłek w wątek - zachować przy przepisywaniu.
- Sender.net: create -> fallback update (PATCH po emailu). `trigger_automation: true` w obu.
- Formularz kontaktowy: zapis do DB i mail to dwa niezależne kroki; mail jest best-effort.

---

## 6. Prowizorki i długi

1. **Brak webhooka dla ebooków - fulfillment w 100% zależny od przeglądarki kupującego.** `stripe-webhook` obsługuje tylko subskrypcje/Klarnę/refundy; nie ma handlera `payment_intent.succeeded` dla `metadata.product == "ebook"`. Jeśli klient zapłaci (np. BLIK w aplikacji banku) i nie wróci na `/ebook/sukces`, **nie dostanie maila ani ebooka** - kasa pobrana, zero fulfillmentu, ratunek tylko ręczny przez krystian@. W nowym systemie: webhook jako główny mechanizm, strona sukcesu tylko jako przyspieszacz UX.
2. **Refund ebooka może skasować członkostwo w klubie.** Handler `charge.refunded` w `stripe-webhook` nie filtruje po `metadata.product` - przy pełnym refundzie ebooka bierze email z charge'a i robi `cancelSubscriptionsImmediately` (na obu kontach Stripe) + `removeFromCircle`. Kupujący ebooka, który jest też członkiem klubu na tym samym emailu i skorzysta z obiecywanego "zwrotu bez pytań w 14 dni", wylatuje z klubu. Bomba z opóźnionym zapłonem.
3. **Refund nie unieważnia tokenów pobrania** - po zwrocie kasy link działa dalej (30 dni / 10 pobrań).
4. **Martwy kod, który wciąż żyje na produkcji**: `EbookCheckoutModal.tsx` (nieimportowany) + funkcja `create-ebook-checkout` (wdrożona, wołalna publicznie, tworzy sesje Stripe). Tabela `newsletter_subscribers` (nieużywana, RLS pozwala na anon INSERT - śmietnik dla botów).
5. **Dane fakturowe i email w query stringu** (`/ebook/sukces?...&nip=...&name=...`) - PII w historii przeglądarki, logach, analytics. Backend ślepo ufa tym wartościom przy update orderu.
6. **Cena/produkt w 3+ miejscach**: stała `AMOUNT` w jednej funkcji, Price ID w drugiej, `amount_paid: 24900` hardcode przy insercie (flow B zapisuje 24900 niezależnie od faktycznego Price), copy "249 zł" we frontendzie. Zmiana ceny = polowanie po repo. Do panelu admina.
7. **Brak rate-limitów wszędzie**: `create-ebook-payment-intent` (spam PaymentIntentów + wierszy pending), `newsletter-subscribe` (mail-bombing dowolnego adresu cudzym kosztem Resend), `send-contact-email` / insert do `contact_messages` (spam). Zero captcha.
8. **`send-contact-email` z brakującym `RESEND_API_KEY` zwraca 200** - cichy "sukces" bez wysyłki. Łatwe do przeoczenia po migracji.
9. **Licznik pobrań nieatomowy** (SELECT potem UPDATE `download_count + 1`) - równoległe requesty mogą przekroczyć limit. Przy 10 pobraniach mało groźne, ale w Postgresie zrobić `UPDATE ... SET download_count = download_count + 1 WHERE ... RETURNING`.
10. **`stripe_payment_intent_id` bez UNIQUE** w `ebook_orders` + wzorzec "select potem insert" w confirm - wyścig dwóch równoległych confirmów może zrobić duplikat orderu (każdy z własnym tokenem).
11. **Placeholderowe emaile `pending+{pi}@befreeclub.pl`** jako obejście NOT NULL - brzydkie, psuje statystyki po emailu; osierocone wiersze pending rosną bez sprzątania.
12. **Hardcode'y frontendowe**: URL projektu Supabase (`fshkdkvoyysphfrfvmni.supabase.co`) wklejony w Newsletter.tsx/NewsletterCTA.tsx/NewsletterConfirm.tsx, publishable key Stripe w Ebook.tsx, default `SENDER_GROUP_IDS` w kodzie funkcji.
13. **Niespójne copy metod płatności**: martwy modal obiecuje "Przelewy24", a oba flow mają tylko `card + blik` (+ wallety w flow A).
14. **Lista newslettera tylko w Sender.net** - brak własnej kopii subskrybentów (tabela-duch nieużywana). Świadoma decyzja czy dług? Przy migracji zdecydować, czy FastAPI ma trzymać lustro listy.
15. **Mail z ebookiem ma inny design niż mail DOI** (jasny, stary styl, inne kolory vs ciemna karta BFC) - do ujednolicenia przy przepisywaniu szablonów.
