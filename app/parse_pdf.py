"""
Parse an Iraqi 6th-grade result PDF (one school) into structured student rows.

Strategy (all verified against the Wasit dataset):
  * numbers (grades/total/average/exam-no) come clean from page.get_text('words')
  * Arabic (names + result ناجح/معيد + subject headers) is decoded from the
    embedded font via glyph.py
  * cells are assembled into rows by y-coordinate; columns assigned by x-center
    against the decoded header row, so missing columns (e.g. blank اللغات) are OK
"""
import os
import re
import fitz

try:
    from .glyph import build_gid2uni, decode_span, normalize_ar, to_logical
except ImportError:
    from glyph import build_gid2uni, decode_span, normalize_ar, to_logical

RESULT_WORDS = {"ناجح", "معيد", "مكمل", "راسب", "مؤجل"}
ZERO_WORD = "صفر"           # grade written as the word zero
ABSENT_MARKS = {"م", "غ"}   # absent / withheld grade marker
ABSENT = "غ"                # normalized absent value stored in grades
SUBJECTS = [
    "الاسلامية", "العربية", "الانكليزية", "الاحياء",
    "الرياضيات", "الكيمياء", "الفيزياء", "اللغات",
]
# normalized subject keys for matching decoded headers, mapped back to canonical
SUBJECTS_NORM = [normalize_ar(s) for s in SUBJECTS]
NORM2CANON = {normalize_ar(s): s for s in SUBJECTS}

EXAMNO_RE = re.compile(r"^\d{12,}$")
NUM_RE = re.compile(r"^\d+(\.\d+)?$")
ARABIC_RE = re.compile(r"[؀-ۿ]")


def _yc(b):  # y-center
    return (b[1] + b[3]) / 2


def _xc(b):
    return (b[0] + b[2]) / 2


def _arabic_tokens(page, gid2uni):
    """Return decoded Arabic tokens: list of dict(text, x, y, x0, x1)."""
    toks = []
    for span in page.get_texttrace():
        if "Arial" not in span.get("font", ""):
            continue
        # split span into words by gaps in x between consecutive glyphs
        chars = span["chars"]
        if not chars:
            continue
        decoded = decode_span(chars, gid2uni)
        # rebuild word groups using space glyphs (gid maps to space) — simpler:
        # decode whole span, split on spaces, approximate positions by bbox
        logical = to_logical(decoded)
        text = normalize_ar(logical).strip()
        if not ARABIC_RE.search(text):
            continue
        xs = [c[3][0] for c in chars] + [c[3][2] for c in chars]
        ys = [c[3][1] for c in chars] + [c[3][3] for c in chars]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        for w in text.split():
            toks.append({"text": w, "x": _xc(bbox), "y": _yc(bbox),
                          "x0": bbox[0], "x1": bbox[2]})
    return toks


def _numbers(page):
    out = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        txt = txt.strip()
        if NUM_RE.match(txt):
            out.append({"text": txt, "x": (x0 + x1) / 2, "y": (y0 + y1) / 2,
                        "x0": x0, "x1": x1})
    return out


def _cluster_rows(items, tol=4.0):
    rows = []
    for it in sorted(items, key=lambda d: d["y"]):
        for r in rows:
            if abs(r["y"] - it["y"]) <= tol:
                r["items"].append(it)
                r["y"] = (r["y"] * r["n"] + it["y"]) / (r["n"] + 1)
                r["n"] += 1
                break
        else:
            rows.append({"y": it["y"], "n": 1, "items": [it]})
    return rows


def _header_columns(rows):
    """Find subject-header x-centers: {subject_norm: x_center}."""
    for row in rows:
        hits = {}
        for i in row["items"]:
            for subj in SUBJECTS_NORM:
                if subj in i["text"]:
                    hits[subj] = i["x"]
        if len(hits) >= 4:  # a real header row
            return hits
    return {}


def parse_page(page, gid2uni, columns):
    ar = _arabic_tokens(page, gid2uni)
    nums = _numbers(page)
    rows = _cluster_rows(ar + nums)
    if not columns:
        columns = _header_columns(rows)

    students = []
    for row in rows:
        items = row["items"]
        exam = next((i for i in items if EXAMNO_RE.match(i["text"])), None)
        if not exam:
            continue  # not a student data row
        examno = exam["text"]
        exam_x = exam["x"]
        res = next((i for i in items
                    if any(rw in i["text"] for rw in RESULT_WORDS)), None)
        result = res["text"] if res else None
        result_x = res["x"] if res else exam_x - 200

        # classify Arabic tokens: some grade cells are WORDS not digits
        #   "صفر" = zero (0), "م"/"غ" = absent marker. These must NOT join the name.
        name_toks = []
        word_cells = []  # grade cells written as words: {"x", "value"}
        for i in items:
            if not ARABIC_RE.search(i["text"]) or i is res:
                continue
            t = i["text"]
            if t == ZERO_WORD:
                word_cells.append({"x": i["x"], "value": 0})
            elif t in ABSENT_MARKS:
                word_cells.append({"x": i["x"], "value": ABSENT})
            elif i["x"] < exam_x:
                name_toks.append(i)
        name = " ".join(t["text"] for t in sorted(name_toks, key=lambda t: -t["x"]))

        ints = [i for i in items if NUM_RE.match(i["text"]) and "." not in i["text"]
                and i["text"] != examno]
        decs = [i for i in items if NUM_RE.match(i["text"]) and "." in i["text"]]

        # column regions by x anchors:
        #   seq: x > exam_x   |   subjects: result_x < x < exam_x   |   total/avg: x < result_x
        subject_cells = [{"x": i["x"], "value": int(i["text"])}
                         for i in ints if result_x < i["x"] < exam_x]
        subject_cells += [w for w in word_cells if result_x < w["x"] < exam_x]
        # المعدل / المجموع are the two columns left of the result column.
        # المعدل is leftmost (smallest x), المجموع next. Both can be integer 0
        # for معيد students (the PDF writes 0, not blank), so use position not
        # "has a decimal" to tell them apart.
        left_cells = [i for i in (ints + decs) if i["x"] <= result_x]
        left_cells.sort(key=lambda i: i["x"])
        average = float(left_cells[0]["text"]) if left_cells else None
        total = int(float(left_cells[-1]["text"])) if len(left_cells) > 1 else None

        # map subject cells to nearest header column
        grades = {}
        ordered = sorted(subject_cells, key=lambda c: -c["x"])  # RTL: first subject rightmost
        if columns:
            for cell in subject_cells:
                subj = min(columns, key=lambda s: abs(columns[s] - cell["x"]))
                grades[NORM2CANON.get(subj, subj)] = cell["value"]
        else:
            for idx, cell in enumerate(ordered):
                if idx < len(SUBJECTS):
                    grades[SUBJECTS[idx]] = cell["value"]

        # Fallback average: if the PDF's المعدل is missing/0/≤1 but the student
        # actually has graded subjects, compute it = sum(grades) / subject count
        # (absent "غ" counts as 0). Keeps المجموع consistent (= the sum).
        numeric = [v for v in grades.values() if isinstance(v, (int, float))]
        if (average is None or average <= 1) and any(v > 0 for v in numeric):
            subj_count = len(grades)
            grade_sum = sum(numeric)  # غ/absent contribute 0
            if subj_count:
                average = round(grade_sum / subj_count, 2)
                total = grade_sum

        students.append({
            "exam_no": examno,
            "name": name,
            "result": result,
            "average": average,
            "total": total,
            "grades": grades,
        })
    return students, columns


def parse_pdf(path):
    doc = fitz.open(path)
    gid2uni = build_gid2uni(doc)
    if not gid2uni:
        raise RuntimeError(f"no embedded font cmap found in {path}")
    students = []
    columns = {}
    for page in doc:
        page_students, columns = parse_page(page, gid2uni, columns)
        students.extend(page_students)
    doc.close()
    # dedup by exam_no (headers repeat across pages shouldn't create dupes)
    seen = {}
    for s in students:
        seen[s["exam_no"]] = s
    return list(seen.values())


if __name__ == "__main__":
    import sys, json
    res = parse_pdf(sys.argv[1])
    print(json.dumps(res[:8], ensure_ascii=False, indent=2))
    print(f"... total {len(res)} students")
