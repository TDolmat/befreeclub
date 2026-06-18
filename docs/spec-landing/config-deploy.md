# Spec: konfiguracja i deploy landinga befreeclub.pl

Inwentaryzacja na 2026-06-10, stan repo `/Users/tomasz/repos/befreeclub/befreeclub` (branch `main`, czysty working tree, HEAD `2a7d40c`).

Zakres tego dokumentu: sekrety i ich rozmieszczenie, harmonogramowanie cronów (z weryfikacją gdzie NAPRAWDĘ żyją), proces deployu Lovable, domeny/DNS, zależności builda, testy. Logika biznesowa edge functions i schemat DB są w osobnych specach; tutaj tylko to, co potrzebne do odtworzenia konfiguracji w monolicie FastAPI.

---

## 1. Repo i model pracy

- GitHub: `git@github.com:TDolmat/befreeclub.git`, praca wyłącznie na `main`, 222 pliki trackowane.
- Trzy źródła commitów: Tomasz (lokalnie), Krystian (lokalnie), `gpt-engineer-app[bot]` (edycje robione w UI Lovable, auto-commit do repo). Sync jest dwukierunkowy: push z lokalnego repo trafia do Lovable, zmiany w Lovable wracają jako commity bota.
- **Brak `LOVABLE_INSTRUCTIONS.md`** w tym repo, mimo że konwencja ekosystemu (CLAUDE.md parasola) każe tam zapisywać zadania deployowe dla usera. Zadania deployowe newslettera są zamiast tego w `supabase/functions/newsletter-subscribe/README.md`.
- `README.md` repo to nietknięty boilerplate Lovable z placeholderem `REPLACE_WITH_PROJECT_ID` (zero wartości dokumentacyjnej).
- Folder `.lovable/` zawiera tylko `plan.md` (plan jednej animacji UI, artefakt robczy). `plan.md` jest też w `.gitignore`.
- Artefakty śmieciowe w root repo: pusty `test.txt`, `OPINIE_ESENCJE.md` (robocze notatki z cytatami do przyszłego komponentu opinii).

## 2. Proces deployu (Lovable)

### Frontend
1. Kod pisany lokalnie (Claude Code) albo w UI Lovable.
2. Push na `main` -> Lovable widzi commit.
3. **Publikacja jest ręczna**: user klika w Lovable "Share -> Publish". Nie ma CI/CD, nie ma automatu.
4. Lovable hostuje statyczny build (Vite) pod własnym CDN + custom domain `befreeclub.pl`.

Znany incydent desyncu: README newslettera (stan 2026-05-07) odnotowuje "Lovable nie deployował od `b34045f`, my mamy do `803114a+`" - czyli prod potrafi wisieć wiele commitów za `main`, bo nikt nie kliknął Publish. Przy migracji na VPS ten problem znika (deploy = git pull + restart), ale do czasu migracji każda zmiana wymaga ręcznego Publish.

### Edge functions
- Zmiany robione przez UI Lovable deployują się same (bot commituje i deployuje).
- Zmiany pisane lokalnie wymagają ręcznego deployu:
  ```bash
  export SUPABASE_ACCESS_TOKEN=sbp_...   # personal access token, NIE jest w repo
  npx supabase functions deploy <nazwa> --project-ref fshkdkvoyysphfrfvmni [--no-verify-jwt]
  ```
- Sekrety funkcji ustawiane przez dashboard Supabase albo Management API:
  ```bash
  curl -X POST -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" -H "Content-Type: application/json" \
    -d '[{"name":"NAZWA","value":"..."}]' \
    "https://api.supabase.com/v1/projects/<ref>/secrets"
  ```

### Migracje DB
- Pliki w `supabase/migrations/*.sql` (10 plików, 154 linie łącznie). Nazewnictwo `YYYYMMDDHHMMSS_<uuid>.sql` - generowane przez Lovable, opisy w nazwach nie istnieją.
- Lovable aplikuje migracje przy zmianach przez swoje UI. **Migracje NIE pokrywają całego stanu żywej bazy** - patrz sekcja crony.

## 3. DWA projekty Supabase (pułapka)

| Projekt | Ref | Właściciel | Rola |
|---|---|---|---|
| Główny (Lovable) | `fshkdkvoyysphfrfvmni` | konto Lovable Tomasza | DB, wszystkie 26 edge functions, storage bucket `ebooks`, crony |
| Newsletter (osobny) | `rxqaedlhkdrkkdpwkyho` | **osobiste konto Krystiana Rudnika** (org `cxwsbcvytfzufxwbxlla`), region eu-central-1, free tier | wg README newslettera: hosting `newsletter-subscribe` + `newsletter-confirm` |

**Niespójność do wyjaśnienia przed migracją**: README newslettera (2026-05-07) mówi, że funkcje newslettera żyją na `rxqaedlhkdrkkdpwkyho`, ALE frontend (`src/components/landing/NewsletterCTA.tsx`, `src/pages/Newsletter.tsx`, `src/pages/NewsletterConfirm.tsx`) woła `https://fshkdkvoyysphfrfvmni.supabase.co/functions/v1/newsletter-*`, a `config.toml` zawiera wpisy `newsletter-subscribe`/`newsletter-confirm`. Najpewniej funkcje zostały później przeniesione/zdublowane do projektu głównego, a README jest nieaktualne. Trzeba sprawdzić w obu dashboardach: gdzie funkcje faktycznie działają i w którym projekcie siedzą sekrety `NEWSLETTER_*` / `SENDER_*`. Projekt Krystiana po migracji do skasowania.

## 4. Sekrety - pełna lista per miejsce (tylko nazwy)

### 4a. Frontend `.env` (UWAGA: plik `.env` jest COMMITOWANY do gita - zawiera tylko wartości publiczne, ale to praktyka Lovable, nie przeoczenie)

| Zmienna | Użycie |
|---|---|
| `VITE_SUPABASE_PROJECT_ID` | `src/pages/Admin.tsx` - budowa URL `https://<id>.supabase.co/functions/v1` |
| `VITE_SUPABASE_URL` | `src/integrations/supabase/client.ts` |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | `src/integrations/supabase/client.ts` (anon key) |

Nie ma `.env.example`. Klucz publiczny Stripe NIE jest w env - sprawdzić w specu checkoutu, najpewniej hardcoded w komponencie.

### 4b. Sekrety edge functions (Supabase Dashboard > Edge Functions > Secrets, projekt `fshkdkvoyysphfrfvmni`)

Automatyczne (wstrzykiwane przez Supabase): `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`.

Ręczne:

| Sekret | Co to | Używany przez |
|---|---|---|
| `STRIPE_SECRET_KEY` | klucz Stripe konta BIEŻĄCEGO | 17 funkcji (cały checkout, webhook, anulacje, pauzy, ebook, promo) |
| `STRIPE_LEGACY_SECRET_KEY` | klucz Stripe konta STAREGO (legacy, drugie konto Stripe - kontekst w pamięci `befreeclub-vps-migration`) | stripe-webhook, circle-cleanup, pause-subscription, request-cancellation, confirm-cancellation, admin-pause-subscription, admin-extend-subscription, admin-stripe-legacy-audit |
| `STRIPE_WEBHOOK_SECRET` | signing secret webhooka Stripe | stripe-webhook |
| `CIRCLE_API_TOKEN` | Circle Admin API v2 | confirm-subscription, confirm-klarna-checkout, reconcile-klarna-checkouts, circle-cleanup, retry-circle-invites, sync-circle-ids, admin-pause-subscription, admin-reinvite-circle, stripe-webhook |
| `CIRCLE_COMMUNITY_ID` | ID community Circle (int jako string) | te same co wyżej |
| `RESEND_API_KEY` | wysyłka maili (Resend) | stripe-webhook, send-contact-email, request-cancellation, confirm-ebook-purchase, reconcile-klarna-checkouts, newsletter-subscribe |
| `ADMIN_TOKEN` | jeden współdzielony token panelu /admin (header `x-admin-token`) | admin-pause-subscription, admin-list-cancellations, admin-extend-subscription, admin-reinvite-circle |
| `CANCELLATION_DOI_SECRET` | HMAC do tokenów potwierdzenia anulacji | request-cancellation, confirm-cancellation |
| `CANCELLATION_FROM_EMAIL` | nadawca maila anulacyjnego | request-cancellation |
| `NEWSLETTER_DOI_SECRET` | HMAC do tokenów double opt-in newslettera (random 48 bajtów) | newsletter-subscribe, newsletter-confirm |
| `NEWSLETTER_FROM_EMAIL` | opcjonalny; default `Be Free Club <krystian@befreeclub.pl>`; wg README tymczasowo ustawiony na adres `onboarding@resend.dev` | newsletter-subscribe |
| `CONFIRM_URL_BASE` | opcjonalny; base URL linku potwierdzającego (default `https://befreeclub.pl`) | newsletter-subscribe |
| `FRONTEND_URL` | base URL frontendu do linków w mailach | request-cancellation, confirm-ebook-purchase |
| `SENDER_API_TOKEN` | API Sender.net (push subskrybenta po DOI) | newsletter-confirm |
| `SENDER_GROUP_IDS` | CSV grup Sendera, default w kodzie `"epnLzm,el06vl"` | newsletter-confirm |

Uwaga nazewnicza: README newslettera dokumentuje `SENDER_GROUP_ID` (liczba pojedyncza, default `epnLzm`), kod używa `SENDER_GROUP_IDS` (mnoga, CSV). Kod jest nowszy, README przestarzałe.

Uwaga vs befreeclub-spec: landing NIE używa `BEFREECLUB_API_KEY` ani wzorca `verify-circle-member` z `befreeclub-spec/03-auth-system.md` - landing nie ma logowania członków w ogóle (Supabase Auth nieużywany). Spec parasolowy opisuje pozostałe apki Lovable, nie ten landing.

### 4c. Sekrety poza repo/Supabase

| Sekret | Gdzie | Po co |
|---|---|---|
| `SUPABASE_ACCESS_TOKEN` (`sbp_...`) | lokalna maszyna / shell | CLI deploy funkcji + Management API |
| Stripe Dashboard (2 konta: bieżące + legacy) | stripe.com | źródło kluczy, konfiguracja webhooka |
| Resend dashboard | resend.com | klucz API, weryfikacja domeny `befreeclub.pl` |
| Sender.net dashboard | sender.net | token API, grupy `general` (`epnLzm`, ~910 subów), `bfc_member` (`bqoL2k`, ~157), nieużywany form `e5yv68`, automation welcome maila (trigger: join group `general`, treść z campaign `DkNq1Y`) |

### 4d. Identyfikatory hardcoded w kodzie (nie sekrety, ale konfiguracja do przeniesienia)

| Co | Wartość | Gdzie |
|---|---|---|
| Microsoft Clarity ID | `vsealx7t9v` | inline `<script>` w `index.html` |
| Meta Pixel ID | `963496946601553` | inline `<script>` w `index.html` (track PageView) |
| Ref projektu Supabase | `fshkdkvoyysphfrfvmni` | hardcoded w 4 plikach: `NewsletterCTA.tsx`, `Newsletter.tsx`, `NewsletterConfirm.tsx`, `CancelConfirm.tsx` (zamiast `VITE_SUPABASE_PROJECT_ID`) |
| SITE_URL sitemapy | `https://befreeclub.pl` | `scripts/generate-sitemap.ts` |
| Ścieżka pliku ebooka | `na-swoich-zasadach.pdf` (bucket `ebooks`) | `download-ebook/index.ts` |

## 5. config.toml i verify_jwt

`supabase/config.toml` zawiera WYŁĄCZNIE: `project_id = "fshkdkvoyysphfrfvmni"` + 17 wpisów `verify_jwt = false`. Nic więcej. Żadnych sekcji `[auth]`, `[db]`, żadnych harmonogramów.

`verify_jwt = false` (wywoływalne bez żadnego JWT - webhooki, linki z maili, crony, panel admina):
`create-checkout`, `confirm-subscription`, `send-contact-email`, `circle-cleanup`, `request-cancellation`, `confirm-cancellation`, `sync-circle-ids`, `pause-subscription`, `stripe-webhook`, `update-payment-method`, `admin-pause-subscription`, `admin-list-cancellations`, `admin-extend-subscription`, `admin-reinvite-circle`, `reconcile-klarna-checkouts`, `newsletter-subscribe`, `newsletter-confirm`.

9 funkcji ISTNIEJE w `supabase/functions/`, ale NIE MA wpisu w config.toml (czyli default `verify_jwt = true` - wystarcza anon key, który `supabase.functions.invoke` dokleja sam):
`admin-stripe-legacy-audit`, `confirm-ebook-purchase`, `confirm-klarna-checkout`, `create-ebook-checkout`, `create-ebook-payment-intent`, `create-klarna-checkout`, `download-ebook`, `retry-circle-invites`, `validate-promo`.

Pułapka: README newslettera pokazuje deploy z flagą `--no-verify-jwt`, która nadpisuje config przy ręcznym deployu. Faktyczny stan verify_jwt na produkcji może więc różnić się od config.toml - przy inwentaryzacji przed wyłączeniem Supabase sprawdzić w dashboardzie per funkcja. W FastAPI to znika: każdy endpoint dostaje jawny mechanizm auth (albo publiczny, albo webhook signature, albo sesja admina).

## 6. Crony - jak są harmonogramowane NAPRAWDĘ (zweryfikowane)

Ustalenia, krok po kroku:

1. `config.toml` - **zero cronów** (potwierdzenie tezy z PROJECT.md admina). Supabase i tak nie wspiera harmonogramów w config.toml.
2. Migracja `20260308122724` instaluje rozszerzenia: `CREATE EXTENSION pg_cron` + `CREATE EXTENSION pg_net`. To jedyny ślad cronów w repo.
3. **Żaden plik w repo nie zawiera `cron.schedule(...)`** - grep po całym repo i historii git: brak.
4. Commit `ffb4ff4` ("Dodano Circle cleanup cron", 2026-03-08, autor gpt-engineer-app[bot]) zmienia TYLKO 3 linie config.toml (wpis verify_jwt dla circle-cleanup).

**Wniosek**: definicje cronów żyją wyłącznie w żywej bazie Postgresa Supabase (tabela `cron.job`), założone ręcznie/one-offem przez SQL editor (tak robi agent Lovable: odpala SQL poza systemem migracji). Są NIEZWERSJONOWANE. Mechanizm wykonania: `pg_cron` -> `net.http_post(url := 'https://fshkdkvoyysphfrfvmni.supabase.co/functions/v1/<fn>', ...)` (po to pg_net).

Kandydaci na zadania cronowe (funkcje pisane jako bezargumentowe batch joby, wywoływane bez body):

| Funkcja | Co robi (skrót) |
|---|---|
| `circle-cleanup` | usuwa z Circle członków bez żadnej "żywej" subskrypcji Stripe (sprawdza OBA konta Stripe; statusy past_due/unpaid/incomplete/paused traktuje jako "zostaje") |
| `reconcile-klarna-checkouts` | domyka checkouty Klarna opłacone bez powrotu na stronę (zaprasza do Circle, wysyła welcome mail) |
| `retry-circle-invites` | ponawia nieudane zaproszenia do Circle (`circle_members` z `active=false` lub `circle_member_id IS NULL`) |
| `sync-circle-ids` | dociąga `circle_member_id` z paginowanego listingu Circle API do tabeli `circle_members` |

**Dokładne harmonogramy (cron expressions) są nieznane z poziomu repo.** Przed migracją OBOWIĄZKOWO zrzucić ze starej bazy:

```sql
SELECT jobid, jobname, schedule, command, active FROM cron.job;
SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 50;  -- historia odpaleń
```

i zarchiwizować wynik w docs migracji. W FastAPI odtworzyć jako APScheduler/celery beat/cron systemowy z jawną konfiguracją w repo.

**Dziura bezpieczeństwa**: wszystkie 4 funkcje cronowe mają `verify_jwt = false` (lub brak auth) i NIE weryfikują żadnego sekretu w handlerze - każdy kto zna URL może je odpalić publicznie. Skutki są ograniczone (operacje quasi-idempotentne), ale np. masowe wywołanie `sync-circle-ids` wali w rate limit Circle API. W FastAPI: zadania wewnętrzne schedulera, bez publicznego endpointu, albo endpoint z sekretem.

## 7. Domeny / DNS / hosting

| Domena | Co | Gdzie wskazuje |
|---|---|---|
| `befreeclub.pl` | landing prod | Lovable hosting (custom domain w Project > Settings > Domains) |
| `befreeclub.pl` w Resend | domena nadawcza maili | rekordy DNS: DKIM TXT `resend._domainkey`, SPF MX + TXT na subdomenie `send`; wg README na 2026-05-07 dodane, czekały na verify - sprawdzić aktualny stan w resend.com/domains |
| `fshkdkvoyysphfrfvmni.supabase.co` | edge functions + DB | Supabase |
| `befreeclub.pro` + subdomeny (`api.`, `admin.`, `sales.`, `leads.`) | ekosystem narzędzi (VPS/Lovable) | NIE dotyczy landinga; `befreeclub-spec/03` twierdzi że "BeFreeClub (Scoper): befreeclub.pro" - nieaktualne względem landinga sprzedażowego, który żyje na `.pl` |

Inne ustalenia hostingowe:
- `public/_headers` (format Netlify-style, respektowany przez CDN Lovable): `Cache-Control: no-cache, must-revalidate` dla `/favicon.png` i `/og-image.jpeg`. Po migracji odtworzyć w Caddy.
- `public/robots.txt`: Allow dla Googlebot/Bingbot/Twitterbot/facebookexternalhit, `Disallow: /admin` dla `*`, wskazuje `Sitemap: https://befreeclub.pl/sitemap.xml`.
- `public/pierwszy-klient.html` - statyczny standalone HTML (65 KB) serwowany spod `/pierwszy-klient.html`, poza Reactem.
- SPA routing: React Router (BrowserRouter) - Lovable robi fallback wszystkich ścieżek do `index.html`; w Caddy potrzebny `try_files` odpowiednik. Po przepisaniu na FastAPI server-side routing załatwia to inaczej.
- Stripe webhook endpoint skonfigurowany w Stripe Dashboard na URL funkcji `stripe-webhook` - przy migracji trzeba przepiąć URL webhooka i wygenerować nowy `STRIPE_WEBHOOK_SECRET`.

## 8. Build i zależności

- **Menedżer pakietów: bałagan.** W repo są naraz TRZY lockfile'y: `bun.lockb` (binarny, sty 2026), `bun.lock` (tekstowy, 3 cze - aktualizowany przez Lovable, który buduje bunem) i `package-lock.json` (2 cze - lokalne `npm install`). Lokalnie działa npm (`npm run dev|build|lint|test`), Lovable używa bun. Każdy świeży `npm i`/`bun i` może rozjechać wersje względem builda Lovable.
- Vite 5.4 + `@vitejs/plugin-react-swc`, TypeScript 5.8, React 18.3, Tailwind 3.4 + shadcn/Radix (komplet pakietów @radix-ui), framer-motion, react-router-dom 6, @tanstack/react-query 5, react-hook-form + zod, Stripe JS (`@stripe/stripe-js`, `@stripe/react-stripe-js`), `@supabase/supabase-js`, fonty self-hosted przez `@fontsource` (inter, permanent-marker, archivo-black, space-grotesk).
- `vite.config.ts`: dev server host `::` port 8080, HMR overlay wyłączony, alias `@ -> ./src`, plugin `lovable-tagger` (componentTagger) tylko w mode development, oraz własny `sitemapPlugin`.
- **Generator sitemapy** (`scripts/generate-sitemap.ts`): odpala się na `buildStart` (build i dev), pisze `public/sitemap.xml`. Trasy statyczne hardcoded: `/`, `/wiedza`, `/kontakt`, `/regulamin`, `/polityka-prywatnosci`. Wpisy blogowe wyciąga z `src/data/blog-posts.ts` **REGEXEM** po `slug:` i `publishedAt:` (nie importuje modułu). Wygenerowany `sitemap.xml` jest commitowany do `public/`. Po migracji: sitemap generowany dynamicznie przez FastAPI z treści w DB (blog ma iść do panelu admina).
- `package.json`: name `vite_react_shadcn_ts`, version 0.0.0 (generyczny szablon Lovable). Skrypty: `dev`, `build`, `build:dev` (build w mode development), `lint` (eslint 9 flat config), `preview`, `test` (vitest run), `test:watch`.
- `index.html`: lang=pl, meta OG/Twitter (og-image `https://befreeclub.pl/og-image.jpeg`), inline Clarity + Meta Pixel, inline CSS loadera startowego (`#initial-loader` ze spinnerem, znika po mount Reacta).

## 9. Testy (vitest)

- Konfiguracja: `vitest.config.ts` - environment jsdom, globals true, setup `src/test/setup.ts` (jedyny mock: `window.matchMedia`), include `src/**/*.{test,spec}.{ts,tsx}`. Zależności: @testing-library/react + jest-dom, jsdom 20.
- **Faktyczne testy: JEDEN plik `src/test/example.test.ts` z asercją `expect(true).toBe(true)`.** Cała infrastruktura testowa to atrapa - zero pokrycia logiki (checkout, promo, anulacje - nic). Informacja z CLAUDE.md parasola "testy (vitest) w befreeclub" opisuje istnienie harness'u, nie realnych testów. Do planu migracji: testy w FastAPI pisać od zera, nie ma czego portować.

## 10. Panel admina na landingu (kontekst configowy)

`/admin` (strona `src/pages/Admin.tsx`, wykluczona w robots.txt): bramka to `window.prompt("Podaj token admina:")`, token leci do `sessionStorage` (`bfc_admin_token`) i jest wysyłany headerem `x-admin-token` do funkcji `admin-*`, które porównują go z sekretem `ADMIN_TOKEN`. Jeden współdzielony token, bez userów, bez audytu, bez expiry (poza czasem życia sesji karty). Funkcje: pauza subskrypcji + usunięcie z Circle, lista anulacji, przedłużenie subskrypcji, re-invite do Circle. Całość do wchłonięcia przez panel `admin.befreeclub.pro` (właściwy auth już tam istnieje).

## 11. Tabele DB tworzone migracjami (skrót dla kontekstu - szczegóły w spec DB)

`contact_messages` (insert anon, read zablokowany), `circle_members` (+ kolumny `stripe_source` default 'current', `purchase_type` default 'subscription', `expires_at`; service_role only), `cancellation_tokens`, `cancellation_reasons` (obie service_role; w `20260528114443` polityki naprawione z RESTRICTIVE na PERMISSIVE - wcześniej RESTRICTIVE-only blokowało dostęp nawet service_role), `ebook_orders`, `ebook_download_tokens`, `newsletter_subscribers` (insert anon; NIEUŻYWANA przez żadną edge function ani frontend - martwa, DOI pcha bezpośrednio do Sender API), storage bucket `ebooks` (prywatny). Brak tabeli dla checkoutów Klarna - `reconcile-klarna-checkouts` odpytuje Stripe API bezpośrednio.

## 12. Mapa migracji na VPS (wnioski configowe)

| Dziś (Lovable/Supabase) | Po migracji (FastAPI monolit na VPS) |
|---|---|
| Lovable Publish (ręczny) | git pull + docker compose, za Caddy hosta |
| 26 edge functions Deno | endpointy FastAPI w module landing/subscriptions |
| pg_cron + pg_net (definicje tylko w żywej bazie!) | scheduler w aplikacji (APScheduler/celery beat), definicje w kodzie |
| sekrety w Supabase Dashboard (2 projekty!) | jeden `.env` na VPS / docker secrets |
| `verify_jwt` per funkcja w config.toml | jawne zależności auth per endpoint |
| `public/_headers` | dyrektywy headers w Caddyfile |
| sitemap commitowany + regex po TS | endpoint `/sitemap.xml` generowany z DB |
| `.env` z VITE_* commitowany | konfiguracja serwerowa, nie w repo |
| webhook Stripe na URL Supabase | przepiąć URL w Stripe Dashboard, nowy `STRIPE_WEBHOOK_SECRET` |

Checklista rzeczy do zrzucenia z żywych systemów PRZED wyłączeniem Supabase:
1. `SELECT * FROM cron.job` (harmonogramy!) + `cron.job_run_details`.
2. Lista sekretów (nazwy + wartości) z dashboardu projektu `fshkdkvoyysphfrfvmni` ORAZ `rxqaedlhkdrkkdpwkyho`.
3. Faktyczny stan `verify_jwt` per wdrożona funkcja (dashboard), bo `--no-verify-jwt` przy ręcznych deployach mógł rozjechać stan względem config.toml.
4. Dump danych tabel (osobny spec DB).
5. Konfiguracja webhooka w Stripe Dashboard (jakie eventy subskrybowane).
6. Stan weryfikacji domeny w Resend i aktualna wartość `NEWSLETTER_FROM_EMAIL`.

---

## Prowizorki i długi

1. **Crony niezwersjonowane** - definicje wyłącznie w żywej bazie (`cron.job`), zero śladu w repo (config.toml i migracje czyste - zweryfikowane grep'em po repo i historii git). Harmonogramy nieznane; bez zrzutu z bazy migracja zgubi joby. To samo dotyczy każdego SQL-a, który Lovable odpalił poza migracjami.
2. **Funkcje cronowe bez żadnej autoryzacji** - `circle-cleanup`, `reconcile-klarna-checkouts`, `retry-circle-invites`, `sync-circle-ids` są publicznie wywoływalne (verify_jwt=false + brak sprawdzania sekretu w handlerze).
3. **Dwa projekty Supabase na jedną apkę** - newsletter na osobistym koncie Krystiana (`rxqaedlhkdrkkdpwkyho`), reszta na projekcie Lovable; README newslettera niespójny z frontendem (frontend woła projekt główny). Nie wiadomo, gdzie naprawdę żyją sekrety NEWSLETTER_*/SENDER_* i która kopia funkcji obsługuje ruch.
4. **Niedokończone TODO newslettera** (README, 2026-05-07): weryfikacja domeny befreeclub.pl w Resend wisi; `NEWSLETTER_FROM_EMAIL` tymczasowo na adresie testowym resend.dev; `SENDER_API_TOKEN` wymaga rotacji (stary wyciekł w czacie); śmieciowy workflow "DO USUNIECIA - probe od Claude Code" w Sender -> Automation; welcome automation w Sender do ręcznego skonfigurowania.
5. **Rozjazd doc vs kod**: `SENDER_GROUP_ID` (README) vs `SENDER_GROUP_IDS` CSV z defaultem `"epnLzm,el06vl"` (kod).
6. **Trzy lockfile'y naraz** (bun.lockb, bun.lock, package-lock.json) - Lovable buduje bunem, lokalnie npm; wersje mogą się rozjechać.
7. **Testy to atrapa** - jeden example test `expect(true).toBe(true)`, zero realnego pokrycia mimo pełnego harness'u vitest.
8. **Hardcoded ref projektu Supabase** w 4 plikach frontendu zamiast env (`NewsletterCTA`, `Newsletter`, `NewsletterConfirm`, `CancelConfirm`).
9. **Panel /admin na window.prompt + sessionStorage + jeden współdzielony ADMIN_TOKEN** - bez kont, bez audytu, bez wygasania.
10. **Deploy ręczny i desynchronizowalny** - prod potrafi wisieć wiele commitów za main (udokumentowany przypadek: brak deployu od `b34045f`); brak LOVABLE_INSTRUCTIONS.md wbrew konwencji ekosystemu.
11. **Sitemap generowany regexem po pliku TS** (`blog-posts.ts`) i commitowany do public/ - kruche; wywali się cicho przy zmianie formatu (try/catch z warn w pluginie).
12. **9 funkcji bez wpisu w config.toml** + ręczne deploye z `--no-verify-jwt` - faktyczny stan verify_jwt na produkcji niepewny, do zinwentaryzowania w dashboardzie.
13. **Martwa tabela `newsletter_subscribers`** - istnieje z polityką "anyone can insert", nieużywana przez nic.
14. **RLS naprawiane po fakcie** - migracja `20260528114443` zamienia błędne polityki RESTRICTIVE na PERMISSIVE dla tabel anulacji (wcześniej zapisy service_role mogły być blokowane).
15. **Śmieci w repo**: pusty `test.txt`, robocze `OPINIE_ESENCJE.md` w root, boilerplate README z placeholderami, stary `bun.lockb`, nieużywany(?) `favicon-v2.png`.
16. **befreeclub-spec nieaktualny względem landinga** - opisuje domenę `befreeclub.pro` i wzorzec auth członkowskiego (`verify-circle-member`, `BEFREECLUB_API_KEY`), którego landing w ogóle nie ma; landing żyje na `befreeclub.pl` bez logowania.
17. **`.env` commitowany do gita** (praktyka Lovable; tylko wartości publiczne VITE_*, ale wzorzec do ubicia przy migracji).
