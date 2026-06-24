# Runbook: deploy FAZY 1 (admin.befreeclub.pro) na nowy VPS

Uzupelnia `RUNBOOK_DEPLOY_VPS.html` o rzeczy dodane po jego napisaniu: `SECRETS_MASTER_KEY`,
migracja `0004_admin_encrypted_secrets`, obowiazkowy `--build` (cryptography), pulapka workerow landingu.

Stan startowy: repo sklonowane na nowy VPS w `~/apps/befreeclub` (koniec kroku 6 runbooka HTML).
Serwer zahartowany, Docker + siec `caddy_network` + pusty Caddy stoja. Robimy od `.env` do zywego admina.
Stary VPS zostaje WLACZONY jako rollback przez cala operacje.

Legenda: **[TOMEK]** = akcja, ktora tylko Ty mozesz zrobic (sekrety, hasla, DNS, dump).

---

## 0. Aktualny kod na VPS + sanity-check + snapshot

NAJPIERW pobierz aktualny kod. Caly projekt (faza 1/2.1, redesign Ustawien, feature
edytowalnych kluczy API, migracja `0004`, ten runbook) wjechal na `main` dopiero teraz -
swiezy clone na VPS mial tylko "Initial commit".
```bash
# NA NOWYM VPS
cd ~/apps/befreeclub
git pull origin main
git log --oneline -1   # ma byc najnowszy commit, NIE "Initial commit"
ls infra/ backend/.env.production.example backend/app/core/secret_box.py   # secret_box.py istnieje = masz feature sekretow
```
Zrob snapshot nowego boxa w OVH. Wszystko az do migracji danych jest odwracalne snapshotem.

## 1. [TOMEK] SECRETS_MASTER_KEY

Backend czyta to (`app/core/secret_box.py`, Fernet; migracja `0004` tworzy `admin.encrypted_secrets`).
Bez klucza panel "Polaczenia API" nie pozwoli edytowac kluczy integracji (cichy fallback na env, nie crash).
Wygeneruj NOWY na prod (NIE kopiuj z dev, rotacja = utrata zapisanych w panelu sekretow):
```bash
docker run --rm python:3.12-slim sh -c \
  'pip -q install cryptography >/dev/null 2>&1 && python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
```
Zapisz 44-znakowy wynik do menedzera hasel.

## 2. [TOMEK] Sekrety fazy 1 + haslo DB

```bash
# NA NOWYM VPS - haslo Postgresa (NOWA wartosc, nie ze starego boxa)
openssl rand -hex 24
```
Do reki: `BOOTSTRAP_ADMIN_TOKEN` = Circle Headless token (ta sama wartosc co stary admin).
Opcjonalnie `OPENAI_API_KEY` (wlacza transkrypcje glosowek + opisy zdjec; puste = te dwie funkcje off).
Sekrety fazy 2 (Stripe x2, Circle API, Resend, Sender, Meta, DOI) **nie teraz**.

## 3. [TOMEK] backend/.env

```bash
cd ~/apps/befreeclub
cp backend/.env.production.example backend/.env
nano backend/.env
```
Wypelnij realnie tylko: `DB_PASS=<openssl z kroku 2>`, `BOOTSTRAP_ADMIN_TOKEN=<Circle token>`,
`SECRETS_MASTER_KEY=<klucz z kroku 1>` (jest juz w szablonie, pusta linia do wypelnienia),
opcjonalnie `OPENAI_API_KEY=<klucz>`.

Zostaw PUSTE: wszystkie `STRIPE_*`, `CIRCLE_*`, `RESEND_*`, `SENDER_*`, `META_*`, DOI.
NIE czysc interwalow workerow (`*_INTERVAL_MS`) - to inty, pusty string = crash. NIE dotykaj
`NODE_ENV`/`PORT`/`DB_HOST`/`CLAUDE_BIN_PATH`/`WEB_DIST_PATH` (wymusza compose).
```bash
cd infra
ln -s ../backend/.env .env          # symlink WYMAGANY do interpolacji ${DB_PASS}
chmod 600 ../backend/.env
ls -l ../backend/.env               # ma byc -rw-------
```

## 4. Build + start (alembic 0004 leci sam)

```bash
cd ~/apps/befreeclub/infra
docker compose up -d --build        # --build OBOWIAZKOWE (ciagnie cryptography>=49 pod secret_box.py)
docker compose ps
docker compose logs backend | tail -30
```
Migracje alembic (w tym `0004_admin_encrypted_secrets`) leca AUTOMATYCZNIE w CMD `alembic upgrade head`
przy starcie backendu. Blad migracji = kontener restartuje sie w petli (czytaj logi). Front buduje sie na Node 24.

> Sam `restart` bez `--build` na starym obrazie = brak cryptography = backend pada przy imporcie. Zawsze `--build`.

## 5. Health + [TOMEK] claude login + konta panelu

```bash
docker compose exec backend curl -fsS http://localhost:3000/health   # {"ok":true}
```
**[TOMEK]** claude login (device flow):
```bash
docker compose exec -it backend claude login   # otworz URL na Macu zalogowanym w Claude Max, Authorize
docker compose exec backend claude --print 'czesc'
```
**[TOMEK]** konta panelu (min 12 znakow, haslo ze stdin) - swiezy fallback, po migracji i tak dzialaja stare:
```bash
docker compose exec -it backend set-auth-password --email tomasz@befreeclub.pl
docker compose exec -it backend set-auth-password --email krystian@befreeclub.pl
```

## 6. [TOMEK] Dump starej bazy (okno serwisowe)

Najbardziej ryzykowny moment. Zrob snapshot OVH STAREGO boxa.
```bash
ssh STARE_IP
cd ~/repos/befreeclub/admin
docker compose stop admin-server                 # okno serwisowe, baza zostaje zywa
mkdir -p ~/backups
docker compose exec -T admin-db pg_dump -U admin -d bfc_admin -Fc > ~/backups/bfc_admin_$(date +%F).dump
ls -lh ~/backups
```
Transfer przez Maca:
```bash
scp STARE_IP:~/backups/bfc_admin_*.dump /tmp/
scp /tmp/bfc_admin_*.dump ubuntu@NOWE_IP:~/
```

## 7. Migracja danych: dump -> migrate_data.py

```bash
cd ~/apps/befreeclub/infra
docker compose exec -T db createdb -U befreeclub bfc_admin_src
cat ~/bfc_admin_*.dump | docker compose exec -T db pg_restore -U befreeclub --no-owner --no-acl -d bfc_admin_src

curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc

cd ~/apps/befreeclub/scripts/data-migration
# DRY-RUN (nic nie zapisuje)
uv run --with asyncpg python migrate_data.py \
  --source-dsn 'postgresql://befreeclub:DB_PASS@127.0.0.1:5434/bfc_admin_src' \
  --target-dsn 'postgresql://befreeclub:DB_PASS@127.0.0.1:5434/befreeclub' --dry-run
# Plan czysty + liczniki OK -> to samo BEZ --dry-run
```
Sukces = exit 0, raport bez slowa ROZJAZD (16 tabel, zachowane id/sekwencje). Exit 1 = rozjazd
(rollback zrobiony, baza nietknieta), exit 2 = blad planu. Powtorka z `--truncate` (potwierdzasz `befreeclub`).
Troubleshooting: `scripts/data-migration/MIGRACJA_DANYCH.md`.

> `admin.encrypted_secrets` **nie jest migrowane** (nowa pusta tabela). 4 klucze API wpiszesz w panelu w fazie 2.

```bash
docker compose exec db psql -U befreeclub -d befreeclub -c 'SELECT count(*) FROM circle_dm.threads'
docker compose exec db psql -U befreeclub -d befreeclub -c 'SELECT id,email FROM admin.users'
docker compose exec -T db dropdb -U befreeclub bfc_admin_src
rm ~/bfc_admin_*.dump
```

## 8. Weryfikacja PRZED DNS (tunel, nie domena)

```bash
# NA MACU
ssh -L 8080:127.0.0.1:3000 ubuntu@NOWE_IP
# drugie okno:
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/health/claude     # zielony = claude zalogowany
```
Potwierdz backend + migracje + claude ZANIM ruszysz DNS. Caddy nadal pusty (cert wymaga DNS na nowy box).

## 9. Caddy blok + [TOMEK] przepiecie DNS

Dopisz blok do `~/caddy/Caddyfile` (matcher `@backend` MUSI byc przed catch-all):
```
admin.befreeclub.pro {
  @backend path /api/* /ws /health /health/*
  reverse_proxy @backend bfc-backend:3000
  reverse_proxy bfc-admin-front:80
}
```
```bash
cd ~/caddy && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```
**[TOMEK]** DNS Hostinger: rekord A `admin.befreeclub.pro` ze starego IP na `NOWE_IP` (TTL 300s).
**NIE** ruszaj rekordow api/scoper/root - zostaja na starym boxie.

Kolejnosc sztywna: aplikacja stoi -> DNS przepiety -> blok Caddy + reload (inaczej ACME challenge poleci
na stary box i cert nie powstanie).

## 10. Weryfikacja na zywej domenie

```bash
# NA MACU (kilka min na propagacje + cert)
dig +short admin.befreeclub.pro     # NOWE_IP
curl https://admin.befreeclub.pro/health   # {"ok":true} przez HTTPS
```
W panelu: logowanie STARYM haslem (scrypt bez zmian), listy watkow/czlonkow/ustawienia/KB/feedback jak przed
migracja, `/health/claude` zielony. **Circle DM na zywo TYLKO na kontach Pawel Wyrozumski i Tomasz Dwa.**
W `/ustawienia` bramki membership (cleanup/klarnaReconcile/inviteRetry) **WYLACZONE** - zostaja off do fazy 2.

---

## PULAPKA: monorepo niesie landing + workery (faza 2)

Monorepo ma moduly landingu (billing/members/newsletter) i 5 workerow startujacych w lifespanie -
w tym landingowe (cleanup czlonkostw, klarna reconcile, invite retry).

**Bezpieczne domyslnie (nic nie robisz):**
- 3 workery landingu maja tick zbramkowany na `admin.settings enabled=false` (SAFE_DEFAULTS = brak wiersza).
  Cleanup dodatkowo `dryRun=true`. Swiezy deploy NIKOGO nie usuwa.
- Webhooki Stripe: panel Stripe decyduje gdzie leci webhook. Dopoki nie przepniesz na nowy backend, nie dostaje ich.
  Bez `STRIPE_WEBHOOK_SECRET` nowy endpoint zwraca 500 "Webhook not configured". Zero podwojnego przetwarzania.
- Sekrety fazy 2 puste = funkcje cicho off, backend startuje bez bledu.

**MUSISZ zostawic wylaczone do fazy 2:**
- Nie wlaczaj w `/ustawienia` bramek cleanup/klarnaReconcile/inviteRetry dopoki zywy landing siedzi na Supabase.
  `cleanup enabled=true + dryRun=false` = realne usuwanie czlonkow z Circle, gryzie sie z Supabase cronem.
- Reczny trigger cleanupu ignoruje bramke `enabled`, ale `dryRun` z ustawien GO OBOWIAZUJE.
- Nie przepinaj webhookow Stripe na nowy backend.

---

## Rollback

DNS w Hostinger: rekord A `admin.befreeclub.pro` z powrotem na `STARE_IP` (TTL 300s = wraca w minuty).
```bash
# STARY VPS
cd ~/repos/befreeclub/admin && docker compose start admin-db admin-server
```
Stara baza byla tylko czytana, wraca stan sprzed. **Sensowny tylko tuz po cutover** - zmiany zrobione w nowym
panelu po przepieciu nie istnieja w starej bazie. Wczesniejsze etapy (przed DNS) odkrecasz snapshotem OVH.
NIE kasuj wolumenow ani kontenerow starego admina przez kilka dni.

## Sciaga: aktualizacja po zmianie kodu

```bash
cd ~/apps/befreeclub && git pull
cd infra
docker compose up -d --build backend          # zmiany w backend/
docker compose up -d --build frontend-admin   # zmiany we frontends/
```
Baza (`db`) i tokeny Claude przezywaja rebuildy.
