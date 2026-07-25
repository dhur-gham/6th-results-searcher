"""
FastAPI backend for the Iraqi 6th-grade exam-results lookup service.

Data is IMMUTABLE after ingest, so reads are aggressively cached:
  * in-process caches for /api/provinces, /api/schools, and exam_no lookups
  * Cache-Control: public, max-age=3600 on all GET result responses so a CDN
    (Cloudflare) absorbs the announcement-day burst.

Runs with zero required env vars against the existing populated SQLite DB:
    python -m uvicorn app.api:app --port 8000
"""
import os
import time
import shutil
import asyncio
import tempfile
from functools import lru_cache

from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile, File, Form, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, case
from sqlalchemy.orm import Session

try:
    from .db import (init_db, reset_all, SessionLocal, Province, School, Student,
                     load_province_counters, recompute_all_stats, get_data_version)
    from .glyph import normalize_ar
    from .ingest import ingest_path
    from . import analytics
except ImportError:  # allow running as a top-level module too
    from db import (init_db, reset_all, SessionLocal, Province, School, Student,
                    load_province_counters, recompute_all_stats, get_data_version)
    import analytics
    from glyph import normalize_ar
    from ingest import ingest_path

# Cache policy is env-driven:
#   CACHE_MODE=setup (default) -> "no-cache": clients/CDN must revalidate, so a
#       re-ingest shows immediately. Use while loading cities.
#   CACHE_MODE=prod            -> "public, max-age=<CACHE_MAX_AGE>": Cloudflare/CDN
#       caches the immutable results and absorbs the result-day burst.
# Server-side in-process caches (below) apply in BOTH modes.
_CACHE_MODE = os.environ.get("CACHE_MODE", "setup").lower()
_CACHE_MAX_AGE = os.environ.get("CACHE_MAX_AGE", "3600")
if _CACHE_MODE == "prod":
    CACHE_HEADERS = {"Cache-Control": f"public, max-age={_CACHE_MAX_AGE}"}
else:
    CACHE_HEADERS = {"Cache-Control": "no-cache"}
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme")
MAX_RESULTS = 50

app = FastAPI(title="6th-grade Results API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def cached_json(payload):
    return JSONResponse(content=payload, headers=CACHE_HEADERS)


# ---------------------------------------------------------------------------
# In-process caches. Data never changes after ingest, so these are safe for the
# lifetime of the process. Ingest clears them.
# ---------------------------------------------------------------------------
_provinces_cache = None
_schools_cache = {}   # key -> list
_student_cache = {}   # exam_no -> dict | None
_stats_cache = {}     # province_code|"" -> payload


def _clear_caches():
    global _provinces_cache
    _provinces_cache = None
    _schools_cache.clear()
    _student_cache.clear()
    _stats_cache.clear()


# Cross-process cache invalidation. Ingest/reset may happen in ANOTHER process
# (a different gunicorn worker, or the bot container) — it bumps data_version
# in the DB, and each worker polls that stamp at most once per poll interval,
# dropping its in-process caches on change. Fixes permanently stale entries
# (including cached exam_no misses from before a province was uploaded).
_VERSION_POLL_SECONDS = float(os.environ.get("CACHE_VERSION_POLL", "5"))
_data_version = None
_version_checked_at = 0.0


def _maybe_refresh_caches():
    global _data_version, _version_checked_at
    now = time.monotonic()
    if now - _version_checked_at < _VERSION_POLL_SECONDS:
        return
    _version_checked_at = now
    try:
        v = get_data_version()
    except Exception:   # transient DB hiccup: keep serving from caches
        return
    if _data_version is None:
        _data_version = v          # first check after startup: nothing cached yet
    elif v != _data_version:
        _data_version = v
        _clear_caches()


@app.get("/api/provinces")
def provinces(db: Session = Depends(get_db)):
    global _provinces_cache
    _maybe_refresh_caches()
    if _provinces_cache is None:
        counts = dict(
            db.execute(
                select(School.province_code, func.count(School.code)).group_by(School.province_code)
            ).all()
        )
        rows = db.execute(select(Province).order_by(Province.name)).scalars().all()
        _provinces_cache = [
            {"code": p.code, "name": p.name, "school_count": counts.get(p.code, 0)}
            for p in rows
        ]
    return cached_json(_provinces_cache)


@app.get("/api/schools")
def schools(
    province: str = Query(...),
    track: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    key = (province, track or "", (q or "").strip())
    _maybe_refresh_caches()
    if key not in _schools_cache:
        stmt = select(School).where(School.province_code == province)
        if track:
            stmt = stmt.where(School.track == track)
        if q and q.strip():
            stmt = stmt.where(School.name.like(f"%{q.strip()}%"))
        stmt = stmt.order_by(School.name)
        schools_list = db.execute(stmt).scalars().all()
        codes = [s.code for s in schools_list]
        counts = {}
        if codes:
            # school codes repeat across provinces -> scope the count to this province
            counts = dict(
                db.execute(
                    select(Student.school_code, func.count(Student.exam_no))
                    .where(Student.province_code == province)
                    .where(Student.school_code.in_(codes))
                    .group_by(Student.school_code)
                ).all()
            )
        _schools_cache[key] = [
            {
                "code": s.code,
                "name": s.name,
                "track": s.track,
                "province_code": s.province_code,
                "student_count": counts.get(s.code, 0),
            }
            for s in schools_list
        ]
    return cached_json(_schools_cache[key])


@app.get("/api/search")
def search(
    exam_no: str | None = None,
    name: str | None = None,
    province: str | None = None,
    school: str | None = None,
    db: Session = Depends(get_db),
):
    _maybe_refresh_caches()
    if exam_no:
        exam_no = exam_no.strip()
        if exam_no in _student_cache:
            found = _student_cache[exam_no]
        else:
            st = db.get(Student, exam_no)
            found = st.to_dict() if st else None
            _student_cache[exam_no] = found
        if found is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "exam_no": exam_no},
            )
        return cached_json(found)

    if name and name.strip():
        norm = normalize_ar(name)
        tokens = [t for t in norm.split() if t]
        phrase = " ".join(tokens)
        stmt = select(Student)
        for t in tokens:
            stmt = stmt.where(Student.name_norm.like(f"%{t}%"))
        if province:
            stmt = stmt.where(Student.province_code == province)
        if school:
            stmt = stmt.where(Student.school_code == school)
        # Rank the closest matches first: exact typed phrase, then phrase-prefix,
        # then phrase-substring, then the rest — each tier alphabetized.
        rank = case(
            (Student.name_norm == phrase, 0),
            (Student.name_norm.like(f"{phrase}%"), 1),
            (Student.name_norm.like(f"%{phrase}%"), 2),
            else_=3,
        )
        stmt = stmt.order_by(rank, Student.name).limit(MAX_RESULTS)
        rows = db.execute(stmt).scalars().all()
        results = [s.to_dict() for s in rows]
        return cached_json({"count": len(results), "results": results})

    raise HTTPException(status_code=400, detail="provide exam_no or name")


def _analytics_payload(province=None):
    """Build (and cache) analytics. Overall = merge of all province counters;
    per-subject and per-city fall straight out of the stored counters."""
    key = province or ""
    if key in _stats_cache:
        return _stats_cache[key]

    counters = load_province_counters()
    if not counters:
        # backfill once for data ingested before analytics existed
        with SessionLocal() as db:
            has_students = db.execute(select(Student.exam_no).limit(1)).first()
        if has_students:
            recompute_all_stats()
            counters = load_province_counters()

    by_code = {c: (name, cnt) for (c, name, cnt) in counters}

    if province is not None:
        if province not in by_code:
            raise HTTPException(status_code=404, detail="province not found")
        name, cnt = by_code[province]
        payload = {"scope": "province", "code": province, "name": name,
                   **analytics.derive(cnt)}
        _stats_cache[key] = payload
        return payload

    overall = analytics.merge_all([cnt for (_c, _n, cnt) in counters])
    provinces_summary = []
    for (c, name, cnt) in counters:
        d = analytics.derive(cnt)
        provinces_summary.append({
            "code": c, "name": name,
            "students": d["students"], "schools": d["schools"],
            "passed": d["passed"], "failed": d["failed"],
            "pass_rate": d["pass_rate"], "avg_of_averages": d["avg_of_averages"],
        })
    provinces_summary.sort(key=lambda x: x["pass_rate"], reverse=True)
    payload = {
        "scope": "overall",
        "provinces_count": len(counters),
        "overall": analytics.derive(overall),   # includes per-subject overall
        "provinces": provinces_summary,          # per-city summary
    }
    _stats_cache[key] = payload
    return payload


@app.get("/api/stats")
def stats(province: str | None = None):
    """Analytics: overall totals + per-subject + per-province. Pass ?province=CODE
    for one city's full breakdown (including its per-subject rows)."""
    _maybe_refresh_caches()
    return cached_json(_analytics_payload(province))


@app.post("/api/stats/recompute")
def stats_recompute(authorization: str | None = Header(None)):
    """Rebuild analytics from the students table. Admin-only (Bearer ADMIN_TOKEN)."""
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    res = recompute_all_stats()
    _clear_caches()
    return res


@app.post("/api/ingest")
async def ingest(
    file: UploadFile = File(...),
    province: str | None = Form(None),
    authorization: str | None = Header(None),
):
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    suffix = os.path.splitext(file.filename or "upload.zip")[1] or ".zip"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)

    # The upload copy and the whole ingest are blocking work: run them in a
    # worker thread so this worker's event loop keeps serving reads (an ingest
    # used to freeze 1/8 of API capacity for its full duration).
    def _save_and_ingest():
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        return ingest_path(tmp_path, province_label=province)

    try:
        stats = await asyncio.to_thread(_save_and_ingest)
        _clear_caches()
        return stats
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@app.post("/api/reset")
def reset(authorization: str | None = Header(None)):
    """Wipe ALL data (provinces/schools/students) to start a fresh stage.
    Irreversible. Protected by the same Bearer ADMIN_TOKEN as ingest."""
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    counts = reset_all()
    _clear_caches()
    return {"cleared": counts}


# ---------------------------------------------------------------------------
# Static frontend. Mounted last so /api/* routes take priority. If web/ is
# empty/missing we still start cleanly.
# ---------------------------------------------------------------------------
_web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(_web_dir) and os.listdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
