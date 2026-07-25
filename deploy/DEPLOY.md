# Deploy on a Contabo VPS (4 vCPU / 8 GB / 100 GB SSD) + Cloudflare

This stack runs **Postgres + API (8 workers) + Telegram bot + Caddy (auto‑HTTPS) +
nightly backups** in one `docker compose` command. Cloudflare (free) sits in front
and caches the immutable results to absorb the result‑day burst.

```
Cloudflare (free CDN)  ->  Caddy :443 (auto‑TLS)  ->  API (gunicorn ×8)  ->  Postgres
                                                   ->  Telegram bot     ->  Postgres
                                                   backup -> nightly pg_dump
```

---

## 0. One‑time: point a domain at the VPS

You need a domain (~$10/yr) — Cloudflare requires one.

1. Buy a domain (Namecheap/Cloudflare Registrar/…).
2. In Cloudflare: **Add a site** → enter the domain → it gives you 2 nameservers.
3. At your registrar, set the domain’s nameservers to those two. Wait for “Active”.
4. In Cloudflare **DNS**, add an **A record**: name `@` (or `results`) → your VPS IP,
   **Proxy status = Proxied (orange cloud)**.
5. In Cloudflare **SSL/TLS** → set mode to **Full (strict)**.

## 1. Prepare the server

```bash
# on the VPS (Ubuntu/Debian)
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker

git clone <your-repo-url> results && cd results
cp .env.example .env
```

## 2. Fill in `.env`

Edit `.env` and set at least:

```ini
POSTGRES_PASSWORD=<a-strong-password>
DATABASE_URL=postgresql+psycopg://results:<same-password>@postgres:5432/results
DOMAIN=results.yourdomain.com
ADMIN_TOKEN=<a-long-random-secret>
TELEGRAM_TOKEN=<from @BotFather, or leave blank to disable the bot>
ADMIN_IDS=<your telegram numeric id, for zip uploads via the bot>
CACHE_MODE=setup        # keep "setup" until all cities are loaded, then switch to prod
```

## 3. Launch

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Caddy fetches HTTPS certs automatically. Visit `https://results.yourdomain.com`.

## 4. Load cities (admin)

Put each city `.zip` (or folder) into `./citydata/` on the host, then:

```bash
docker compose -f docker-compose.prod.yml exec api \
  python -m app.ingest "/data/31_بغداد.zip"
```

`/data` inside the container = `./citydata` on the host. Province name is taken
from the file name (`31_بغداد`). ~15 seconds per city. Repeat for every city.

(You can also upload a zip by sending it to the Telegram bot as an admin, or:
`curl -X POST https://results.yourdomain.com/api/ingest -H "Authorization: Bearer $ADMIN_TOKEN" -F province=31_بغداد -F file=@31_بغداد.zip`.)

## 5. Go live (turn on CDN caching)

Once all cities are loaded and verified:

```bash
# set CACHE_MODE=prod in .env, then:
docker compose -f docker-compose.prod.yml up -d
```

Now responses say `Cache-Control: public, max-age=3600` and Cloudflare caches them.
Add a Cloudflare **Cache Rule**: if URI path starts with `/api/` → **Eligible for cache**
(Cloudflare respects the origin `max-age`). Optionally enable **Tiered Cache**.

To push fresh data after a later re‑ingest: Cloudflare → **Caching → Purge Everything**
(the dataset is tiny, so a full purge is fine).

## 6. Operations

- **Logs:** `docker compose -f docker-compose.prod.yml logs -f api`
- **Restart after code update:** `git pull && docker compose -f docker-compose.prod.yml up -d --build`
- **Backups:** nightly `pg_dump` lands in `./backups/` (last 14 kept). Copy them off‑box periodically.
- **Restore:** `gunzip -c backups/results_YYYY…​.sql.gz | docker compose -f docker-compose.prod.yml exec -T postgres psql -U results results`
- **VPS snapshot:** take your 1 Contabo snapshot after the first successful deploy as a restore point.

## Capacity note
Dataset is < 1 GB and read‑only, so it lives in RAM. Budget on the box: Postgres
~1–2 GB, API workers ~1 GB, bot ~150 MB, OS+Caddy ~1 GB → ~4 GB of 8 GB used.
With Cloudflare in front, a 50k‑simultaneous result‑day spike is served mostly from
the edge; the VPS handles the unique‑request trickle comfortably.
