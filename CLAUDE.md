# CLAUDE.md — working guide for AI agents in this repo

Read this first. It captures the domain knowledge and gotchas that are NOT obvious
from the code. User-facing docs: `README.md` (usage) and `deploy/DEPLOY.md` (VPS).

## What this app is

Fast lookup of Iraqi Ministry of Education **6th-grade (السادس الإعدادي) exam results**.
The ministry publishes results as one **ZIP per province**, containing one **PDF per
school**, each a table of students + per-subject grades. Students search by **exam
number** or **Arabic name**; they pick province → school (optional) → search.

Flow: `PDF → parser → database → API → {web page, Telegram bot}`.

## Repo map

```
app/
  glyph.py       Arabic glyph decoding (the hard part — see below). DO NOT casually edit.
  parse_pdf.py   PDF -> student rows. Coordinate-based table extraction.
  db.py          SQLAlchemy models. SQLite default, Postgres via DATABASE_URL.
  ingest.py      province folder/zip/rar -> DB (idempotent upsert; parallel PDF parse
                 + bulk upsert). CLI: python -m app.ingest
  analytics.py   Pure aggregation: per-province counters -> merged overall +
                 per-subject/per-city metrics. Counters stored raw so they merge exactly.
  api.py         FastAPI: /api/provinces, /api/schools, /api/search, /api/stats,
                 POST /api/ingest, POST /api/reset (wipe-all for a new stage). Serves web/.
  bot.py         Telegram bot. Queries DB directly. Self-disables without TELEGRAM_TOKEN.
                 Admin ingest runs as a background job; optional local Bot API server.
web/             Static RTL Arabic SPA (index.html + styles.css + app.js). No build step.
deploy/          Caddyfile, backup.sh, DEPLOY.md (Contabo VPS + Cloudflare).
docker-compose.yml        local (SQLite)
docker-compose.local.yml  local (SQLite) + local Bot API server for big uploads
docker-compose.prod.yml   production (Postgres + API + bot + Bot API server + Caddy + backup)
data/            SQLite db + temp (gitignored)
```

## The #1 thing to understand: Arabic decoding

The PDFs embed a **subsetted Identity-H font** (`ABCDEE+Arial,Bold`). Normal text
extraction (`page.get_text()`) returns **Greek/Coptic mojibake** for Arabic. The fix,
already implemented in `glyph.py`:

1. Read the PDF's **own embedded font cmap** (via `fontTools`), invert glyph-id → unicode.
2. Get glyph ids from `page.get_texttrace()` (NOT `get_text`).
3. `NFKC`-normalize (presentation forms → base letters), then **reverse** (visual → logical order).

This is self-adapting per PDF — no hardcoded table. **Numbers extract fine** from
`get_text("words")`; only Arabic needs the glyph path. Verified: 339 PDFs / 27,179
students / 13.6s / 0 failures.

## Parser rules / gotchas (parse_pdf.py)

- Table columns are found by **coordinate** (y-clustering into rows; x-position for
  columns), anchored on the exam-number column and the result column.
- **Some grade cells are WORDS, not digits:** `صفر` = the number 0; `م`/`غ` = absent.
  These are Arabic, so they MUST be routed into grades — never into the name. (This
  was a real bug: 17k names got `صفر`/`م` glued on.) See `ZERO_WORD`, `ABSENT_MARKS`.
- **المعدل / المجموع** are the two columns left of the result column. For معيد
  (repeat) students the PDF writes them as integer `0`, so detect them by position,
  not "has a decimal".
- **Computed-average fallback:** if المعدل is missing/`0`/≤1 but the student has any
  subject > 0, المعدل = sum(grades)/subject_count (absent `غ` counts as 0), and المجموع
  is set to that sum. (Product rule requested by the owner.)
- `grades` values are `int` or the string `"غ"` (absent). Frontend colors: ≥50 green,
  <50 red, `غ` gray.

## Data model facts that drive everything

- **Data is write-once / immutable** after ingest. Results never change. This is why
  aggressive caching + trivial horizontal scaling work.
- Dataset is tiny: whole country ≈ 300–500k rows, **< 1 GB, fits in RAM**.
- `exam_no` is the primary key and globally unique → exam-number search is an O(1)
  indexed lookup and sidesteps Arabic entirely. Prefer it.
- **School codes are only unique WITHIN a province** (e.g. 55051 "externals"
  repeats in every province). So `School`'s PK is composite `(province_code, code)`,
  and `Student` carries `province_code` + `school_code` (composite FK). Always scope
  school lookups/counts by province — never join/filter on `school_code` alone.
- `name_norm` (normalized via `glyph.normalize_ar`) is the indexed search column.
  Always normalize a name query the SAME way before matching.

## How to run / common tasks

Windows note: this dev machine's console is cp1252 — **always** prefix Python that
prints Arabic with `PYTHONIOENCODING=utf-8` and run `python -X utf8`, or you get
`UnicodeEncodeError`. (Writing Arabic to files is fine; only stdout breaks.)

- **Run locally:** `python -m uvicorn app.api:app --port 8012` → http://localhost:8012
- **Add a city:** `python -m app.ingest "path/to/26_واسط"` (folder or .zip; idempotent).
- **Modify parsing:** edit `parse_pdf.py`/`glyph.py`, then re-ingest to update the DB.
- **Deploy:** see `deploy/DEPLOY.md` (docker-compose.prod.yml on a VPS).

## Operational gotchas (hit this session — don't repeat)

- **SQLite lock:** WAL + `busy_timeout=60000` (see `db.py`) let a writer wait up to
  60s for the lock, so a re-ingest while uvicorn is running usually just blocks briefly
  instead of failing. A long ingest can still time out under heavy read load — if you
  hit "database is locked", stop the server → ingest → start it. (Postgres has no such
  issue, and can run `INGEST_CONCURRENCY` > 1.)
- **Port already in use:** starting uvicorn on a port that's still bound silently
  fails; the OLD process keeps answering with STALE data/cache. Kill all uvicorn
  first (`pkill -f "uvicorn app.api"`, or on Windows kill the `python.exe` whose
  CommandLine matches `uvicorn`), confirm the port is free, THEN start.
- **Stale results after a fix are usually a CACHE, not a bug.** Two layers:
  (1) the API's in-process dict cache — restart the server to clear;
  (2) the browser/CDN — `CACHE_MODE=setup` sends `no-cache` so re-ingests show
  immediately; `CACHE_MODE=prod` sends `max-age=3600` for Cloudflare. When a user
  reports "still showing old value", verify the DB + a fresh API call FIRST before
  assuming the parser is wrong.

## Scaling / caching (why 50k concurrent is easy)

Read-only + immutable + tiny dataset. Three cache layers: in-process (RAM, always
on), browser (via header), CDN (Cloudflare, production only — absorbs the result-day
burst). Scale the stateless API horizontally behind a load balancer; Postgres does
thousands of indexed reads/sec and is barely touched. See README "Scaling".

## Conventions

- Keep the parser (`glyph.py`, `parse_pdf.py`) changes verified against the real
  `26_واسط` dataset before committing — run a full-folder parse and check
  `students`, `errors`, and contaminated-name counts.
- Don't add heavy deps; pymupdf + fonttools + sqlalchemy + fastapi is the whole stack.
- The web frontend is intentionally build-free vanilla JS — keep it that way.
- Everything must run with SANE DEFAULTS (no env) locally: SQLite, bot disabled.
