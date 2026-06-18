# Spec: Billing i checkout (landing befreeclub.pl)

Rekonesans 1:1 obecnego kodu. Cel: dać komplet wiedzy do przepisania w FastAPI bez czytania oryginału.

Pliki źródłowe (stan na 2026-06-10):

- `befreeclub/supabase/functions/create-checkout/index.ts`
- `befreeclub/supabase/functions/confirm-subscription/index.ts`
- `befreeclub/supabase/functions/create-klarna-checkout/index.ts`
- `befreeclub/supabase/functions/confirm-klarna-checkout/index.ts`
- `befreeclub/supabase/functions/reconcile-klarna-checkouts/index.ts`
- `befreeclub/supabase/functions/validate-promo/index.ts`
- `befreeclub/supabase/functions/update-payment-method/index.ts`
- `befreeclub/supabase/functions/stripe-webhook/index.ts`
- Frontend: `src/components/landing/CheckoutModal.tsx`, `src/components/landing/Pricing.tsx`, `src/hooks/useCheckoutPrefetch.ts`, `src/hooks/usePendingConfirmation.ts`, `src/pages/Success.tsx`, `src/pages/UpdateCard.tsx`, `src/config/promo.ts`
- DB: `supabase/migrations/*` (tabela `circle_members`)

WAŻNE: katalog `supabase/functions/_shared/` NIE istnieje. Każda funkcja jest samowystarczalnym plikiem. Helper `inviteToCircle()` jest skopiowany (copy-paste, drobne różnice w logach) w 4 plikach: `confirm-subscription`, `confirm-klarna-checkout`, `reconcile-klarna-checkouts`, `stripe-webhook`. Helper wysyłki maila powitalnego Klarna jest zduplikowany w `reconcile-klarna-checkouts` i `stripe-webhook` (treść prawie identyczna, webhook ma jedno zdanie więcej o ratach). W porcie: jeden moduł `shared/` (Stripe, Circle, Resend) pisany raz.

---

## 1. Architektura ogólna

Dwa niezależne tory zakupu członkostwa:

1. **Subskrypcja kartą** (odnawialna, Stripe Subscriptions): SetupIntent na froncie (Stripe Elements) -> `confirm-subscription` tworzy Customer + Subscription + zaprasza do Circle. Bez Stripe Checkout (własny modal).
2. **Klarna / płatność jednorazowa** (nieodnawialna): Stripe Checkout Session w `mode=payment` -> dostęp czasowy z `expires_at` w DB. Trzy redundantne ścieżki potwierdzenia (webhook, strona /sukces, reconcile-sweep).

Obie ścieżki kończą się tym samym: zaproszenie e-mailowe do społeczności Circle (POST do Circle Admin API v2) + wiersz w tabeli `circle_members` w Supabase. To JEDYNA tabela DB używana przez billing. Nie ma lokalnej tabeli subskrypcji, klientów ani płatności - źródłem prawdy o subskrypcjach jest wyłącznie Stripe (odpytywany na żywo).

### Dwa konta Stripe

| Konto | Sekret | Rola |
|---|---|---|
| **current** | `STRIPE_SECRET_KEY` | Aktywne konto sprzedażowe. CAŁY nowy checkout (karta, Klarna, promo, zmiana karty, ebook) działa wyłącznie na nim. Frontendowy publishable key (hardcoded w `CheckoutModal.tsx` i `UpdateCard.tsx`): `pk_live_51SsMPe...` należy do tego konta. |
| **legacy** | `STRIPE_LEGACY_SECRET_KEY` | Stare konto (historycznie Krystiana), na którym wciąż żyją odnawiające się subskrypcje starych członków. Nowych zakupów tam nie ma. |

Gdzie używane jest legacy (w zakresie tego speca): tylko `stripe-webhook` przy obsłudze refundu - (a) lookup e-maila klienta gdy charge nie ma e-maila, (b) anulowanie WSZYSTKICH subskrypcji danego e-maila na OBU kontach. Poza tym specem legacy używają: `pause-subscription`, `request-cancellation`, `confirm-cancellation`, `circle-cleanup`, `admin-pause-subscription`, `admin-extend-subscription`, `admin-stripe-legacy-audit` (wzorzec "spytaj current, potem legacy").

Czego legacy NIE ma w tym zakresie: `update-payment-method` szuka klienta TYLKO na koncie current. Stary członek z subskrypcją na legacy dostaje "Nie znaleźliśmy konta z takim adresem email" i nie może zmienić karty przez landing.

Kolumna `circle_members.stripe_source` (`'current'` / `'legacy'`, default `'current'`) mówi, na którym koncie żyje płatność członka.

### Wersje i biblioteki

- Stripe SDK: `stripe@18.5.0` (esm.sh), `apiVersion: "2025-08-27.basil"` wszędzie w tym zakresie (uwaga: `admin-stripe-legacy-audit` używa starszego `stripe@17.6.0`).
- Pułapka wersji basil: w API 2025+ `current_period_end` przeniesiono z subskrypcji do `items[].current_period_end`, a `invoice.subscription` do `invoice.parent.subscription_details`. Kod webhooka czyta STARE pole `invoice.subscription` - patrz Prowizorki #2.
- Supabase JS: `@supabase/supabase-js@2.57.2`, klient z `SUPABASE_SERVICE_ROLE_KEY` (omija RLS).
- Maile: Resend REST API (`https://api.resend.com/emails`), nadawca `Be Free Club <noreply@befreeclub.pl>`, reply-to `krystian@befreeclub.pl`.
- Circle: `https://app.circle.so/api/admin/v2/community_members`, Bearer `CIRCLE_API_TOKEN`, body z `community_id` (int z `CIRCLE_COMMUNITY_ID`).

### Sekrety (nazwy zmiennych, wartości w Supabase Edge Function secrets, NIE w repo)

`STRIPE_SECRET_KEY`, `STRIPE_LEGACY_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `CIRCLE_API_TOKEN`, `CIRCLE_COMMUNITY_ID`, `RESEND_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.

W repo hardcoded (jawnie, nie sekret, ale do przeniesienia do configu): publishable key Stripe (2 pliki frontu), wszystkie price ID, kwoty planów.

---

## 2. Plany i ceny (HARDCODED w wielu miejscach)

| planId | Nazwa marketingowa | Okres | Cena | Price ID (konto current, recurring) |
|---|---|---|---|---|
| `quarterly` | Starter | 3 mies. | 639 zł (213 zł/mies) | `price_1TdVWjDlsrz5Z08F0gc9nskb` |
| `semiannual` | Pro / "Najczęściej wybierany" | 6 mies. | 879 zł (147 zł/mies) | `price_1TdVWkDlsrz5Z08FydX2azl9` |
| `annual` | Master / "Najlepsza wartość" | 12 mies. | 1489 zł (124 zł/mies) | `price_1T8aHeDlsrz5Z08FmgzIUyTB` |

Mapa `PRICE_MAP` jest skopiowana 1:1 w `create-checkout` i `confirm-subscription`. Trzecia kopia (z kwotami w groszach i nazwami) jako `PLAN_CONFIG` w `create-klarna-checkout`. Ceny wyświetlane są osobno hardcoded w `Pricing.tsx` (`mainPlans` + `PLAN_INFO`, z `regularPrice` do przekreślenia: 2988/1494/747 zł) i w `Success.tsx` (`PLAN_PRICE_PLN` do Meta Pixel Purchase). Zmiana ceny = edycja 4-5 plików + Stripe Dashboard. Docelowo (decyzja 2026-06-10): plany i ceny zarządzane z zakładki "Landing page" w adminie.

Klarna dostępna TYLKO dla `semiannual` (87900 gr) i `annual` (148900 gr). Brak Klarny dla `quarterly` (świadome - próg opłacalności).

---

## 3. DB: tabela `circle_members`

Jedyna tabela billingowa. Złożona z 3 migracji:

```sql
CREATE TABLE public.circle_members (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL UNIQUE,
  circle_member_id TEXT,                                   -- id członka w Circle (string), NULL gdy invite się nie udał
  invited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  active BOOLEAN NOT NULL DEFAULT true,                    -- czy ma mieć dostęp do Circle
  stripe_source TEXT NOT NULL DEFAULT 'current',           -- 'current' | 'legacy'
  purchase_type TEXT NOT NULL DEFAULT 'subscription',      -- 'subscription' | 'one_time' (Klarna)
  expires_at TIMESTAMPTZ                                   -- tylko dla one_time; NULL = subskrypcja (żyje wg Stripe)
);
-- RLS: policy "Service role only" FOR ALL TO service_role
-- Indeksy: idx_circle_members_purchase_type, idx_circle_members_expires_at (partial, WHERE expires_at IS NOT NULL)
```

Semantyka: wiersz = członek Circle zaproszony przez landing. Upserty zawsze `onConflict: "email"`. Dla subskrypcji `expires_at` jest NULL - wygaszaniem zajmuje się osobny `circle-cleanup` (osobny spec) na bazie statusów w Stripe. Dla Klarny `expires_at` wyznacza koniec dostępu.

Pułapka: e-mail jest kluczem unikalnym, ale nie wszystkie ścieżki normalizują wielkość liter (patrz Prowizorki #6).

---

## 4. Flow zakupu subskrypcji kartą (krok po kroku)

### 4.1 `create-checkout` (POST, verify_jwt=false - publiczny)

Request: `{ "planId": "quarterly" | "semiannual" | "annual" }`

1. Mapuje planId -> price ID z `PRICE_MAP` (nieznany plan -> 500 `{"error": "Invalid plan: X"}`).
2. Tworzy **SetupIntent** (nie PaymentIntent!): `payment_method_types: ["card"]`, `usage: "off_session"`, `metadata: { price_id, plan_id }`.
3. Response 200: `{ "clientSecret": "...", "setupIntentId": "seti_..." }`.

Dlaczego SetupIntent z `usage: off_session`: podczas pierwszego 3DS bank dostaje mandat SCA "karta będzie obciążana bez udziału klienta". Bez tego polskie banki (mBank, ING, Santander, PKO) odrzucały odnowienia z `authentication_required`. To jest sedno całej konstrukcji - NIE zastępować Checkout Sessionem w mode=subscription bez zachowania tej semantyki.

Konsekwencja: zero płatności na tym etapie. Karta jest tylko tokenizowana i autoryzowana 3DS-em, obciążenie robi dopiero `confirm-subscription`.

### 4.2 Frontend: modal checkoutu (`CheckoutModal.tsx` + `useCheckoutPrefetch.ts`)

- **Prefetch:** 5 s po wejściu na stronę front woła `create-checkout` dla planu `semiannual` "na zapas" (`useCheckoutPrefetch("semiannual", 5000)`). Klik w inny plan -> fetch dla niego. Skutek: każdy wizytator landinga generuje co najmniej 1 porzucony SetupIntent w Stripe. Dodatkowo `getOrFetch()` ma buga: po cache-miss robi `fetchForPlan` i ZARAZ POTEM drugi, świeży request - 2 SetupIntenty na otwarcie modalu bez prefetchu.
- Modal renderuje Stripe Elements (`PaymentElement` card-only, locale pl, ciemny theme) + `ExpressCheckoutElement` (Apple Pay / Google Pay, `emailRequired: true`).
- Pola: e-mail (wymagany), checkbox "Chcę fakturę VAT" -> pole NIP (wymagane gdy zaznaczony), sekcja "Mam kod promocyjny" (tylko gdy flaga `PROMO_CAMPAIGN_ACTIVE` w `src/config/promo.ts` = true; flaga przełączana commitem).
- Submit: najpierw `savePendingConfirmation()` do localStorage (klucz `bfc_pending_confirmation`, payload: `{setupIntentId, planId, email, planName, wantInvoice, nip, promoCode, savedAt}`), potem `stripe.confirmSetup({redirect: "if_required", return_url: "<origin>/sukces?email=...&plan=<planName>&setupIntentId=...&planId=..."})`.
- Dwa scenariusze:
  - **bez redirectu 3DS**: front od razu woła `confirm-subscription`, po sukcesie `navigate("/sukces", {state})`.
  - **z redirectem 3DS**: bank przekierowuje na `/sukces?...&redirect_status=succeeded`; strona `/sukces` woła `confirm-subscription` (promoCode odzyskuje z localStorage). `redirect_status != succeeded` -> redirect na `/?checkout_failed=true&planId=...`, co w `Pricing.tsx` otwiera modal ponownie z toastem błędu.
- **Mechanizm pending confirmation** (`usePendingConfirmation`, montowany w `App.tsx`): jeśli user zamknął kartę między 3DS a potwierdzeniem, przy następnej wizycie (dowolna ścieżka poza `/sukces` i `/newsletter*`) front automatycznie ponawia `confirm-subscription` z danych w localStorage. Wpis wygasa po 1 h. Przy błędzie wpis NIE jest czyszczony (retry przy kolejnej wizycie).
- Po błędzie `confirmSetup`/`confirm-subscription` front woła `fetchFreshSetupIntent()` (SetupIntent jest skonsumowany/terminalny, trzeba nowy).
- Express Checkout: e-mail brany z `event.billingDetails.email` - może być pusty, wtedy `confirm-subscription` poleci błędem "Missing required fields".
- Meta Pixel: `InitiateCheckout` przy kliku planu (Pricing), `Purchase` na `/sukces` po `confirmed` (wartość z hardcoded `PLAN_PRICE_PLN`; bez uwzględnienia rabatu promo - pixel raportuje pełną cenę).

### 4.3 `confirm-subscription` (POST, verify_jwt=false - publiczny)

Request: `{ setupIntentId, planId, email, wantInvoice?, nip?, promoCode? }` (wymagane pierwsze 3).

Krok po kroku:

1. Walidacja pól i planId -> price ID.
2. **Promo (server-side, nigdy nie ufa klientowi):** jeśli `promoCode` podany - `stripe.promotionCodes.list({code: UPPERCASE(trim), active: true, limit: 1})`; dodatkowy własny check `expires_at`. Nieznaleziony/wygasły/błąd lookupu -> promo po cichu pominięte (zakup idzie BEZ rabatu, bez informowania usera!).
3. `setupIntents.retrieve(setupIntentId)` - status musi być `succeeded`, inaczej 500.
4. Wyciąga `payment_method` z SetupIntent (brak -> 500).
5. **Rekoncyliacja Customer <-> PaymentMethod** (sedno edge case'ów Apple/Google Pay):
   - `paymentMethods.retrieve(pm)` - sprawdza czy PM już wisi na jakimś customerze (`pm.customer`).
   - Równolegle `customers.list({email, limit: 1})` (uwaga: bierze tylko pierwszego, duplikaty customerów po e-mailu są możliwe w Stripe).
   - Przypadki:
     a. PM ma customera i ten istnieje -> używamy TEGO customera (nawet jeśli e-mail się różni od podanego!).
     b. PM ma customera, ale usuniętego -> `paymentMethods.detach(pm)`, potem reuse customera z listy po e-mailu albo `customers.create({email, metadata: {wants_invoice, nip}?})`, potem `attach`.
     c. PM bez customera + istnieje customer z e-mailem -> attach do niego.
     d. PM bez customera + brak customera -> create + attach.
6. **Idempotencja:** `subscriptions.list({customer, status: "active", limit: 10})` - jeśli którakolwiek aktywna subskrypcja ma item z tym samym price ID -> return 200 `{subscriptionId: <ID PIERWSZEJ aktywnej subskrypcji z listy - niekoniecznie tej dopasowanej>, status: "active", alreadyExisted: true}`. Bez Circle invite (zakłada się, że już był). Idempotencja działa tylko per ten sam plan - kupno innego planu przez aktywnego członka tworzy DRUGĄ subskrypcję.
7. Update customera: metadata `{wants_invoice: "true", nip}` (gdy faktura), `invoice_settings.default_payment_method = pm`.
8. NIP jako Tax ID (`customers.createTaxId(type: "pl_nip")`) - TYLKO dla świeżo utworzonego customera (`createdNewCustomer`), błąd ignorowany. Dla istniejącego customera NIP ląduje tylko w metadata.
9. **Tworzenie subskrypcji:** `subscriptions.create({customer, items: [{price}], default_payment_method: pm, payment_behavior: "error_if_incomplete", off_session: true, payment_settings: {payment_method_types: ["card"], save_default_payment_method: "on_subscription"}, discounts: [{promotion_code}]?})`.
   - `error_if_incomplete`: jeśli pierwsze obciążenie nie przejdzie, create rzuca błąd -> 500 -> front pokazuje "płatność się nie powiodła" i odświeża SetupIntent.
   - `off_session: true`: każe Stripe użyć mandatu SCA z SetupIntentu - klucz do bezproblemowych odnowień.
10. Jeśli status != `active` -> 200 `{subscriptionId, status, circleInvited: false}` (front traktuje nie-active jako błąd i pokazuje komunikat o nieudanej płatności).
11. **Circle invite:** `inviteToCircle(email)` - POST do Circle Admin API v2 `community_members` z `skip_invitation: false` (Circle sam wysyła e-mail z zaproszeniem). 3 próby, backoff 1s/2s/3s, 4xx (poza 429) bez retry. Zwraca `data.id || data.community_member.id` jako string albo null.
12. Upsert do `circle_members`: `{email, circle_member_id, active: <czy invite się udał>}` (bez `purchase_type`/`expires_at` - zostają defaulty `subscription`/NULL; bez normalizacji wielkości liter e-maila - leci jak przyszedł z requestu).
13. Response 200: `{subscriptionId, status: "active", circleInvited: bool}`. Nieudany invite NIE wycofuje subskrypcji - tylko log "Manual intervention may be needed" i `active: false` w DB (do ręcznej naprawy / `retry-circle-invites`).

Mail powitalny dla subskrypcji kartą: BRAK z naszej strony - zaproszenie wysyła sam Circle.

---

## 5. Kody promocyjne

### 5.1 `validate-promo` (POST, brak wpisu w config.toml -> verify_jwt=true, wymaga anon JWT Supabase)

Request: `{ code }`. Normalizacja: `trim().toUpperCase()`.

Lookup: `stripe.promotionCodes.list({code, active: true, limit: 1})` na koncie current + własny check `expires_at`.

Response zawsze 200 (nawet błędy!):
- nieznaleziony: `{valid: false, reason: "not_found"}`
- wygasły: `{valid: false, reason: "expired"}`
- wyjątek: `{valid: false, reason: "error", message}`
- OK: `{valid: true, code, promotionCodeId, discountPercent, discountAmount, currency, duration, durationInMonths, expiresAt}`

Frontend używa tylko `code`, `discountPercent`, `expiresAt`. `promotionCodeId` zwracany, ale celowo NIE używany przez backend (confirm/klarna robią własny lookup po tekście kodu - "never trust client").

### 5.2 Zachowanie na froncie

- Flaga `PROMO_CAMPAIGN_ACTIVE` (plik `src/config/promo.ts`, boolean w kodzie, zmiana = commit + deploy). Gdy false: znika baner, znika pole kodu w modalu, znika auto-aktywacja z URL. Backend i tak waliduje, więc stare linki są bezpieczne.
- Auto-aktywacja z URL: `?promo=KOD` na stronie głównej -> walidacja -> rabat widoczny na cenach (`Math.round(price * (1 - percent/100))`) i w modalu.
- Wyliczenia rabatu na froncie są tylko prezentacyjne - autorytatywna matematyka dzieje się w Stripe przez `discounts: [{promotion_code}]`.
- Kod promo przeżywa redirect 3DS przez localStorage (`bfc_pending_confirmation`).
- Promo działa i dla subskrypcji (rabat wg `duration` kuponu - uwaga: kupon `once` rabatuje tylko pierwszy okres subskrypcji, potem pełna cena; to zachowanie Stripe, nie kodu) i dla Klarny (jednorazowa kwota, rabat widoczny w Stripe Checkout UI).
- Rabaty kwotowe (`discountAmount`): backend by je przyjął, ale front pokazuje rabat TYLKO procentowy (`discountPercent`); kupon kwotowy wyświetli pełną cenę w UI, a obciąży obniżoną.

---

## 6. Klarna / płatność jednorazowa

### 6.1 Czemu osobny tor

Klarna nie wspiera `off_session` recurring - nie da się jej użyć do subskrypcji odnawialnej. Stąd: jednorazowa płatność za cały okres (6/12 mies.) przez Stripe Checkout Session w `mode=payment`, dostęp ograniczony `expires_at` w `circle_members`, po upływie wygaszany przez `circle-cleanup` (osobny spec). User płaci Klarnie w ratach / za 30 dni - to sprawa Klarny, my dostajemy całość od razu.

### 6.2 `create-klarna-checkout` (POST, brak w config.toml -> verify_jwt=true)

Request: `{ planId: "semiannual" | "annual", email?, promoCode? }` (front wysyła tylko planId i promoCode, bez e-maila - e-mail zbiera Stripe Checkout).

1. `PLAN_CONFIG` hardcoded: semiannual = 87900 gr / 6 mies. / "Be Free Club - 6 miesięcy"; annual = 148900 gr / 12 mies. / "Be Free Club - 12 miesięcy". Pole `priceId` w tym configu jest MARTWE - sesja używa ad-hoc `price_data`, nie price ID (recurring price nie przejdzie w mode=payment). Komentarz w kodzie wprost: "Currently we reuse the recurring price IDs..." - nieaktualny względem implementacji.
2. Promo: identyczny server-side lookup jak w `confirm-subscription` (cichy skip przy braku).
3. `checkout.sessions.create`:
   - `mode: "payment"`, `payment_method_types: ["klarna", "card", "blik"]` - UWAGA: "przycisk Klarna" otwiera checkout, w którym da się też zapłacić kartą i BLIK-iem jednorazowo. To furtka do nieodnawialnego członkostwa kartą/BLIK-iem - świadomie lub nie, tak działa.
   - `line_items: [{price_data: {currency: "pln", product_data: {name, description: "Dostęp do społeczności Be Free Club przez N miesięcy. Płatność jednorazowa."}, unit_amount}, quantity: 1}]`
   - `success_url: <origin>/sukces?source=klarna&plan=<planId>&session_id={CHECKOUT_SESSION_ID}`
   - `cancel_url: <origin>/?checkout_failed=true&planId=<planId>`
   - `metadata` (i kopia w `payment_intent_data.metadata`): `{source: "klarna_checkout", plan_id, duration_months}` - `source` to dyskryminator dla webhooka/reconcile.
   - `locale: "pl"`, `billing_address_collection: "auto"`, `customer_creation: "always"` gdy brak e-maila, `discounts` gdy promo. `origin` z nagłówka requestu, fallback `https://befreeclub.pl`.
4. Response: `{ url, sessionId }` - front robi `window.location.href = url`.

### 6.3 Potwierdzenie - TRZY redundantne ścieżki (Klarna potrafi potwierdzić płatność asynchronicznie, minuty-dni)

Wspólna logika nadania dostępu (powtórzona 3 razy z drobnymi różnicami):
- e-mail z `session.customer_email || session.customer_details.email`
- `durationMonths` z `metadata.duration_months` (fallback w confirm/reconcile: `PLAN_DURATIONS[plan_id]`, w webhooku brak fallbacku)
- `expiresAt = teraz + durationMonths` (liczone od momentu POTWIERDZENIA, nie zakupu)
- idempotencja: jeśli wiersz `circle_members` ma `active=true` i `circle_member_id` -> tylko update `expires_at`, bez ponownego invite
- inaczej: `inviteToCircle` + upsert `{email, circle_member_id, active, purchase_type: "one_time", expires_at, stripe_source: "current", invited_at}`

| | `confirm-klarna-checkout` | `stripe-webhook` | `reconcile-klarna-checkouts` |
|---|---|---|---|
| Trigger | strona `/sukces?source=klarna` (fallback gdyby webhook nie zdążył/nie doszedł) | event `checkout.session.completed` LUB `checkout.session.async_payment_succeeded` | ręczny POST (sweep ostatnich 7 dni) |
| Auth | verify_jwt=true (anon JWT) | podpis Stripe | **BRAK - verify_jwt=false, zero auth w kodzie** |
| Wejście | `{sessionId}` -> `sessions.retrieve`, wymaga `metadata.source === "klarna_checkout"` (inaczej throw) i `payment_status === "paid"` (inaczej 409) | filtr `metadata.source` + `payment_status === "paid"` (unpaid -> czeka na async event) | `checkout.sessions.list({created: {gte: now-7d}, limit: 100})` z paginacją, filtr source+paid |
| Gdy już aktywny | **nadpisuje** `expires_at` bezwarunkowo (+ ustawia purchase_type/stripe_source) | **nadpisuje** `expires_at` bezwarunkowo | bump tylko gdy nowy termin DŁUŻSZY (max) |
| Normalizacja e-maila | trim+lowercase | **BRAK lowercase** | trim+lowercase |
| Mail powitalny (Resend) | NIE wysyła | wysyła po udanym invite | wysyła po udanym invite |
| Gdy invite padnie | 502 `{error: "Circle invite failed"}` (front: toast "wymaga ręcznego sprawdzenia") | log "manual action needed", 200 | licznik `inviteFailed`, wiersz z `active=false` zostaje (następny run spróbuje znowu) |

Mail powitalny Klarna: temat "Witaj w Be Free Club - dostęp aktywny ✅", treść: plan, data wygaśnięcia (pl-PL), info że zaproszenie do Circle przyjdzie osobno, podpis Krystian. Wersja webhookowa ma dodatkowe zdanie o ratach Klarny.

`reconcile-klarna-checkouts` zwraca: `{success, scanned, klarnaPaid, alreadyHandled, newlyInvited, inviteFailed, errors[]}`. Skanuje WSZYSTKIE checkout sessions z 7 dni (też ebookowe - odfiltrowuje po metadata). Nie jest wołany z frontu ani z żadnego crona w repo - pomyślany jako siatka bezpieczeństwa odpalana ręcznie (lub cronem skonfigurowanym poza repo - brak śladu konfiguracji). Czemu publiczny: prowizorka, żeby dało się odpalić curlem bez tokena. Patrz Prowizorki #1.

---

## 7. Zmiana karty: `update-payment-method` + strona `/aktualizuj-karte`

Endpoint (verify_jwt=false, publiczny) z dwoma akcjami rozróżnianymi **query paramem** `?action=`:

### `?action=create-intent`, body `{email}`

1. **"Autoryzacja" usera: NIE MA ŻADNEJ. Wystarczy znać e-mail subskrybenta.** Brak tokena, OTP, magic linka. Konsekwencje: enumeracja e-maili członków (404 "Nie znaleźliśmy konta..." vs 200) oraz możliwość podpięcia własnej karty pod cudzą subskrypcję (atak mało opłacalny - płaciłbyś za ofiarę - ale to wciąż manipulacja cudzym kontem bez weryfikacji). Świadomy trade-off na rzecz UX (linki w mailach o nieudanej płatności prowadzą tu bez logowania).
2. `customers.list({email, limit: 1})` TYLKO na koncie current (legacy odcięte - patrz sekcja 1).
3. Sprawdza, czy customer ma subskrypcję w statusie `active|past_due|unpaid|trialing` (list status=all, limit 20). Brak -> 404 po polsku.
4. Tworzy SetupIntent: `{customer, payment_method_types: ["card"], usage: "off_session", metadata: {purpose: "update_payment_method", customer_email}}`.
5. Response: `{clientSecret, setupIntentId}`.

### `?action=confirm`, body `{setupIntentId}`

Po `stripe.confirmSetup` na froncie (return_url `/aktualizuj-karte?confirmed=1&si=...` - uwaga: strona NIE obsługuje tych paramów po powrocie z 3DS, patrz Prowizorki #9):

1. Retrieve SetupIntent, wymaga `succeeded`; bierze z niego `customer` i `payment_method` (kto zna setupIntentId, ten może potwierdzić - id działa jak bearer token).
2. `customers.update(invoice_settings.default_payment_method = pm)`.
3. Dla każdej subskrypcji `active|past_due|unpaid|trialing`: `subscriptions.update({default_payment_method: pm})`.
4. Dla subskrypcji `past_due|unpaid`: list otwartych faktur (status=open, limit 5) i dla każdej `invoices.pay({payment_method: pm})` - natychmiastowa próba ściągnięcia zaległości nową kartą. Błąd płatności pojedynczej faktury jest logowany i połykany.
5. Response: `{success: true, subscriptionsUpdated, invoicesRetried}` - front pokazuje liczby.

Strona `/aktualizuj-karte` (UpdateCard.tsx): krok 1 e-mail -> krok 2 Stripe Elements (wallets: auto) -> krok 3 sukces. Linkowana z maila o nieudanej płatności (`https://befreeclub.pl/aktualizuj-karte`).

---

## 8. `stripe-webhook` (POST, verify_jwt=false)

Weryfikacja: nagłówek `stripe-signature` + `stripe.webhooks.constructEventAsync(body, sig, STRIPE_WEBHOOK_SECRET)`. **Jeden sekret = jeden endpoint na JEDNYM koncie Stripe (current).** Eventy z konta legacy nie są nigdzie odbierane (każdy endpoint webhooka ma własny secret) - patrz Prowizorki #4. Lista subskrybowanych eventów żyje tylko w Stripe Dashboard, nie w repo. Brak tabeli idempotencji eventów (Stripe potrafi dostarczyć event 2x - obsługi są w większości naturalnie idempotentne, ale mail o nieudanej płatności poleciałby podwójnie). Zawsze zwraca 200 `{received: true}` jeśli podpis OK, nawet gdy obsługa częściowo padła (500 tylko przy nieobsłużonym wyjątku).

### Obsługiwane eventy (wszystkie inne: ignorowane, 200)

**`invoice.payment_failed`** (nieudane odnowienie subskrypcji):
1. Skip gdy `invoice.subscription` puste ("non-subscription invoice") - UWAGA na wersję API basil, pole mogło zniknąć (Prowizorki #2).
2. Skip gdy brak `invoice.customer_email` lub brak `hosted_invoice_url`.
3. Wysyła przez Resend mail "płatność nie powiodła się": kwota (`amount_due/100 + waluta`), przycisk CTA z linkiem do `hosted_invoice_url` (Stripe hosted invoice = zapłać + autoryzuj 3DS jednym klikiem), link do `/aktualizuj-karte`, data następnej próby (`next_payment_attempt`, pl-PL), numer próby w temacie (próba 1: "autoryzuj jednym kliknięciem", kolejne: "Ponowna próba N"). Mail przy KAŻDEJ nieudanej próbie (harmonogram prób = Stripe Smart Retries, konfigurowany w Dashboardzie).
4. ŻADNYCH zmian w DB/Circle - zawieszaniem dostępu po wyczerpaniu prób zajmuje się `circle-cleanup` (osobny spec).

**`checkout.session.completed` + `checkout.session.async_payment_succeeded`** (Klarna): logika opisana w sekcji 6.3 (kolumna "stripe-webhook"). Sesje bez `metadata.source === "klarna_checkout"` (np. ebook) - skip. `completed` z `payment_status != "paid"` (Klarna pending) - skip, czekamy na `async_payment_succeeded`.

**`charge.refunded`** (auto-egzekucja refundu):
1. Tylko PEŁNE refundy (`charge.refunded === true && amount_refunded >= amount`); częściowe - log i skip.
2. Ustala e-mail: `charge.billing_details.email` -> `charge.receipt_email` -> retrieve customera na current -> retrieve customera na **legacy**. Brak e-maila -> log i skip.
3. `cancelSubscriptionsImmediately` na OBU kontach: dla maks. 5 customerów z tym e-mailem, wszystkie subskrypcje poza `canceled|incomplete_expired` -> `subscriptions.cancel(id)` - anulowanie **NATYCHMIASTOWE** (komentarze w kodzie dwukrotnie kłamią "at period end"; faktyczny kod tnie od razu - i słusznie przy refundzie, ale komentarz myli).
4. `removeFromCircle`: lookup `circle_members` po e-mailu, DELETE w Circle API (`community_members/{id}?community_id=`; 404 traktowane jak sukces), update `active=false` w DB. Brak wiersza w DB = nic się nie dzieje w Circle (członkowie spoza landinga nietykani).
5. **Brak filtra rodzaju charge'a**: pełny refund EBOOKA (lub dowolnego innego charge'a tego e-maila) także kasuje wszystkie subskrypcje klubu i wyrzuca z Circle. Patrz Prowizorki #5 - to najgrubsza mina w całym billingu.

---

## 9. Macierz endpointów i auth

| Funkcja | verify_jwt (config.toml) | Realna ochrona |
|---|---|---|
| create-checkout | false | brak (publiczny, bez rate limitu) |
| confirm-subscription | false | brak; "dowodem" jest posiadanie setupIntentId w statusie succeeded |
| validate-promo | (brak wpisu) true | anon JWT Supabase |
| create-klarna-checkout | (brak wpisu) true | anon JWT Supabase |
| confirm-klarna-checkout | (brak wpisu) true | anon JWT + sessionId jako dowód |
| reconcile-klarna-checkouts | false | **BRAK JAKIEJKOLWIEK** |
| update-payment-method | false | brak; "auth" = znajomość e-maila |
| stripe-webhook | false | podpis Stripe (STRIPE_WEBHOOK_SECRET) |

Niespójność true/false wygląda na przypadkową (wpisy dodawane ad hoc). CORS wszędzie `*`. W porcie FastAPI: jawnie zdecydować per endpoint (webhook - podpis; reconcile - auth admina/cron; reszta publiczna z rate limitem; update-payment-method docelowo w panelu admina wg decyzji z 2026-06-10).

---

## 10. Edge case'y i rzeczy zaskakujące (poza sekcją Prowizorki)

1. SetupIntent zamiast Checkout/PaymentIntent to ŚWIADOMA decyzja pod SCA/mandat dla polskich banków - zachować przy porcie.
2. Pierwsze obciążenie subskrypcji dzieje się w `subscriptions.create` z `error_if_incomplete` - "udany 3DS" na froncie nie oznacza jeszcze pobrania pieniędzy; odmowa przy create = user widzi błąd, środki nie schodzą.
3. PM z Apple/Google Pay może przyjść już przypięty do customera - wtedy kod używa TEGO customera, ignorując podany e-mail (subskrypcja może wylądować na innym e-mailu niż wpisany; Circle invite idzie na e-mail z formularza).
4. `customers.list({email, limit: 1})` w kilku miejscach - przy duplikatach customerów wybór jest niejawny (najnowszy wg Stripe). Refund skanuje do 5 customerów, reszta flow tylko 1.
5. Nieudany Circle invite NIE blokuje płatności: subskrypcja żyje, wiersz `active=false` czeka na ręczną akcję / `retry-circle-invites`.
6. Promo niepoprawne = cichy fallback do pełnej ceny (user, który ma kod w localStorage po wygaśnięciu kampanii, zapłaci pełną cenę bez ostrzeżenia).
7. Klarna `expires_at` liczone od potwierdzenia, nie zakupu; `setMonth()` w JS przy 31. dniu miesiąca może przeskoczyć (31.03 + 6 mies. -> 01.10).
8. Tor "Klarna" przyjmuje też card i blik (jednorazowo) - są więc członkowie z `purchase_type=one_time`, którzy nigdy Klarny nie dotknęli.
9. Idempotencja `confirm-subscription` zwraca id "pierwszej aktywnej" subskrypcji, niekoniecznie tej z dopasowanym planem (kosmetyka, front używa tylko statusu).
10. Meta Pixel Purchase nie odpala się dla Klarny (success URL Klarny nie ma parametru `planId`, a `PLAN_PRICE_PLN` kluczowane po planId) - zaniżone konwersje w Ads.
11. Mail o nieudanej płatności idzie tylko, gdy faktura ma `hosted_invoice_url` i `customer_email` - inaczej cichy skip.
12. `retry-circle-invites`, `sync-circle-ids`, `circle-cleanup`, pauzy i anulowania - poza zakresem tego speca (osobne pliki spec-landing), ale piszą po tej samej tabeli `circle_members`.

---

## 11. Prowizorki i długi

1. **`reconcile-klarna-checkouts` jest w pełni publiczny** (verify_jwt=false, zero auth w kodzie). Każdy może POST-em odpalić skan Stripe i wysyłkę maili. Gorzej: **re-invituje wiersze z `active=false`** - po pełnym refundzie Klarny (webhook ustawia active=false) wywołanie reconcile w oknie 7 dni od zakupu PRZYWRACA dostęp i wysyła mail powitalny (refundowana sesja ma nadal `payment_status: "paid"`). W porcie: auth + sprawdzanie refundów charge'a, albo całkiem wyciąć na rzecz poprawnych webhooków.
2. **Ryzyko martwego pola `invoice.subscription`**: webhook czyta stare pole, które w API basil (2025+) przeniesiono do `invoice.parent.subscription_details`. Kształt eventu dyktuje wersja API ustawiona NA ENDPOINCIE webhooka w Dashboardzie (nieznana, poza repo). Jeśli endpoint jest na nowej wersji, maile o nieudanych płatnościach NIGDY nie wychodzą (cichy skip "non-subscription"). Zweryfikować na prodzie przed portem. Dla kontrastu `admin-stripe-legacy-audit` ma już obejście na analogiczny problem z `current_period_end`.
3. **Komentarze kłamią**: webhook-refund mówi "cancel at period end", kod robi cancel natychmiast; `create-klarna-checkout` opisuje "reuse recurring price IDs", a używa `price_data` (pole `priceId` w `PLAN_CONFIG` martwe).
4. **Legacy bez webhooka**: jest tylko jeden `STRIPE_WEBHOOK_SECRET` (konto current). Refundy i nieudane płatności na koncie legacy NIE generują żadnej reakcji (ścieżka "legacy" w handleRefund odpala się tylko dla eventów z current, do anulowania bliźniaczych subskrypcji po e-mailu). Stare subskrypcje legacy z nieudanym odnowieniem nie dostają maila.
5. **`charge.refunded` bez filtra produktu**: pełny refund JAKIEGOKOLWIEK charge'a (np. ebooka za grosze) kasuje natychmiast wszystkie subskrypcje klubowe tego e-maila na obu kontach i wyrzuca z Circle. Przy porcie: filtrować po metadata charge'a/payment intentu.
6. **Niespójna normalizacja e-maili**: webhook (ścieżka Klarna) nie robi lowercase, confirm/reconcile robią; `confirm-subscription` w ogóle nie normalizuje. `email` jest UNIQUE - "Jan@x.pl" i "jan@x.pl" to dwa wiersze. W porcie: lowercase+trim na wejściu wszędzie + migracja czyszcząca.
7. **Trzy kopie logiki nadawania dostępu Klarna** z różnicami (nadpisanie vs max przy `expires_at`, mail vs brak maila, 4 kopie `inviteToCircle`, 2 kopie szablonu maila). Port: jedna funkcja domenowa `grant_one_time_access()`.
8. **Hardcode wszystkiego**: price ID x2 backend + config Klarna, ceny x2 frontend (Pricing, Success/pixel), kwoty Klarny w groszach, publishable key x2, flaga promo w kodzie (`PROMO_CAMPAIGN_ACTIVE`, toggle = commit + deploy przez Lovable). Docelowo: tabela planów + ustawienia w adminie (decyzja 2026-06-10).
9. **`update-payment-method` bez weryfikacji tożsamości** (tylko e-mail), bez konta legacy (starzy członkowie odcięci od zmiany karty), a `return_url` `/aktualizuj-karte?confirmed=1&si=...` wraca na stronę, która tych paramów NIE obsługuje - karta po redirectcie 3DS jest zapisana w Stripe, ale `?action=confirm` nigdy nie leci: default PM i subskrypcje nieprzepięte, user widzi formularz e-maila od nowa. Działa tylko ścieżka bez redirectu.
10. **Spam SetupIntentów**: prefetch tworzy SetupIntent dla każdego wizytatora po 5 s; `getOrFetch` przy cache-miss tworzy DWA. Brak rate limitu na publicznym `create-checkout`. Śmietnik w Stripe + wektor na proste DoS-owanie limitów API.
11. **Brak idempotencji webhooków** (brak tabeli przetworzonych event id) i brak `Idempotency-Key` przy create'ach w Stripe - duplikat eventu/double-click ratowany tylko miękkimi checkami ("already subscribed", "already active").
12. **Idempotencja per plan**: aktywny subskrybent kupujący INNY plan dostaje drugą równoległą subskrypcję (podwójne obciążenia). Brak ostrzeżenia.
13. **NIP/faktury połowicznie**: Tax ID dokładany tylko nowym customerom, dla istniejących NIP ląduje wyłącznie w metadata; brak walidacji NIP; samo `wants_invoice` nigdzie dalej nie jest konsumowane automatycznie (faktury ogarniane ręcznie?).
14. **Cron poza repo**: brak konfiguracji harmonogramu dla reconcile (i innych sweepów) w repie - jeśli coś je woła, to konfiguracja żyje tylko w Supabase/zewnętrznie. Przy porcie odtworzyć harmonogram świadomie (APScheduler/cron w kontenerze).
15. **`?action=` w query stringu** przy POST do `update-payment-method` - dwa endpointy udające jeden. W porcie rozbić na osobne route'y.
