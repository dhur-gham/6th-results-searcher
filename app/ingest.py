"""
Ingest a province into the database.

Accepts either:
  * a directory:  <province>/<track>/<code>_<schoolname>.pdf
  * a .zip of that structure

Province is taken from --province "26_واسط" (code_name) or the top folder name.
Idempotent: re-ingesting upserts, never duplicates.

Usage:
  python -m app.ingest "C:/path/26_واسط"
  python -m app.ingest results.zip --province "26_واسط"
"""
import os
import re
import sys
import zipfile
import tempfile
import argparse

try:
    from .parse_pdf import parse_pdf
    from .glyph import normalize_ar
    from .db import init_db, SessionLocal, Province, School, Student
except ImportError:
    from parse_pdf import parse_pdf
    from glyph import normalize_ar
    from db import init_db, SessionLocal, Province, School, Student

SCHOOL_RE = re.compile(r"^(\d+)[_\-\s]+(.+?)\.pdf$", re.IGNORECASE)
PROV_RE = re.compile(r"^(\d+)[_\-\s]+(.+)$")
KNOWN_TRACKS = {"علمي", "أدبي", "ادبي", "فنون", "تطبيقي", "احيائي"}


def parse_province(label: str):
    label = os.path.basename(label.rstrip("/\\"))
    m = PROV_RE.match(label)
    if m:
        return m.group(1), m.group(2).strip()
    return label, label


def _extract_zip(path):
    tmp = tempfile.mkdtemp(prefix="ingest_")
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp)
    # if the zip contains a single top folder, descend into it
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


def ingest_path(path, province_label=None, progress=None):
    init_db()
    cleanup = None
    if path.lower().endswith(".zip"):
        root, cleanup = _extract_zip(path)
        province_label = province_label or os.path.splitext(os.path.basename(path))[0]
    else:
        root = path
        province_label = province_label or path

    pcode, pname = parse_province(province_label)
    stats = {"schools": 0, "students": 0, "errors": []}

    with SessionLocal() as db:
        prov = db.get(Province, pcode)
        if not prov:
            prov = Province(code=pcode, name=pname)
            db.add(prov)
            db.flush()

        for pdf_path, track in iter_school_pdfs(root):
            fname = os.path.basename(pdf_path)
            m = SCHOOL_RE.match(fname)
            scode = m.group(1) if m else os.path.splitext(fname)[0]
            sname = m.group(2).strip() if m else fname
            try:
                rows = parse_pdf(pdf_path)
            except Exception as e:
                stats["errors"].append(f"{fname}: {e}")
                continue

            school = db.get(School, scode)
            if not school:
                school = School(code=scode, name=sname, track=track, province_code=pcode)
                db.add(school)
            else:
                school.name, school.track, school.province_code = sname, track, pcode
            db.flush()

            for r in rows:
                st = db.get(Student, r["exam_no"])
                if not st:
                    st = Student(exam_no=r["exam_no"])
                    db.add(st)
                st.name = r["name"]
                st.name_norm = normalize_ar(r["name"])
                st.result = r["result"]
                st.total = r["total"]
                st.average = r["average"]
                import json as _json
                st.grades_json = _json.dumps(r["grades"], ensure_ascii=False)
                st.school_code = scode
            stats["schools"] += 1
            stats["students"] += len(rows)
            if progress:
                progress(fname, len(rows))
        db.commit()

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
