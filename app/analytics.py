"""
Results analytics — pure, dependency-free aggregation.

Design: we keep raw *counters* (sums + counts) per province, never averages, so
that provinces merge into an exact overall total with no precision loss. Derived
percentages/averages are computed at read time from the counters.

Counter shape (JSON-serializable):
    {
      "students": int,          # rows in scope
      "passed":   int,          # result == ناجح/ناجحة
      "schools":  int,          # distinct schools in scope
      "avg_sum":  float,        # sum of non-null student averages
      "avg_count": int,         # how many had a non-null average
      "subjects": {             # per-subject counters
         "<subject>": {"num": int, "pass": int, "absent": int, "sum": int}
      }
    }
  - num    = numeric grades seen (denominator for the subject average / pass rate)
  - pass   = numeric grades >= 50
  - absent = "غ" (or any non-numeric) cells
  - sum    = sum of the numeric grades
"""

PASS_WORDS = {"ناجح", "ناجحة"}
PASS_MARK = 50  # >= 50 is a passing subject grade (mirrors the frontend colors)


def blank():
    return {"students": 0, "passed": 0, "schools": 0,
            "avg_sum": 0.0, "avg_count": 0, "subjects": {}}


def accumulate(c, result, average, grades):
    """Fold one student into counters `c`."""
    c["students"] += 1
    if (result or "").strip() in PASS_WORDS:
        c["passed"] += 1
    if isinstance(average, (int, float)):
        c["avg_sum"] += float(average)
        c["avg_count"] += 1
    subs = c["subjects"]
    for subj, g in (grades or {}).items():
        s = subs.get(subj)
        if s is None:
            s = subs[subj] = {"num": 0, "pass": 0, "absent": 0, "sum": 0}
        if isinstance(g, (int, float)):
            s["num"] += 1
            s["sum"] += int(g)
            if g >= PASS_MARK:
                s["pass"] += 1
        else:
            s["absent"] += 1   # "غ" and any non-numeric marker


def merge(a, b):
    """Combine two counters into a new one (for overall = sum of provinces)."""
    out = blank()
    for k in ("students", "passed", "schools", "avg_count"):
        out[k] = a[k] + b[k]
    out["avg_sum"] = a["avg_sum"] + b["avg_sum"]
    subs = out["subjects"]
    for src in (a["subjects"], b["subjects"]):
        for subj, s in src.items():
            d = subs.get(subj)
            if d is None:
                d = subs[subj] = {"num": 0, "pass": 0, "absent": 0, "sum": 0}
            for k in ("num", "pass", "absent", "sum"):
                d[k] += s[k]
    return out


def merge_all(counter_list):
    out = blank()
    for c in counter_list:
        out = merge(out, c)
    return out


def _pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


def derive(c):
    """Counters -> presentation metrics (percentages, averages, subject rows)."""
    students = c["students"]
    passed = c["passed"]
    subjects = {}
    for subj, s in c["subjects"].items():
        num = s["num"]
        subjects[subj] = {
            "students": num + s["absent"],
            "graded": num,
            "absent": s["absent"],
            "pass": s["pass"],
            "fail": num - s["pass"],
            "avg": round(s["sum"] / num, 2) if num else None,
            "pass_rate": _pct(s["pass"], num),
        }
    # subjects sorted by pass_rate desc for convenient display
    subjects = dict(sorted(subjects.items(),
                           key=lambda kv: kv[1]["pass_rate"], reverse=True))
    return {
        "students": students,
        "schools": c["schools"],
        "passed": passed,
        "failed": students - passed,
        "pass_rate": _pct(passed, students),
        "avg_of_averages": round(c["avg_sum"] / c["avg_count"], 2) if c["avg_count"] else None,
        "subjects": subjects,
    }
