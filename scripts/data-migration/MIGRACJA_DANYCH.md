# Migracja danych: bfc_admin -> befreeclub

Skrypt `migrate_data.py` kopiuje dane ze starej bazy admina (`bfc_admin`, schemat `public`, Hono+Drizzle) do nowej bazy `befreeclub` (schematy `admin` i `circle_dm`, FastAPI).

Skrypt NIE tworzy struktur. DDL nowej bazy (schematy, tabele, enumy, seed `circle_dm.settings`) stawia alembic nowego backendu. `alembic upgrade head` musi być odpalony PRZED migracją danych.

## Co robi skrypt

- Kopiuje 16 tabel z zachowaniem `id` (binarny COPY, batche po 1000 wierszy).
- Kolejność wg FK: rodzice przed dziećmi. Kolejność siedzi w stałej `MAPPING` w skrypcie.
- `circle_dm.settings`: upsert zamiast INSERT, bo alembic seeduje wiersz `id=1`.
- Mapuje kolumny introspekcją z `information_schema` obu baz. Kolumna bez odpowiednika po drugiej stronie (poza kolumnami z defaultem w nowej bazie) to twardy błąd. Nic nie ginie po cichu.
- Po kopii ustawia sekwencje (`setval` na `max(id)`). Tabele z PK tekstowym (`admin.sessions`) pomija automatycznie.
- Weryfikuje: count, max(id), liczniki per status, min/max created_at. Raport tabelką.
- Cały zapis w JEDNEJ transakcji docelowej. Błąd albo rozjazd w weryfikacji = rollback, nowa baza zostaje nietknięta. Źródło czytane w spójnym snapshocie (repeatable read, readonly).

Kody wyjścia: `0` = OK, `1` = rozjazd w weryfikacji (zrobiony rollback), `2` = błąd planu/połączenia/SQL, `130` = przerwane Ctrl+C.

## Uruchomienie

```bash
cd scripts/data-migration
uv run --with asyncpg python migrate_data.py [opcje]
```

| Opcja | Co robi | Default |
|---|---|---|
| `--source-dsn` | stara baza | `postgresql://tomasz@localhost:5432/bfc_admin` |
| `--target-dsn` | nowa baza | `postgresql://tomasz@localhost:5432/befreeclub` |
| `--dry-run` | pokaż plan i liczniki, nic nie zapisuj | wyłączone |
| `--truncate` | wyczyść tabele docelowe przed kopią (TRUNCATE CASCADE). Wymaga potwierdzenia: wpisania nazwy bazy docelowej | wyłączone |

`--truncate` używaj przy powtórce migracji (np. po nieudanym przebiegu albo świeższym dumpie). Na czystą bazę po alembicu nie jest potrzebny, seedowany wiersz settings załatwia upsert.

## Mapa nazw

| Stara tabela (public) | Nowa tabela | Uwagi |
|---|---|---|
| `auth_accounts` | `admin.users` | konta logowania do panelu |
| `auth_sessions` | `admin.sessions` | PK tekstowy, bez sekwencji |
| `feedback_items` | `admin.feedback_items` | |
| `admin_accounts` | `circle_dm.accounts` | konta Circle.so do wysyłki DM |
| `community_members` | `circle_dm.members` | |
| `dm_threads` | `circle_dm.threads` | |
| `dm_messages` | `circle_dm.messages` | |
| `message_image_descriptions` | `circle_dm.message_image_descriptions` | |
| `draft_sessions` | `circle_dm.draft_sessions` | |
| `draft_iterations` | `circle_dm.draft_iterations` | |
| `sent_messages` | `circle_dm.sent_messages` | |
| `thread_checkups` | `circle_dm.checkups` | |
| `app_settings` | `circle_dm.settings` | singleton id=1, upsert |
| `kb_documents` | `circle_dm.kb_documents` | duże wiersze (base64) |
| `assistant_conversations` | `circle_dm.assistant_conversations` | |
| `assistant_messages` | `circle_dm.assistant_messages` | |

Zmiany nazw kolumn (tylko dwie, systematyczne):

| Stara kolumna | Nowa | Gdzie |
|---|---|---|
| `auth_account_id` | `user_id` | `admin.sessions`, `admin.feedback_items`, `circle_dm.assistant_conversations` |
| `admin_account_id` | `account_id` | `circle_dm.threads`, `circle_dm.members`, `circle_dm.kb_documents` |

Reszta kolumn przechodzi 1:1 po nazwie.

## (a) Migracja lokalna (dev)

Wymagania: lokalny Postgres (Postgres.app/Homebrew), `uv`, stara baza `bfc_admin` z danymi.

1. Jeśli nie masz lokalnie starej bazy, odtwórz ją z dumpa:

```bash
createdb bfc_admin
psql bfc_admin < bfc_admin.sql      # albo: pg_restore -d bfc_admin bfc_admin.dump
```

2. Stwórz nową bazę i postaw DDL alembikiem:

```bash
createdb befreeclub
cd backend
uv run alembic upgrade head        # DATABASE_URL wg konfigu backendu
```

3. Dry-run. Zobaczysz plan, liczniki wierszy i ewentualne błędy mapowania kolumn:

```bash
cd ../scripts/data-migration
uv run --with asyncpg python migrate_data.py --dry-run
```

4. Migracja właściwa:

```bash
uv run --with asyncpg python migrate_data.py
```

5. Skrypt sam drukuje raport weryfikacji. Spot-check ręczny:

```bash
psql befreeclub -c "SELECT count(*) FROM circle_dm.threads"
psql befreeclub -c "SELECT id, email FROM admin.users"
```

6. Powtórka (np. po nowym dumpie): dodaj `--truncate` i potwierdź wpisując `befreeclub`.

7. Test logowania: hashe scrypt przechodzą bez zmian, stare hasło musi działać w nowym panelu.

## (b) Migracja PROD (VPS)

Stary stack: `~/repos/befreeclub/admin` na VPS. Kontenery `bfc-admin` (server) i `bfc-admin-db` (Postgres 16, user `admin`, baza `bfc_admin`, port `127.0.0.1:5433` na hoście). Hasło `DB_PASS` w `~/repos/befreeclub/admin/.env`.

Nowy stack: monorepo na VPS, compose w `infra/`. Wartości z `infra/docker-compose.yml`: backend w kontenerze `bfc-backend` (HTTP na 3000), baza `befreeclub` w kontenerze `bfc-db` (user `befreeclub`, na hoście `127.0.0.1:5434`), hasło w `DB_PASS` w `backend/.env`. Frontend admina to osobny kontener `bfc-admin-front` (nginx), w migracji danych nie bierze udziału.

Ważne przed startem: nowa baza Postgres musi mieć te same initdb args co stara (`--encoding=UTF8 --locale=C`), inaczej zmieni się collation i sortowanie tekstu.

### Krok 1: stop starego admina

Zatrzymujemy tylko serwer. Baza zostaje uruchomiona, jest źródłem migracji.

```bash
ssh vps
cd ~/repos/befreeclub/admin
docker compose stop admin-server
```

Od tego momentu admin.befreeclub.pro nie działa (okno serwisowe). Nic nowego nie wpada do starej bazy.

### Krok 2: dump bezpieczeństwa

```bash
mkdir -p ~/backups
docker compose exec -T admin-db pg_dump -U admin -d bfc_admin -Fc \
  > ~/backups/bfc_admin_$(date +%F).dump
```

Alternatywnie z hosta przez loopback (jeśli pg_dump jest na hoście): `pg_dump "postgresql://admin:DB_PASS@127.0.0.1:5433/bfc_admin" -Fc -f ~/backups/...`.

Dump to ubezpieczenie. Migracja i tak czyta z żywej bazy, nie z dumpa.

### Krok 3: nowy stack + DDL

```bash
cd ~/repos/befreeclub/befreeclub/infra
docker compose up -d
docker compose logs backend | tail -20
```

Alembic leci automatycznie przy każdym starcie kontenera backendu (CMD obrazu). Ręcznie w razie potrzeby:

```bash
docker compose exec backend alembic upgrade head
```

Sprawdź, że schematy istnieją:

```bash
docker compose exec db psql -U befreeclub -d befreeclub \
  -c "SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('admin','circle_dm')"
```

### Krok 4: migracja danych

Z hosta VPS, oba Postgresy po loopbacku. Najpierw dry-run:

```bash
cd ~/repos/befreeclub/befreeclub/scripts/data-migration
uv run --with asyncpg python migrate_data.py \
  --source-dsn "postgresql://admin:DB_PASS@127.0.0.1:5433/bfc_admin" \
  --target-dsn "postgresql://befreeclub:NOWE_HASLO@127.0.0.1:5434/befreeclub" \
  --dry-run
```

Plan czysty, liczniki się zgadzają z oczekiwaniami? Odpal bez `--dry-run`. Kod wyjścia 0 i raport bez ROZJAZD = dane skopiowane i zacommitowane.

### Krok 5: weryfikacja nowego panelu

```bash
docker compose exec backend curl -fsS http://localhost:3000/health
```

Po przepięciu Caddy (krok 6): zaloguj się starym hasłem, sprawdź listę wątków, członków, ustawienia, KB i feedback. Porównaj na oko ze stanem sprzed migracji.

### Krok 6: przepięcie Caddy

W `/var/www/caddy/Caddyfile` blok `admin.befreeclub.pro` wskazuje na stary kontener (`reverse_proxy bfc-admin:3000`). Przepięcie = podmiana CAŁEGO bloku domeny na split routing: API, WS i healthchecki do backendu, reszta (SPA) do nginx frontu. Oba nowe kontenery (`bfc-backend`, `bfc-admin-front`) muszą być w sieci `caddy_network`.

```caddy
admin.befreeclub.pro {
    @backend path /api/* /ws /health /health/*
    reverse_proxy @backend bfc-backend:3000
    reverse_proxy bfc-admin-front:80
}
```

```bash
cd /var/www/caddy
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
curl https://admin.befreeclub.pro/health
```

### Krok 7: po przepięciu

Stary stack zostaje na VPS jako rollback (server zatrzymany, baza może też stanąć: `docker compose stop admin-db`). Nie kasuj wolumenu `admin-db-data` ani kontenerów dopóki nowy panel nie przejdzie pełnej weryfikacji w realnym użyciu (kilka dni). Dopiero potem `docker compose down`.

## Rollback

```bash
# 1. Caddy z powrotem na stary kontener
# /var/www/caddy/Caddyfile:
#   admin.befreeclub.pro { reverse_proxy bfc-admin:3000 }
cd /var/www/caddy
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile

# 2. Start starego admina
cd ~/repos/befreeclub/admin
docker compose start admin-db admin-server   # admin-db tylko jeśli był zatrzymany
```

Stara baza była tylko czytana, więc wraca dokładnie stan sprzed migracji. Uwaga: wszystko co zrobiono w NOWYM panelu po przepięciu (wysłane DM-y, notatki, statusy) nie istnieje w starej bazie. Rollback po godzinach pracy w nowym panelu = utrata tych zmian.

## Troubleshooting

**`schema "circle_dm" does not exist`, `relation ... does not exist`, brak enumów** - alembic nie był odpalony na bazie docelowej. Skrypt wypisze to też jako błąd planu z podpowiedzią. Odpal `alembic upgrade head` i powtórz.

**`duplicate key value violates unique constraint` przy kopiowaniu** - tabele docelowe nie są puste (np. ktoś klikał w nowym panelu przed migracją albo poprzedni przebieg padł po commicie). Powtórz z `--truncate`.

**`duplicate key` w aplikacji PO migracji** - sekwencje nie nadążyły za skopiowanymi id. Skrypt ustawia je sam, ale gdyby coś poszło nie tak, ręcznie per tabela:

```sql
SELECT setval(pg_get_serial_sequence('circle_dm.threads', 'id'),
              (SELECT max(id) FROM circle_dm.threads), true);
```

**`peer authentication failed` / pyta o hasło** - na Linuxie połączenie po sockecie unixowym używa peer auth. Skrypt i tak łączy się po TCP, ale DSN musi mieć hosta i hasło: `postgresql://user:haslo@127.0.0.1:5433/baza`. Na Macu (Postgres.app/Homebrew) defaulty bez hasła działają.

**Błąd planu: kolumna bez odpowiednika** - DDL alembica różni się od starego schematu (literówka w nazwie, brakująca kolumna, kolumna NOT NULL bez defaultu). Skrypt celowo nie zgaduje. Popraw DDL albo dopisz rename do `MAPPING` w skrypcie.

**`ROZJAZD` w raporcie weryfikacji** - transakcja docelowa została wycofana, nowa baza bez zmian. Porównaj wartości w raporcie (count, max id, statusy, zakres created_at), znajdź tabelę z różnicą i sprawdź jej DDL.

**Wolno przy `kb_documents`** - normalne, tabela trzyma pliki PDF jako base64 w kolumnie text. Batch 1000 wierszy, po prostu poczekaj.

**`InterfaceError: prefetch argument can only be specified for iterable cursor`** - bug naprawiony 2026-06-10. asyncpg pozwala na `prefetch=` tylko przy iteracji `async for`; awaitowany kursor (tak czyta skrypt) rzuca ten błąd. Skrypt nie używa już `prefetch` (batch i tak kontroluje jawne `cursor.fetch(BATCH_SIZE)`). Jeśli widzisz ten błąd, masz starą wersję skryptu - zaktualizuj. Cel pozostał nietknięty (transakcja docelowa wycofana).

## Założenia do weryfikacji po powstaniu DDL alembica

- Nazwy tabel i kolumn w DDL zgodne z mapą wyżej (skrypt i tak to sprawdzi introspekcją).
- Enumy w nowych schematach mają te same wartości co stare (10 enumów wg `docs/spec/db-schema.md`).
- Alembic seeduje `circle_dm.settings` wierszem `id=1` (skrypt zakłada upsert).
- Alembic: lokalnie `uv run alembic upgrade head` w `backend/`, na VPS automat przy każdym starcie kontenera backendu (potwierdzone, CMD obrazu).
- Wartości z `infra/docker-compose.yml` (potwierdzone): kontener `bfc-backend`, HTTP `3000`, baza na loopbacku `5434`, user i baza `befreeclub`, hasło `DB_PASS` w `backend/.env`.
- Initdb args nowej bazy: `--encoding=UTF8 --locale=C`, jak w starej.
