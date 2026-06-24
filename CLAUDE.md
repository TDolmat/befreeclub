# CLAUDE.md - monorepo `befreeclub` (migracja na VPS)

Docelowe monorepo ekosystemu Be Free Club: backend FastAPI (modularny monolit), frontendy React (pnpm workspace), infra Docker, migracje danych. Zastępuje stare apki Lovable/Supabase. Orientacja: `docs/ARCHITEKTURA.md`. Plan landinga: `docs/PLAN_LANDING.md`. Deploy: `docs/RUNBOOK_DEPLOY_VPS.html` + `infra/README.md`. Specy 1:1 starego kodu: `docs/spec/` (admin), `docs/spec-landing/` (landing).

## Komendy

- Backend: `cd backend && uv run uvicorn app.main:app --port 3000 --reload`, testy `uv run pytest tests/ -q`, lint `uv run ruff check app`, migracje `uv run alembic upgrade head`.
- Frontendy: `cd frontends && pnpm dev | build | typecheck` (biome zamiast eslint).
- Lokalny dev i mocki zewnętrznych API: `docs/DEV.md`. Taski VS Code: `.vscode/tasks.json`.

## Architektura w skrócie

Moduł = schemat Postgresa = prefiks URL. Backend: `app/modules/<modul>/` (admin, circle_dm, billing, members, newsletter, landing). Wspólny kod w `app/core/`. Jedno repo, ale kilka kontenerów (backend + nginx fronty + Postgres) - patrz `infra/`.

## DYREKTYWA: ustawienia zawsze do panelu admina

**Każdą nową rzecz kontrolną, którą dodajesz w backendzie, od razu, z własnej inicjatywy, wystawiasz w centralnej sekcji Ustawienia panelu admina.** Nie czekaj, aż user o to poprosi. Dotyczy: włączników/flag (workery, funkcje), progów, interwałów, nazw modeli Ach, limitów/budżetów, parametrów niesekretnych (community ID, grupy Sender, maile nadawcze, Pixel ID), promptów.

Mechanizm: tabela `admin.settings` (klucz per ustawienie, JSONB) + serwis `app/modules/admin/services/settings.py` (cache + bezpieczny fallback) + endpointy `/api/admin/settings/*` + sekcja w `frontends/admin` (`/ustawienia`). Ustawienie z bazy **nadpisuje** fallback z env. Katalog wszystkich ustawień: `docs/spec-landing/ustawienia-katalog.md` - przy dodaniu nowego dopisz tam wiersz.

Twarde zasady:

1. **Bezpieczne domyślne.** Nic destrukcyjnego nie włącza się samo. Świeży deploy = wszystko, co może skasować dane albo ruszyć realnych ludzi (cleanup członkostw, deprovisioning, masowe akcje), jest DOMYŚLNIE WYŁĄCZONE albo w trybie cienia. Brak wiersza w `admin.settings` = bezpieczny fallback, nigdy „włączone".
2. **Sekrety integracji edytowalne TYLKO przez szyfrowany store.** 4 klucze API (OpenAI, Resend, Sender, Meta CAPI) można ustawiać i odczytywać w panelu (Połączenia API). Leżą zaszyfrowane Fernetem w `admin.encrypted_secrets`, master key w env `SECRETS_MASTER_KEY`. Odczyt: maska w statusie (np. `sk-…AB12`) + pełna wartość tylko przez `GET /api/admin/connections/{key}/secret/reveal` (świadoma akcja za auth). Wartość NIGDY nie leci w GET listy, w `detail` błędu ani do logów. Env = opcjonalny fallback (resolver: DB odszyfrowany > env > brak). Brak/zły master key = bezpieczny fallback na env, nigdy crash. **Status-only z env (NIE edytowalne w panelu): Stripe current+legacy, Circle** (oraz poza panelem: webhook secrety Stripe, sekrety DOI, token bootstrapu). Mechanizm: `app/core/secret_box.py` (Fernet) + `app/modules/admin/services/secrets.py` (cache, resolver). Nowy sekret edytowalny = dopisz do `secrets.SECRET_KEYS` i nadaj `secret_key` w `connections.py`.
3. **Destrukcyjne przełączniki za potwierdzeniem.** Włączenie realnego usuwania/wysyłki to świadoma akcja z wyraźnym ostrzeżeniem w UI.

## DYREKTYWA: unit testy w backendzie

**Każdą zmianę i nową logikę w backendzie, którą da się sensownie pokryć unit testem, pokrywasz unit testem** (pytest, w `backend/tests/`). Z własnej inicjatywy, nie czekaj aż user poprosi. Test zostaje w repo na stałe, jest częścią suite, nie jednorazówką.

Dotyczy logiki gdzie regresja boli: walidatory, serwisy, transformacje, mapowania, parsowanie, edge case'y, kształty odpowiedzi API. Nie dotyczy trywialnego glue/konfiguracji gdzie test nic nie wnosi.

**Po każdej zmianie w backendzie profilaktycznie odpalasz `cd backend && uv run pytest tests/ -q`** żeby złapać regresje, zanim uznasz robotę za zrobioną. Jak coś się wywali, naprawiasz przed zakończeniem. Przy zmianie zachowania aktualizujesz istniejące testy tak, żeby sprawdzały nowe (poprawne) zachowanie, a nie kasujesz asercji.

## Konwencje (z `docs/spec/port-kontrakt.md`)

- API JSON camelCase (pydantic `CamelModel`, alias to_camel). Błąd: `{"error": "..."}`, walidacja 400 (nie 422). Daty: `toISOString` z `Z`.
- Auth: `require_auth` na `/api/*` (poza `/api/auth/*` i `/health*`). Dev bypass gdy `NODE_ENV != production`.
- Jeden proces uvicorn (stan in-memory: cache, semafory, WS). Nie skalować na multi-worker bez przeniesienia stanu.
- Port 1:1 starego kodu: nazwy pól, kształty, prompty bajt w bajt. Naprawione quirki: `docs/spec/port-odstepstwa.md`, `docs/spec-landing/port2-odstepstwa.md`.

## Głos marki i reguły ekosystemu

Obowiązuje `../../CLAUDE.md` (umbrella) i globalne preferencje. Teksty dla usera po polsku, na Ty, bez długich myślników, bez korpomowy. Circle DM testować tylko na kontach Paweł Wyrozumski i Tomasz Dwa. Nigdy nie dodawać asystentów AI do pól tekstowych. Commit/push tylko jako Tomek, na `main`, bez wzmianki o AI.
