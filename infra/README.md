# Infra - deploy na VPS

Trzy kontenery: `bfc-db` (Postgres 16), `bfc-backend` (FastAPI, czyste API + WS), `bfc-admin-front` (nginx ze zbudowanym SPA). Frontu NIE serwuje backend. Split routing po stronie Caddy hosta.

Wzorce (healthchecki, wolumeny, user uid=1000, claude login) przeniesione ze starego admina: `docs/spec/deploy.md`.

## Build i start

```bash
cd infra
docker compose up -d --build
docker compose ps
docker compose logs backend | tail -20
```

Migracje alembica lecą automatycznie przy każdym starcie backendu (idempotentne). Błąd migracji = kontener nie wstaje i się restartuje.

## Pierwszy setup na VPS

1. Deploy key + klon repo:

```bash
ssh tomasz@vps
cd ~/repos/befreeclub
git clone git@github.com:<org>/befreeclub.git befreeclub
cd befreeclub
```

(Jeśli repo prywatne: `ssh-keygen -t ed25519 -f ~/.ssh/befreeclub_deploy`, klucz publiczny jako deploy key w GitHubie, wpis w `~/.ssh/config`.)

2. Env. Jeden plik sekretów: `backend/.env`. Symlink w `infra/` daje docker compose dostęp do `${DB_PASS}` przy interpolacji:

```bash
cp backend/.env.production.example backend/.env
# uzupelnij DB_PASS (openssl rand -hex 24), opcjonalnie OPENAI_API_KEY i BOOTSTRAP_*
cd infra
ln -s ../backend/.env .env
```

3. Start: `docker compose up -d --build`.

4. Claude login (device-code flow, tokeny lądują w wolumenie `claude-config`):

```bash
docker compose exec -it backend claude login
# URL otwierasz na Macu zalogowany w Claude Max, Authorize
docker compose exec backend claude --print "Powiedz czesc."
```

5. Konto logowania do panelu:

```bash
docker compose exec -it backend set-auth-password --email tomasz@befreeclub.pl
```

Hasło ze stdin (bez echa), min 12 znaków. Ta sama komenda zmienia hasło i unieważnia wszystkie sesje konta.

6. Blok w `/var/www/caddy/Caddyfile` (patrz niżej) + reload.

## Caddyfile - split routing

API, WebSocket i healthchecki idą do backendu, cała reszta (SPA) do nginx:

```caddy
admin.befreeclub.pro {
    @backend path /api/* /ws /health /health/*
    reverse_proxy @backend bfc-backend:3000
    reverse_proxy bfc-admin-front:80
}
```

Kolejność ma znaczenie: reverse_proxy z matcherem `@backend` musi być PRZED catch-all. Caddy trzyma kolejność deklaracji dla tej samej dyrektywy, pierwszy pasujący wygrywa. WebSocket upgrade na `/ws` Caddy robi sam.

Reload:

```bash
cd /var/www/caddy
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
curl https://admin.befreeclub.pro/health
```

## Update po zmianach kodu

```bash
ssh vps
cd ~/repos/befreeclub/befreeclub
git pull
cd infra
docker compose up -d --build backend          # zmiany w backend/
docker compose up -d --build frontend-admin   # zmiany w frontends/
```

Restart backendu NIE zdejmuje frontu i odwrotnie - to osobne kontenery, deploye są niezależne. `db` nie ruszamy. Dane (wolumen `bfc-db-data`) i tokeny Claude (`claude-config`) przeżywają rebuildy.

## Monitoring

Publiczne endpointy backendu (pre-auth, pinguje je n8n):

- `GET /health`
- `GET /health/claude` (z `?deep=1` realny round-trip do Claude, cache 1h)
