"""Professional Telegram Quiz Creator Bot handlers."""

import asyncio
import csv
import io
import json
import logging
import re
from html import escape
from urllib.request import Request, urlopen

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import cache, db

logger = logging.getLogger(__name__)

DB_KEY = "db"
REDIS_KEY = "redis"


# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

BOT_COMMANDS = (
    ("start", "Start the bot"),
    ("create", "Create a new quiz"),
    ("myquizzes", "View quizzes you created"),
    ("settings", "Configure quiz settings"),
    ("stop", "Stop the current quiz"),
    ("help", "Show all commands"),
    ("section", "Create a multi-section quiz"),
    ("mistakes", "Review incorrect answers"),
    ("bookmarks", "Review bookmarked questions"),
    ("pause", "Pause the current quiz"),
    ("resume", "Resume a paused quiz"),
    ("fast", "Decrease question timer"),
    ("slow", "Increase question timer"),
    ("stoppoll", "Stop an active poll"),
    ("testbook", "Import an attempted quiz from Testbook"),
    ("tutorial", "Show bot tutorials and guides"),
    ("cancel", "Cancel current process"),
)


# =========================================================
# IN-MEMORY USER STATE
# =========================================================

DRAFTS = {}
SESSIONS = {}
ACTIVE_POLLS = {}


STATE_TITLE = "title"
STATE_SUBJECT = "subject"
STATE_COUNT = "count"
STATE_TIME = "time"
STATE_NEGATIVE = "negative"
STATE_QUESTION = "question"
STATE_OPTION_A = "option_a"
STATE_OPTION_B = "option_b"
STATE_OPTION_C = "option_c"
STATE_OPTION_D = "option_d"
STATE_CORRECT = "correct"
STATE_EXPLANATION = "explanation"

MENU_HELP = "Help"
MENU_TUTORIAL = "Tutorial"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [MENU_HELP, MENU_TUTORIAL],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Choose an option",
)


# =========================================================
# DATABASE
# =========================================================

CREATE_QUIZZES_TABLE = """
CREATE TABLE IF NOT EXISTS quizzes (
    id SERIAL PRIMARY KEY,
    creator_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    subject TEXT,
    question_count INTEGER NOT NULL DEFAULT 0,
    time_limit INTEGER NOT NULL DEFAULT 30,
    negative_marking DOUBLE PRECISION NOT NULL DEFAULT 0,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_QUESTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_questions (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    question_no INTEGER NOT NULL,
    question TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    option_d TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    explanation TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def ensure_quiz_tables(pool):
    if pool is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(CREATE_QUIZZES_TABLE)
        await conn.execute(CREATE_QUESTIONS_TABLE)


# =========================================================
# HELPERS
# =========================================================

def get_pool(context):
    return context.bot_data.get(DB_KEY)


def user_id(update):
    user = update.effective_user
    return user.id if user else None


def valid_answer(value):
    return value.strip().upper() in {"A", "B", "C", "D"}


def format_preview(draft):
    title = escape(draft["title"])
    subject = escape(draft.get("subject") or "Not specified")

    text = (
        "<b>📋 QUIZ PREVIEW</b>\n\n"
        f"<b>Title:</b> {title}\n"
        f"<b>Subject:</b> {subject}\n"
        f"<b>Questions:</b> {len(draft['questions'])}\n"
        f"<b>Time:</b> {draft['time_limit']} minutes\n"
        f"<b>Negative marking:</b> {draft['negative_marking']}\n\n"
    )

    for i, q in enumerate(draft["questions"], 1):
        text += (
            f"<b>Q{i}.</b> {escape(q['question'])}\n"
            f"A. {escape(q['option_a'])}\n"
            f"B. {escape(q['option_b'])}\n"
            f"C. {escape(q['option_c'])}\n"
            f"D. {escape(q['option_d'])}\n"
            f"✅ Correct: {q['correct_answer']}\n"
        )

        if q.get("explanation"):
            text += f"💡 {escape(q['explanation'])}\n"

        text += "\n"

    return text


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    pool = get_pool(context)

    if pool:
        await db.upsert_user(
            pool,
            user.id,
            user.username,
            user.first_name,
        )
        await ensure_quiz_tables(pool)

    await message.reply_text(
        f"Welcome to <b>Brain Booster BPSC Quiz Bot</b>, "
        f"{escape(user.first_name or 'User')}!\n\n"
        "Create, manage and attempt professional quizzes "
        "from one place.\n\n"
        "Use /create to create your first quiz.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    text = """
<b>🧠 Brain Booster BPSC Quiz Bot</b>

<b>Quiz Creation</b>
/create — Create a new quiz
/myquizzes — View your quizzes
/settings — Configure quiz settings
/section — Create a multi-section quiz
/cancel — Cancel creation/editing

<b>Quiz Control</b>
/stop — Stop current quiz
/pause — Pause current quiz
/resume — Resume paused quiz
/fast — Decrease question timer
/slow — Increase question timer
/stoppoll — Stop active poll

<b>Review</b>
/mistakes — Review incorrect answers
/bookmarks — Review bookmarked questions

<b>Import & Help</b>
/testbook — Import a quiz from Testbook URL
/tutorial — Tutorials and guides
/help — Show this help
/start — Start the bot

Subjects are quiz metadata only. They are not commands.
"""

    await message.reply_text(text, parse_mode="HTML")


# =========================================================
# TUTORIAL
# =========================================================

async def tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    text = """
<b>📘 QUIZ CREATOR TUTORIAL</b>

<b>Method 1 — Text creation</b>

Send:
<code>/create</code>

The bot will ask:

1. Quiz title
2. Subject (optional)
3. Number of questions
4. Time limit
5. Negative marking
6. Question
7. Option A
8. Option B
9. Option C
10. Option D
11. Correct answer
12. Explanation

<b>Method 2 — File creation</b>

Send /create and then upload:
• .txt
• .csv
• .json

<b>TXT format</b>

<code>
Question: Who wrote Arthashastra?
A: Kautilya
B: Kalidasa
C: Banabhatta
D: Megasthenes
Correct: A
Explanation: Arthashastra is traditionally attributed to Kautilya.
</code>

<b>JSON format</b>

<code>
{
  "title": "BPSC Practice Test",
  "subject": "History",
  "questions": [
    {
      "question": "Question text",
      "option_a": "Option A",
      "option_b": "Option B",
      "option_c": "Option C",
      "option_d": "Option D",
      "correct_answer": "A",
      "explanation": "Explanation"
    }
  ]
}
</code>

Subject is optional metadata.
It does NOT become a Telegram command.
"""

    await message.reply_text(text, parse_mode="HTML")


# =========================================================
# CREATE
# =========================================================

async def create_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    DRAFTS[uid] = {
        "state": STATE_TITLE,
        "title": "",
        "subject": "",
        "question_count": 0,
        "time_limit": 30,
        "negative_marking": 0,
        "questions": [],
        "current_question": {},
        "section_mode": False,
        "sections": [],
    }

    await message.reply_text(
        "<b>📝 CREATE NEW QUIZ</b>\n\n"
        "Quiz का title भेजें.\n\n"
        "आप बाद में /cancel से creation रोक सकते हैं।",
        parse_mode="HTML",
    )


# =========================================================
# SECTION QUIZ
# =========================================================

async def section_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    DRAFTS[uid] = {
        "state": STATE_TITLE,
        "title": "",
        "subject": "",
        "question_count": 0,
        "time_limit": 30,
        "negative_marking": 0,
        "questions": [],
        "current_question": {},
        "section_mode": True,
        "sections": [],
    }

    await message.reply_text(
        "<b>📚 MULTI-SECTION QUIZ</b>\n\n"
        "पहले quiz title भेजें.\n"
        "इसके बाद quiz questions सामान्य creation flow में add होंगे.\n\n"
        "Sections बाद में database metadata में configure किए जा सकते हैं।",
        parse_mode="HTML",
    )


# =========================================================
# CREATE TEXT PROCESSOR
# =========================================================

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message or not message.text:
        return

    draft = DRAFTS.get(uid)

    if not draft:
        return

    text = message.text.strip()
    state = draft["state"]

    if state == STATE_TITLE:
        draft["title"] = text
        draft["state"] = STATE_SUBJECT

        await message.reply_text(
            "📚 <b>Subject</b> भेजें.\n\n"
            "यह केवल quiz metadata है.\n"
            "Subject नहीं देना है तो <code>skip</code> लिखें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_SUBJECT:
        draft["subject"] = (
            "" if text.lower() == "skip" else text
        )
        draft["state"] = STATE_COUNT

        await message.reply_text(
            "🔢 कितने questions चाहिए?\n\n"
            "उदाहरण: <code>25</code>",
            parse_mode="HTML",
        )
        return

    if state == STATE_COUNT:
        try:
            count = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल संख्या भेजें. उदाहरण: 25"
            )
            return

        if not 1 <= count <= 500:
            await message.reply_text(
                "❌ Questions 1 से 500 के बीच रखें."
            )
            return

        draft["question_count"] = count
        draft["state"] = STATE_TIME

        await message.reply_text(
            "⏱️ Time limit कितने minutes का होगा?\n\n"
            "उदाहरण: <code>30</code>",
            parse_mode="HTML",
        )
        return

    if state == STATE_TIME:
        try:
            minutes = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल minutes की संख्या भेजें."
            )
            return

        if not 1 <= minutes <= 300:
            await message.reply_text(
                "❌ Time limit 1 से 300 minutes के बीच रखें."
            )
            return

        draft["time_limit"] = minutes
        draft["state"] = STATE_NEGATIVE

        await message.reply_text(
            "➖ Negative marking भेजें.\n\n"
            "0 = None\n"
            "0.25 = 1/4\n"
            "0.33 = 1/3\n\n"
            "Example: <code>0.25</code>",
            parse_mode="HTML",
        )
        return

    if state == STATE_NEGATIVE:
        try:
            negative = float(text)
        except ValueError:
            await message.reply_text(
                "❌ Example: 0, 0.25 या 0.33"
            )
            return

        if not 0 <= negative <= 1:
            await message.reply_text(
                "❌ Negative marking 0 से 1 के बीच रखें."
            )
            return

        draft["negative_marking"] = negative
        draft["state"] = STATE_QUESTION

        await message.reply_text(
            "✅ Settings complete.\n\n"
            "अब <b>Question 1</b> भेजें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_QUESTION:
        draft["current_question"] = {
            "question": text
        }
        draft["state"] = STATE_OPTION_A
        await message.reply_text("🔹 Option A भेजें.")
        return

    if state == STATE_OPTION_A:
        draft["current_question"]["option_a"] = text
        draft["state"] = STATE_OPTION_B
        await message.reply_text("🔹 Option B भेजें.")
        return

    if state == STATE_OPTION_B:
        draft["current_question"]["option_b"] = text
        draft["state"] = STATE_OPTION_C
        await message.reply_text("🔹 Option C भेजें.")
        return

    if state == STATE_OPTION_C:
        draft["current_question"]["option_c"] = text
        draft["state"] = STATE_OPTION_D
        await message.reply_text("🔹 Option D भेजें.")
        return

    if state == STATE_OPTION_D:
        draft["current_question"]["option_d"] = text
        draft["state"] = STATE_CORRECT

        await message.reply_text(
            "✅ Correct answer भेजें:\n\n"
            "<code>A</code> / <code>B</code> / "
            "<code>C</code> / <code>D</code>",
            parse_mode="HTML",
        )
        return

    if state == STATE_CORRECT:
        answer = text.upper()

        if answer not in {"A", "B", "C", "D"}:
            await message.reply_text(
                "❌ केवल A, B, C या D भेजें."
            )
            return

        draft["current_question"]["correct_answer"] = answer
        draft["state"] = STATE_EXPLANATION

        await message.reply_text(
            "💡 Explanation भेजें.\n\n"
            "नहीं देना है तो <code>skip</code> लिखें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_EXPLANATION:
        draft["current_question"]["explanation"] = (
            "" if text.lower() == "skip" else text
        )

        draft["questions"].append(
            draft["current_question"].copy()
        )
        draft["current_question"] = {}

        current = len(draft["questions"])
        required = draft["question_count"]

        if current >= required:
            draft["state"] = "complete"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "👁 Preview",
                        callback_data="draft_preview",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "➕ Add Question",
                        callback_data="draft_add",
                    ),
                    InlineKeyboardButton(
                        "💾 Save Quiz",
                        callback_data="draft_save",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="draft_cancel",
                    )
                ],
            ])

            await message.reply_text(
                f"✅ <b>{current}/{required}</b> questions complete.\n\n"
                "अब action चुनें:",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            draft["state"] = STATE_QUESTION

            await message.reply_text(
                f"✅ Question {current} saved.\n\n"
                f"अब Question {current + 1} भेजें."
            )


# =========================================================
# SAVE DRAFT
# =========================================================

async def save_draft(uid, context):
    draft = DRAFTS.get(uid)

    if not draft:
        return None

    pool = get_pool(context)

    if pool is None:
        raise RuntimeError("Database is not connected.")

    await ensure_quiz_tables(pool)

    async with pool.acquire() as conn:
        quiz_id = await conn.fetchval(
            """
            INSERT INTO quizzes
            (
                creator_id,
                title,
                subject,
                question_count,
                time_limit,
                negative_marking,
                published
            )
            VALUES ($1,$2,$3,$4,$5,$6,FALSE)
            RETURNING id
            """,
            uid,
            draft["title"],
            draft["subject"],
            len(draft["questions"]),
            draft["time_limit"],
            draft["negative_marking"],
        )

        for number, q in enumerate(
            draft["questions"], 1
        ):
            await conn.execute(
                """
                INSERT INTO quiz_questions
                (
                    quiz_id,
                    question_no,
                    question,
                    option_a,
                    option_b,
                    option_c,
                    option_d,
                    correct_answer,
                    explanation
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                quiz_id,
                number,
                q["question"],
                q["option_a"],
                q["option_b"],
                q["option_c"],
                q["option_d"],
                q["correct_answer"],
                q.get("explanation", ""),
            )

    return quiz_id


# =========================================================
# DRAFT CALLBACKS
# =========================================================

async def draft_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    uid = query.from_user.id
    draft = DRAFTS.get(uid)

    if not draft:
        await query.message.reply_text(
            "❌ Draft नहीं मिला. /create से नया quiz शुरू करें."
        )
        return

    if query.data == "draft_preview":
        await query.message.reply_text(
            format_preview(draft),
            parse_mode="HTML",
        )
        return

    if query.data == "draft_add":
        draft["state"] = STATE_QUESTION

        await query.message.reply_text(
            f"➕ Question {len(draft['questions']) + 1} भेजें."
        )
        return

    if query.data == "draft_cancel":
        DRAFTS.pop(uid, None)

        await query.message.reply_text(
            "❌ Quiz creation cancelled."
        )
        return

    if query.data == "draft_save":
        try:
            quiz_id = await save_draft(uid, context)
        except Exception as exc:
            logger.exception("Failed to save quiz")
            await query.message.reply_text(
                f"❌ Quiz save नहीं हुआ:\n<code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        title = escape(draft["title"])
        questions = len(draft["questions"])

        DRAFTS.pop(uid, None)

        await query.message.reply_text(
            f"✅ <b>Quiz saved successfully.</b>\n\n"
            f"<b>Quiz ID:</b> <code>{quiz_id}</code>\n"
            f"<b>Title:</b> {title}\n"
            f"<b>Questions:</b> {questions}\n"
            f"<b>Status:</b> Draft\n\n"
            f"Use /myquizzes to manage it.",
            parse_mode="HTML",
        )


# =========================================================
# FILE IMPORT
# =========================================================

def parse_json_quiz(raw):
    data = json.loads(raw)

    if isinstance(data, list):
        questions = data
        title = "Imported Quiz"
        subject = ""
    else:
        title = data.get("title", "Imported Quiz")
        subject = data.get("subject", "")
        questions = data.get("questions", [])

    normalized = []

    for q in questions:
        normalized.append({
            "question": str(q["question"]),
            "option_a": str(q["option_a"]),
            "option_b": str(q["option_b"]),
            "option_c": str(q["option_c"]),
            "option_d": str(q["option_d"]),
            "correct_answer": str(
                q["correct_answer"]
            ).upper(),
            "explanation": str(
                q.get("explanation", "")
            ),
        })

    return title, subject, normalized


def parse_csv_quiz(raw):
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)

    questions = []

    for row in rows:
        questions.append({
            "question": row["question"],
            "option_a": row["option_a"],
            "option_b": row["option_b"],
            "option_c": row["option_c"],
            "option_d": row["option_d"],
            "correct_answer": row[
                "correct_answer"
            ].upper(),
            "explanation": row.get(
                "explanation", ""
            ),
        })

    return "Imported Quiz", "", questions


def parse_txt_quiz(raw):
    blocks = re.split(
        r"\n\s*\n",
        raw.strip(),
    )

    questions = []

    for block in blocks:
        lines = block.splitlines()

        item = {}

        for line in lines:
            if ":" not in line:
                continue

            key, value = line.split(
                ":",
                1,
            )

            key = key.strip().lower()
            value = value.strip()

            mapping = {
                "question": "question",
                "a": "option_a",
                "option a": "option_a",
                "b": "option_b",
                "option b": "option_b",
                "c": "option_c",
                "option c": "option_c",
                "d": "option_d",
                "option d": "option_d",
                "correct": "correct_answer",
                "answer": "correct_answer",
                "explanation": "explanation",
            }

            if key in mapping:
                item[mapping[key]] = value

        required = {
            "question",
            "option_a",
            "option_b",
            "option_c",
            "option_d",
            "correct_answer",
        }

        if required.issubset(item):
            item["correct_answer"] = (
                item["correct_answer"].upper()
            )
            item.setdefault("explanation", "")
            questions.append(item)

    return "Imported Quiz", "", questions


async def handle_quiz_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    uid = user_id(update)

    if not message or not uid or not message.document:
        return

    document = message.document
    filename = document.file_name or ""

    if not filename.lower().endswith(
        (".json", ".csv", ".txt")
    ):
        await message.reply_text(
            "❌ केवल .json, .csv या .txt file supported है."
        )
        return

    telegram_file = await document.get_file()
    data = await telegram_file.download_as_bytearray()

    try:
        raw = bytes(data).decode(
            "utf-8-sig"
        )

        if filename.lower().endswith(".json"):
            title, subject, questions = (
                parse_json_quiz(raw)
            )
        elif filename.lower().endswith(".csv"):
            title, subject, questions = (
                parse_csv_quiz(raw)
            )
        else:
            title, subject, questions = (
                parse_txt_quiz(raw)
            )

        if not questions:
            raise ValueError(
                "No valid questions found."
            )

        for q in questions:
            if q["correct_answer"] not in {
                "A", "B", "C", "D"
            }:
                raise ValueError(
                    "Correct answer must be A/B/C/D."
                )

        DRAFTS[uid] = {
            "state": "complete",
            "title": title,
            "subject": subject,
            "question_count": len(questions),
            "time_limit": 30,
            "negative_marking": 0,
            "questions": questions,
            "current_question": {},
            "section_mode": False,
            "sections": [],
        }

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data="draft_preview",
                )
            ],
            [
                InlineKeyboardButton(
                    "💾 Save Quiz",
                    callback_data="draft_save",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                ),
            ],
        ])

        await message.reply_text(
            f"✅ File imported.\n\n"
            f"Questions found: {len(questions)}\n\n"
            "Preview करें या Save Quiz दबाएँ.",
            reply_markup=keyboard,
        )

    except Exception as exc:
        await message.reply_text(
            f"❌ File import failed:\n{escape(str(exc))}",
            parse_mode="HTML",
        )


# =========================================================
# MY QUIZZES
# =========================================================

async def my_quizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    uid = user_id(update)

    if not message or not uid:
        return

    pool = get_pool(context)

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    await ensure_quiz_tables(pool)

    rows = await pool.fetch(
        """
        SELECT id, title, subject,
               question_count,
               time_limit,
               negative_marking,
               published
        FROM quizzes
        WHERE creator_id = $1
        ORDER BY id DESC
        LIMIT 50
        """,
        uid,
    )

    if not rows:
        await message.reply_text(
            "आपने अभी कोई quiz create नहीं किया है.\n\n"
            "/create से शुरू करें."
        )
        return

    for row in rows:
        status = (
            "🟢 Published"
            if row["published"]
            else "🟡 Draft"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data=f"quiz_view:{row['id']}",
                ),
                InlineKeyboardButton(
                    "✏️ Edit",
                    callback_data=f"quiz_edit:{row['id']}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🚀 Publish",
                    callback_data=f"quiz_publish:{row['id']}",
                ),
                InlineKeyboardButton(
                    "🗑 Delete",
                    callback_data=f"quiz_delete:{row['id']}",
                ),
            ],
        ])

        await message.reply_text(
            f"<b>#{row['id']} — {escape(row['title'])}</b>\n\n"
            f"Subject: {escape(row['subject'] or 'Not specified')}\n"
            f"Questions: {row['question_count']}\n"
            f"Time: {row['time_limit']} min\n"
            f"Negative: {row['negative_marking']}\n"
            f"Status: {status}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# =========================================================
# QUIZ MANAGEMENT CALLBACKS
# =========================================================

async def quiz_management(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    uid = query.from_user.id
    action, raw_id = query.data.split(":", 1)

    try:
        quiz_id = int(raw_id)
    except ValueError:
        return

    pool = get_pool(context)

    if pool is None:
        await query.message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    if action == "quiz_delete":
        result = await pool.execute(
            """
            DELETE FROM quizzes
            WHERE id=$1 AND creator_id=$2
            """,
            quiz_id,
            uid,
        )

        await query.message.reply_text(
            "🗑 Quiz deleted."
            if not result.endswith("0")
            else "❌ Quiz नहीं मिला."
        )
        return

    if action == "quiz_publish":
        result = await pool.execute(
            """
            UPDATE quizzes
            SET published=TRUE
            WHERE id=$1
              AND creator_id=$2
              AND question_count > 0
            """,
            quiz_id,
            uid,
        )

        await query.message.reply_text(
            "🚀 Quiz published."
            if not result.endswith("0")
            else "❌ Quiz publish नहीं किया जा सका."
        )
        return

    if action == "quiz_view":
        row = await pool.fetchrow(
            """
            SELECT q.title,
                   q.subject,
                   q.question_count,
                   q.time_limit,
                   q.negative_marking
            FROM quizzes q
            WHERE q.id=$1 AND q.creator_id=$2
            """,
            quiz_id,
            uid,
        )

        if not row:
            await query.message.reply_text(
                "❌ Quiz नहीं मिला."
            )
            return

        questions = await pool.fetch(
            """
            SELECT question_no,
                   question,
                   option_a,
                   option_b,
                   option_c,
                   option_d,
                   correct_answer,
                   explanation
            FROM quiz_questions
            WHERE quiz_id=$1
            ORDER BY question_no
            """,
            quiz_id,
        )

        text = (
            f"<b>📋 {escape(row['title'])}</b>\n\n"
            f"Subject: {escape(row['subject'] or 'Not specified')}\n"
            f"Questions: {row['question_count']}\n"
            f"Time: {row['time_limit']} min\n"
            f"Negative: {row['negative_marking']}\n\n"
        )

        for q in questions:
            text += (
                f"<b>Q{q['question_no']}.</b> "
                f"{escape(q['question'])}\n"
                f"A. {escape(q['option_a'])}\n"
                f"B. {escape(q['option_b'])}\n"
                f"C. {escape(q['option_c'])}\n"
                f"D. {escape(q['option_d'])}\n"
                f"Correct: {q['correct_answer']}\n\n"
            )

        await query.message.reply_text(
            text,
            parse_mode="HTML",
        )
        return

    if action == "quiz_edit":
        await query.message.reply_text(
            f"✏️ Editing Quiz #{quiz_id}\n\n"
            "Edit mode में quiz को modify करने के लिए "
            "question-level actions आगे जोड़े जा सकते हैं."
        )


# =========================================================
# SETTINGS
# =========================================================

async def settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    uid = user_id(update)

    if not message or not uid:
        return

    session = SESSIONS.setdefault(
        uid,
        {
            "timer": 30,
            "paused": False,
            "active": False,
        },
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏱ Timer -30 sec",
                callback_data="setting_fast",
            ),
            InlineKeyboardButton(
                "⏱ Timer +30 sec",
                callback_data="setting_slow",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 Reset",
                callback_data="setting_reset",
            )
        ],
    ])

    await message.reply_text(
        f"<b>⚙️ Quiz Settings</b>\n\n"
        f"Current question timer: "
        f"<b>{session['timer']} sec</b>\n\n"
        "Timer को /fast और /slow से भी बदल सकते हैं.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def settings_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    uid = query.from_user.id

    session = SESSIONS.setdefault(
        uid,
        {
            "timer": 30,
            "paused": False,
            "active": False,
        },
    )

    if query.data == "setting_fast":
        session["timer"] = max(
            10,
            session["timer"] - 30,
        )

    elif query.data == "setting_slow":
        session["timer"] = min(
            600,
            session["timer"] + 30,
        )

    elif query.data == "setting_reset":
        session["timer"] = 30

    await query.message.reply_text(
        f"⚙️ Timer: {session['timer']} seconds"
    )


# =========================================================
# QUIZ SESSION CONTROLS
# =========================================================

async def stop_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    SESSIONS.pop(uid, None)

    await message.reply_text(
        "🛑 Current quiz session stopped."
    )


async def pause_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    session = SESSIONS.setdefault(
        uid,
        {
            "timer": 30,
            "paused": False,
            "active": False,
        },
    )

    session["paused"] = True

    await message.reply_text(
        "⏸ Quiz paused.\n\n"
        "Use /resume to continue."
    )


async def resume_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    session = SESSIONS.get(uid)

    if not session or not session.get("paused"):
        await message.reply_text(
            "कोई paused quiz नहीं है."
        )
        return

    session["paused"] = False

    await message.reply_text(
        "▶️ Quiz resumed."
    )


async def fast_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    session = SESSIONS.setdefault(
        uid,
        {
            "timer": 30,
            "paused": False,
            "active": False,
        },
    )

    session["timer"] = max(
        10,
        session["timer"] - 10,
    )

    await message.reply_text(
        f"⚡ Question timer decreased to "
        f"{session['timer']} seconds."
    )


async def slow_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    session = SESSIONS.setdefault(
        uid,
        {
            "timer": 30,
            "paused": False,
            "active": False,
        },
    )

    session["timer"] = min(
        600,
        session["timer"] + 10,
    )

    await message.reply_text(
        f"🐢 Question timer increased to "
        f"{session['timer']} seconds."
    )


# =========================================================
# MISTAKES
# =========================================================

async def mistakes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    data = SESSIONS.get(uid, {}).get(
        "mistakes",
        [],
    )

    if not data:
        await message.reply_text(
            "✅ इस session में कोई incorrect answer recorded नहीं है."
        )
        return

    text = "<b>❌ Incorrect Answers</b>\n\n"

    for i, item in enumerate(data, 1):
        text += (
            f"{i}. {escape(item['question'])}\n"
            f"Your answer: {item['your_answer']}\n"
            f"Correct: {item['correct_answer']}\n\n"
        )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# BOOKMARKS
# =========================================================

async def bookmarks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    data = SESSIONS.get(uid, {}).get(
        "bookmarks",
        [],
    )

    if not data:
        await message.reply_text(
            "🔖 अभी कोई bookmarked question नहीं है."
        )
        return

    text = "<b>🔖 Bookmarked Questions</b>\n\n"

    for i, question in enumerate(data, 1):
        text += (
            f"{i}. {escape(str(question))}\n\n"
        )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# STOP POLL
# =========================================================

async def stoppoll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    poll_id = ACTIVE_POLLS.pop(uid, None)

    if poll_id is None:
        await message.reply_text(
            "कोई active poll नहीं है."
        )
        return

    try:
        await context.bot.stop_poll(
            chat_id=message.chat_id,
            message_id=poll_id,
        )

        await message.reply_text(
            "🛑 Active poll stopped."
        )

    except Exception as exc:
        await message.reply_text(
            f"❌ Poll stop नहीं हुआ: {escape(str(exc))}",
            parse_mode="HTML",
        )


# =========================================================
# TESTBOOK IMPORT
# =========================================================

async def fetch_url(url):
    def _fetch():
        request = Request(
            url,
            headers={
                "User-Agent":
                "Mozilla/5.0 QuizBot/1.0"
            },
        )

        with urlopen(
            request,
            timeout=15,
        ) as response:
            return response.read(
                2_000_000
            ).decode(
                "utf-8",
                errors="ignore",
            )

    return await asyncio.to_thread(_fetch)


async def testbook(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    uid = user_id(update)

    if not message or not uid:
        return

    if not context.args:
        await message.reply_text(
            "<b>Testbook Import</b>\n\n"
            "Usage:\n"
            "<code>/testbook https://...</code>",
            parse_mode="HTML",
        )
        return

    url = context.args[0]

    if not url.startswith(
        ("https://", "http://")
    ):
        await message.reply_text(
            "❌ Valid http/https URL भेजें."
        )
        return

    if "testbook.com" not in url.lower():
        await message.reply_text(
            "❌ कृपया Testbook URL दें."
        )
        return

    await message.reply_text(
        "⏳ Testbook page पढ़ने की कोशिश कर रहा हूँ..."
    )

    try:
        raw = await fetch_url(url)

        # Public JSON endpoint / embedded JSON
        try:
            title, subject, questions = (
                parse_json_quiz(raw)
            )
        except Exception:
            title, subject, questions = (
                parse_txt_quiz(raw)
            )

        if not questions:
            raise ValueError(
                "Public quiz questions could not be extracted."
            )

        DRAFTS[uid] = {
            "state": "complete",
            "title": title,
            "subject": subject,
            "question_count": len(questions),
            "time_limit": 30,
            "negative_marking": 0,
            "questions": questions,
            "current_question": {},
            "section_mode": False,
            "sections": [],
        }

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data="draft_preview",
                ),
                InlineKeyboardButton(
                    "💾 Save",
                    callback_data="draft_save",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                )
            ],
        ])

        await message.reply_text(
            f"✅ {len(questions)} questions imported.\n\n"
            "Preview या Save चुनें.",
            reply_markup=keyboard,
        )

    except Exception as exc:
        await message.reply_text(
            "❌ Testbook import failed.\n\n"
            "यह URL private/login-protected हो सकता है "
            "या page structure supported नहीं है.\n\n"
            f"<code>{escape(str(exc))}</code>",
            parse_mode="HTML",
        )


# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uid = user_id(update)
    message = update.effective_message

    if not uid or not message:
        return

    removed = False

    if uid in DRAFTS:
        DRAFTS.pop(uid, None)
        removed = True

    if uid in SESSIONS:
        SESSIONS.pop(uid, None)
        removed = True

    if removed:
        await message.reply_text(
            "❌ Current process cancelled."
        )
    else:
        await message.reply_text(
            "कोई active creation/editing process नहीं है."
        )


# =========================================================
# MENU BUTTONS
# =========================================================

async def menu_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    if message.text == MENU_HELP:
        await help_command(update, context)

    elif message.text == MENU_TUTORIAL:
        await tutorial(update, context)


# =========================================================
# UNKNOWN COMMAND
# =========================================================

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        "❌ Unknown command.\n\n"
        "Use /help to see available commands."
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if isinstance(
        error,
        (Conflict, NetworkError, TimedOut),
    ):
        logger.warning(
            "Telegram transient error: %s",
            error,
        )
        return

    logger.exception(
        "Unhandled Telegram error: %s",
        update,
        exc_info=error,
    )


# =========================================================
# SET COMMAND MENU
# =========================================================

async def set_bot_commands(
    application: Application,
):
    commands = [
        BotCommand(name, description)
        for name, description in BOT_COMMANDS
    ]

    await application.bot.set_my_commands(
        commands
    )


# =========================================================
# REGISTER ALL HANDLERS
# =========================================================

def register_handlers(
    application: Application,
):

    # -----------------------------------------------------
    # EXACTLY THE REQUESTED COMMANDS
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("create", create_quiz)
    )

    application.add_handler(
        CommandHandler("myquizzes", my_quizzes)
    )

    application.add_handler(
        CommandHandler("settings", settings)
    )

    application.add_handler(
        CommandHandler("stop", stop_quiz)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("section", section_command)
    )

    application.add_handler(
        CommandHandler("mistakes", mistakes)
    )

    application.add_handler(
        CommandHandler("bookmarks", bookmarks)
    )

    application.add_handler(
        CommandHandler("pause", pause_quiz)
    )

    application.add_handler(
        CommandHandler("resume", resume_quiz)
    )

    application.add_handler(
        CommandHandler("fast", fast_timer)
    )

    application.add_handler(
        CommandHandler("slow", slow_timer)
    )

    application.add_handler(
        CommandHandler("stoppoll", stoppoll)
    )

    application.add_handler(
        CommandHandler("testbook", testbook)
    )

    application.add_handler(
        CommandHandler("tutorial", tutorial)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    # -----------------------------------------------------
    # CALLBACKS
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            draft_buttons,
            pattern=r"^draft_",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            quiz_management,
            pattern=r"^quiz_(view|edit|publish|delete):",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            settings_buttons,
            pattern=r"^setting_",
        )
    )

    # -----------------------------------------------------
    # FILE UPLOAD
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_quiz_file,
        ),
        group=0,
    )

    # -----------------------------------------------------
    # QUIZ CREATION TEXT
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=1,
    )

    # -----------------------------------------------------
    # REPLY KEYBOARD
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({MENU_HELP}|{MENU_TUTORIAL})$"
            ),
            menu_button,
        ),
        group=2,
    )

    # -----------------------------------------------------
    # UNKNOWN COMMAND
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=3,
    )
