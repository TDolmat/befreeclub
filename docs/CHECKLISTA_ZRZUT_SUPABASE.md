# Checklista: zrzut z żywego Supabase przed migracją landinga

Do zrobienia ręcznie (wymaga dostępu do dashboardów). Nic tu nie zmienia proda, wszystko jest tylko do odczytu. Bez tych danych nie da się bezpiecznie domknąć fazy 2.5 (migracja i przepięcie). Wyniki wrzucaj do `docs/zrzut-supabase/` (katalog jest w .gitignore? sprawdzić: sekrety NIE idą do gita, reszta może).

## 1. Crony (KRYTYCZNE, istnieją tylko w żywej bazie)

W SQL Editor projektu głównego (`fshkdkvoyysphfrfvmni`):

```sql
SELECT * FROM cron.job;
SELECT jobid, runid, status, return_message, start_time
FROM cron.job_run_details ORDER BY start_time DESC LIMIT 50;
```

Zapisz wynik (CSV/screenshot). To jedyne źródło harmonogramów circle-cleanup, reconcile-klarna-checkouts itd. Worker w nowym backendzie ma domyślne interwały, ale chcemy odtworzyć realne.

## 2. Sekrety edge functions (dwa projekty!)

- Projekt główny: Dashboard → Edge Functions → Secrets. Spisz NAZWY i wartości wszystkich (ok. 15, lista nazw w `docs/spec-landing/config-deploy.md`). Wartości do menedżera haseł, nie do gita.
- Projekt newsletterowy na koncie Krystiana (`rxqaedlhkdrkkdpwkyho`): to samo. Przy okazji ustal, czy funkcje newslettera na prodzie faktycznie chodzą z projektu głównego czy z tego drugiego (README mówi jedno, frontend woła drugie). Najprościej: Dashboard → Edge Functions → Logs w obu projektach i zobacz, gdzie wpadają wywołania newsletter-subscribe.

## 3. Realny stan verify_jwt

Dashboard → Edge Functions: dla każdej z 26 funkcji spisz, czy ma włączoną weryfikację JWT. Stan na prodzie może się różnić od config.toml (deploye szły z --no-verify-jwt).

## 4. Dane

Connection string: Dashboard → Settings → Database. Potem lokalnie:

```bash
pg_dump "<CONNECTION_STRING>" \
  --table=public.circle_members --table=public.cancellation_reasons \
  --table=public.cancellation_tokens --table=public.ebook_orders \
  --table=public.ebook_download_tokens --table=public.contact_messages \
  --table=public.newsletter_subscribers \
  --data-only --column-inserts > zrzut-landing-dane.sql
```

Plus plik ebooka: Dashboard → Storage → bucket `ebooks` → pobierz `na-swoich-zasadach.pdf` (pójdzie na wolumen VPS, zmienna `EBOOK_FILE_PATH`).

## 5. Stripe (dashboard, oba konta)

- Developers → Webhooks: spisz endpoint(y), subskrybowane eventy i **wersję API endpointu** (ważne: od tego zależy kształt pola subscription w invoice.payment_failed).
- Developers → API version konta.
- Konto legacy: potwierdź, że nie ma żadnego webhooka (tak wynika z kodu).
- NIE zmieniaj niczego. Przepięcie webhooków na nowy backend to faza 2.5.

## 6. Analityka

- Meta Pixel ID i Clarity ID są w `index.html` (mamy w spec). Do CAPI potrzebny będzie **token Conversions API**: Meta Events Manager → źródło danych (pixel) → Ustawienia → Conversions API → wygeneruj token. Zapisz jako `META_CAPI_TOKEN` do menedżera haseł.
- Sender.net: potwierdź ID grup (kod ma default `epnLzm,el06vl`) i zrotuj `SENDER_API_TOKEN` (stary wyciekł wg README).

## 7. Rotacje przy okazji (po migracji, przed wygaszeniem)

- `ADMIN_TOKEN` (zahardkodowany w źródle admin-stripe-legacy-audit, jest w gicie) - po przejściu na panel admina przestaje istnieć, ale do czasu wygaszenia Supabase zrotuj.
- `SENDER_API_TOKEN` (wyciek), weryfikacja domeny w Resend + `NEWSLETTER_FROM_EMAIL` (możliwe że stoi na onboarding@resend.dev).
