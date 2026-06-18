# Spec: Frontend landinga befreeclub.pl (apka `befreeclub/`)

Inwentaryzacja pod przepisanie na VPS (FastAPI, modularny monolit). Stan na 2026-06-10.
Zakres: routing, mapa stron, źródła treści, flow użytkownika, wywołania edge functions, env vars, storage keys, tracking. Backend (edge functions, DB, Stripe) ma osobny spec - tu opisuję wszystko od strony przeglądarki, łącznie z payloadami.

Stack: React 18 + Vite (SWC) + react-router-dom (BrowserRouter) + Tailwind/shadcn + framer-motion + @stripe/react-stripe-js + supabase-js. Deploy: Lovable. Brak SSR - czysty SPA.

---

## 1. Routing i szkielet aplikacji

`src/main.tsx` -> `createRoot` -> `App`. `index.html` zawiera statyczny loader `#initial-loader` (spinner CSS w środku `#root`, znika gdy React zamontuje, bo render nadpisuje zawartość `#root`).

`src/App.tsx`:
- `QueryClientProvider` (react-query jest zainstalowany, ale **nigdzie nieużywany** poza providerem),
- `Toaster` (shadcn) + `Sonner` (dwa systemy toastów naraz; sonner używany w checkout/admin, use-toast w newsletter/kontakt),
- `ScrollToTop` - na mount robi `window.scrollTo(1,1)` (hack), przy zmianie ścieżki scroll top, przy hashu (`/#pricing`) `scrollIntoView` z 50 ms opóźnieniem,
- `PendingConfirmationRetry` - globalny hook retry potwierdzenia subskrypcji (sekcja 5.4).

### Tabela routingu (22 strony)

| Route | Komponent | Rola |
|---|---|---|
| `/` | `Index` | Landing sprzedażowy (sekcje, sekcja 3) |
| `/sukces` | `Success` | Strona po zakupie subskrypcji + server-side confirm (3DS/Klarna fallback) |
| `/regulamin` | `Terms` | Regulamin, statyczny JSX |
| `/polityka-prywatnosci` | `Privacy` | Polityka prywatności, statyczny JSX |
| `/kontakt` | `Contact` | Formularz kontaktowy -> tabela `contact_messages` + mail |
| `/anuluj` | `Cancel` | Wieloetapowy flow anulowania/zamrażania subskrypcji |
| `/anuluj/potwierdz` | `CancelConfirm` | Landing magic linka z maila, wykonuje confirm |
| `/anuluj/anulowano` | `CancelSuccess` | Potwierdzenie anulowania, czyta `?access_until=` |
| `/aktualizuj-karte` | `UpdateCard` | Samoobsługowa zmiana karty (SetupIntent) |
| `/pierwszy-klient` | `PierwszyKlient` | Twardy redirect (`window.location.replace`) na `/wiedza/pierwszy-klient` (legacy URL) |
| `/wiedza` | `Wiedza` | Lista artykułów bloga |
| `/wiedza/:slug` | `WiedzaArticle` | Pojedynczy artykuł |
| `/admin` | `Admin` | Zalążkowy panel admina (sekcja 8) |
| `/ebook` | `Ebook` | Strona produktowa ebooka + inline checkout Stripe |
| `/ebook/sukces` | `EbookSuccess` | Potwierdzenie zakupu ebooka, polling confirm |
| `/ebook/pobierz` | `EbookDownload` | Pobieranie ebooka tokenem z maila |
| `/newsletter` | `Newsletter` | Dedykowana strona zapisu na newsletter (DOI) |
| `/newsletter/potwierdz` | `NewsletterConfirm` | Landing tokenu DOI |
| `/newsletter/witaj` | `NewsletterWelcome` | Powitanie po DOI + Pixel Lead |
| `/playbook` | `Playbook` | Artykuł "AI Playbook" za hasłem (lead magnet newslettera) |
| `/gwarancja` | `Gwarancja` | Strona warunków gwarancji 30 dni |
| `*` | `NotFound` | 404 (loguje console.error z pathname) |

---

## 2. Konfiguracja, env vars, klucze, storage

### Env vars frontu (`.env`, prefix VITE)
- `VITE_SUPABASE_URL` - URL projektu Supabase (klient supabase-js),
- `VITE_SUPABASE_PUBLISHABLE_KEY` - anon key,
- `VITE_SUPABASE_PROJECT_ID` - używany **tylko** w `Admin.tsx` do zbudowania `https://{id}.supabase.co/functions/v1`.

### Wartości zaszyte w kodzie (do przeniesienia do configu przy przepisaniu)
- Stripe **publishable key** `pk_live_51SsMPeDls...` wklejony literalnie w 3 plikach: `CheckoutModal.tsx`, `UpdateCard.tsx`, `Ebook.tsx`.
- Ref projektu Supabase `fshkdkvoyysphfrfvmni` zaszyty literalnie jako baza URL funkcji w: `CancelConfirm.tsx` (`CANCEL_FN_BASE`), `Newsletter.tsx` i `NewsletterCTA.tsx` (`NEWSLETTER_FN_BASE`). Te trzy strony robią surowe `fetch()` zamiast `supabase.functions.invoke`.
- Microsoft Clarity id `vsealx7t9v` (index.html, inline script).
- Meta Pixel id `963496946601553` (index.html, inline script; `PageView` strzela na load).
- Video VSL w Hero: Bunny Stream iframe `player.mediadelivery.net/embed/613167/40e166e2-240f-4f98-a3e5-0dbaba148957` + VideoObject JSON-LD.
- Hasło do `/playbook`: `zyciejestfajne1` (porównanie po normalizacji: lowercase, trim, zdjęcie diakrytyków).
- Maile kontaktowe w copy: `kontakt@befreeclub.pl` (Cancel), `krystian@befreeclub.pl` (UpdateCard, EbookSuccess, Gwarancja, Terms, Privacy, Contact, nadawca newslettera).

### Feature flag
`src/config/promo.ts`: `export const PROMO_CAMPAIGN_ACTIVE = true`. Steruje: bannerem kodu na Pricing, auto-aktywacją `?promo=KOD` z URL, sekcją "Mam kod promocyjny" w CheckoutModal. Zmiana = edycja pliku + commit + deploy.

### Klucze localStorage / sessionStorage
| Klucz | Storage | Co trzyma |
|---|---|---|
| `bfc_pending_confirmation` | localStorage | `{setupIntentId, planId, email, planName, wantInvoice?, nip?, promoCode?, savedAt}` - zapis przed `stripe.confirmSetup` (na wypadek redirectu 3DS), TTL 1 h |
| `bfc_admin_token` | sessionStorage | token admina wpisany w `window.prompt` |
| `bfc_playbook_unlocked` | localStorage | `"1"` po podaniu hasła do Playbooka |
| `bfc_exit_popup_shown` | sessionStorage | `"1"` - exit popup pokazany raz na sesję |

### Meta Pixel - eventy (`src/lib/fbpixel.ts`, cienki wrapper na `window.fbq`, fail-safe na adblock)
- `PageView` - automatycznie z base code w index.html (tylko raz, na load - SPA nie raportuje zmian route!),
- `InitiateCheckout` - klik "Wybierz plan" w Pricing (`{content_name, value, currency: PLN}`),
- `Purchase` - Success.tsx po `confirmed`, wartość z lokalnej mapy `PLAN_PRICE_PLN = {quarterly: 639, semiannual: 879, annual: 1489}`; **strzela tylko gdy `planId` przyszedł w query params** (patrz Prowizorki #9),
- `Lead` - NewsletterWelcome po DOI (`content_name: "newsletter_doi_confirmed"`).

### Build
`package.json`: `dev|build|build:dev|lint|preview|test (vitest)`. `vite.config.ts`: port 8080, host `::`, alias `@ -> ./src`, `lovable-tagger` tylko w dev, hmr overlay off, plugin sitemap (sekcja 7).

---

## 3. Strona główna `/` (Index) - sekcje i źródła treści

**Wszystkie treści landinga są hardcoded w komponentach TSX.** Zero DB, zero CMS, zero plików md. Każda zmiana ceny/opinii/copy = edycja kodu + deploy przez Lovable. To główny powód przeniesienia treści do panelu admina.

Kolejność sekcji: `Header` -> `Hero` -> `ProofBar` -> `WhyItWorks` -> `SocialProof` -> `LookInside` -> `ForWhom` -> `Timeline` -> `Pricing` -> `InvestmentMath` -> `FAQ` -> `NewsletterCTA` -> `FinalCTA` -> `Footer` + `AmbientBackground` (tło) + `ExitIntentPopup`.

| Komponent | Treść / dane (wszystko inline w pliku) |
|---|---|
| `Header` | fixed top bar, logo, CTA "Zobacz plany" scrollujące do `#pricing` (na podstronach bez sekcji pricing klik to no-op) |
| `Hero` | H1 "Zbuduj biznes, który da Ci wolność", subheadline, 3 benefity (z liczbą "70 000zł/msc"), iframe VSL Bunny Stream, VideoObject JSON-LD, 2 CTA (scroll do pricing / look-inside), nota gwarancji |
| `ProofBar` | 4 animowane liczniki: 4+ warsztaty/mies., 5+ narzędzi AI, 90+ lekcji, **200+ aktywnych członków**; rotator 16 mini-cytatów (shuffle poza pierwszym, zmiana co 5 s, klik scrolluje do `#opinie`) |
| `WhyItWorks` | sekcja "dlaczego działa", statyczne copy |
| `SocialProof` | **16 opinii** w tablicy `testimonials` (treść, imię, rola, zdjęcie z `src/assets/*.jpg/png` albo inicjały+kolor), ręczna tablica `displayOrder` ustalająca kolejność tak, by konkretne osoby nie sąsiadowały; dwa marquee (desktop) / trzy (mobile), pauza na hover, karty rozwijane |
| `LookInside` | 5 kart sticky-stack (Kursy/LIVE/Narzędzia AI/Community/Aplikacja) ze screenshotami z `src/assets`; opis "Kursy" ma specjalny link otwierający FAQ item przez CustomEvent `open-faq` z detail `item-1` (kruche sprzężenie z indeksem FAQ) |
| `ForWhom` | dla kogo jest klub, statyczne |
| `Timeline` | 4 kroki "dni 1-7 / do 14 / 14-30 / od 30" ze scroll-progress |
| `Pricing` | **cennik - patrz sekcja 5.1** |
| `InvestmentMath` | tabela ROI z zaszytymi liczbami: 147 zł/msc, 879 zł / 6 mies., 3 500 zł pierwszy klient, 52 370 zł średni zarobek, 5858% ROI |
| `FAQ` | 7 pytań; odpowiedzi to JSX (w tym rozbudowany opis modułów kursów i 5 narzędzi AI: AI Scoper, AI Sales Mentor, AI Offer Builder, AI Leads Generator, AI Skill Finder); nasłuchuje `window` event `open-faq` |
| `NewsletterCTA` | formularz newslettera (sekcja 6.4) |
| `FinalCTA` | "Liczba miejsc ograniczona", 4 highlighty, CTA scroll do pricing |
| `Footer` | linki: /wiedza, /regulamin, /polityka-prywatnosci, /kontakt, copyright |
| `ExitIntentPopup` | desktop only (`pointer: fine`), aktywny po 30 s, trigger `mouseleave` przy `clientY <= 10`, raz na sesję, nie pokazuje się gdy otwarty checkout (`[data-checkout-modal]` w DOM); copy "od **4 zł/dzień**" + gwarancja; CTA scroll do pricing |

Opinie/teaserowe liczby występują w **pięciu** niezależnych miejscach: SocialProof (16), ProofBar (16 cytatów - skróty tych samych osób), `ArticleUI.tsx` (testimonials z tymi samymi zdjęciami w CTA artykułów), `Gwarancja.tsx` (tablica `RESULTS` - 17 wpisów "N klientów w M dni"), `Ebook.tsx` (inline opinia "Kuba Jońca"). Przy migracji: jedna tabela opinii w DB + flagi gdzie wyświetlać.

---

## 4. Cennik i checkout subskrypcji

### 4.1 Plany (hardcoded w `Pricing.tsx`, ZDUBLOWANE)

Tablica `mainPlans`:
| planId | nazwa na karcie | okres | cena | cena/mies. | przekreślona | inne |
|---|---|---|---|---|---|---|
| `annual` | "Najlepsza wartość" | 12 miesięcy | 1489 zł | 124 zł | 2988 zł | badge "Oszczędzasz 1499 zł", Klarna |
| `semiannual` | "Najczęściej wybierany" | 6 miesięcy | 879 zł | 147 zł | 1494 zł | `popular`+`featured`, Klarna |
| `quarterly` | "Starter" | 3 miesiące | 639 zł | 213 zł | 747 zł | - |

W TYM SAMYM pliku druga mapa `PLAN_INFO` z **innymi nazwami** (`quarterly: Starter`, `semiannual: Pro`, `annual: Master`) - te nazwy idą do CheckoutModal i do `/sukces` jako `planName`. Trzecia kopia cen w `Success.tsx` (`PLAN_PRICE_PLN`) do Pixela. Mobile: kolejność z tablicy (najdroższy pierwszy), desktop: CSS order (najtańszy pierwszy). Pod kartami lista 6 features wspólnych + link do `/gwarancja` + napis "Cena wkrótce wzrośnie." (animowany pulse).

Backend przelicza realną cenę po `planId` - frontend wysyła tylko id i ewentualny kod promo.

### 4.2 Prefetch SetupIntent (`useCheckoutPrefetch`)
- Po 5 s od wejścia na `/` prefetch `create-checkout` dla `semiannual`, cache w state per planId.
- Klik w plan: `fetchForPlan(planId)` (jeśli brak w cache) + otwarcie modala + Pixel `InitiateCheckout`.
- Zamknięcie modala: `invalidate(planId)` (SetupIntent mógł zostać skonsumowany).
- **Bug**: `getOrFetch` przy cache-miss woła `fetchForPlan` (jeden create-checkout) i zaraz potem robi DRUGI `create-checkout` bezwarunkowo - powstają dwa SetupIntenty (patrz Prowizorki #5).

### 4.3 Kody promocyjne
- `?promo=KOD` w URL (gdy `PROMO_CAMPAIGN_ACTIVE`) -> `validate-promo` `{code}` -> response `{valid, code, discountPercent, expiresAt}` lub `{valid:false, reason: "expired" | ...}` -> banner nad cennikiem + przeliczenie cen na kartach (`Math.round(price * (1 - pct/100))`).
- W modalu collapsible "Mam kod promocyjny" -> ten sam `validate-promo`. Kod trafia potem do `confirm-subscription` / `create-klarna-checkout`; backend i tak waliduje ponownie.

### 4.4 CheckoutModal - flow płatności kartą (embedded Stripe Elements)
1. Modal dostaje `clientSecret` + `setupIntentId` (prefetch albo `create-checkout {planId}`).
2. `<Elements>` z theme "night", locale pl. `ExpressCheckoutElement` (Apple Pay / Google Pay; separator "lub zapłać kartą" tylko gdy dostępne) + formularz: email (wymagany), `PaymentElement` (karty, wallets off, terms off), checkbox "Chcę fakturę VAT" -> pole NIP.
3. Submit: `savePendingConfirmation(...)` do localStorage (zabezpieczenie przed redirectem 3DS), potem `stripe.confirmSetup` z `return_url = {origin}/sukces?email=&plan={planName}&setupIntentId=&planId=` i `redirect: "if_required"`.
4. Bez redirectu: `confirm-subscription` z payloadem `{setupIntentId, planId, email, wantInvoice, nip?, promoCode?}`. Odpowiedź: `{status}` - jeśli `status !== "active"` rzucany błąd "płatność się nie powiodła" (incomplete). Sukces: `clearPendingConfirmation()`, `navigate("/sukces", {state:{email, planName}})`.
5. Błąd confirmSetup lub confirm: toast + `onRefreshNeeded()` = świeży `create-checkout` (SetupIntent skonsumowany/terminalny).
6. Express checkout: email z `billingDetails`, reszta identyczna.

### 4.5 Klarna
`KlarnaButton` w modalu tylko dla `semiannual`/`annual` (na kartach badge "Zapłać później z Klarna"). Klik -> `create-klarna-checkout {planId, promoCode?}` -> `window.location.href = data.url` (Stripe Checkout hosted). Powrót: `/sukces?source=klarna&session_id=...`.

### 4.6 `/sukces` (Success.tsx) - cztery ścieżki wejścia
Czyta: router `state {email, planName}` LUB query params `email, plan, setupIntentId, planId, redirect_status, source, session_id`.
1. **source=klarna**: `confirm-klarna-checkout {sessionId}` (fallback na webhook); błąd -> toast "płatność przyjęta, zaproszenie wymaga ręcznego sprawdzenia"; sukces -> clear pending + confirmed.
2. **redirect_status != succeeded** (failed 3DS): redirect na `/?checkout_failed=true&planId=...` - Pricing łapie te paramy, toast "Płatność się nie powiodła", auto-otwiera modal z tym planem, czyści URL.
3. **redirect_status=succeeded + setupIntentId + planId** (powrót z 3DS): `confirm-subscription {setupIntentId, planId, email, promoCode}` - promoCode odzyskany z localStorage `bfc_pending_confirmation`.
4. **state.email** (flow bez redirectu, już potwierdzone w modalu): tylko clear + confirmed.
Brak danych -> redirect na `/` po 100 ms. Podczas confirm pokazuje `SurferLoader`. Po confirmed: Pixel `Purchase` (tylko ścieżki z `planId` w URL) i ekran "Witaj w klubie!" z instrukcją "sprawdź maila - zaproszenie do BFC".

### 4.7 Globalny retry (`usePendingConfirmation`)
Na mount apki (każda strona poza `/sukces` i `/newsletter*`): jeśli w localStorage jest `bfc_pending_confirmation` młodszy niż 1 h -> ponawia `confirm-subscription` z pełnym payloadem. Sukces nowy -> toast "Subskrypcja została aktywowana!" + nawigacja na `/sukces`. Odpowiedź `{alreadyExisted: true}` -> tylko cleanup. Błąd -> zostawia wpis (retry przy następnej wizycie). Chroni przed scenariuszem: 3DS ok, ale user zamknął kartę przed potwierdzeniem.

---

## 5. Subskrypcja - samoobsługa usera

### 5.1 Anulowanie `/anuluj` (Cancel.tsx) - state machine `email -> reason -> (freeze) -> sent | frozen`
1. **email**: tylko walidacja niepustości i przejście dalej. Copy obiecuje "Wyślemy Ci kod weryfikacyjny" - **nic nie jest wysyłane na tym kroku** (komentarz w kodzie: mail leci dopiero przy "Mimo wszystko chcę anulować").
2. **reason** (retention screen): lista 4 benefitów które straci; wybór powodu (radio): `expensive` (tip o zachowaniu ceny), `no-time` (oferuje zamrożenie), `not-meeting-expectations` (tip z mailem), `other`. Duży CTA "Zostaję w klubie!" (powrót na `/`), mały szary link "Mimo wszystko chcę anulować" (wymaga wybranego powodu) -> `supabase.functions.invoke("request-cancellation", {email, reason})` -> backend wysyła mail z magic linkiem (HMAC token, 60 min) -> krok **sent** ("sprawdź skrzynkę").
3. **freeze** (tylko z powodu `no-time`): wybór 14/30/60 dni + pole na **6-cyfrowy kod** ("który wysłaliśmy na {email}") -> `pause-subscription {email, code, freeze_days}` -> odpowiedź `{resumes_at}` -> krok **frozen** z datą wznowienia. **UWAGA: w obecnym flow żaden krok nie wysyła użytkownikowi tego kodu** - patrz Prowizorki #1.
4. Magic link z maila -> `/anuluj/potwierdz?token=...` (CancelConfirm): od razu na mount surowy `fetch POST {CANCEL_FN_BASE}/confirm-cancellation {token}`; sukces -> `navigate /anuluj/anulowano?access_until={data.access_until}`; błąd -> ekran z komunikatem i linkiem "Zacznij od nowa" do `/anuluj`.
5. `/anuluj/anulowano` (CancelSuccess): czysto prezentacyjna, formatuje `access_until` na pl-PL ("Masz dostęp do klubu do X, po tym dniu nie zostaniesz obciążony").

### 5.2 Zamrażanie - relacja z adminem
Ścieżka samoobsługowa (5.1 krok 3) jest w praktyce martwa (brak kodu), więc zamrażanie robi się przez `/admin` (sekcja 8). W nowym systemie: albo naprawić wysyłkę kodu, albo świadomie zostawić zamrażanie tylko adminowi.

### 5.3 Aktualizacja karty `/aktualizuj-karte` (UpdateCard.tsx) - kroki `email -> card -> success`
1. **email**: `update-payment-method?action=create-intent` body `{email}` (action w query stringu invoke!) -> `{clientSecret, setupIntentId}`. Błędy parsowane z `fnError.context.text()`.
2. **card**: `<Elements>` + `PaymentElement` (wallets auto, terms card never). `stripe.confirmSetup` z `return_url = {origin}/aktualizuj-karte?confirmed=1&si={setupIntentId}`, `redirect: "if_required"`.
3. Bez redirectu: `update-payment-method?action=confirm` body `{setupIntentId}` -> `{subscriptionsUpdated, invoicesRetried}` (backend: attach PM jako default na wszystkie subskrypcje + retry otwartych faktur).
4. **success**: "Zaktualizowaliśmy płatność dla N subskrypcji. Ponowiliśmy też M zaległych płatności."
5. **Dziura**: strona NIE czyta query params - po powrocie z redirectu 3DS (`?confirmed=1&si=...`) user ląduje z powrotem na kroku email i `action=confirm` nigdy nie zostaje wywołany. Patrz Prowizorki #3.

---

## 6. Ebook, newsletter, kontakt

### 6.1 `/ebook` (Ebook.tsx) - strona produktowa + inline checkout
- Treść produktu hardcoded: tytuł "Na swoich zasadach: Od 0 do 10 000 zł/msc", cena **249 zł** (w copy i na guziku - realna kwota ustalana przez backend w PaymentIncie), gwarancja "10 000 zł w 90 dni albo zwrot", "+830 sprzedanych egzemplarzy", galeria 3 obrazków z `src/assets`, jedna opinia inline (Kuba Jońca).
- Na mount: `create-ebook-payment-intent {}` -> `{clientSecret, paymentIntentId}`; błąd -> przycisk retry.
- Formularz: email, `PaymentElement` (karta/BLIK, wallets w PaymentElement off), `ExpressCheckoutElement` (Apple/Google Pay osobno), checkbox faktury -> nazwa firmy + NIP (walidacja `^\d{10}$` po zdjęciu spacji/myślników).
- `stripe.confirmPayment` z `return_url = {origin}/ebook/sukces?payment_intent_id={pi}&email={email}[&invoice=1&nip=&name=]`, `redirect: if_required`, `receipt_email`. Bez redirectu - ręczna nawigacja na ten sam URL. **Dane faktury (NIP, nazwa) jadą przez query params.**
- Ustawia własny `document.title`/description w useEffect i przywraca przy unmount.

### 6.2 `/ebook/sukces` (EbookSuccess.tsx)
- Czyta `session_id` (legacy flow Stripe Checkout) LUB `payment_intent_id`/`payment_intent` + `email`, `invoice`, `nip`, `name`.
- Polling: `confirm-ebook-purchase {sessionId?, paymentIntentId?, email?, wantInvoice, nip?, invoiceName?}` - do **8 prób co 2 s** (fallback na opóźnienie webhooka). Sukces -> `{downloadUrl, email}` -> przycisk "Pobierz ebooka teraz" (zwykły `<a href>`), info że mail z linkiem też poszedł.
- Błąd po 8 próbach -> komunikat z mailem `krystian@befreeclub.pl` "wyślemy ebooka ręcznie".

### 6.3 `/ebook/pobierz?token=` (EbookDownload.tsx)
- Klik "Pobierz PDF" -> `download-ebook {token}` -> `{url, remainingDownloads}` -> `window.location.href = url` (signed URL). Pokazuje "pozostałe pobrań: N". Copy: "Link aktywny 30 dni · do 10 pobrań" (limity egzekwuje backend, tabela `ebook_download_tokens`).

### 6.4 Martwy kod: `EbookCheckoutModal.tsx`
Komponent ze starym flow (redirect do Stripe Checkout przez `create-ebook-checkout {email, wantInvoice, nip?, invoiceName?}` -> `window.location.href = data.url`). **Nigdzie nieimportowany.** EbookSuccess wciąż obsługuje `session_id` z tego flow.

### 6.5 Newsletter (DOI)
Dwa formularze o identycznej logice: `/newsletter` (Newsletter.tsx, pełna strona z tłem foto+grain, licznik "+6 000 osób", resend) i `NewsletterCTA` na landingu (bez resend).
- Walidacja zod: imię 1-80 znaków, email max 255.
- Surowy `fetch POST {NEWSLETTER_FN_BASE}/newsletter-subscribe {name, email}` (email lowercase). Sukces -> ekran "Już prawie. Jeszcze jeden klik."
- Newsletter.tsx ma dodatkowo przycisk "Otwórz mój mail w skrzynce": mapa ~30 domen email -> URL webmaila (gmail/outlook/wp/o2/onet/interia/proton/...), z deep-linkiem wyszukiwania po nadawcy `krystian@befreeclub.pl`; nieznana domena -> default gmail. Oraz "wyślij ponownie" = ponowny `newsletter-subscribe`.
- Mail DOI -> `/newsletter/potwierdz?token=` (NewsletterConfirm): na mount `fetch POST newsletter-confirm {token}`; sukces -> `navigate /newsletter/witaj?name={data.name}`; błąd -> ekran z CTA "Zapisz się ponownie".
- `/newsletter/witaj` (NewsletterWelcome): Pixel `Lead`, copy o mailu powitalnym z **AI Playbookiem** (link + hasło do `/playbook` przychodzą mailem).

### 6.6 `/kontakt` (Contact.tsx)
- Walidacja zod (imię ≤100, email ≤255, wiadomość ≤5000).
- **Bezpośredni INSERT z przeglądarki**: `supabase.from("contact_messages").insert({name, email, message})` - wymaga polityki RLS pozwalającej anon INSERT.
- Potem best-effort `send-contact-email {name, email, message}` (błąd ignorowany - "wiadomość i tak jest w DB").

---

## 7. Blog "Wiedza" i Playbook

### 7.1 Skąd są artykuły
**Artykuły to komponenty Reacta, nie dane.** 
- Rejestr: `src/data/blog-posts.ts` - tablica `blogPosts: BlogPost[]` z polami `{slug, title, description, excerpt, cover (import obrazka z assets), category, readingMinutes, publishedAt (ISO string), author, Content (React.lazy import)}` + helper `getPostBySlug`.
- Treści: `src/content/articles/*.tsx` - 4 pliki (PierwszyKlient 649 linii, UmiejetnosciFreelancerskie 761, AgenciAI 840, AIPlaybook 2054). Pisane JSX-em z biblioteką komponentów `src/components/blog/ArticleUI.tsx` (778 linii: `Chapter`, `Lead`, `PullQuote`, `DoIt`, `Compare`, `Msg`, `ClubCTA`, `InlineCTA`, `FinalCTA`, `GuaranteeBox`, `PricePerDay`, `Testimonials`, `UrgencyBanner`, `WhatsInsidePopup`...). Część komponentów to wbudowane CTA sprzedażowe klubu z opiniami (te same zdjęcia co SocialProof) i linkiem `CLUB_URL = https://befreeclub.pl/`.
- AIPlaybook nie jest w `blogPosts` - żyje tylko pod `/playbook`. `PlaybookUI.tsx` (952 linie) to analogiczna biblioteka komponentów dla playbooka.
- Autor wszystkich: Krystian Rudnik.

Przy migracji do FastAPI: artykuły trzeba przepisać na format danych (markdown/HTML w DB) albo świadomie zostawić jako szablony - obecna forma (komponenty z interaktywnymi CTA) nie przenosi się 1:1 do CMS-a.

### 7.2 `/wiedza` i `/wiedza/:slug`
- Lista: grid kart z `blogPosts` (cover, tytuł, excerpt, czas czytania). Meta + canonical ustawiane imperatywnie w useEffect.
- Artykuł: `getPostBySlug`, brak -> render `NotFound`. `Suspense` + `SurferLoader` (ozdobny loader). Pełny zestaw meta OG/twitter + JSON-LD `Article` wstrzykiwany do `<head>` w useEffect (script id `article-jsonld`). **Wszystko client-side - bez SSR crawlery bez JS widzą tylko domyślne meta z index.html.**

### 7.3 `/playbook`
- Gate hasłem: hardcoded `zyciejestfajne1`, porównanie po normalizacji, odblokowanie zapisane w localStorage bez TTL. Hasło dystrybuowane w mailu powitalnym newslettera. Treść (AIPlaybook.tsx) i hasło są w publicznym bundlu JS.
- Po odblokowaniu: artykuł 35 min czytania, autor Krystian Rudnik, data 2026-05-08.

### 7.4 Sitemap
`scripts/generate-sitemap.ts` odpalany jako plugin Vite na `buildStart` (build i dev): 5 tras statycznych (`/`, `/wiedza`, `/kontakt`, `/regulamin`, `/polityka-prywatnosci`) + artykuły **parsowane regexem** z `blog-posts.ts` (slug + publishedAt). Zapis do `public/sitemap.xml`. W sitemap NIE ma: `/ebook`, `/newsletter`, `/gwarancja`, `/playbook`, `/pierwszy-klient`.

---

## 8. `/admin` (Admin.tsx) - zalążkowy panel admina

To NIE jest admin.befreeclub.pro - to ukryta podstrona landinga, powstała do ratowania operacji na subskrypcjach.

### Autoryzacja (prymitywna, celowo)
- Na mount: jeśli brak `bfc_admin_token` w sessionStorage -> `window.prompt("Podaj token admina:")` -> zapis do sessionStorage.
- Każdy request: nagłówek `x-admin-token: {token}` do `https://{VITE_SUPABASE_PROJECT_ID}.supabase.co/functions/v1/...` (surowy fetch, nie invoke).
- Odpowiedź 401 -> czyszczenie tokenu + prośba o ponowne wpisanie. Walidacja tokenu wyłącznie po stronie edge functions (sekret po stronie backendu - nazwa zmiennej do potwierdzenia w spec backendu).
- Brak kont, ról, audytu "kto"; wylogowanie = czyszczenie sessionStorage.

### Funkcje panelu
1. **Zamroź subskrypcję**: email + dni (1-365, default 30) + checkbox "Usuń z Circle (przywrócenie automatyczne po wznowieniu Stripe)" (default ON). `confirm()` przeglądarki, potem `POST admin-pause-subscription {email, freeze_days, remove_from_circle}`. Wynik (sukces lub błąd) renderowany jako surowy `JSON.stringify` w `<pre>`.
2. **Dodaj z powrotem do Circle**: email + checkbox "Pomiń email z zaproszeniem" (default ON; use case: wyrzucony z Circle, ale ma aktywną subskrypcję, np. zmienił plan na roczny). `POST admin-reinvite-circle {email (lowercase), skip_invitation}`. Wynik raw JSON.
3. **Historia (ostatnie 200)**: `POST admin-list-cancellations {}` -> `{rows: [{id, email, reason, action, freeze_days, created_at}]}` - czyta tabelę `cancellation_reasons`. Tabelka: data / email / akcja / powód / dni.

W supabase/functions istnieją też admin-extend-subscription i admin-stripe-legacy-audit - **bez UI we froncie** (wołane ręcznie; opis w spec backendu).

---

## 9. Mapa: strona/komponent -> wywołania backendu

| Frontend | Endpoint | Metoda wywołania | Payload |
|---|---|---|---|
| useCheckoutPrefetch / CheckoutModal | `create-checkout` | invoke | `{planId}` |
| Pricing (`?promo=`) / CheckoutModal | `validate-promo` | invoke | `{code}` |
| CheckoutModal / Success (3DS) / usePendingConfirmation | `confirm-subscription` | invoke | `{setupIntentId, planId, email, wantInvoice?, nip?, promoCode?}` |
| CheckoutModal (KlarnaButton) | `create-klarna-checkout` | invoke | `{planId, promoCode?}` -> redirect `url` |
| Success (`source=klarna`) | `confirm-klarna-checkout` | invoke | `{sessionId}` |
| Cancel | `request-cancellation` | invoke | `{email, reason}` |
| Cancel (freeze) | `pause-subscription` | invoke | `{email, code, freeze_days}` |
| CancelConfirm | `confirm-cancellation` | **raw fetch** (hardcoded URL) | `{token}` |
| UpdateCard | `update-payment-method?action=create-intent` | invoke | `{email}` |
| UpdateCard | `update-payment-method?action=confirm` | invoke | `{setupIntentId}` |
| Ebook | `create-ebook-payment-intent` | invoke | `{}` |
| EbookSuccess | `confirm-ebook-purchase` | invoke (polling 8x2s) | `{sessionId?, paymentIntentId?, email?, wantInvoice, nip?, invoiceName?}` |
| EbookDownload | `download-ebook` | invoke | `{token}` |
| EbookCheckoutModal (MARTWY) | `create-ebook-checkout` | invoke | `{email, wantInvoice, nip?, invoiceName?}` |
| Newsletter / NewsletterCTA | `newsletter-subscribe` | **raw fetch** (hardcoded URL) | `{name, email}` |
| NewsletterConfirm | `newsletter-confirm` | **raw fetch** (hardcoded URL) | `{token}` |
| Contact | tabela `contact_messages` | supabase insert (anon) | `{name, email, message}` |
| Contact | `send-contact-email` | invoke (best-effort) | `{name, email, message}` |
| Admin | `admin-list-cancellations` | **raw fetch** + `x-admin-token` | `{}` |
| Admin | `admin-pause-subscription` | raw fetch + `x-admin-token` | `{email, freeze_days, remove_from_circle}` |
| Admin | `admin-reinvite-circle` | raw fetch + `x-admin-token` | `{email, skip_invitation}` |

Funkcje istniejące w repo, **niewoływane z frontu** (webhook/cron/ręczne): `stripe-webhook`, `circle-cleanup`, `reconcile-klarna-checkouts`, `retry-circle-invites`, `sync-circle-ids`, `admin-extend-subscription`, `admin-stripe-legacy-audit`.

Tabele DB widoczne w typach frontu (`integrations/supabase/types.ts`): `cancellation_reasons`, `cancellation_tokens`, `circle_members`, `contact_messages`, `ebook_download_tokens`, `ebook_orders`, `newsletter_subscribers`. Front dotyka bezpośrednio tylko `contact_messages`.

---

## 10. Prowizorki i długi

1. **Martwy flow zamrażania po stronie usera.** `/anuluj` krok "freeze" prosi o 6-cyfrowy kod "który wysłaliśmy na {email}", ale w obecnym flow ŻADEN krok nie wywołuje niczego, co by ten kod wysłało (`handleFreeze` tylko zmienia step; `request-cancellation` generuje HMAC-token do magic linka, nie 6-cyfrowy kod; `pause-subscription` weryfikuje kod z tabeli `cancellation_tokens`). User nie ma jak samodzielnie zamrozić - dlatego robi to admin przez `/admin`. Przy przepisywaniu: zdecydować, czy naprawić, czy wyrzucić ścieżkę.
2. **Kłamiące copy na `/anuluj`**: krok email mówi "Wyślemy Ci kod weryfikacyjny, aby potwierdzić Twoją tożsamość" - faktycznie nic nie jest wysyłane na tym kroku, a anulowanie idzie magic linkiem.
3. **UpdateCard nie obsługuje powrotu z 3DS.** `return_url` to `/aktualizuj-karte?confirmed=1&si=...`, ale komponent nie czyta query params - po redirecie user widzi pusty krok email, a serwerowy confirm (`action=confirm`: attach PM, retry faktur) nigdy nie odpala. Działa tylko ścieżka bez redirectu.
4. **Admin = window.prompt + token w sessionStorage.** Jeden współdzielony token, brak audytu kto wykonał akcję, wyniki operacji jako surowy JSON w `<pre>`, `confirm()` przeglądarki jako zabezpieczenie. Świadomie zalążkowe - całość do zastąpienia panelem admina na VPS.
5. **Podwójny SetupIntent w `useCheckoutPrefetch.getOrFetch`**: przy cache-miss wykonuje `fetchForPlan` i bezpośrednio po nim drugi `create-checkout` - dwa SetupIntenty na jedno otwarcie modala (śmieci w Stripe, niegroźne ale brzydkie).
6. **Hardcoded ref projektu Supabase** (`fshkdkvoyysphfrfvmni`) w `CancelConfirm`, `Newsletter`, `NewsletterCTA` zamiast użycia klienta/env - trzy strony robią surowe fetch'e, reszta `supabase.functions.invoke`. Niespójność + pułapka przy zmianie projektu.
7. **Stripe publishable key wklejony w 3 plikach** (CheckoutModal, UpdateCard, Ebook) zamiast w jednym module/env.
8. **Ceny i nazwy planów w wielu miejscach**: `Pricing.mainPlans` (nazwy marketingowe), `Pricing.PLAN_INFO` (inne nazwy: Starter/Pro/Master), `Success.PLAN_PRICE_PLN` (kwoty do Pixela), `InvestmentMath` (147 zł, 879 zł), `ExitIntentPopup` ("4 zł/dzień"). Zmiana cennika = polowanie po plikach. W nowym systemie: plany w DB, zarządzane z admina.
9. **Pixel Purchase strzela tylko w flow z redirectem.** W podstawowym flow (bez 3DS) nawigacja na `/sukces` idzie przez router state, który nie zawiera `planId` - `PLAN_PRICE_PLN[planId]` jest undefined i event nie jest wysyłany (kod celowo nie strzela bez ceny). Większość zakupów prawdopodobnie nie raportuje Purchase do Mety.
10. **Meta Pixel PageView tylko raz na load** - SPA nie raportuje wirtualnych pageview przy zmianie route.
11. **Hasło Playbooka i cała jego treść w publicznym bundlu JS** - "ochrona" jest iluzoryczna; hasło hardcoded, unlock w localStorage bez wygasania.
12. **EbookCheckoutModal to martwy kod** (stary flow Stripe Checkout); `EbookSuccess` wciąż utrzymuje obsługę `session_id` z tego flow.
13. **NIP i nazwa firmy w query params** na `/ebook/sukces` (lądują w historii przeglądarki / logach).
14. **SEO client-side w SPA**: meta OG/twitter/canonical/JSON-LD per podstrona ustawiane w useEffect; crawlery bez JS (większość scraperów OG) widzą tylko statyczne meta z `index.html`. Przy przepisaniu na FastAPI server-side rendering meta rozwiązuje to od ręki.
15. **Sitemap generowany regexem** z pliku TS (`slug:`/`publishedAt:`) na buildStart - kruche; nie obejmuje `/ebook`, `/newsletter`, `/gwarancja`.
16. **Rozjazd domen w dokumentach prawnych**: Regulamin mówi, że serwis działa pod "befreeclub.pro", a landing to befreeclub.pl; Polityka prywatności podaje administratora jako "Krystian" bez nazwiska/podmiotu. Daty obu: 8 marca 2026.
17. **FAQ "Czy wystawiacie faktury?"** odsyła do "panelu klienta", który nie istnieje - dane do faktury zbierane są tylko checkboxem przy checkout.
18. **Kruche sprzężenia UI**: LookInside otwiera FAQ przez `CustomEvent("open-faq", {detail:"item-1"})` - złamie się przy zmianie kolejności pytań; ExitIntentPopup wykrywa otwarty checkout po selektorze `[data-checkout-modal]`.
19. **Niewykorzystane zależności**: react-query (tylko provider), spora część radixa; dwa systemy toastów (sonner + shadcn use-toast) używane równolegle.
20. **`/pierwszy-klient`** to strona-przekierowanie przez `window.location.replace` (pełny reload zamiast routera) - legacy URL z kampanii.
21. **Polling jako fallback webhooka** w EbookSuccess (8 prób co 2 s) i confirm-klarna-checkout na `/sukces` - frontend łata wyścig z webhookami Stripe.
22. **Liczby marketingowe rozsiane i niespójne**: ProofBar "200+ członków" (vs ~150 realnie wg kontekstu marki), "+6 000" newslettera, "+830 egzemplarzy" ebooka, "90+ lekcji" - wszystko hardcoded, do centralizacji w adminie.

---

## 11. Wskazówki do przepisania (synteza)

- **Treść do DB/admina**: plany+ceny, opinie (jedna tabela, flagi rozmieszczenia), statystyki marketingowe (liczniki), FAQ, kroki Timeline, warunki gwarancji, artykuły bloga, konfiguracja promo (zamiast flagi w kodzie), copy ebooka/cena.
- **Strony czysto statyczne** (Terms, Privacy, Gwarancja, NotFound): szablony Jinja, treść może zostać w plikach albo iść do DB.
- **Flow wymagające JS** (checkout embedded Stripe, anulowanie, update karty, ebook checkout): albo lekkie wyspy JS na Jinja, albo utrzymanie Stripe Elements - logika serwerowa przejdzie do FastAPI 1:1 wg payloadów z sekcji 9.
- **SEO**: SSR meta/OG/JSON-LD i sitemap z DB rozwiążą długi #14 i #15 naturalnie.
- **Admin landinga** (`/admin`) znika - jego trzy operacje (pause/reinvite/historia) przenoszą się do istniejącego panelu admin na VPS.
