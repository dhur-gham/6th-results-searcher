"""
Telegram bot for the Iraqi 6th-grade exam-results lookup service.

Design goals:
  * Runs in the SAME process as the DB (no HTTP hop): queries SessionLocal
    directly, reusing the exact search logic style of app/api.py.
  * NEVER crashes when unconfigured. If TELEGRAM_TOKEN is unset/empty the bot
    prints a clear message and exits cleanly (0), so `docker compose up` with no
    .env still brings the whole system up.

Env:
  TELEGRAM_TOKEN   bot token from @BotFather. Unset/empty  -> bot disabled.
  ADMIN_IDS        comma-separated Telegram user ids allowed to ingest .zip
                   uploads. Unset/empty -> ingest via bot disabled.

Run:
  python -m app.bot
"""
import os
import sys
import time
import asyncio
import logging

try:
    from .db import (init_db, reset_all, SessionLocal, Province, School, Student,
                     load_province_counters, get_data_version)
    from .glyph import normalize_ar
    from .ingest import ingest_path
    from . import analytics
except ImportError:  # allow running as a top-level module too
    from db import (init_db, reset_all, SessionLocal, Province, School, Student,
                    load_province_counters, get_data_version)
    from glyph import normalize_ar
    from ingest import ingest_path
    import analytics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

_INGEST_SEM = None


def _ingest_semaphore():
    """Lazily create the ingest concurrency gate (bound to the running loop).

    Default is 1: each job already parallelizes across all cores with its own
    process pool, so running jobs side by side just oversubscribes the CPU
    (3 jobs x 8 workers = 24 processes on a 4-vCPU box) — sequential jobs
    finish a multi-province batch FASTER. Raise INGEST_CONCURRENCY only on
    hosts with many spare cores."""
    global _INGEST_SEM
    if _INGEST_SEM is None:
        n = max(1, int(os.environ.get("INGEST_CONCURRENCY", "1")))
        _INGEST_SEM = asyncio.Semaphore(n)
    return _INGEST_SEM


async def _ingest_job(bot, chat_id, target, province_label, tmp_path):
    """Background ingest job: runs concurrently with other jobs (bounded by the
    semaphore), does the CPU/DB work off the event loop, reports when done.

    While parsing, a status message is edited in place (rate-limited) so the
    admin can tell a slow ingest from a hung one."""
    sem = _ingest_semaphore()
    try:
        async with sem:
            t0 = time.monotonic()

            status_msg = None
            try:
                status_msg = await bot.send_message(
                    chat_id, f"⏳ «{province_label}» بدأت المعالجة...")
            except Exception:   # progress is best-effort, never fail the job
                pass

            loop = asyncio.get_running_loop()
            done = {"schools": 0, "students": 0}
            last_edit = [0.0]

            def _progress(_school_name, n_students):
                # Called from the ingest worker thread for every parsed school.
                done["schools"] += 1
                done["students"] += n_students
                now = time.monotonic()
                if status_msg is None or now - last_edit[0] < 5:
                    return
                last_edit[0] = now
                text = (f"⏳ «{province_label}»\n"
                        f"المدارس: {done['schools']} — الطلاب: {done['students']}")

                async def _edit():
                    try:
                        await status_msg.edit_text(text)
                    except Exception:   # rate-limited / message unchanged
                        pass

                asyncio.run_coroutine_threadsafe(_edit(), loop)

            stats = await asyncio.to_thread(
                ingest_path, target, province_label, _progress)
            dt = time.monotonic() - t0
        _clear_query_caches()  # new data -> drop cached lookups/counts
        await bot.send_message(
            chat_id,
            f"✅ «{province_label}»\n"
            f"المدارس: {stats.get('schools', 0)}\n"
            f"الطلاب: {stats.get('students', 0)}\n"
            f"الأخطاء: {len(stats.get('errors', []))}\n"
            f"⏱ {dt:.1f} ثانية")
    except Exception as e:  # pragma: no cover - defensive
        log.exception("ingest job failed")
        await bot.send_message(chat_id, f"❌ فشل الاستيراد «{province_label}»: {e}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

# ---------------------------------------------------------------------------
# DB query helpers — same logic style as app/api.py, but direct (no HTTP).
# ---------------------------------------------------------------------------
try:
    from sqlalchemy import select, case, func
except ImportError:  # pragma: no cover - sqlalchemy is a hard dep of db.py
    select = case = func = None

PAGE_SIZE = 8   # results shown per page in name-search (paginated, not capped)

# In-process query caches. Results are immutable after ingest, so entries are
# valid for the process lifetime; _clear_query_caches() runs after ingest/reset.
# Bounded: when a cache hits the cap it is simply cleared (dataset is small
# enough that refilling is cheap, and this keeps the code trivial).
_CACHE_CAP = 200_000
_exam_cache = {}    # exam_no -> dict | None (negative lookups cached too)
_count_cache = {}   # (normalized phrase, province) -> total match count


def _cache_put(cache: dict, key, value):
    if len(cache) >= _CACHE_CAP:
        cache.clear()
    cache[key] = value


def _clear_query_caches():
    _exam_cache.clear()
    _count_cache.clear()


# Cross-process cache invalidation: an ingest via the API container bumps
# data_version in the DB; poll it (at most every few seconds) so this process
# drops stale entries too — mirror of the same mechanism in app/api.py.
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
        _data_version = v
    elif v != _data_version:
        _data_version = v
        _clear_query_caches()


def lookup_exam_no(exam_no: str):
    """Exact primary-key lookup. Returns Student.to_dict() or None."""
    exam_no = (exam_no or "").strip()
    if not exam_no:
        return None
    _maybe_refresh_caches()
    if exam_no in _exam_cache:
        return _exam_cache[exam_no]
    with SessionLocal() as db:
        st = db.get(Student, exam_no)
        found = st.to_dict() if st else None
    _cache_put(_exam_cache, exam_no, found)
    return found


def search_by_name(name: str, province: str | None = None,
                   offset: int = 0, limit: int = PAGE_SIZE):
    """normalize_ar + AND of LIKE %token% on name_norm, RANKED so the closest
    matches come first (whole typed phrase before scattered tokens), and
    PAGINATED (offset/limit) instead of hard-capped.

    Returns (results, total) — total is the full match count across all pages.
    """
    norm = normalize_ar(name or "")
    tokens = [t for t in norm.split() if t]
    if not tokens:
        return [], 0
    _maybe_refresh_caches()
    phrase = " ".join(tokens)
    count_key = (phrase, province or "")
    with SessionLocal() as db:
        base = select(Student)
        for t in tokens:
            base = base.where(Student.name_norm.like(f"%{t}%"))
        if province:
            base = base.where(Student.province_code == province)
        # The exact count is the expensive half of the search — cache it so
        # page navigation (and repeated identical queries) don't re-count.
        total = _count_cache.get(count_key)
        if total is None:
            total = db.execute(
                select(func.count()).select_from(base.subquery())
            ).scalar() or 0
            _cache_put(_count_cache, count_key, total)
        # Relevance: exact phrase, then phrase-prefix, then phrase-substring,
        # then the rest — each tier alphabetized.
        rank = case(
            (Student.name_norm == phrase, 0),
            (Student.name_norm.like(f"{phrase}%"), 1),
            (Student.name_norm.like(f"%{phrase}%"), 2),
            else_=3,
        )
        rows = db.execute(
            base.order_by(rank, Student.name).offset(offset).limit(limit)
        ).scalars().all()
        return [s.to_dict() for s in rows], total


def list_provinces():
    """[(code, name), ...] ordered by name."""
    with SessionLocal() as db:
        rows = db.execute(select(Province).order_by(Province.name)).scalars().all()
        return [(p.code, p.name) for p in rows]


def has_data() -> bool:
    """True once at least one province has been ingested. Mirrors the web
    frontend's empty-state gate (web/app.js `boot()`), which hides search
    until data exists."""
    with SessionLocal() as db:
        return db.execute(select(Province.code).limit(1)).first() is not None


# ---------------------------------------------------------------------------
# Arabic UI text + formatting.
# ---------------------------------------------------------------------------
SUBJECT_EMOJI = "📘"


def _esc(text) -> str:
    """Escape for HTML parse mode."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_card(d: dict) -> str:
    """A nicely formatted Arabic result card (HTML parse mode)."""
    school = d.get("school") or {}
    result = (d.get("result") or "").strip()
    # ناجح / معيد status line
    if result:
        status = f"✅ <b>{_esc(result)}</b>"
    else:
        status = "ℹ️ <b>غير متوفر</b>"

    avg = d.get("average")
    total = d.get("total")
    avg_line = f"📊 <b>المعدل:</b> {avg}" if avg is not None else "📊 <b>المعدل:</b> —"
    total_line = f"➕ <b>المجموع:</b> {total}" if total is not None else "➕ <b>المجموع:</b> —"

    lines = [
        f"👤 <b>{_esc(d.get('name'))}</b>",
        f"🔢 <b>الرقم الامتحاني:</b> <code>{_esc(d.get('exam_no'))}</code>",
        "",
        f"🏫 <b>المدرسة:</b> {_esc(school.get('name')) or '—'}",
        f"📚 <b>الفرع:</b> {_esc(school.get('track')) or '—'}",
        f"🗺️ <b>المحافظة:</b> {_esc(school.get('province')) or '—'}",
        "",
        f"{status}",
        avg_line,
        total_line,
    ]

    grades = d.get("grades") or {}
    if grades:
        lines.append("")
        lines.append("<b>الدرجات:</b>")
        for subject, grade in grades.items():
            lines.append(f"{SUBJECT_EMOJI} {_esc(subject)}: <b>{_esc(grade)}</b>")

    return "\n".join(lines)


PASS_MARK = 50   # web frontend colors: >=50 green, <50 red, "غ" gray


def _rich_grade_row(subject, grade) -> str:
    """One <tr> of the rich grades table. `grade` is int or the string "غ"."""
    if isinstance(grade, int):
        mark = "✅" if grade >= PASS_MARK else "❌"
        val = f"<b>{grade}</b>" if grade >= PASS_MARK else str(grade)
    else:  # absent
        mark, val = "🚫", _esc(grade)
    return (f"<tr><td>{_esc(subject)}</td>"
            f'<td align="center">{val}</td>'
            f'<td align="center">{mark}</td></tr>')


def format_rich_card(d: dict) -> str:
    """sendRichMessage (HTML-style) version of format_card: same info, but the
    grades render as a real bordered table instead of emoji lines."""
    school = d.get("school") or {}
    result = (d.get("result") or "").strip()
    status = f"✅ <b>{_esc(result)}</b>" if result else "ℹ️ <b>غير متوفر</b>"
    avg = d.get("average")
    total = d.get("total")

    parts = [
        f"<h3>👤 {_esc(d.get('name'))}</h3>",
        f"<p>🔢 <b>الرقم الامتحاني:</b> <code>{_esc(d.get('exam_no'))}</code></p>",
        "<blockquote>"
        f"🏫 <b>المدرسة:</b> {_esc(school.get('name')) or '—'}<br>"
        f"📚 <b>الفرع:</b> {_esc(school.get('track')) or '—'}<br>"
        f"🗺️ <b>المحافظة:</b> {_esc(school.get('province')) or '—'}"
        "</blockquote>",
        f"<p>{status}</p>",
    ]

    grades = d.get("grades") or {}
    rows = [_rich_grade_row(s, g) for s, g in grades.items()]
    # summary rows at the bottom of the same table
    avg_txt = _esc(avg) if avg is not None else "—"
    total_txt = _esc(total) if total is not None else "—"
    rows.append(f'<tr><td><b>المجموع</b></td>'
                f'<td align="center"><b>{total_txt}</b></td><td></td></tr>')
    rows.append(f'<tr><td><b>المعدل</b></td>'
                f'<td align="center"><b>{avg_txt}</b></td><td></td></tr>')
    parts.append(
        "<table bordered striped><caption>📖 الدرجات</caption>"
        "<tr><th>المادة</th><th>الدرجة</th><th>الحالة</th></tr>"
        + "".join(rows) + "</table>")
    return "".join(parts)


# Flipped on the first definitive rejection of sendRichMessage (old Bot API
# server / method not rolled out) so every later card skips straight to the
# classic HTML fallback instead of paying a failed round-trip each time.
_rich_send_disabled = False


async def send_result_card(message, d: dict):
    """Send a student result card, preferring a rich message (real table for
    the grades) and falling back to the classic HTML card."""
    global _rich_send_disabled
    if not _rich_send_disabled:
        try:
            await message.get_bot().do_api_request(
                "sendRichMessage",
                api_kwargs={
                    "chat_id": message.chat_id,
                    "rich_message": {
                        "html": format_rich_card(d),
                        "skip_entity_detection": True,
                    },
                },
            )
            return
        except Exception as e:
            from telegram.error import BadRequest, EndPointNotFound
            if isinstance(e, (EndPointNotFound, BadRequest)):
                _rich_send_disabled = True
                log.warning("sendRichMessage unavailable (%s) — "
                            "falling back to classic HTML cards", e)
            else:  # transient (network/timeout): fall back for this send only
                log.warning("sendRichMessage failed (%s); using HTML fallback", e)
    await message.reply_text(format_card(d), parse_mode="HTML")


def _fmt_overall(derived, provinces_count):
    d = derived
    lines = [
        "📊 <b>إحصائيات عامة</b>",
        f"المحافظات: <b>{provinces_count}</b> · المدارس: <b>{d['schools']}</b> · "
        f"الطلاب: <b>{d['students']}</b>",
        f"✅ ناجح: <b>{d['passed']}</b> · 🔁 معيد: <b>{d['failed']}</b> · "
        f"نسبة النجاح: <b>{d['pass_rate']}%</b>",
    ]
    if d["avg_of_averages"] is not None:
        lines.append(f"📈 معدل المعدلات: <b>{d['avg_of_averages']}</b>")
    subs = list(d["subjects"].items())
    if subs:
        lines.append("\n<b>المواد (الأعلى نجاحًا):</b>")
        for subj, s in subs[:5]:
            lines.append(f"📘 {_esc(subj)}: {s['pass_rate']}% "
                         f"(معدل {s['avg'] if s['avg'] is not None else '—'})")
        if len(subs) > 5:
            lines.append("<b>الأدنى نجاحًا:</b>")
            for subj, s in subs[-3:]:
                lines.append(f"📕 {_esc(subj)}: {s['pass_rate']}% "
                             f"(معدل {s['avg'] if s['avg'] is not None else '—'})")
    return "\n".join(lines)


def _fmt_province(name, derived):
    d = derived
    lines = [
        f"📊 <b>{_esc(name)}</b>",
        f"المدارس: <b>{d['schools']}</b> · الطلاب: <b>{d['students']}</b>",
        f"✅ ناجح: <b>{d['passed']}</b> · 🔁 معيد: <b>{d['failed']}</b> · "
        f"نسبة النجاح: <b>{d['pass_rate']}%</b>",
    ]
    if d["avg_of_averages"] is not None:
        lines.append(f"📈 معدل المعدلات: <b>{d['avg_of_averages']}</b>")
    if d["subjects"]:
        lines.append("\n<b>حسب المادة:</b>")
        for subj, s in d["subjects"].items():
            avg = s["avg"] if s["avg"] is not None else "—"
            lines.append(
                f"📘 {_esc(subj)}: نجاح {s['pass_rate']}% · معدل {avg} · "
                f"غائب {s['absent']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram handlers. Imported lazily inside build_application so that importing
# this module (e.g. the ast.parse verification) never requires the telegram lib.
# ---------------------------------------------------------------------------
WELCOME = (
    "🎓 <b>نتائج السادس</b>\n\n"
    "أهلاً بك! اختر طريقة البحث عن نتيجتك:\n\n"
    "🔢 البحث بالرقم الامتحاني — أرسل رقمك مباشرة.\n"
    "🔤 البحث بالاسم — اختر المحافظة ثم أرسل الاسم."
)

# Same wording as the web empty-state gate (web/app.js showWaiting()).
WAITING_MSG = (
    "⏳ <b>النتائج قيد التحضير…</b>\n"
    "سيظهر البحث تلقائيًا عند اكتمال رفع النتائج.\n\n"
    "يتم الآن تجهيز البيانات من قبل المشرف."
)

# conversation state keys stored in context.user_data
MODE_NAME = "awaiting_name"
SELECTED_PROVINCE = "province"
SEARCH_Q = "search_q"        # last name query, for page navigation
SEARCH_PROV = "search_prov"  # province the last search was scoped to (or None)


def build_application(token: str):
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        ContextTypes,
        filters,
    )

    ADMIN_IDS = {
        int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
        if x.strip().isdigit()
    }

    def main_menu_kb():
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔢 بحث بالرقم الامتحاني", callback_data="mode:exam")],
                [InlineKeyboardButton("🔤 بحث بالاسم", callback_data="mode:name")],
            ]
        )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        if not await asyncio.to_thread(has_data):
            await update.effective_message.reply_text(WAITING_MSG, parse_mode=ParseMode.HTML)
            return
        await update.effective_message.reply_text(
            WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
        )

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "أرسل رقمك الامتحاني مباشرة، أو استخدم /start لاختيار البحث بالاسم.",
            parse_mode=ParseMode.HTML,
        )

    async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not await asyncio.to_thread(has_data):
            await query.message.reply_text(WAITING_MSG, parse_mode=ParseMode.HTML)
            return
        data = query.data or ""
        if data == "mode:exam":
            context.user_data.pop(MODE_NAME, None)
            await query.message.reply_text(
                "🔢 أرسل الرقم الامتحاني الآن.", parse_mode=ParseMode.HTML
            )
        elif data == "mode:name":
            # show provinces to pick from
            provs = await asyncio.to_thread(list_provinces)
            prov_buttons = [
                InlineKeyboardButton(name, callback_data=f"prov:{code}")
                for code, name in provs
            ]
            buttons = [
                prov_buttons[i:i + 2] for i in range(0, len(prov_buttons), 2)
            ]
            buttons.append(
                [InlineKeyboardButton("🌐 كل المحافظات", callback_data="prov:")]
            )
            await query.message.reply_text(
                "🗺️ اختر المحافظة (أو كل المحافظات):",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

    async def on_province(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        code = (query.data or "prov:").split("prov:", 1)[1]
        context.user_data[SELECTED_PROVINCE] = code or None
        context.user_data[MODE_NAME] = True
        await query.message.reply_text(
            "🔤 أرسل الاسم للبحث الآن.", parse_mode=ParseMode.HTML
        )

    async def on_result_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        exam_no = (query.data or "res:").split("res:", 1)[1]
        d = await asyncio.to_thread(lookup_exam_no, exam_no)
        if d:
            await send_result_card(query.message, d)
        else:
            await query.message.reply_text("لم يتم العثور على النتيجة.")

    def _results_keyboard(results, offset, total):
        """Each result is a row of TWO buttons — name | 🗺️ province — (both open
        the same card), then a nav row (⬅️ السابق / صفحة X/Y / التالي ➡️) when
        there is more than one page."""
        buttons = []
        for d in results:
            name = d.get("name") or d.get("exam_no")
            prov_name = (d.get("school") or {}).get("province") or "—"
            exam = d["exam_no"]
            buttons.append([
                InlineKeyboardButton(name[:40], callback_data=f"res:{exam}"),
                InlineKeyboardButton(f"🗺️ {prov_name}"[:40], callback_data=f"res:{exam}"),
            ])
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        cur = offset // PAGE_SIZE + 1
        if pages > 1:
            nav = []
            if offset > 0:
                nav.append(InlineKeyboardButton(
                    "⬅️ السابق", callback_data=f"pg:{max(0, offset - PAGE_SIZE)}"))
            nav.append(InlineKeyboardButton(f"صفحة {cur}/{pages}", callback_data="noop"))
            if offset + PAGE_SIZE < total:
                nav.append(InlineKeyboardButton(
                    "التالي ➡️", callback_data=f"pg:{offset + PAGE_SIZE}"))
            buttons.append(nav)
        return InlineKeyboardMarkup(buttons)

    async def send_results_page(message, context, q, prov, offset, edit=False):
        """Run the paginated search and render one page. A single exact hit is
        shown as a full card; otherwise a paginated pick-list. When edit=True
        (page navigation) the existing message is edited in place instead of
        sending a new one."""
        context.user_data[SEARCH_Q] = q
        context.user_data[SEARCH_PROV] = prov
        results, total = await asyncio.to_thread(
            search_by_name, q, province=prov, offset=offset, limit=PAGE_SIZE)
        if total == 0:
            await message.reply_text(
                "❌ لم يتم العثور على نتائج بهذا الاسم. جرّب /start مرة أخرى.",
                parse_mode=ParseMode.HTML,
            )
            return
        if total == 1 and results:
            await send_result_card(message, results[0])
            return
        text = f"🔎 وجدت {total} نتيجة — اختر الاسم:"
        markup = _results_keyboard(results, offset, total)
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except Exception:  # message unchanged / too old -> fall back to send
                pass
        await message.reply_text(text, reply_markup=markup)

    async def on_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if (query.data or "") == "noop":
            return
        try:
            offset = int((query.data or "pg:0").split("pg:", 1)[1])
        except ValueError:
            offset = 0
        q = context.user_data.get(SEARCH_Q)
        if not q:
            await query.message.reply_text("انتهت الجلسة. استخدم /start للبحث من جديد.")
            return
        prov = context.user_data.get(SEARCH_PROV)
        await send_results_page(query.message, context, q, prov, offset, edit=True)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        if not await asyncio.to_thread(has_data):
            await update.effective_message.reply_text(WAITING_MSG, parse_mode=ParseMode.HTML)
            return

        # Auto-detect: a long digit string => exam number lookup.
        digits = text.replace(" ", "")
        if digits.isdigit() and len(digits) >= 5 and not context.user_data.get(MODE_NAME):
            d = await asyncio.to_thread(lookup_exam_no, digits)
            if d:
                await send_result_card(update.effective_message, d)
            else:
                await update.effective_message.reply_text(
                    "❌ لا توجد نتيجة بهذا الرقم الامتحاني.\nتأكد من الرقم أو استخدم /start.",
                    parse_mode=ParseMode.HTML,
                )
            return

        # Name-search mode.
        if context.user_data.get(MODE_NAME):
            province = context.user_data.get(SELECTED_PROVINCE)
            context.user_data.pop(MODE_NAME, None)
            await send_results_page(update.effective_message, context,
                                    text, province, 0)
            return

        # Fallback: treat any other input as a name search across all provinces.
        _, total = await asyncio.to_thread(search_by_name, text, offset=0, limit=1)
        if total == 0:
            await update.effective_message.reply_text(
                "لم أفهم طلبك. استخدم /start للبدء، أو أرسل رقمك الامتحاني.",
                parse_mode=ParseMode.HTML,
            )
            return
        await send_results_page(update.effective_message, context, text, None, 0)

    async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        doc = update.effective_message.document
        if not doc:
            return
        if not ADMIN_IDS:
            await update.effective_message.reply_text(
                "🚫 رفع الملفات عبر البوت غير مُفعّل (لم يتم ضبط ADMIN_IDS)."
            )
            return
        if not user or user.id not in ADMIN_IDS:
            await update.effective_message.reply_text("🚫 هذه الميزة للمشرفين فقط.")
            return
        fname = (doc.file_name or "").lower()
        if not fname.endswith((".zip", ".rar")):
            await update.effective_message.reply_text("أرسل ملف .zip أو .rar يحتوي مجلدات المحافظة.")
            return

        local_mode = os.environ.get("TELEGRAM_LOCAL", "").strip() in ("1", "true", "True")

        # Public Bot API caps bot downloads (getFile) at 20 MB. With a local Bot
        # API server (TELEGRAM_LOCAL=1) there is NO limit, so skip this guard.
        TG_LIMIT = 20 * 1024 * 1024
        if not local_mode and (doc.file_size or 0) > TG_LIMIT:
            mb = (doc.file_size or 0) / 1024 / 1024
            await update.effective_message.reply_text(
                f"⚠️ الملف كبير ({mb:.0f} ميغابايت). تيليجرام يسمح للبوت بتنزيل 20 ميغابايت كحد أقصى.\n\n"
                "للمحافظات الكبيرة استخدم أحد البدائل (بلا حدود):\n"
                "• رفع عبر الـ API:\n"
                "  curl -X POST <domain>/api/ingest -H \"Authorization: Bearer <ADMIN_TOKEN>\" "
                "-F province=<code_name> -F file=@province.rar\n"
                "• أو فعّل خادم Bot API المحلي (TELEGRAM_LOCAL=1) لرفع حتى 2 غيغابايت."
            )
            return

        import tempfile
        suffix = ".rar" if fname.endswith(".rar") else ".zip"
        tmp_path = None
        province_label = os.path.splitext(doc.file_name or "")[0]
        try:
            # Download (I/O). With a local Bot API server getFile makes the server
            # fetch the whole file first, so give it generous timeouts.
            tg_file = await doc.get_file(
                read_timeout=1800, connect_timeout=60,
                write_timeout=60, pool_timeout=60,
            )
            local_path = getattr(tg_file, "file_path", None)
            if local_mode and local_path and os.path.isfile(local_path):
                ingest_target = local_path            # shared volume, no download
            else:
                fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                await tg_file.download_to_drive(tmp_path)
                ingest_target = tmp_path
        except Exception as e:  # pragma: no cover - defensive
            log.exception("download failed")
            await update.effective_message.reply_text(f"❌ فشل التنزيل: {e}")
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return

        # Fire a background job and return immediately, so every file is accepted
        # right away and jobs run concurrently (bounded by INGEST_CONCURRENCY).
        asyncio.create_task(_ingest_job(
            context.bot, update.effective_chat.id,
            ingest_target, province_label, tmp_path))
        await update.effective_message.reply_text(
            f"📥 «{province_label}» أُضيفت للمعالجة في الخلفية.")

    def _load_analytics():
        """[(code, name, counters), ...] -> overall + per-province derived."""
        counters = load_province_counters()
        overall = analytics.merge_all([c for (_c, _n, c) in counters])
        return counters, overall

    async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        counters, overall = await asyncio.to_thread(_load_analytics)
        if not counters or overall["students"] == 0:
            await update.effective_message.reply_text(
                "لا توجد بيانات بعد. أرسل نتائج المحافظات أولًا.")
            return
        # per-province drill-down buttons (2 per row)
        provs = sorted(counters, key=lambda t: analytics.derive(t[2])["pass_rate"],
                       reverse=True)
        buttons, row = [], []
        for (code, name, _c) in provs:
            row.append(InlineKeyboardButton(name, callback_data=f"stats:{code}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row:
            buttons.append(row)
        await update.effective_message.reply_text(
            _fmt_overall(analytics.derive(overall), len(counters)),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

    async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        code = (query.data or "stats:").split("stats:", 1)[1]
        for (c, name, cnt) in await asyncio.to_thread(load_province_counters):
            if c == code:
                await query.message.reply_text(
                    _fmt_province(name, analytics.derive(cnt)),
                    parse_mode=ParseMode.HTML)
                return
        await query.message.reply_text("لا توجد إحصائيات لهذه المحافظة.")

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: wipe ALL data to start a fresh stage. Two-step confirm."""
        user = update.effective_user
        if not ADMIN_IDS:
            await update.effective_message.reply_text(
                "🚫 هذه الميزة غير مُفعّلة (لم يتم ضبط ADMIN_IDS).")
            return
        if not user or user.id not in ADMIN_IDS:
            await update.effective_message.reply_text("🚫 هذه الميزة للمشرفين فقط.")
            return
        # show current totals so the admin knows what they're about to erase
        def _totals():
            with SessionLocal() as db:
                return (
                    db.execute(select(func.count(Student.exam_no))).scalar() or 0,
                    db.execute(select(func.count()).select_from(School)).scalar() or 0,
                    db.execute(select(func.count()).select_from(Province)).scalar() or 0,
                )
        n_stu, n_sch, n_prov = await asyncio.to_thread(_totals)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 نعم، احذف الكل", callback_data="reset:yes"),
            InlineKeyboardButton("إلغاء", callback_data="reset:no"),
        ]])
        await update.effective_message.reply_text(
            "⚠️ <b>حذف جميع البيانات نهائيًا</b>\n\n"
            f"المحافظات: {n_prov}\nالمدارس: {n_sch}\nالطلاب: {n_stu}\n\n"
            "سيبدأ النظام فارغًا (مثلاً لرفع نتائج الدور الثاني). "
            "هذا الإجراء لا يمكن التراجع عنه.",
            parse_mode=ParseMode.HTML, reply_markup=kb)

    async def on_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user = update.effective_user
        # re-check admin on the confirm tap (buttons can be forwarded)
        if not ADMIN_IDS or not user or user.id not in ADMIN_IDS:
            await query.edit_message_text("🚫 هذه الميزة للمشرفين فقط.")
            return
        if (query.data or "") == "reset:no":
            await query.edit_message_text("❎ تم الإلغاء. لم يتم حذف أي شيء.")
            return
        await query.edit_message_text("⏳ جارٍ حذف جميع البيانات...")
        try:
            counts = await asyncio.to_thread(reset_all)
            _clear_query_caches()  # data gone -> cached hits would be stale
            await query.edit_message_text(
                "✅ تم حذف جميع البيانات. النظام جاهز لمرحلة جديدة.\n\n"
                f"المحذوف — المحافظات: {counts['provinces']}، "
                f"المدارس: {counts['schools']}، الطلاب: {counts['students']}")
        except Exception as e:  # pragma: no cover - defensive
            log.exception("reset failed")
            await query.edit_message_text(f"❌ فشل الحذف: {e}")

    # PTB's default is to process updates STRICTLY SEQUENTIALLY — one slow
    # search would queue every other user. Handle updates concurrently; DB
    # work runs off the event loop via asyncio.to_thread in the handlers.
    bot_concurrency = max(1, int(os.environ.get("BOT_CONCURRENCY", "256")))
    builder = Application.builder().token(token).concurrent_updates(bot_concurrency)
    # Optional local Bot API server (no 20 MB download cap, up to 2 GB uploads,
    # and files arrive as a local path — no download round-trip). Env-gated so
    # the default (public api.telegram.org) is unchanged.
    if os.environ.get("TELEGRAM_LOCAL", "").strip() in ("1", "true", "True"):
        base = os.environ.get("TELEGRAM_BASE_URL", "http://telegram-bot-api:8081/bot")
        base_file = os.environ.get("TELEGRAM_BASE_FILE_URL", "http://telegram-bot-api:8081/file/bot")
        builder = builder.base_url(base).base_file_url(base_file).local_mode(True)
        log.info("Using LOCAL Bot API server at %s", base)
    application = builder.build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CallbackQueryHandler(on_reset, pattern=r"^reset:"))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CallbackQueryHandler(on_stats, pattern=r"^stats:"))
    application.add_handler(CallbackQueryHandler(on_menu, pattern=r"^mode:"))
    application.add_handler(CallbackQueryHandler(on_province, pattern=r"^prov:"))
    application.add_handler(CallbackQueryHandler(on_result_pick, pattern=r"^res:"))
    application.add_handler(CallbackQueryHandler(on_page, pattern=r"^(pg:|noop$)"))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )
    return application


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_TOKEN not set; bot disabled")
        return 0

    # Ensure schema exists (safe/idempotent) before serving queries.
    init_db()
    log.info("Starting Telegram bot (long polling)...")
    application = build_application(token)
    # drop_pending_updates: ignore the backlog on startup so restarting the bot
    # doesn't re-ingest files that were already sent.
    application.run_polling(allowed_updates=None, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
