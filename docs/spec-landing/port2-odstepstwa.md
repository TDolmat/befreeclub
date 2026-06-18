# Port fazy 2.1 (landing: billing + members + newsletter) - raport odstępstw i decyzji

Stan: 2026-06-10, po review i poprawkach (zadanie [poprawki-2]). Kod w `backend/app/modules/{billing,members,newsletter}`. Dokument dla Tomka: co naprawiono, co świadomie działa inaczej niż oryginał, co wymaga Twojej akceptacji albo decyzji. Uzupełnia `PLAN_LANDING.md` i zaktualizowany `port-kontrakt-2.md` (kontrakt jest już zgodny z kodem).

## A. Co naprawiono względem oryginału

Naprawy z planu ("Naprawy wpisane w port"):

1. Webhooki: idempotencja po event_id, filtr `charge.refunded` po produkcie (refund ebooka nie kasuje już członkostwa), obsługa obu pól subscription na fakturze, NOWY endpoint webhooka dla konta legacy.
2. Zmiana karty: magic link HMAC na maila zamiast "podaj email i hulaj". Naprawiony powrót z 3DS. Działa też dla legacy. Dostępna z panelu admina.
3. Ebook: fulfillment webhook-first (payment_intent.succeeded), powrót przeglądarki to tylko przyspieszacz. Refund unieważnia tokeny pobrania.
4. Workery zamiast publicznych cron-endpointów. Cleanup na enum statusu, koniec z re-invitowaniem wyrzuconych.
5. Normalizacja maili (lower+trim) wszędzie, CHECK w DB.
6. Rate limiting na publicznych endpointach + Idempotency-Key przy każdym create w Stripe.
7. Wspólny kod zamiast copy-paste (grant Klarny x3, invite Circle x4, mapy planów).
8. Reconcile Klarny odmawia dostępu po refundzie (publiczny endpoint potrafił PRZYWRACAĆ dostęp po zwrocie).

Naprawy z review 2.1 (znaleziska audytu, wszystkie z testami regresyjnymi):

9. BLOCKER: cleanup wymaga OBU kluczy Stripe i konfiguracji Circle PRZED przetworzeniem kogokolwiek (jak oryginał). Bez tego deploy bez klucza legacy wyrzuciłby z Circle wszystkich członków z subą tylko na legacy.
10. HIGH: lookup customera po emailu ma fallback `customers.search` (case-insensitive). Filtr email w `customers.list` jest case-sensitive, a starzy customerzy bywają z wielką literą. Bez tego: duplikat customera i DRUGA pełnopłatna suba dla płacącego członka. Dotyczy wszystkich miejsc: checkout, anulowanie, zmiana karty, pauza, extend, refund, panel.
11. HIGH: refund z nierozstrzygniętym produktem (chwilowy błąd Stripe przy doczytaniu PI) NIE wpada już w ścieżkę członkowską. Błąd propaguje, event ląduje jako error do ręcznego ponowienia. Wcześniej refund ebooka przy podwójnej awarii kasował wszystkie suby i wyrzucał z Circle.
12. HIGH: wygasły dostęp czasowy (one_time/manual) nie jest usuwany, gdy email ma żywą subę w Stripe. Scenariusz: aktywny subskrybent dokupił dostęp w torze Klarna, source przeskoczył na one_time, po wygaśnięciu okna wylatywał płacąc dalej za subskrypcję.
13. Blokada drugiej równoległej suby działa po CAŁYM emailu: oba konta, wszyscy customerzy, statusy active/trialing/past_due/unpaid. Pauza i przedłużenie adminowe żyją jako trialing, więc bez tego spauzowany członek kupujący ponownie dostawał drugą subę.
14. Klarna: expires_at kotwiczone w `session.created`, nie w "teraz". Reconcile tyka co godzinę, przy "teraz + N" każdy tick przedłużał dostęp (do ~7 dni gratis na kupującego) i sypał setkami eventów "extended".
15. Provisioning serializowany per email (advisory lock w PG). Webhook i browserowy confirm odpalają się w tej samej sekundzie. Bez locka: 500 dla usera po udanej płatności, fałszywy error webhooka, stracony Purchase do Meta CAPI, możliwy podwójny invite do Circle.
16. Refund ebooka, który wyprzedził fulfillment (Stripe nie gwarantuje kolejności eventów), zostawia tombstone "refunded". Spóźniony payment_intent.succeeded nie wyda ebooka po zwrocie.
17. Ebook bez receipt_email na PI: email brany z billing_details charge'a. Fulfillment nie stoi w 100% na dyscyplinie frontu.
18. Nowy endpoint POST `/api/billing/admin/webhook-events/{id}/reprocess`. Event połknięty przez błąd obsługi albo crash w trakcie (Stripe nie ponawia przez dedup) da się ponowić z panelu. Handlery są idempotentne.
19. members.status "paused" jest teraz faktycznie ustawiany (pauza adminowa) i zdejmowany (extend/clear_pause oraz webhook invoice.paid przy naturalnym wznowieniu). Wcześniej filtr "paused" w panelu zawsze zwracał pusto.
20. Token anulowania zużywany ATOMOWO przed operacjami Stripe. Równoległy podwójny POST nie wstawia już duplikatu do audytu.
21. Timeline członka bez szumu: czyste decyzje keep cleanupu i porażki retry idą do loggera, nie do members.events (wcześniej ~600 wierszy dziennie).
22. Temat maila DOI newslettera: dzień bez zera wiodącego ("3.06.2026"), jak Intl w oryginale.
23. Plany sprzedawalne tylko na koncie current (guard). Plan zaseedowany na legacy dawałby pobraną płatność bez nadania dostępu.
24. Szablon `.env.production.example` ma pełną sekcję fazy 2 (wcześniej zero zmiennych: po przepięciu webhooki dawałyby 500, a maile/Circle/CAPI byłyby cicho wyłączone). Compose montuje wolumen na PDF ebooka.

## B. Świadome odstępstwa od oryginału / kontraktu

Wszystkie poniższe są zamierzone. Flow usera bez zmian, zachowanie "tak samo ale prawidłowo".

- Zapis atrybucji subskrypcji robi DOPIERO confirm (zgodnie z kontraktem). Wcześniejsza implementacja zapisywała przy setup-intencie, przez co prefetch frontu (każdy wizytator po 5 s) śmiecił tabelą. Przy retrym confirma niepuste pola atrybucji z requestu są dosztukowywane do istniejącego wiersza.
- expires_at Klarny liczone od `session.created`, nie od potwierdzenia (zmiana quirka #7 ze speca). Kupujący dostaje dokładnie N miesięcy od zakupu. Kto potwierdzi Klarną po kilku dniach, dostanie nieco mniej niż w oryginale (tam: potwierdzenie + N). To poprawna semantyka i jedyny sposób, żeby confirm, webhook i reconcile dawały identyczny termin.
- Członek z subą past_due/unpaid tego samego planu, który "kupuje jeszcze raz" zamiast naprawić kartę, dostaje 409 z komunikatem o kontakcie z Krystianem. Oryginał utworzyłby drugą subę (a stara mogła się potem ściągnąć przez Smart Retries = podwójne obciążenie).
- Token anulowania jest jednorazowy (oryginał: wielokrotnego użytku). Rejestr in-memory, restart resetuje, ale okno ogranicza exp 60 min. Token zmiany karty zostaje wielokrotnego użytku.
- Idempotency-Key `sub-create-{setupIntentId}`: Stripe replayuje też ODMOWY (402) przez 24 h dla tego samego klucza. Klucz zostaje (chroni przed podwójnym obciążeniem). Wymóg dla frontu 2.4 w sekcji E.
- request-link zmiany karty zawsze odpowiada `{"ok": true}` (anty-enumeracja). Oryginał zdradzał 404, czy email ma subę.
- Aliasy endpointów (`/pause`, `/extend`, `/cancel`, `/request`, `/session`) skasowane. Zostały tylko ścieżki kanoniczne z kontraktu.
- Błąd wysyłki maila payment_failed nie jest połykany. Event ląduje jako error, panel widzi niedostarczone maile, admin może ponowić przez reprocess.
- Worker cleanupu przy niekompletnej konfiguracji pomija tick z WARN w logu. Ręczny trigger admina dostaje głośny błąd.

## C. NOWE treści PL wymagające Twojej akceptacji

Te teksty nie istniały w oryginale. Wklejone dosłownie z kodu.

1. Blokada drugiego planu (checkout, HTTP 409):

> Masz już aktywną subskrypcję Be Free Club. Żeby zmienić plan, napisz na krystian@befreeclub.pl.

2. Odmowa po refundzie Klarny (confirm, HTTP 409):

> Płatność została zwrócona. Napisz na krystian@befreeclub.pl, jeśli to pomyłka.

3. Mail zmiany karty (nowy, oryginał nie wysyłał żadnego; szablon ciemnej karty BFC jak mail anulowania):

- Temat: `Zmiana karty płatniczej Be Free Club`
- Nagłówek: `Zaktualizuj kartę płatniczą`
- Treść: `Otrzymaliśmy prośbę o zmianę karty płatniczej do Twojej subskrypcji. Kliknij poniższy przycisk, żeby bezpiecznie podać dane nowej karty.`
- Treść: `Nowa karta zostanie podpięta do Twojej subskrypcji. Jeśli masz zaległą płatność, spróbujemy ją od razu opłacić nową kartą.`
- Przycisk: `Zmieniam kartę`
- Stopka: `Link wygasa za 60 minut. Jeśli to nie Ty zainicjowałeś zmianę, zignoruj ten mail. Nic się nie zmieni.`
- Reply-to: kontakt@befreeclub.pl

4. Ebook niedostępny (plan nieaktywny w DB, HTTP 409):

> Ebook jest obecnie niedostępny.

5. Pozostałe nowe komunikaty (drobne):

- Rate limit (wszystkie publiczne endpointy): `Zbyt wiele prób. Spróbuj ponownie później.`
- Zły/wygasły token zmiany karty: `Link wygasł lub jest nieprawidłowy. Wróć na stronę zmiany karty i wyślij nowy.`
- Panel, błąd wysyłki linku: `Nie udało się wysłać emaila z linkiem.`
- Confirm karty bez 3DS: `Karta nie została potwierdzona ({status}).`
- Pobranie ebooka, brak pliku na dysku: `Plik tymczasowo niedostępny. Spróbuj za chwilę.`

## D. Decyzje do podjęcia (skonsolidowane)

1. **past_due i ponowny zakup tego samego planu**: teraz 409 i kontakt z Krystianem zamiast samoobsługowego "kup jeszcze raz". Rekomendacja: zostawić. Zapobiega podwójnym obciążeniom, takich przypadków będzie kilka w roku.
2. **Interwał cleanupu (default 6 h)**: oryginalny harmonogram żyje tylko w prod DB Supabase (`cron.job`). Rekomendacja: potwierdzić przy zrzucie z migracji i wpisać do `.env` na VPS.
3. **Eventy webhooka dla legacy**: plan zakłada `invoice.payment_failed` + `charge.refunded`. Rekomendacja: dołożyć `invoice.payment_succeeded`, żeby flip statusu paused->active i historia odnowień działały też dla legacy. Koszt zero (handler wspólny).
4. **Jednorazowa normalizacja emaili customerów w Stripe** (lowercase na obu kontach): alternatywa do fallbacku search, który już działa. Rekomendacja: odpuścić. Search załatwia sprawę bez ruszania danych w Stripe.
5. **befreeclub-api (cache Circle na VPS)**: members ma własną integrację Circle. Wygaszamy po fazie 2 czy zostaje dla innych konsumentów? (otwarte z PLAN_LANDING, do sprawdzenia kto z niego korzysta).
6. **Widok "do zafakturowania" w adminie**: dane są w billing (wants_invoice, nip). W zakresie fazy 2.2 czy osobny temat? (otwarte z PLAN_LANDING).
7. **Pauza a Circle**: pauza adminowa z `remove_from_circle=false` zostawia członka w Circle (status paused tylko w DB i Stripe). To zachowanie oryginału. Jeśli pauza ma też zabierać dostęp do społeczności, trzeba to dodać świadomie (dziś robi to tylko flaga remove_from_circle).

## E. Ścieżki frontowe zakładane przez backend (wejście dla fazy 2.4)

Maile i redirecty backendu linkują do tych ścieżek. Front MUSI je obsłużyć:

| Ścieżka | Źródło linku | Uwagi |
|---|---|---|
| `/anuluj/potwierdz?token=<HMAC>` | mail anulowania | front robi POST `/api/billing/cancellation/confirm` z przycisku (nie z useEffect) |
| `/aktualizuj-karte?token=<HMAC>` | mail zmiany karty (nowy) | czyta `?token=`, woła POST `/api/billing/payment-method/setup-intent` |
| `/aktualizuj-karte` (bez tokenu) | mail payment_failed (hardcoded URL) | krok email -> POST `/request-link` |
| `/aktualizuj-karte` (powrót z 3DS) | return_url Stripe | front trzyma token w return_url, `setup_intent` id czyta z parametrów Stripe, woła POST `/confirm` |
| `/ebook/pobierz?token=<hex64>` | mail dostawy ebooka | GET `/api/billing/ebook/download?token=` |
| `/newsletter/potwierdz?token=<HMAC>` | mail DOI | baza z env `CONFIRM_URL_BASE` (musi się pokrywać) |
| `/sukces?source=klarna&plan=<slug>&session_id=<id>` | success_url Klarny | POST `/api/billing/checkout/klarna/confirm` |
| `/?checkout_failed=true&planId=<slug>` | cancel_url Klarny | landing pokazuje komunikat |

Twarde wymogi dla frontu 2.4 (z review):

- **Pending-confirmation**: po definitywnym 402 z `/checkout/confirm` front MUSI wyczyścić wpis pending w localStorage i odświeżyć SetupIntent w modalu. Idempotency key replayuje odmowę przez 24 h, auto-retry ze starym `seti` dostanie tę samą odmowę.
- **Ebook**: przekazywać `receipt_email` w `stripe.confirmPayment` (obie ścieżki: express i formularz). Backend ma fallback z billing_details, ale receipt_email to ścieżka pierwszego wyboru.
- **Atrybucja**: wysyłać `attribution` w `/checkout/confirm` (tam jest punkt zapisu dla subskrypcji); dla Klarny i ebooka przy create (bez zmian).
- Stopki maili linkują do `https://befreeclub.pl/` i `https://www.instagram.com/krystianbefree/`.

## F. Checklista migracji (faza 2.5)

1. **Stripe Dashboard, eventy do zasubskrybowania** (na endpointy nowego backendu):
   - current (`/api/billing/webhooks/stripe/current`): `invoice.payment_failed`, `invoice.payment_succeeded`, `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `charge.refunded`, `payment_intent.succeeded`
   - legacy (`/api/billing/webhooks/stripe/legacy`): `invoice.payment_failed`, `charge.refunded` (+ rekomendowane `invoice.payment_succeeded`, decyzja D3)
2. **Sekrety**: pełna lista w `backend/.env.production.example` (sekcja "Faza 2"). Wymagane przed przepięciem: `STRIPE_SECRET_KEY`, `STRIPE_LEGACY_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_LEGACY_WEBHOOK_SECRET`, `CIRCLE_API_TOKEN`, `CIRCLE_COMMUNITY_ID`, `RESEND_API_KEY`, `CANCELLATION_DOI_SECRET`, `NEWSLETTER_DOI_SECRET`. Rotacje: ADMIN_TOKEN (wycofany z kodu, zrotować w Supabase), SENDER_API_TOKEN (wyciek), weryfikacja NEWSLETTER_FROM_EMAIL w Resend.
3. **cron.job**: zrzut `SELECT * FROM cron.job` + `job_run_details` z żywego Supabase PRZED wygaszeniem. Potwierdzić interwał cleanupu (env `MEMBERSHIP_CLEANUP_INTERVAL_MS`, default 6 h).
4. **PDF ebooka**: eksport z bucketa Supabase do `<repo>/data/ebook/Na-swoich-zasadach-jako-freelancer.pdf` na VPS. Compose montuje `../data/ebook` -> `/data/ebook` (read-only), `EBOOK_FILE_PATH` ustawione w szablonie env.
5. **Migracja danych**: `circle_members` -> `members.members` (wyliczenie enum statusu + flagi `protected` zamiast hardcoded PROTECTED_EMAILS), `ebook_orders`/`ebook_download_tokens` -> `billing.*`, `cancellation_reasons` -> `billing.cancellation_reasons`, `contact_messages` -> `newsletter.contact_messages`.
6. **Plany**: seed 0002 zweryfikowany 1:1 z oryginałem (price ID i kwoty). Wszystkie sprzedawalne plany MUSZĄ być na koncie current (guard w kodzie to wymusza).
7. **Po przepięciu**: smoke test webhooka (zły podpis -> 400), pierwszy przebieg cleanupu ręcznie z panelu (trigger `membership_cleanup`) i przegląd decyzji PRZED zostawieniem workera samemu sobie.
