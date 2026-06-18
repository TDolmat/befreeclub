# Plan fazy 2: landing befreeclub.pl + rozbudowa panelu admina

Stan: PROJEKT do akceptacji Tomka, 2026-06-10. Źródło wiedzy o obecnym systemie: `docs/spec-landing/` (6 speców z pełnego reconu kodu). Architektura bazowa: `docs/ARCHITEKTURA.md`.

## Decyzje Tomka, które ten plan realizuje

1. Landing zostaje czystym landingiem. Sprzedaż, treść, podstrony typu zmiana karty. Nic więcej.
2. Całe zarządzanie subskrypcjami idzie do panelu admina: przepinanie kart, anulowania, pauzy, podgląd odnowień, nieudane płatności z powodami, filtrowanie subskrybentów.
3. Zarządzanie treścią landinga (ceny, pakiety, kody rabatowe, artykuły) z zakładki "Landing page" w adminie. Koniec z edycją cen przez commit i deploy.
4. W adminie powstaje centralna sekcja "Ustawienia": ustawienia wszystkich aplikacji + podłączenia do zewnętrznych API.
5. Toole AI nie są przenoszone. Po fazie 2 planujemy jeden mega-tool (osobny etap).

## Co dziś naprawdę robi landing (pigułka z reconu)

- **Subskrypcje kartą**: SetupIntent na froncie, potem `confirm-subscription` tworzy subskrypcję off_session. Dwa konta Stripe: current (wszystko nowe) i legacy (stare suby, tylko odpytywane).
- **Klarna**: osobny tor. Jednorazowa płatność za okres z góry, dostęp czasowy `expires_at`, trzy redundantne ścieżki potwierdzenia.
- **Lifecycle**: anulowanie przez magic link HMAC na maila. Pauza self-service jest martwa (UI prosi o kod, którego nic nie wysyła). Pauzy i przedłużenia robi się przez ukryty `/admin` z jednym wspólnym tokenem z `window.prompt`.
- **Członkostwo**: jedyna tabela `circle_members` to log zaproszeń do Circle. Źródłem prawdy o subach jest Stripe. Cron `circle-cleanup` wyrzuca z Circle tych, którzy nie mają żywej suby na żadnym koncie.
- **Ebook**: PaymentIntent + mail z tokenem pobrania. Fulfillment w 100% zależy od powrotu przeglądarki kupującego. Bez webhooka.
- **Newsletter**: stateless double opt-in, lista żyje wyłącznie w Sender.net. Możliwe, że funkcje stoją na DRUGIM projekcie Supabase na koncie Krystiana (README mówi jedno, kod drugie).
- **Treść**: 100% hardcoded w TSX. Ceny w 5 miejscach, opinie zduplikowane, artykuły Wiedza jako komponenty React, flaga promocji w kodzie.
- **Crony**: zdefiniowane ręcznie w żywej bazie Supabase, nie ma ich w repo. Funkcje cronowe są publiczne, bez żadnej autoryzacji.

### Bomby znalezione w recone (naprawiamy W TRAKCIE portu, nie kopiujemy)

1. **Refund ebooka kasuje członkostwo w klubie**: handler `charge.refunded` nie filtruje produktu. Pełny refund czegokolwiek anuluje wszystkie suby tego maila na obu kontach i wyrzuca z Circle.
2. **Zmiana karty bez autoryzacji**: wystarczy znać czyjś email. Plus zepsuty powrót z 3DS (karta zapisuje się w Stripe, suby nie zostają przepięte).
3. **Publiczny `reconcile-klarna-checkouts` potrafi PRZYWRÓCIĆ dostęp po refundzie** (refundowana sesja ma dalej `payment_status=paid`).
4. **Konto legacy nie ma webhooka**: nieudane odnowienia i refundy starych członków nie wywołują żadnej reakcji. Starzy członkowie nie mają też jak zmienić karty.
5. **Webhook może być głuchy na nieudane płatności**: czyta stare pole `invoice.subscription`, które nowsze wersje API Stripe przeniosły. Do weryfikacji na prodzie.
6. **`retry-circle-invites` ponownie zaprasza celowo wyrzuconych** (nie odróżnia "invite failed" od "removed").
7. Brak idempotencji webhooków, brak rate limitów, niespójna normalizacja maili przy UNIQUE, zahardkodowany ADMIN_TOKEN w źródle (w gicie, do rotacji).

## Architektura docelowa

### Backend: nowe moduły monolitu FastAPI

Zgodnie z zasadą moduł = schemat PG = prefiks URL:

| Moduł | Schemat | Co robi |
|---|---|---|
| `billing` | `billing` | Stripe (oba konta), checkout subskrypcji i ebooka, webhooki, kody rabatowe, plany/ceny, lifecycle (anulowania, pauzy, przedłużenia), Klarna |
| `members` | `members` | Stan członka (enum statusu zamiast bool!), provisioning/deprovisioning Circle, ochrona kont, historia zmian |
| `newsletter` | `newsletter` | Double opt-in, integracja Sender.net, formularz kontaktowy |
| `landing` | `landing` | Treść landinga: plany (prezentacja), opinie, FAQ, liczniki, artykuły Wiedza, ustawienia kampanii |

Kluczowe nowe tabele (skrót, szczegóły przy implementacji):
- `billing.webhook_events` (event_id UNIQUE, payload, processed_at) - idempotencja i historia. Fundament panelu subskrypcji: "komu coś nie przeszło i czemu" bierzemy z zapisanych eventów, nie z odpytywania Stripe na żywo.
- `billing.plans` - plany z price ID per konto Stripe, cena, interwał, aktywność. Koniec z hardcode w 5 miejscach.
- `members.members` - email (znormalizowany, UNIQUE), circle_member_id, status ENUM (`invited`, `active`, `paused`, `pending_removal`, `removed`, `invite_failed`, `protected`), źródło (`subscription`, `one_time`, `manual`), expires_at, timestampy. Zastępuje `circle_members.active` bool i hardcoded PROTECTED_EMAILS.
- `landing.articles` - artykuły jako markdown w DB (tytuł, slug, treść, SEO meta, publikacja). Koniec z artykułami jako komponentami React.
- `landing.content` - opinie, FAQ, liczniki, flagi kampanii.

Crony przechodzą do backendu jako workery (jak polling w circle_dm): cleanup członkostw, reconcile Klarny, retry zaproszeń. Wersjonowane w repo, bez publicznych endpointów. Ręczne odpalenie z panelu admina zamiast curl na goły URL.

### Admin: trzy nowe sekcje

**1. Subskrypcje** (najważniejsze)
- Lista subskrybentów: połączony widok Stripe (oba konta) + members + Circle. Filtry: status suby, plan, konto (current/legacy), problemy płatności, status w Circle, źródło (karta/Klarna/manual).
- Karta osoby: timeline (zakup, odnowienia, nieudane płatności z powodem z webhooka, anulowania z powodem, pauzy, akcje adminów), stan w Circle, historia maili.
- Akcje: anuluj (natychmiast / koniec okresu), pauza na X dni, przedłuż, wyślij link zmiany karty, re-invite do Circle, usuń z Circle, oznacz jako chroniony. Każda akcja audytowana (kto, kiedy, co).
- Widok "Problemy": nieudane odnowienia do obsłużenia, wygasające karty przed odnowieniem (jest w audycie legacy, wciągamy jako stały widok).

**2. Landing page** (CMS)
- Plany i ceny (edycja = zmiana w DB, landing czyta przez API; price ID zarządzane per plan).
- Kody rabatowe: tworzenie i podgląd użyć (przez Stripe API, nie własna implementacja).
- Artykuły Wiedza: edytor markdown, draft/publikacja, SEO meta.
- Opinie, FAQ, liczniki, włącznik kampanii promocyjnej (dziś: commit + deploy).

**3. Ustawienia** (centralna sekcja, projekt pod przyszłe aplikacje)
- Podział per aplikacja: Circle DM (to co dziś w `/circle-dm/settings`), Billing, Landing, Newsletter, w przyszłości mega-tool.
- Podłączenia zewnętrznych API: Circle, Stripe current, Stripe legacy, OpenAI, Resend, Sender.net. Każde z przyciskiem "test połączenia" i statusem.
- Zasada sekretów: klucze API zostają w env na VPS (nie w DB). W ustawieniach trzymamy: które połączenie jest aktywne, parametry niesekretne (community ID, grupy Sender, progi, modele AI, maile nadawcze), przełączniki funkcji. Panel pokazuje obecność sekretu ("ustawiony w env") bez wartości.
- Architektura: jedna tabela `admin.settings` (klucz per moduł, JSONB, wersjonowanie zmian) + rejestr definicji w kodzie modułów. Sekcja Circle DM migruje do tego mechanizmu, stary endpoint zostaje jako alias do czasu aktualizacji frontu.

### Frontend landinga

Rekomendacja: **kopiujemy obecny React SPA do `frontends/landing/`** (jak admin w fazie 1), serwowany osobnym kontenerem nginx. Zmiany ograniczone do: treść z API zamiast hardcode (ceny, opinie, FAQ, artykuły, flagi), wywołania edge functions zamienione na `/api/billing/*` itd., sprzątnięcie hardcoded refów Supabase i kluczy.

SEO: blog Wiedza i strony statyczne dostają prerendering na buildzie (vite SSG dla tras z listy + artykuły generowane z DB w czasie builda albo prosty prerender hookiem). Pełny SSR (Astro/Next) to opcja na później, nie blokujemy nim fazy 2. Do decyzji Tomka, patrz pytania.

### Co umiera

- Cały projekt Supabase (po migracji i okresie przejściowym), 26 edge functions, drugi projekt Supabase od newslettera.
- Ukryty `/admin` na landingu, `window.prompt`, wspólny ADMIN_TOKEN (rotacja sekretów przy okazji: ADMIN_TOKEN, SENDER_API_TOKEN po wycieku, weryfikacja NEWSLETTER_FROM_EMAIL).
- Martwy kod: flow zamrożenia po kodzie, EbookCheckoutModal, tabela `newsletter_subscribers`, `cancellation_tokens`.

## Naprawy wpisane w port (nie kopiujemy bugów)

1. Webhook: idempotencja po event_id, filtr `charge.refunded` po produkcie, obsługa obu pól subscription (stare i nowe API), **drugi endpoint webhooka dla konta legacy**.
2. Zmiana karty: magic link HMAC na maila (ten sam wzorzec co anulowanie) zamiast "podaj email i hulaj". Naprawiony powrót z 3DS. Dostępna też z panelu admina ("wyślij link").
3. Ebook: fulfillment webhook-first (payment_intent.succeeded), powrót przeglądarki tylko jako przyspieszenie UX. Refund unieważnia tokeny pobrania.
4. Workery zamiast publicznych cron-endpointów. Logika cleanup z enum statusu members (koniec z re-invitowaniem wyrzuconych).
5. Normalizacja maili (lower+trim) wszędzie + migracja czyszcząca duplikaty.
6. Rate limiting na publicznych endpointach (checkout, newsletter, kontakt) + Idempotency-Key przy create w Stripe.
7. Wspólny kod zamiast copy-paste x4 (invite do Circle, szablony maili, mapy planów) - to załatwia architektura modułów.

## Migracja danych i przepięcie

1. **Przed wszystkim, na żywym Supabase**: zrzut `SELECT * FROM cron.job` + `job_run_details` (harmonogramy są TYLKO tam), inwentaryzacja realnych ustawień verify_jwt w dashboardzie, spis sekretów z dashboardu obu projektów (głównego i newsletterowego Krystiana), eksport danych wszystkich 7 tabel + plik ebooka z bucketa.
2. Skrypt migracji danych (wzorzec z fazy 1): `circle_members` → `members.members` z wyliczeniem enum statusu (active+brak suby w Stripe = do wyjaśnienia ręcznie przed migracją), `ebook_orders` + `ebook_download_tokens` → `billing.*`, `cancellation_reasons` → `billing.cancellation_reasons`, `contact_messages` → `newsletter.contact_messages`. Treść landinga (ceny, opinie, FAQ, artykuły) wprowadzana do DB jednorazowym seedem z obecnego kodu.
3. Stripe: webhook endpoints przepinamy w dashboardzie (current + NOWY legacy) na `api` nowego backendu. Price ID zostają te same, zero zmian w Stripe.
4. DNS/Caddy: `befreeclub.pl` → nowy kontener landinga, `/api/*` → backend. Stary Supabase zostaje w trybie tylko-do-odczytu przez okres przejściowy (rollback).
5. Okres równoległy: nowy backend odbiera webhooki, stary system wyłączony z zapisu. Brak stanu współdzielonego = przepięcie atomowe per domena.

## Fazowanie

- **2.0 Deploy fazy 1**: admin z `migracja/befreeclub` wchodzi na VPS i przejmuje admin.befreeclub.pro (git init, pierwszy build, migracja danych admina wg MIGRACJA_DANYCH.md). Bez tego nie ma na czym budować.
- **2.1 Backend billing+members**: **ZROBIONE (kod, przed deployem)** - moduły billing/members/newsletter, webhook events, plany w DB, workery, wszystkie naprawy bezpieczeństwa + poprawki z review. Raport odstępstw, nowe treści PL do akceptacji, decyzje do podjęcia i checklista migracji: `docs/spec-landing/port2-odstepstwa.md`. Kontrakt `docs/spec-landing/port-kontrakt-2.md` zaktualizowany do stanu kodu.
- **2.2 Admin Subskrypcje + Ustawienia**: panel zarządzania subskrybentami na nowym backendzie + centralna sekcja ustawień (Circle DM migruje do nowego mechanizmu).
- **2.3 Backend landing+newsletter + Admin Landing page**: treść do DB, CMS w adminie, newsletter i kontakt.
- **2.4 Frontend landinga**: kopia SPA, treść z API, nowe endpointy płatności, prerender SEO.
- **2.5 Migracja i przepięcie**: dane, webhooki Stripe, DNS, okres równoległy, wygaszenie Supabase.

Każda podfaza ma działający produkt na końcu. 2.1+2.2 dowozi panel subskrypcji ZANIM ruszymy landing (panel działa na żywych danych Stripe niezależnie od tego, gdzie stoi landing).

## Decyzje Tomka 2026-06-10 (akceptacja planu)

- Edge functions przenosimy na backend. Ma działać TAK SAMO i PRAWIDŁOWO: flow bez zmian dla usera, naprawy z sekcji "Naprawy wpisane w port" wchodzą.
- Własne flow zamiast Stripe Customer Portal (zostaje jak jest, tylko poprawnie). Legacy dostaje webhook. Klarna zostaje. Newsletter zostaje na Sender.net. SEO: prerender (rekomendacja przyjęta domyślnie).
- **Analityka jako twarde wymaganie**: pełna analiza landingu. UTM-y przechwytywane na wejściu, doklejane do checkoutu, zapisywane w billing + metadata Stripe. Meta Pixel naprawiony na froncie (wirtualne PageView w SPA, Purchase dla każdej ścieżki). Po stronie serwera Meta Conversions API: Purchase/Lead z webhooka z event_id do deduplikacji z pikselem. Konwersja liczy się nawet bez powrotu przeglądarki na stronę sukcesu.
- Migracja fazy 1 (admin na VPS) jeszcze NIE zrobiona - do ogarnięcia równolegle, nie blokuje budowy modułów fazy 2.

## Pytania do Tomka (blokują odpowiednie podfazy)

1. **Stripe Customer Portal vs własne flow** (blokuje 2.1): Stripe ma gotowy hostowany portal (zmiana karty, anulowanie, faktury). Możemy nim załatwić zmianę karty i anulowania taniej i bezpieczniej, kosztem brandingu i kontroli (np. retencja przy anulowaniu by odpadła albo zostaje nasza). Rekomendacja: własne flow (już je mamy, panel admina i tak musi umieć więcej), ale portal warto rozważyć przynajmniej dla legacy.
2. **Konto legacy** (blokuje 2.1): dokładamy mu webhook i obsługę w panelu (rekomendacja, mały koszt) czy planujemy migrację starych subów na konto current (większa operacja, osobny projekt)?
3. **Klarna**: zostawiamy obecny model "rok z góry, dostęp czasowy"? Wchodzi 1:1 w nowy billing, pytanie czy w ogóle ją utrzymujemy (konwersje vs utrzymanie osobnego toru).
4. **Newsletter**: zostajemy przy Sender.net (port 1:1 integracji) czy budujemy własną listę w DB + wysyłka Resend? Rekomendacja: zostać przy Sender, nie otwierać trzeciego frontu w tej fazie.
5. **SEO landinga**: wystarczy prerender bloga i stron statycznych na buildzie (rekomendacja, mały koszt) czy chcesz pełny SSR (duża przebudowa frontu, osobna podfaza)?
6. **befreeclub-api** (cache Circle na VPS): moduł members będzie miał własną integrację Circle. Wygaszamy befreeclub-api po fazie 2 czy zostaje dla innych konsumentów? Do sprawdzenia, kto z niego dziś korzysta.
7. **Faktury za ebook/klub**: dziś ręczne. Czy w zakresie fazy 2 ma być widok "do zafakturowania" w adminie (dane są w billing), czy integracja z systemem faktur to osobny temat?

## Czego ten plan świadomie NIE robi

- Nie przenosi tooli AI (decyzja: mega-tool, osobny etap po fazie 2).
- Nie rusza Circle DM poza migracją jego ustawień do nowej sekcji Ustawienia.
- Nie zmienia wyglądu ani copy landinga (to osobna praca z Krystianem; CMS da narzędzie).
- Nie robi pełnego SSR (chyba że decyzja w pytaniu 5 będzie inna).
