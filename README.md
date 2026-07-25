# نتائج السادس — Iraqi 6th-Grade Results Fast Lookup

A fast lookup service for Iraqi Ministry of Education 6th-grade exam results.
Students search by **exam number** or by **name** (optionally scoped to a
province), on the web or through a **Telegram bot**.

The hard part — decoding the Ministry's result PDFs, which embed a subsetted
Identity-H font that yields mojibake under normal extraction — is already solved
in `app/glyph.py` + `app/parse_pdf.py`. Everything downstream reads clean,
normalized Arabic.

## Architecture

```
  Ministry PDFs                          reads (immutable, cacheable)
  (embedded glyph  ─▶  parser  ─▶  SQLite / Postgres  ─┬─▶  FastAPI  ─▶  web (static)
   font, visual        (glyph-      (Student,          │     (app/api.py)   (web/, Vercel)
   order Arabic)        decoded)     School, Province) │
                                                       └─▶  Telegram bot (app/bot.py)
                                                             direct SessionLocal — no HTTP hop
```

Results **never change** after ingest, so every read is a single indexed lookup
and every response is cacheable. The API sets `Cache-Control: public,
max-age=3600` and keeps in-process caches, so a CDN (Cloudflare) in front
absorbs the result-announcement-day burst with near-zero origin load.

## Components

| File | Role |
|------|------|
| `app/glyph.py` | Glyph decoder + Arabic normalization (`normalize_ar`) |
| `app/parse_pdf.py` | Parse one school PDF -> student rows |
| `app/ingest.py` | Ingest a folder or `.zip` into the DB (idempotent upsert) |
| `app/db.py` | SQLAlchemy models; SQLite default, Postgres via `DATABASE_URL` |
| `app/api.py` | FastAPI: `/api/provinces`, `/api/schools`, `/api/search`, `POST /api/ingest`; serves `web/` |
| `app/bot.py` | Telegram bot; queries the DB directly (same process, no HTTP) |
| `web/` | Static frontend (province -> school -> search) |

## Quickstart

```bash
pip install -r requirements.txt
```

**Ingest a province** (already done once — 339 schools / ~27k students):

```bash
python -m app.ingest "C:/Users/pc/Desktop/26_واسط" --province "26_واسط"
```

**Run the API + web frontend:**

```bash
python -m uvicorn app.api:app --port 8000
# open http://localhost:8000
```

**Run the Telegram bot:**

```bash
# PowerShell:  $env:TELEGRAM_TOKEN = "123456:ABC..."
# bash:        export TELEGRAM_TOKEN=123456:ABC...
python -m app.bot
```

If `TELEGRAM_TOKEN` is unset/empty the bot prints
`TELEGRAM_TOKEN not set; bot disabled` and exits cleanly (0) — nothing crashes.

**Everything at once with Docker:**

```bash
docker compose up
```

Brings up `api` (port 8000) + `bot` on SQLite with no configuration. Add a
`.env` (from `.env.example`) to enable the bot and/or switch to Postgres.

## The Telegram bot

- `/start` -> welcome + inline keyboard: **🔢 بحث بالرقم الامتحاني** / **🔤 بحث بالاسم**.
- Send a long digit string any time -> auto-detected as an **exam number**; replies with a formatted Arabic result card (name, school/track/province, ناجح/معيد, المعدل, المجموع, subject grades). Null average/total (معيد) is handled gracefully.
- **Name search**: pick a province (or "كل المحافظات"), then send the name. Matching uses `normalize_ar` + AND of `LIKE %token%` on `name_norm`, capped at 20 results; tap a result to see its full card.
- **Admin ingest**: a user whose id is in `ADMIN_IDS` can send a `.zip` document; the bot downloads it and calls `ingest_path`, replying with stats. If `ADMIN_IDS` is unset, bot ingest is disabled.

## Ingesting more provinces

A province is a folder `<code>_<name>/<track>/<code>_<school>.pdf`, or a `.zip`
of that structure. Three ways in:

- **CLI:** `python -m app.ingest "path/or/file.zip" --province "27_بغداد"`
- **API:** `POST /api/ingest` (multipart `file=@province.zip`, optional `province` field) with header `Authorization: Bearer <ADMIN_TOKEN>`
- **Telegram:** send the `.zip` as an admin (see above)

Ingest is idempotent — re-ingesting upserts, never duplicates.

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `DATABASE_URL` | SQLite `data/results.db` | Set to `postgresql+psycopg://user:pass@host/db` for shared production |
| `DATA_DIR` | `./data` | Where SQLite + temp files live |
| `TELEGRAM_TOKEN` | *(empty)* | Bot token from @BotFather. Empty -> bot disabled |
| `ADMIN_IDS` | *(empty)* | Comma-separated Telegram user ids allowed to ingest via bot |
| `ADMIN_TOKEN` | `changeme` | Bearer token for `POST /api/ingest` — change in production |

**Use-defaults-until-filled:** with no `.env`, the system runs on SQLite, the
API serves immediately, and the bot self-disables. Fill env vars only for the
features you want.

## Deployment

A cheap, burst-proof split:

- **Postgres:** [Supabase](https://supabase.com) — set `DATABASE_URL` to its `postgresql+psycopg://...` connection string. Ingest once; results are immutable.
- **API + bot:** [Railway](https://railway.app) (or Fly/Render/any container host) from this repo's `Dockerfile`. Run two services from the same image: API uses the default `CMD`; the bot overrides it with `python -m app.bot`. Set `DATABASE_URL`, `TELEGRAM_TOKEN`, `ADMIN_IDS`, `ADMIN_TOKEN`.
- **Web frontend:** [Vercel](https://vercel.com) serves `web/` statically (`vercel.json` sets `outputDirectory: web`). Point it at the API by defining `window.API_BASE` — add a tiny inline script in `web/index.html`, e.g. `<script>window.API_BASE="https://your-api.up.railway.app"</script>` (the frontend reads `window.API_BASE`, falling back to same-origin).
- **Cloudflare** in front of the API caches the `Cache-Control: public, max-age=3600` GET responses, so result-day traffic is served from edge, not origin.

Locally, `docker compose up` reproduces api + bot; add `--profile pg` to also
start a Postgres container (set `DATABASE_URL` in `.env` accordingly).
