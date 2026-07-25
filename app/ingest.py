"""
Ingest a province into the database.

Accepts either:
  * a directory:  <province>/<track>/<code>_<schoolname>.pdf
  * a .zip or .rar of that structure

Province is taken from --province "26_واسط" (code_name) or the top folder name.
Idempotent: re-ingesting upserts, never duplicates.

Usage:
  python -m app.ingest "C:/path/26_واسط"
  python -m app.ingest results.zip --province "26_واسط"
"""
import os
import re
import sys
import json
import zipfile
import tempfile
import argparse

try:
    from .parse_pdf import parse_pdf
    from .glyph import normalize_ar
    from .db import (init_db, SessionLocal, Province, School, Student,
                     save_province_stats, bump_data_version)
    from . import analytics
except ImportError:
    from parse_pdf import parse_pdf
    from glyph import normalize_ar
    from db import (init_db, SessionLocal, Province, School, Student,
                    save_province_stats, bump_data_version)
    import analytics

SCHOOL_RE = re.compile(r"^(\d+)[_\-\s]+(.+?)\.pdf$", re.IGNORECASE)
PROV_RE = re.compile(r"^(\d+)[_\-\s]+(.+)$")
KNOWN_TRACKS = {"علمي", "أدبي", "ادبي", "فنون", "تطبيقي", "احيائي"}


def parse_province(label: str):
    label = os.path.basename(label.rstrip("/\\"))
    m = PROV_RE.match(label)
    if m:
        return m.group(1), m.group(2).strip()
    return label, label


def is_archive(path):
    return path.lower().endswith((".zip", ".rar"))


# Common Windows install locations for a RAR backend (rarfile needs one of these
# when they aren't on PATH). Linux/Docker uses `unar` installed via the Dockerfile.
_WIN_UNRAR = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
]
_WIN_7ZIP = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]


def _which_any(names):
    import shutil
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _extract_rar(path, out):
    """Extract a .rar by calling a real extractor directly (rarfile's backend
    auto-detection is unreliable in containers). Tries several tools in order."""
    import subprocess
    candidates = []
    bsdtar = _which_any(["bsdtar"])
    if bsdtar:
        candidates.append([bsdtar, "-xf", path, "-C", out])
    unar = _which_any(["unar"])
    if unar:
        candidates.append([unar, "-force-overwrite", "-quiet",
                            "-output-directory", out, path])
    unrar = _which_any(["unrar", "unrar-free"]) or \
        next((p for p in _WIN_UNRAR if os.path.isfile(p)), None)
    if unrar:
        candidates.append([unrar, "x", "-y", "-idq", path, out + os.sep])
    sevenz = _which_any(["7z", "7za"]) or \
        next((p for p in _WIN_7ZIP if os.path.isfile(p)), None)
    if sevenz:
        candidates.append([sevenz, "x", "-y", "-o" + out, path])

    if not candidates:
        raise RuntimeError(
            "no RAR extractor found. Linux: `apt install libarchive-tools unar`; "
            "Windows: install WinRAR or 7-Zip.")
    last = ""
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
            # success = tool returned 0 AND produced at least one extracted file
            if r.returncode == 0 and any(os.scandir(out)):
                return
            last = (r.stderr or b"").decode("utf-8", "replace")[:300] or f"exit {r.returncode}"
        except Exception as e:  # tool missing/execution error -> try next
            last = str(e)
    raise RuntimeError(f"RAR extraction failed ({last}).")


def _extract_archive(path):
    tmp = tempfile.mkdtemp(prefix="ingest_")
    if path.lower().endswith(".rar"):
        _extract_rar(path, tmp)
    else:
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
    # if the archive contains a single top folder, descend into it
    entries = [os.path.join(tmp, e) for e in os.listdir(tmp)]
    dirs = [e for e in entries if os.path.isdir(e)]
    if len(dirs) == 1 and not any(os.path.isfile(e) for e in entries):
        return dirs[0], tmp
    return tmp, tmp


def iter_school_pdfs(root):
    """Yield (pdf_path, track). Track = immediate parent folder name if known."""
    for dirpath, _dirs, files in os.walk(root):
        track = os.path.basename(dirpath)
        if track not in KNOWN_TRACKS:
            track = ""
        for f in files:
            if f.lower().endswith(".pdf"):
                yield os.path.join(dirpath, f), (track or _guess_track(dirpath))


def _guess_track(dirpath):
    for part in dirpath.replace("\\", "/").split("/"):
        if part in KNOWN_TRACKS:
            return part
    return "غير محدد"


def _bulk_upsert(db, model, rows, index_elements, update_cols, chunk=500):
    """Batched INSERT ... ON CONFLICT DO UPDATE. Works on SQLite and Postgres."""
    if not rows:
        return
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _insert
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        stmt = _insert(model)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={c: getattr(stmt.excluded, c) for c in update_cols},
        )
        db.execute(stmt, batch)


def _parse_school(job):
    """Worker: parse one school PDF. Runs in a separate process (CPU-bound)."""
    pdf_path, track = job
    fname = os.path.basename(pdf_path)
    m = SCHOOL_RE.match(fname)
    scode = m.group(1) if m else os.path.splitext(fname)[0]
    sname = m.group(2).strip() if m else fname
    try:
        rows = parse_pdf(pdf_path)
        # Per-student CPU work (name normalization + grades JSON) happens
        # here in the parallel worker, not in the serial consumer — it was
        # a measurable single-threaded tail on large provinces.
        for r in rows:
            r["name_norm"] = normalize_ar(r["name"])
            r["grades_json"] = json.dumps(r["grades"], ensure_ascii=False)
        return (scode, sname, track, rows, None)
    except Exception as e:  # pragma: no cover - defensive
        return (scode, sname, track, None, f"{fname}: {e}")


def ingest_path(path, province_label=None, progress=None, workers=None):
    """Ingest a province. PDF parsing is parallelized across CPU cores; DB writes
    stay serial in this process (safe for SQLite; fast bulk upsert)."""
    init_db()
    cleanup = None
    if is_archive(path):
        root, cleanup = _extract_archive(path)
        province_label = province_label or os.path.splitext(os.path.basename(path))[0]
    else:
        root = path
        province_label = province_label or path

    pcode, pname = parse_province(province_label)
    stats = {"schools": 0, "students": 0, "errors": []}
    jobs = list(iter_school_pdfs(root))

    if workers is None:
        workers = int(os.environ.get("INGEST_WORKERS", "0")) or min(os.cpu_count() or 2, 8)

    school_rows = {}   # scode -> dict
    student_rows = []  # list of dicts
    counters = analytics.blank()   # analytics accumulated student-by-student

    def apply_result(res):
        scode, sname, track, rows, err = res
        if err:
            stats["errors"].append(err)
            return
        school_rows[scode] = {"code": scode, "name": sname, "track": track,
                              "province_code": pcode}
        for r in rows:
            analytics.accumulate(counters, r["result"], r["average"], r["grades"])
            student_rows.append({
                "exam_no": r["exam_no"],
                "name": r["name"],
                "name_norm": r["name_norm"],
                "result": r["result"],
                "total": r["total"],
                "average": r["average"],
                "grades_json": r["grades_json"],
                "province_code": pcode,
                "school_code": scode,
            })
        stats["schools"] += 1
        stats["students"] += len(rows)
        if progress:
            progress(sname, len(rows))

    # ---- Parse phase: NO session/transaction open. (A session here used to
    # hold SQLite's write lock for the entire multi-minute parse, which is what
    # made concurrent ingests die with "database is locked".)
    executor = None
    if workers > 1 and len(jobs) > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            executor = ProcessPoolExecutor(max_workers=workers)
        except Exception:  # pool unavailable on this platform -> sequential
            executor = None
    if executor is not None:
        # A mid-iteration pool failure (e.g. BrokenProcessPool) propagates:
        # falling back here would re-apply results already accumulated and
        # double-count analytics / duplicate exam_nos in the upsert batches.
        with executor as ex:
            for res in ex.map(_parse_school, jobs, chunksize=4):
                apply_result(res)
    else:
        for job in jobs:
            apply_result(_parse_school(job))

    # ---- Write phase: one short transaction. Province is upserted like the
    # rest (get-then-insert raced when two jobs ingested the same new province).
    with SessionLocal() as db:
        _bulk_upsert(db, Province, [{"code": pcode, "name": pname}],
                     ["code"], ["name"])
        # Bulk upsert (one batched statement per chunk) instead of per-row I/O.
        _bulk_upsert(db, School, list(school_rows.values()),
                     ["province_code", "code"], ["name", "track"])
        _bulk_upsert(db, Student, student_rows, ["exam_no"],
                     ["name", "name_norm", "result", "total", "average",
                      "grades_json", "province_code", "school_code"])
        db.commit()

    # Persist this province's analytics counters (overall is merged on read).
    counters["schools"] = len(school_rows)
    save_province_stats(pcode, pname, counters)
    bump_data_version()   # tell every process to drop its in-process caches

    if cleanup:
        import shutil
        shutil.rmtree(cleanup, ignore_errors=True)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="province folder or .zip")
    ap.add_argument("--province", help='province label e.g. "26_واسط"')
    args = ap.parse_args()
    stats = ingest_path(args.path, args.province,
                        progress=lambda f, n: print(f"  {f}: {n}"))
    print(f"\nDone. schools={stats['schools']} students={stats['students']} "
          f"errors={len(stats['errors'])}")
    for e in stats["errors"][:10]:
        print("  ERR", e)


if __name__ == "__main__":
    main()
