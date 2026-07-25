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
import shutil
import tempfile
from functools import lru_cache

from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile, File, Form, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func
from sqlalchemy.orm import Session

try:
    from .db import init_db, SessionLocal, Province, School, Student
    from .glyph import normalize_ar
    from .ingest import ingest_path
except ImportError:  # allow running as a top-level module too
    from db import init_db, SessionLocal, Province, School, Student
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


def _clear_caches():
    global _provinces_cache
    _provinces_cache = None
    _schools_cache.clear()
    _student_cache.clear()


@app.get("/api/provinces")
def provinces(db: Session = Depends(get_db)):
    global _provinces_cache
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
            counts = dict(
                db.execute(
                    select(Student.school_code, func.count(Student.exam_no))
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
        stmt = select(Student)
        for t in tokens:
            stmt = stmt.where(Student.name_norm.like(f"%{t}%"))
        if province:
            stmt = stmt.join(School, Student.school_code == School.code).where(
                School.province_code == province
            )
        if school:
            stmt = stmt.where(Student.school_code == school)
        stmt = stmt.order_by(Student.name).limit(MAX_RESULTS)
        rows = db.execute(stmt).scalars().all()
        results = [s.to_dict() for s in rows]
        return cached_json({"count": len(results), "results": results})

    raise HTTPException(status_code=400, detail="provide exam_no or name")


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
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        stats = ingest_path(tmp_path, province_label=province)
        _clear_caches()
        return stats
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Static frontend. Mounted last so /api/* routes take priority. If web/ is
# empty/missing we still start cleanly.
# ---------------------------------------------------------------------------
_web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(_web_dir) and os.listdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
