# Kontrakt portu fazy 2 (landing befreeclub.pl) - mapa endpointow, wlasnosc plikow, sygnatury

Umowa miedzy agentami piszacymi port edge functions na monolit FastAPI.
Fundament fazy 2 (core: stripe_client/email/meta_capi, modele 4 schematow,
migracja 0002 z seedem planow, stuby routerow, rejestracja w main.py) JUZ
ISTNIEJE - nie przepisuj go. Kazdy agent pisze TYLKO swoje pliki z sekcji 2
(stuby nadpisuje w calosci).

Zrodla prawdy: specy `docs/spec-landing/*.md` (semantyka biznesowa 1:1),
oryginalny TS w `/Users/tomasz/repos/befreeclub/befreeclub/supabase/functions`
(przy watpliwosci czytaj), `docs/PLAN_LANDING.md` (sekcje "Naprawy wpisane
w port" + "Decyzje Tomka 2026-06-10" - te naprawy SA czescia kontraktu).

**Konwencje fazy 1 obowiazuja nadal** (`docs/spec/port-kontrakt.md` sekcja 1):
CamelModel + dump() z `app.core.schemas`, daty jako IsoDateTime (toISOString
z `Z`), bledy `{"error": ...}` przez HTTPException/JSONResponse (globalne
handlery w main.py), `Depends(get_session)` w route'ach / `async_session_maker`
w serwisach i workerach, jawny `await session.commit()`, logger
`create_logger("<scope>")`, workery wg wzorca z fazy 1 (idempotentny start,
flaga reentrancy, stop = cancel).

## 1. Zasady twarde fazy 2 (rozszerzenie konwencji)

### 1.1 Bledy biznesowe = HTTP 4xx (SWIADOMA ZMIANA vs Supabase)

Oryginal zwracal bledy biznesowe jako **200 + {"error": ...}** (obejscie
supabase-js). Nowy front bedzie pisany pod nowe API, wiec port zwraca
**normalne kody HTTP**: 400 walidacja, 401/403 auth, 404 nie znaleziono,
409 stan (np. payment nie-paid), 410 wygasly link, 429 rate limit, 502 blad
zewnetrznego API. Body zawsze `{"error": "<komunikat>"}`. **Komunikaty PL
widoczne dla usera kopiuj BAJT W BAJT ze specow/TS** (np. "Nie znaleziono
aktywnej subskrypcji dla tego adresu email.", "Link wygasł. Napisz na
krystian@befreeclub.pl po nowy."). Wyjatek: `POST /promo/validate` zwraca
200 z `{"valid": false, "reason": ...}` - to odpowiedz biznesowa, nie blad
(ksztalt 1:1 ze specem).

### 1.2 Normalizacja maili

`from app.core.email import normalize_email` (lower+trim) na **KAZDYM**
wejsciu emaila: request body, email ze Stripe (customer_details, receipt_email,
billing_details), email z Circle. DB to wymusza: `members.members` ma CHECK
`email = lower(btrim(email))` - insert nieznormalizowanego = wyjatek.
Tabele billing/newsletter nie maja CHECK (porty), ale zasada obowiazuje.

### 1.3 Magic linki HMAC (wzor z request-cancellation, 1:1)

```
payload = {"email": <znormalizowany>, "exp": <epoch MILISEKUNDY>, ...ekstra}
token   = b64url(JSON(payload)) + "." + b64url(HMAC_SHA256(secret, b64url(JSON(payload))))
```

- b64url bez paddingu (`=` zdjete), JSON bez spacji.
- Weryfikacja: split po "." (dokladnie 2 czesci), przelicz HMAC,
  **`hmac.compare_digest`** (constant-time), potem parse payloadu i check
  `exp` vs `Date.now()` w ms. Blad = jeden wspolny komunikat (anulowanie:
  "Link wygasł lub jest nieprawidłowy. Wróć na stronę anulowania i wyślij
  nowy.").
- Sekrety: anulowanie -> `CANCELLATION_DOI_SECRET` (exp +60 min, ekstra pole
  `reason` gdy 1..60 znakow); zmiana karty -> TEN SAM `CANCELLATION_DOI_SECRET`
  z ekstra polem `purpose: "update_payment_method"` (exp +60 min) - payloady
  rozroznialne, sekret jeden; newsletter DOI -> `NEWSLETTER_DOI_SECRET`
  (exp +14 dni, ekstra pole `name`).
- Tokeny sa stateless; ZUZYWALNOSC zalezy od flow (stan po review 2.1):
  token ANULOWANIA jest JEDNORAZOWY (rejestr zuzytych in-memory w
  billing/services/magic_link.py; restart procesu resetuje rejestr, okno
  ponownego uzycia ogranicza exp 60 min; token NIE jest zuzywany, gdy
  confirm konczy sie 404/bledem Stripe). Token zmiany karty pozostaje
  wielokrotnego uzytku w oknie exp. Potwierdzenie wymaga KLIKNIECIA na
  stronie (front robi POST z przycisku, nie z useEffect) - to naprawa,
  odnotowana dla agenta frontu.

### 1.4 Rate limiting (publiczne endpointy)

Reuzyj in-memory limitera fazy 1: `from app.modules.admin.services.rate_limit
import is_locked, record_failure, client_ip`. Wzorzec w handlerze:

```python
key = f"<nazwa-endpointu>|{client_ip(request)}"
if is_locked(key)["locked"]:
    raise HTTPException(429, "Zbyt wiele prób. Spróbuj ponownie później.")
record_failure(key)   # kazdy request zuzywa probe
```

Polityka domyslna limitera (5 prob / 15 min -> lock 1 h) pasuje do endpointow
WYSYLAJACYCH MAILE: `/cancellation/request`, `/payment-method/request-link`,
`/newsletter/subscribe`, `/newsletter/contact`. Dla endpointow checkoutu
(`/checkout/setup-intent`, `/checkout/klarna`, `/ebook/payment-intent`,
`/promo/validate`) 5/15min to za malo (retry po bledzie karty) - agent
[billing-checkout] kopiuje wzorzec `_Bucket` z rate_limit.py do
`app/modules/billing/services/rate_limit.py` z parametrami: 30 prob / 15 min,
lock 15 min. Webhooki i endpointy admin: bez limitera.

### 1.5 Stripe

- `from app.core.stripe_client import ...` - StripeAccount, get_client,
  find_on_accounts, configured_accounts, webhook_secret_for, request_options,
  new_idempotency_key. apiVersion przypiety: `2025-08-27.basil`.
- **Tylko metody async przez namespace v1**:
  `await client.v1.customers.list_async(params={"email": email, "limit": 100})`.
- Kazde "znajdz po emailu" przeszukuje OBA konta przez `find_on_accounts`
  (kolejnosc current -> legacy). Jedna wspolna funkcja
  `billing.services.subscriptions.find_subscriptions_by_email` (sekcja 3)
  zamiast 5 niespojnych implementacji oryginalu.
- **Idempotency-Key przy KAZDYM create** (naprawa #6). Klucz naturalny gdy
  jest: `subscriptions.create` -> `f"sub-create-{setup_intent_id}"`,
  Klarna session -> `new_idempotency_key("klarna")`, ebook PI ->
  `new_idempotency_key("ebook-pi")`. Przekaz przez
  `options=request_options(idempotency_key=...)`.
- Pulapki basil: `current_period_end` czytaj z `items.data[0].current_period_end`
  z fallbackiem na stare pole suba; subskrypcje faktury czytaj z OBU pol:
  `invoice.parent.subscription_details.subscription` ORAZ legacy
  `invoice.subscription` (naprawa #1).
- Webhook: `stripe.Webhook.construct_event(payload_bytes, sig_header, secret)`
  (sync, lokalna kryptografia) z sekretem per konto (`webhook_secret_for`).

### 1.6 Maile

`from app.core.email import send_email, EmailConfigError, EmailSendError,
DEFAULT_FROM, is_configured`. From defaultowy = `Be Free Club
<noreply@befreeclub.pl>`; nadawcy konfigurowalne per flow:
`CANCELLATION_FROM_EMAIL` (fallback DEFAULT_FROM), `NEWSLETTER_FROM_EMAIL`
(fallback `Be Free Club <krystian@befreeclub.pl>`). **Tresci HTML i tematy
maili PL kopiuj DOSLOWNIE z oryginalnych index.ts** (ciemna karta BFC
#1a1b1f/#2c2d31/#ECE183, stopki, reply-to). Mail DOI newslettera: timestamp
pl-PL Europe/Warsaw w temacie + naglowek `X-Entity-Ref-ID: {uuid}` (Gmail
nie skleja watkow) - zachowac. Defaulty URL: `FRONTEND_URL` fallback
`https://befreeclub.pl`, `CONFIRM_URL_BASE` fallback
`https://befreeclub.pl/newsletter/potwierdz`.

### 1.7 Sekrety

Tylko nazwy env przez `settings` (`app.core.config`) - pola juz dopisane,
wszystkie opcjonalne (pusty string = None). Nowe vs Supabase:
`STRIPE_LEGACY_WEBHOOK_SECRET`, `META_PIXEL_ID`, `META_CAPI_TOKEN`,
`EBOOK_FILE_PATH`. Wyciete: `SUPABASE_*` (nie istnieja), `ADMIN_TOKEN`
(zastapiony sesja panelu z fazy 1; stary token do rotacji przy migracji).

## 2. Mapa: edge function -> nowy endpoint (26 funkcji)

Auth: **public** (+ewentualny rate limit wg 1.4), **admin** = require_auth
z fazy 1 (montowanie w main.py juz zalatwia dependency), **webhook** =
podpis Stripe w handlerze, **token** = HMAC/token w body lub query.

| # | Edge function | Nowy endpoint | Auth | Zmiany vs oryginal |
|---|---|---|---|---|
| 1 | create-checkout | POST `/api/billing/checkout/setup-intent` | public, RL-checkout | + Idempotency-Key; request przyjmuje `attribution` (sekcja 4), ale zapis atrybucji robi dopiero confirm (prefetch frontu nie smieci tabela) |
| 2 | confirm-subscription | POST `/api/billing/checkout/confirm` | public | + normalize_email; + attribution.store(kind=subscription, object=setupIntentId) i kopiowanie utm/fbclid/fbp/fbc do metadata subskrypcji; + members.provision zamiast copy-paste inviteToCircle+upsert; + Idempotency-Key `sub-create-{seti}`; response dodatkowo `latestInvoiceId` (pixel Purchase eventID); bledy 4xx zamiast 500/200; + blokada drugiej rownoleglej suby (review 2.1): OBA konta, wszyscy customerzy emaila, statusy active/trialing/past_due/unpaid -> 409 z SECOND_PLAN_MESSAGE (ten sam plan + active = idempotentne `alreadyExisted`); lookup customera z fallbackiem customers.search (case-insensitive) |
| 3 | validate-promo | POST `/api/billing/promo/validate` | public, RL-checkout | ksztalt odpowiedzi 1:1 (zawsze 200 z `valid`); lookup tylko konto current |
| 4 | create-klarna-checkout | POST `/api/billing/checkout/klarna` | public, RL-checkout | + attribution.store(kind=klarna, object=cs_id, email=NULL) + utm do metadata sesji I payment_intent_data.metadata; + Idempotency-Key; semantyka sesji 1:1 (mode=payment, klarna+card+blik, price_data ad-hoc, kwoty z billing.plans zamiast PLAN_CONFIG) |
| 5 | confirm-klarna-checkout | POST `/api/billing/checkout/klarna/confirm` | public | wspolna `grant_one_time_access` (sekcja 3) zamiast 3 kopii; expires_at = session.created + N miesiecy (kotwica, review 2.1 - nie "teraz + N"), bump tylko w gore (max); odmowa gdy charge sesji zrefundowany (naprawa prowizorki #1); response dodatkowo `paymentIntentId` (pixel) |
| 6 | reconcile-klarna-checkouts | worker `billing/services/klarna_reconcile_worker.py` + POST `/api/billing/admin/workers/klarna_reconcile/run` | admin (trigger) | KONIEC z publicznym endpointem; sprawdza refundy przed nadaniem dostepu; expires kotwiczone w session.created (max-bump - sweep co godzine nie pelza terminem); ta sama `grant_one_time_access` |
| 7 | update-payment-method | POST `/api/billing/payment-method/request-link` + `/setup-intent` + `/confirm` | public, RL-mail (request-link) / token HMAC (setup-intent) | NAPRAWA #2: magic link HMAC na maila zamiast "podaj email"; request-link zwraca ZAWSZE `{"ok": true}` (anty-enumeracja - swiadoma zmiana); szuka klienta na OBU kontach (legacy odzyskuje zmiane karty); confirm przepina default PM + suby + retry otwartych faktur jak oryginal; naprawiony powrot z 3DS (front po redirectcie wola /confirm z setupIntentId z URL) |
| 8 | stripe-webhook | POST `/api/billing/webhooks/stripe/current` | webhook (STRIPE_WEBHOOK_SECRET) | idempotencja przez billing.webhook_events (INSERT ... ON CONFLICT (event_id) DO NOTHING - duplikat = natychmiast 200); charge.refunded FILTRUJE po produkcie (metadata.product=="ebook" -> tylko ebook: status refunded + revoked_at na tokenach, ZERO ruszania subskrypcji); refund subowy: cancel na obu kontach + members.schedule_removal; invoice.payment_failed czyta OBA pola subscription; NOWE eventy: invoice.payment_succeeded (CAPI Purchase sub), payment_intent.succeeded z metadata.product=="ebook" (fulfillment webhook-first - tworzy order+token+mail bez udzialu przegladarki); Klarna jak oryginal przez grant_one_time_access; strzaly Meta CAPI wg sekcji 4; zly podpis -> 400; blad obslugi -> wpis `error`, processed_at NULL, response 200 |
| 9 | (brak - NOWY) | POST `/api/billing/webhooks/stripe/legacy` | webhook (STRIPE_LEGACY_WEBHOOK_SECRET) | NAPRAWA #4: konto legacy dostaje webhook; obsluguje invoice.payment_failed (mail o nieudanej platnosci - starzy czlonkowie w koncu cos dostaja) i charge.refunded; wspolne handlery z parametrem konta |
| 10 | request-cancellation | POST `/api/billing/cancellation/request` | public, RL-mail | logika i mail 1:1 (HMAC token exp 60 min, reason 1..60 znakow do payloadu); "Nie znaleziono aktywnej subskrypcji..." jako 404 |
| 11 | confirm-cancellation | POST `/api/billing/cancellation/confirm` | token HMAC | cancel_at_period_end na obu kontach 1:1; wpis cancellation_reasons ZAWSZE (reason z payloadu albo `"not-given"` - panel potrzebuje pelnej historii); response `{success, cancelled, access_until}` 1:1 |
| 12 | pause-subscription | **NIE PORTOWANE** | - | martwy flow (nic nie generuje 6-cyfrowych kodow, tabela cancellation_tokens bez producenta - NIE portujemy i jej tez nie); pauza zyje tylko jako akcja admina (#16) |
| 13 | circle-cleanup | worker `members/services/cleanup_worker.py` + POST `/api/billing/admin/workers/membership_cleanup/run` | admin (trigger) | KONIEC z publicznym endpointem; logika na enum statusu members.members + flaga `protected` z DB (koniec z PROTECTED_EMAILS w kodzie); one_time po expires_at, ale wygasly one_time/manual z ZYWA suba w Stripe zostaje (review 2.1); subskrypcyjni przez find_subscriptions_by_email (oba konta); GUARD konfiguracji jak oryginal (brak ktoregos klucza Stripe/Circle = abort przebiegu PRZED przetworzeniem kogokolwiek); usuniecie -> status `removed` + wpis members.events (czyste keep tylko do loggera) |
| 14 | retry-circle-invites | worker `members/services/invite_retry_worker.py` + POST `/api/billing/admin/workers/invite_retry/run` | admin (trigger) | NAPRAWA: ponawia TYLKO status `invite_failed` (nigdy `removed` - koniec z re-invitowaniem wyrzuconych); event invite_retried tylko przy sukcesie |
| 15 | sync-circle-ids | POST `/api/members/sync-circle-ids` (bez `/run` - jednorazowa operacja, nie worker) | admin (trigger) | narzedzie one-off 1:1 (paginacja Circle, match po lowercase email, uzupelnia circle_member_id) |
| 16 | admin-pause-subscription | POST `/api/billing/admin/subscriptions/pause` | admin | wyszukiwanie subow PELNE jak self-service (100 customerow, statusy active/trialing/past_due/unpaid), nie limit 1/active; + billing.audit_log; + members status `paused` (provisioning.set_pause_state; powrot na `active` robi admin-extend/clear_pause oraz webhook invoice.paid przy naturalnym wznowieniu); remove_from_circle przez members (status, nie bool) |
| 17 | admin-extend-subscription | POST `/api/billing/admin/subscriptions/extend` | admin | semantyka trial_end/add_months/clear_pause/resumes_at 1:1 (mechanizm przedluzenia = trial, dwustopniowo przy pauzie); + audit_log |
| 18 | admin-list-cancellations | GET `/api/billing/admin/cancellations` | admin | ostatnie 200 wierszy cancellation_reasons desc, `{"rows": [...]}` |
| 19 | admin-reinvite-circle | POST `/api/members/{member_id}/reinvite` (per id, nie po emailu; 404 gdy brak; body opcjonalne `{skip_invitation}` default true) | admin | members.provision(source=member.source - source czlonka zostaje bez zmian, skip_invitation); UWAGA z oryginalu: manual bez suby w Stripe wywali nastepny cleanup - admin moze od razu ustawic `protected` |
| 20 | admin-stripe-legacy-audit | GET `/api/billing/admin/legacy-audit` | admin | KONIEC z tokenem hardcoded w zrodle; agregaty/risks/problem_rows 1:1 ze speca (billing-lifecycle.md sekcja 5) |
| 21 | create-ebook-checkout | **NIE PORTOWANE** | - | martwy flow B (modal nieimportowany); jedyny flow ebooka = PaymentIntent |
| 22 | create-ebook-payment-intent | POST `/api/billing/ebook/payment-intent` | public, RL-checkout | kwota z billing.plans (slug `ebook`), metadata `{product: "ebook"}` 1:1; + attribution.store(kind=ebook, object=pi_id, email=NULL) + utm do metadata PI; + Idempotency-Key; BEZ wiersza pending z placeholderowym emailem (dlug #11) - order powstaje przy potwierdzeniu z realnym emailem |
| 23 | confirm-ebook-purchase | POST `/api/billing/ebook/confirm` | public | tylko sciezka `paymentIntentId` (sessionId byl martwym flow B); idempotentny: upsert orderu po stripe_payment_intent_id (UNIQUE), reuse waznego tokenu, mail RAZ (guard email_sent_at); fulfillment jest webhook-first - ten endpoint to przyspieszacz UX (front retry'uje 8x2s, wiec 409 gdy PI nie-succeeded zostaje); response `{success, email, downloadUrl, token}` 1:1 |
| 24 | download-ebook | GET `/api/billing/ebook/download?token=...` | token z DB | streaming PDF z `EBOOK_FILE_PATH` (FileResponse, naglowek content-disposition `Na-swoich-zasadach-jako-freelancer.pdf`) zamiast signed URL; licznik ATOMOWO (`UPDATE ... SET download_count = download_count + 1 WHERE download_count < max_downloads ... RETURNING`); check revoked_at (refund); komunikaty 1:1: 404 "Nieprawidłowy link", 410 "Link wygasł. Napisz na krystian@befreeclub.pl po nowy.", 429 "Limit pobrań wyczerpany. Napisz na krystian@befreeclub.pl." |
| 25 | newsletter-subscribe | POST `/api/newsletter/subscribe` | public, RL-mail | walidacje i mail DOI 1:1 (name 1..80, email regex+max 255 lowercase, token HMAC 14 dni, temat z timestampem, X-Entity-Ref-ID); 502 gdy Resend padnie |
| 26 | newsletter-confirm | POST `/api/newsletter/confirm` | token HMAC | push Sender.net 1:1 (POST /subscribers -> fallback PATCH /subscribers/{email}, trigger_automation: true, SENDER_GROUP_IDS fallback "epnLzm,el06vl", trim tokenu + zdjecie prefiksu "Bearer "); + Meta CAPI **Lead** (sekcja 4); response `{ok: true, name}` |
| 27 | send-contact-email | POST `/api/newsletter/contact` | public, RL-mail | INSERT do newsletter.contact_messages robi BACKEND (koniec z anon key z frontu) + best-effort mail do krystian@befreeclub.pl (blad maila NIE psuje requestu - zapis do DB wystarcza, jak oryginal); brak RESEND_API_KEY = cichy sukces (1:1) |

Nieportowane podsumowanie: **pause-subscription** (martwy flow kodow),
**create-ebook-checkout** (martwy flow B), tabele `cancellation_tokens`
i `newsletter_subscribers` (martwe, bez odpowiednika w nowym schemacie).

Dodatkowe endpointy bez odpowiednika w edge functions:
- GET `/api/billing/plans` (public) - cennik z DB, ZAIMPLEMENTOWANY w fundamencie.
- `/api/landing/*` - tresc landinga (artykuly, content_blocks), faza 2.3, stuby istnieja.
- GET `/api/members` (admin) - listing czlonkow do panelu Subskrypcje ([admin-api]).
- POST `/api/billing/admin/payment-method/send-link` (admin) - "wyslij link zmiany karty" z panelu.
- POST `/api/billing/admin/webhook-events/{id}/reprocess` (admin, review 2.1) -
  reczne ponowienie eventu po bledzie obslugi/crashu (Stripe nie retry'uje,
  bo dedup po event_id odpowiada 200; handlery idempotentne).

Eventy do zasubskrybowania w Stripe Dashboard przy przepieciu (notatka do
migracji): current = `invoice.payment_failed`, `invoice.payment_succeeded`,
`checkout.session.completed`, `checkout.session.async_payment_succeeded`,
`charge.refunded`, `payment_intent.succeeded`; legacy =
`invoice.payment_failed`, `charge.refunded`.

## 3. Wlasnosc plikow per agent

```
backend/
  app/core/
    stripe_client.py, email.py, meta_capi.py            [fundament-2 GOTOWE]
    config.py (pola fazy 2)                             [fundament-2 GOTOWE]
  app/modules/billing/
    models.py                                           [fundament-2 GOTOWE]
    schemas.py            (AttributionIn, PlanOut gotowe; wlasne DTO dopisuja
                           agenci NA KONCU pliku, nie ruszajac cudzych)
    services/plans.py, services/attribution.py          [fundament-2 GOTOWE]
    routes/plans.py                                     [fundament-2 GOTOWE]
    routes/checkout.py, routes/promo.py,
      routes/payment_method.py (STUBY -> port)          [billing-checkout]
    services/checkout.py, services/promo.py,
      services/payment_method.py, services/rate_limit.py (nowe) [billing-checkout]
    routes/ebook.py (STUB -> port), services/ebook.py (nowy)    [billing-ebook]
    routes/webhooks.py (STUB -> port),
      services/webhook_handlers.py, services/capi_events.py (nowe) [billing-webhook]
    routes/cancellation.py (STUB -> port),
      services/cancellation.py, services/magic_link.py (nowe)   [billing-lifecycle]
    services/subscriptions.py (nowy - WSPOLNE szukanie subow,
      sygnatury sekcja 4; pisze billing-lifecycle, uzywaja wszyscy) [billing-lifecycle]
    services/klarna_reconcile_worker.py (nowy)          [workers]
    routes/admin.py (STUB -> port), services/audit.py (nowy)    [admin-api]
  app/modules/members/
    models.py, schemas.py                               [fundament-2 GOTOWE / wspolny]
    services/provisioning.py (STUB -> pelny port)       [members]
    services/circle.py (nowy - klient Circle Admin API v2:
      invite 3 proby backoff 1s/2s/3s, remove, list paginowany)  [members]
    services/cleanup_worker.py, services/retry_invites.py,
      services/sync_circle_ids.py (nowe)                [workers]
    routes/admin.py (STUB -> port; trigger'y workerow wolaja
      funkcje z services/)                              [admin-api]
  app/modules/newsletter/
    models.py                                           [fundament-2 GOTOWE]
    schemas.py, routes/public.py (STUB -> port),
      services/doi.py, services/sender.py, services/contact.py (nowe) [newsletter-contact]
  app/modules/landing/                                  (stub; faza 2.3, przyszly agent)
  app/main.py            - rejestracja JUZ zrobiona; [workers] dopisuje TYLKO
                           start/stop workerow w lifespan (wzorzec fazy 1)
  alembic/versions/0002_*.py                            [fundament-2 GOTOWE]
  tests/test_stripe_client.py, test_email.py,
    test_meta_capi.py, conftest.py                      [fundament-2 GOTOWE]
  tests/test_<twoj-modul>_*.py                          kazdy agent swoje
```

Zelazna zasada schematow PG: tabele schematu pisze tylko modul-wlasciciel.
Billing NIE pisze do members.members - wola members.provision / schedule_removal.
Members NIE czyta Stripe bezposrednio - dostaje decyzje od billing
(wyjatek: cleanup_worker wola billing.services.subscriptions, bo to odczyt).

## 4. Sygnatury cross-module (ZAMROZONE)

Wszystko `async def`. Zmiana sygnatury = uzgodnienie w tym pliku, nie po cichu.

```python
# app/modules/members/services/provisioning.py            [members]
@dataclass(frozen=True) ProvisionResult:
    member_id: int; circle_invited: bool; circle_member_id: str | None; already_active: bool
async def provision(email: str, name: str | None, *, source: str,
                    expires_at: datetime | None = None,
                    skip_invitation: bool = False) -> ProvisionResult
    # invite do Circle (3 proby, backoff 1s/2s/3s; 4xx poza 429 bez retry) +
    # upsert members.members po znormalizowanym emailu + wpis members.events.
    # Caly przebieg serializowany per email (pg_advisory_xact_lock, review 2.1
    # - rownolegly confirm+webhook nie robi IntegrityError ani 2x invite).
    # Nieudany invite NIE rzuca: circle_invited=False, status invite_failed.
    # Czlonek juz active: bez ponownego invite (already_active=True); dla
    # one_time aktualizuje expires_at TYLKO w gore (max); source NIE jest
    # degradowane z one_time na subscription dopoki expires_at w przyszlosci;
    # event "extended" tylko gdy cos sie zmienilo (review 2.1).
async def set_pause_state(email: str, paused: bool, *, by: str | None = None) -> bool
    # flip statusu active <-> paused (pauza adminowa #16, wznowienie z
    # webhooka invoice.paid); inne statusy = no-op False. NOWE w review 2.1.
async def schedule_removal(email: str, *, reason: str) -> bool
    # status pending_removal + wpis events; False gdy brak czlonka albo protected.
    # Fizyczne usuniecie z Circle robi cleanup_worker.
async def is_protected(email: str) -> bool

# app/modules/billing/services/plans.py                   [fundament - GOTOWE]
async def get_by_slug(slug: str, *, session: AsyncSession | None = None) -> Plan | None
async def list_active(*, session: AsyncSession | None = None) -> list[Plan]

# app/modules/billing/services/attribution.py             [fundament - GOTOWE]
async def store(session, *, kind: str, stripe_object_id: str, email: str | None = None,
                attribution: AttributionIn | None = None, client_ip: str | None = None,
                client_ua: str | None = None) -> CheckoutAttribution
    # INSERT bez commitu (caller commituje razem z reszta checkoutu).

# app/modules/billing/services/subscriptions.py           [billing-lifecycle]
CANCELLABLE_STATUSES = {"active", "trialing", "past_due", "unpaid"}
KEEP_STATUSES = {"active", "trialing", "past_due", "unpaid", "incomplete", "paused"}
@dataclass FoundSubscription:
    account: StripeAccount; customer_id: str; subscription: dict  # surowy obiekt SDK
async def find_subscriptions_by_email(email: str, *, statuses: set[str] | None = None,
                                      max_customers: int = 100) -> list[FoundSubscription]
    # OBA konta (current -> legacy), wszyscy customerzy po emailu, wszystkie suby.
async def has_live_access(email: str) -> bool
    # logika KEEP z circle-cleanup: KEEP_STATUSES albo canceled z
    # current_period_end (items[0] -> fallback stare pole) w przyszlosci.
async def grant_one_time_access(*, email: str, duration_months: int,
                                payment_intent_id: str | None,
                                purchased_at: datetime | None = None) -> ProvisionResult
    # wspolna logika Klarny (3 kopie oryginalu -> jedna): refund-check charge'a,
    # members.provision(source="one_time",
    #   expires_at=max(stare, (purchased_at|teraz) + N miesiecy)) -
    # purchased_at = checkout_session.created (kotwica, review 2.1: sweep
    # reconcile co godzine nie moze pelzac terminem),
    # mail powitalny Klarna (tresc 1:1 ze stripe-webhook - wersja ze zdaniem o ratach).

# app/core/email.py                                       [fundament - GOTOWE]
def normalize_email(email: str) -> str                    # sync; lower+trim
def is_configured() -> bool
async def send_email(*, to: str | list[str], subject: str, html: str,
                     from_email: str | None = None, reply_to: str | None = None,
                     headers: dict[str, str] | None = None) -> str   # id wiadomosci
    # rzuca EmailConfigError (brak klucza) / EmailSendError (.status, .body)

# app/core/meta_capi.py                                   [fundament - GOTOWE]
def hash_email(email: str) -> str                         # sync; sha256(lower+trim)
def is_configured() -> bool
async def send_event(*, event_name: str, event_id: str, event_time: int,
                     user_data: CapiUserData, custom_data: CapiCustomData | None = None,
                     event_source_url: str | None = None,
                     action_source: str = "website") -> bool
    # CapiUserData(email=SUROWY, fbc, fbp, client_ip, client_ua) - hashuje klient.
    # CapiCustomData(value=PLN floatem nie grosze, currency, content_name).
    # False gdy wylaczony (brak env, log info raz) albo blad (log warn) - NIGDY nie rzuca.

# app/core/stripe_client.py                               [fundament - GOTOWE]
class StripeAccount(StrEnum): CURRENT = "current"; LEGACY = "legacy"
def get_client(account) -> stripe.StripeClient            # raise StripeConfigError
def configured_accounts() -> list[StripeAccount]          # [current, legacy]
def webhook_secret_for(account) -> str | None
async def find_on_accounts(fn) -> tuple[StripeAccount, T] | None
    # fn: async (account, client) -> T | None; pierwszy nie-None wygrywa
def request_options(*, idempotency_key: str | None = None) -> dict
def new_idempotency_key(prefix: str) -> str

# app/modules/billing/services/audit.py                   [admin-api]
async def log_action(session, *, admin_user_id: int | None, action: str,
                     target_email: str | None, payload: dict) -> None
    # w dev require_auth daje DEV_FAKE_AUTH (id=0, nie istnieje w admin.users) -
    # wtedy zapisuj admin_user_id=None (kolumna nullable wlasnie po to).
```

## 5. Spec analityki (TWARDE WYMAGANIE)

### 5.1 Ksztalt atrybucji w requestach checkoutu

Kazdy request tworzacy/finalizujacy checkout przyjmuje opcjonalne pole
`attribution` (DTO `AttributionIn` w `billing/schemas.py`, GOTOWE):

```json
{ "attribution": { "utmSource": "...", "utmMedium": "...", "utmCampaign": "...",
                   "utmTerm": "...", "utmContent": "...", "fbclid": "...",
                   "fbp": "...", "fbc": "...", "referrer": "...",
                   "landingPage": "..." } }
```

Wszystkie pola opcjonalne; brak calego `attribution` = legalny (organic).
Front zbiera UTM-y/fbclid na WEJSCIU na strone (sessionStorage), fbp/fbc
z cookies `_fbp`/`_fbc`. client_ip i client_ua bierze BACKEND z requestu
(`client_ip(request)` z fazy 1 + naglowek user-agent), nie front.

Punkty zapisu (attribution.store) i powiazany stripe_object_id:

| kind | endpoint zapisujacy | stripe_object_id | email |
|---|---|---|---|
| subscription | `/checkout/confirm` | setup intent (`seti_`) | z requestu |
| klarna | `/checkout/klarna` | checkout session (`cs_`) | NULL (zbiera Stripe) |
| ebook | `/ebook/payment-intent` | payment intent (`pi_`) | NULL (placeholder nie istnieje) |

Dodatkowo utm_*/fbclid/fbp/fbc wedruja do **metadata Stripe** (subskrypcji /
sesji+PI Klarny / PI ebooka) - webhook ma je pod reka bez JOINa.

### 5.2 event_id (deduplikacja CAPI <-> Meta Pixel)

Front MUSI uzyc tego samego event_id w pixelu (`fbq('track', 'Purchase',
{...}, {eventID})`), dlatego endpointy potwierdzen zwracaja potrzebne id.

| Event | event_id | Skad front go ma |
|---|---|---|
| Purchase (subskrypcja) | **id pierwszej faktury** suba (`in_...`) | `latestInvoiceId` w response `/checkout/confirm` |
| Purchase (Klarna) | **payment_intent id** sesji (`pi_...`) | `paymentIntentId` w response `/checkout/klarna/confirm` |
| Purchase (ebook) | **payment_intent id** (`pi_...`) | front ma go z URL-a powrotu |
| Lead (newsletter) | `sha256(normalize_email(email) + ":" + YYYY-MM-DD)` (UTC) | response `/newsletter/confirm` może zwrocic `eventId` |

### 5.3 Kto strzela CAPI

**Purchase: WYLACZNIE webhook handler** ([billing-webhook]) - konwersja
liczy sie nawet bez powrotu przegladarki:
- subskrypcja: `invoice.payment_succeeded` z `billing_reason=subscription_create`
  (kolejne odnowienia NIE sa Purchase),
- Klarna: `checkout.session.completed` / `async_payment_succeeded` (paid),
- ebook: `payment_intent.succeeded` z `metadata.product == "ebook"`.

user_data: email z eventu (znormalizowany, hashuje klient CAPI), fbp/fbc/
client_ip/client_ua z `checkout_attributions` (lookup po stripe_object_id
albo z metadata Stripe). Gdy brak `fbc`, a jest `fbclid`: zbuduj
`fb.1.<created_at_ms_atrybucji>.<fbclid>`. custom_data: value = kwota w PLN
(float, `amount/100`), currency "pln", content_name = slug planu / "ebook".
event_source_url: landing_page z atrybucji (fallback `https://befreeclub.pl`).

**Lead: handler `/newsletter/confirm`** ([newsletter-contact]) - jedyny
moment potwierdzonej konwersji newslettera (nie ma webhooka). Brak atrybucji
w tym flow - user_data tylko email (+ ip/ua z requestu).

Idempotencja strzalow: webhook_events gwarantuje jednorazowa obsluge eventu
Stripe; przy retrym po bledzie (processed_at NULL) dopuszczalny drugi strzal -
Meta deduplikuje po event_id.

## 6. Srodowisko dev / weryfikacja (kazdy agent po robocie)

```
cd /Users/tomasz/repos/befreeclub/migracja/befreeclub/backend
uv run ruff check app tests --fix
uv run python -c "import app.main"
uv run pytest tests/<twoje> -q
# migracja (tylko gdy zmieniasz schemat - schemat fazy 2 jest GOTOWY, nie ruszaj):
dropdb --if-exists befreeclub_test && createdb befreeclub_test
DATABASE_URL= DB_NAME=befreeclub_test uv run alembic upgrade head
```

Testy: pytest + respx/httpx MockTransport dla logiki krytycznej (webhook
handlery z podpisem, magic linki, grant_one_time_access, promo). NIE dotykac
baz `bfc_admin` ani `befreeclub` (bez `_test`). Sekrety tylko nazwami env.
