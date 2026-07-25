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
import logging

try:
    from .db import init_db, SessionLocal, Province, School, Student
    from .glyph import normalize_ar
    from .ingest import ingest_path
except ImportError:  # allow running as a top-level module too
    from db import init_db, SessionLocal, Province, School, Student
    from glyph import normalize_ar
    from ingest import ingest_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

MAX_RESULTS = 20

# ---------------------------------------------------------------------------
# DB query helpers — same logic style as app/api.py, but direct (no HTTP).
# ---------------------------------------------------------------------------
try:
    from sqlalchemy import select
except ImportError:  # pragma: no cover - sqlalchemy is a hard dep of db.py
    select = None


def lookup_exam_no(exam_no: str):
    """Exact primary-key lookup. Returns Student.to_dict() or None."""
    exam_no = (exam_no or "").strip()
    if not exam_no:
        return None
    with SessionLocal() as db:
        st = db.get(Student, exam_no)
        return st.to_dict() if st else None


def search_by_name(name: str, province: str | None = None):
    """normalize_ar + AND of LIKE %token% on name_norm, capped at MAX_RESULTS."""
    norm = normalize_ar(name or "")
    tokens = [t for t in norm.split() if t]
    if not tokens:
        return []
    with SessionLocal() as db:
        stmt = select(Student)
        for t in tokens:
            stmt = stmt.where(Student.name_norm.like(f"%{t}%"))
        if province:
            stmt = stmt.join(School, Student.school_code == School.code).where(
                School.province_code == province
            )
        stmt = stmt.order_by(Student.name).limit(MAX_RESULTS)
        rows = db.execute(stmt).scalars().all()
        return [s.to_dict() for s in rows]


def list_provinces():
    """[(code, name), ...] ordered by name."""
    with SessionLocal() as db:
        rows = db.execute(select(Province).order_by(Province.name)).scalars().all()
        return [(p.code, p.name) for p in rows]


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

# conversation state keys stored in context.user_data
MODE_NAME = "awaiting_name"
SELECTED_PROVINCE = "province"


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
        data = query.data or ""
        if data == "mode:exam":
            context.user_data.pop(MODE_NAME, None)
            await query.message.reply_text(
                "🔢 أرسل الرقم الامتحاني الآن.", parse_mode=ParseMode.HTML
            )
        elif data == "mode:name":
            # show provinces to pick from
            provs = list_provinces()
            buttons = [
                [InlineKeyboardButton(name, callback_data=f"prov:{code}")]
                for code, name in provs
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
        d = lookup_exam_no(exam_no)
        if d:
            await query.message.reply_text(format_card(d), parse_mode=ParseMode.HTML)
        else:
            await query.message.reply_text("لم يتم العثور على النتيجة.")

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.effective_message.text or "").strip()
        if not text:
            return

        # Auto-detect: a long digit string => exam number lookup.
        digits = text.replace(" ", "")
        if digits.isdigit() and len(digits) >= 5 and not context.user_data.get(MODE_NAME):
            d = lookup_exam_no(digits)
            if d:
                await update.effective_message.reply_text(
                    format_card(d), parse_mode=ParseMode.HTML
                )
            else:
                await update.effective_message.reply_text(
                    "❌ لا توجد نتيجة بهذا الرقم الامتحاني.\nتأكد من الرقم أو استخدم /start.",
                    parse_mode=ParseMode.HTML,
                )
            return

        # Name-search mode.
        if context.user_data.get(MODE_NAME):
            province = context.user_data.get(SELECTED_PROVINCE)
            results = search_by_name(text, province=province)
            context.user_data.pop(MODE_NAME, None)
            if not results:
                await update.effective_message.reply_text(
                    "❌ لم يتم العثور على نتائج بهذا الاسم. جرّب /start مرة أخرى.",
                    parse_mode=ParseMode.HTML,
                )
                return
            if len(results) == 1:
                await update.effective_message.reply_text(
                    format_card(results[0]), parse_mode=ParseMode.HTML
                )
                return
            buttons = []
            for d in results:
                school = (d.get("school") or {}).get("name") or ""
                label = d.get("name") or d.get("exam_no")
                if school:
                    label = f"{label} — {school}"
                buttons.append(
                    [InlineKeyboardButton(label[:60], callback_data=f"res:{d['exam_no']}")]
                )
            await update.effective_message.reply_text(
                f"🔎 وجدت {len(results)} نتيجة. اختر الاسم:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Fallback: treat any other input as a name search across all provinces.
        results = search_by_name(text)
        if not results:
            await update.effective_message.reply_text(
                "لم أفهم طلبك. استخدم /start للبدء، أو أرسل رقمك الامتحاني.",
                parse_mode=ParseMode.HTML,
            )
            return
        buttons = [
            [InlineKeyboardButton((d.get("name") or d["exam_no"])[:60],
                                  callback_data=f"res:{d['exam_no']}")]
            for d in results
        ]
        await update.effective_message.reply_text(
            f"🔎 وجدت {len(results)} نتيجة. اختر الاسم:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

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
        if not fname.endswith(".zip"):
            await update.effective_message.reply_text("أرسل ملف .zip يحتوي مجلدات المحافظة.")
            return

        await update.effective_message.reply_text("⏳ جارٍ تنزيل الملف ومعالجته...")
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(tmp_path)
            stats = ingest_path(tmp_path)
            await update.effective_message.reply_text(
                "✅ تم الاستيراد.\n"
                f"المدارس: {stats.get('schools', 0)}\n"
                f"الطلاب: {stats.get('students', 0)}\n"
                f"الأخطاء: {len(stats.get('errors', []))}"
            )
        except Exception as e:  # pragma: no cover - defensive
            log.exception("ingest failed")
            await update.effective_message.reply_text(f"❌ فشل الاستيراد: {e}")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(on_menu, pattern=r"^mode:"))
    application.add_handler(CallbackQueryHandler(on_province, pattern=r"^prov:"))
    application.add_handler(CallbackQueryHandler(on_result_pick, pattern=r"^res:"))
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
    application.run_polling(allowed_updates=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
