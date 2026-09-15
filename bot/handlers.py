<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Brain Booster BPSC Quiz - handlers.py</title>

<style>
    body {
        margin: 0;
        background: #111827;
        color: #f9fafb;
        font-family: Arial, sans-serif;
    }

    .container {
        max-width: 1000px;
        margin: 20px auto;
        padding: 20px;
    }

    h1 {
        font-size: 24px;
    }

    button {
        background: #2563eb;
        color: white;
        border: 0;
        padding: 12px 18px;
        border-radius: 8px;
        cursor: pointer;
        font-size: 15px;
        margin-bottom: 12px;
    }

    button:hover {
        background: #1d4ed8;
    }

    textarea {
        width: 100%;
        height: 80vh;
        box-sizing: border-box;
        background: #020617;
        color: #e5e7eb;
        border: 1px solid #374151;
        border-radius: 10px;
        padding: 15px;
        font-family: monospace;
        font-size: 13px;
        line-height: 1.5;
        resize: vertical;
    }

    .note {
        background: #1f2937;
        padding: 15px;
        border-radius: 10px;
        margin-bottom: 15px;
        line-height: 1.6;
    }
</style>
</head>

<body>

<div class="container">

<h1>Brain Booster BPSC Quiz — handlers.py</h1>

<div class="note">
    पूरा code नीचे है।<br>
    <b>Copy handlers.py</b> दबाएँ → GitHub में
    <b>bot/handlers.py</b> खोलें → पूरा पुराना code हटाएँ →
    यह code paste करें → Commit करें।
</div>

<button onclick="copyCode()">📋 Copy handlers.py</button>

<textarea id="code" spellcheck="false">
"""Professional Telegram Quiz Creator Bot handlers."""

import csv
import io
import json
import logging
import re
from html import escape
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from telegram import (
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
# COMMAND MENU
# =========================================================

BOT_COMMANDS = (
    ("start", "Start the bot and show welcome message"),
    ("create", "Create a new quiz"),
    ("myquizzes", "View quizzes created by you"),
    ("settings", "Configure quiz settings"),
    ("stop", "Stop the current quiz"),
    ("help", "Show all commands"),
    ("section", "Create a quiz with timed sections"),
    ("mistakes", "Review incorrectly answered questions"),
    ("bookmarks", "Review bookmarked questions"),
    ("pause", "Pause the current quiz"),
    ("resume", "Resume a paused quiz"),
    ("fast", "Decrease question timer"),
    ("slow", "Increase question timer"),
    ("stoppoll", "Stop an active poll"),
    ("testbook", "Import an attempted quiz from Testbook URL"),
    ("tutorial", "Show bot tutorials and guides"),
    ("cancel", "Cancel current creation or editing process"),
)


# =========================================================
# STATES
# =========================================================

STATE_TITLE = "title"
STATE_SUBJECT = "subject"
STATE_TIME = "time"
STATE_NEGATIVE = "negative"
STATE_BATCH = "batch"
STATE_COMPLETE = "complete"
STATE_SECTION_TITLE = "section_title"
STATE_SECTION_TIME = "section_time"
STATE_SECTION_QUESTIONS = "section_questions"


# =========================================================
# IN-MEMORY USER SESSIONS
# =========================================================

DRAFTS = {}
QUIZ_SESSIONS = {}


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
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    sections JSONB NOT NULL DEFAULT '[]'::jsonb,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_QUESTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_questions (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL
        REFERENCES quizzes(id) ON DELETE CASCADE,
    question_no INTEGER NOT NULL,
    question TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    option_d TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    explanation TEXT DEFAULT '',
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
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    pool = context.bot_data.get(DB_KEY)

    if pool:
        await db.upsert_user(
            pool,
            user.id,
            user.username,
            user.first_name,
        )

        await ensure_quiz_tables(pool)

    keyboard = ReplyKeyboardMarkup(
        [
            ["📝 Create Quiz", "📚 My Quizzes"],
            ["⚙️ Settings", "📖 Tutorial"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option",
    )

    await message.reply_text(
        f"Welcome to <b>Brain Booster BPSC Quiz Bot</b>, "
        f"{escape(user.first_name or 'User')}!\n\n"
        "Create, manage and attempt professional quizzes "
        "from one place.\n\n"
        "Use <b>/create</b> to create a quiz.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    text = """
<b>🧠 PROFESSIONAL QUIZ BOT</b>

<b>Quiz Creation</b>
/create — Create a new quiz
/myquizzes — View your quizzes
/settings — Configure quiz settings
/section — Create timed sections
/cancel — Cancel current process

<b>Quiz Control</b>
/stop — Stop current quiz
/pause — Pause current quiz
/resume — Resume quiz
/fast — Decrease question timer
/slow — Increase question timer
/stoppoll — Stop active poll

<b>Review</b>
/mistakes — Review incorrect answers
/bookmarks — Review bookmarked questions

<b>Import & Help</b>
/testbook — Import attempted Testbook quiz
/tutorial — Tutorials and guides
/help — Show commands
/start — Start bot

<b>Subjects are metadata only.</b>
They are not Telegram commands.
"""

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# TUTORIAL
# =========================================================

async def tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    text = """
<b>📖 QUIZ CREATOR TUTORIAL</b>

<b>Step 1</b>
/create भेजें.

<b>Step 2</b>
Quiz title दें.

<b>Step 3</b>
Subject दें या <code>skip</code> लिखें.

<b>Step 4</b>
Time limit दें.

<b>Step 5</b>
Negative marking दें.

<b>Step 6</b>
अब सारे questions एक ही message में भेजें.

<b>Format:</b>

<code>
Q1. भारत की राजधानी क्या है?
A. पटना
B. दिल्ली
C. मुंबई
D. जयपुर
Correct: B
Explanation: नई दिल्ली भारत की राजधानी है.

Q2. बिहार की राजधानी क्या है?
A. गया
B. पटना
C. भागलपुर
D. दरभंगा
Correct: B
Explanation: पटना बिहार की राजधानी है.
</code>

आप 1 से 500 तक questions एक batch में भेज सकते हैं।

File भी upload कर सकते हैं।
Supported:
TXT
CSV
JSON

इसके बाद:
Preview → Save Quiz
"""

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# SETTINGS
# =========================================================

def default_settings():
    return {
        "question_timer": 30,
        "show_explanation": True,
        "shuffle_questions": False,
        "shuffle_options": False,
        "allow_back": True,
        "show_score": True,
    }


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.setdefault(
        user.id,
        {
            "settings": default_settings(),
            "running": False,
            "paused": False,
            "poll_active": False,
            "current_question": 0,
        },
    )

    s = session["settings"]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"⏱ Timer: {s['question_timer']} sec",
                    callback_data="setting_timer",
                )
            ],
            [
                InlineKeyboardButton(
                    f"💡 Explanation: "
                    f"{'ON' if s['show_explanation'] else 'OFF'}",
                    callback_data="setting_explanation",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🔀 Shuffle Questions: "
                    f"{'ON' if s['shuffle_questions'] else 'OFF'}",
                    callback_data="setting_shuffle_q",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🔀 Shuffle Options: "
                    f"{'ON' if s['shuffle_options'] else 'OFF'}",
                    callback_data="setting_shuffle_o",
                )
            ],
        ]
    )

    await message.reply_text(
        "<b>⚙️ Quiz Settings</b>\n\n"
        "यह settings आपके current quiz session पर लागू होंगी.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# CREATE QUIZ
# =========================================================

async def create_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    DRAFTS[user.id] = {
        "state": STATE_TITLE,
        "title": "",
        "subject": "",
        "question_count": 0,
        "time_limit": 30,
        "negative_marking": 0,
        "questions": [],
        "sections": [],
        "settings": default_settings(),
    }

    await message.reply_text(
        "<b>📝 CREATE NEW QUIZ</b>\n\n"
        "Quiz का title भेजें.\n\n"
        "इसके बाद basic settings लेने के बाद "
        "आप सारे questions <b>एक ही message</b> में भेज सकेंगे.\n\n"
        "❌ रोकने के लिए /cancel",
        parse_mode="HTML",
    )


# =========================================================
# BATCH QUESTION PARSER
# =========================================================

def parse_batch_questions(text: str):
    text = text.replace("\r\n", "\n").strip()

    if not text:
        return []

    blocks = re.split(
        r"(?im)(?=^\s*(?:Q(?:uestion)?\s*\d+\s*[\.\):\-]))",
        text,
    )

    questions = []

    for block in blocks:
        block = block.strip()

        if not block:
            continue

        block = re.sub(
            r"(?im)^\s*(?:Q(?:uestion)?\s*\d+\s*[\.\):\-])\s*",
            "",
            block,
            count=1,
        )

        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip()
        ]

        if not lines:
            continue

        question = lines[0]

        option_a = None
        option_b = None
        option_c = None
        option_d = None
        correct = None
        explanation = ""

        explanation_mode = False

        for line in lines[1:]:

            match = re.match(
                r"^([ABCD])\s*[\.\):\-]\s*(.+)$",
                line,
                re.IGNORECASE,
            )

            if match:
                letter = match.group(1).upper()
                value = match.group(2).strip()

                if letter == "A":
                    option_a = value
                elif letter == "B":
                    option_b = value
                elif letter == "C":
                    option_c = value
                elif letter == "D":
                    option_d = value

                explanation_mode = False
                continue

            match = re.match(
                r"(?i)^(?:correct\s*answer|correct|answer)"
                r"\s*:\s*([ABCD])\s*$",
                line,
            )

            if match:
                correct = match.group(1).upper()
                explanation_mode = False
                continue

            if re.match(
                r"(?i)^explanation\s*:",
                line,
            ):
                explanation = re.sub(
                    r"(?i)^explanation\s*:\s*",
                    "",
                    line,
                ).strip()

                explanation_mode = True
                continue

            if explanation_mode:
                explanation += " " + line

        if all(
            [
                question,
                option_a,
                option_b,
                option_c,
                option_d,
                correct,
            ]
        ):
            questions.append(
                {
                    "question": question,
                    "option_a": option_a,
                    "option_b": option_b,
                    "option_c": option_c,
                    "option_d": option_d,
                    "correct_answer": correct,
                    "explanation": explanation.strip(),
                }
            )

    return questions


# =========================================================
# FILE PARSERS
# =========================================================

def parse_json_questions(text: str):
    data = json.loads(text)

    if isinstance(data, dict):
        data = data.get(
            "questions",
            data.get("data", []),
        )

    result = []

    for item in data:
        if not isinstance(item, dict):
            continue

        question = str(
            item.get("question", "")
        ).strip()

        options = item.get("options", {})

        if isinstance(options, list):
            options = {
                "A": options[0] if len(options) > 0 else "",
                "B": options[1] if len(options) > 1 else "",
                "C": options[2] if len(options) > 2 else "",
                "D": options[3] if len(options) > 3 else "",
            }

        correct = str(
            item.get(
                "correct_answer",
                item.get("correct", ""),
            )
        ).strip().upper()

        if correct.startswith("OPTION "):
            correct = correct[-1:]

        q = {
            "question": question,
            "option_a": str(options.get("A", "")).strip(),
            "option_b": str(options.get("B", "")).strip(),
            "option_c": str(options.get("C", "")).strip(),
            "option_d": str(options.get("D", "")).strip(),
            "correct_answer": correct,
            "explanation": str(
                item.get("explanation", "")
            ).strip(),
        }

        if (
            q["question"]
            and q["option_a"]
            and q["option_b"]
            and q["option_c"]
            and q["option_d"]
            and correct in {"A", "B", "C", "D"}
        ):
            result.append(q)

    return result


def parse_csv_questions(text: str):
    result = []

    reader = csv.DictReader(
        io.StringIO(text)
    )

    for row in reader:

        q = {
            "question": (
                row.get("question")
                or row.get("Question")
                or ""
            ).strip(),

            "option_a": (
                row.get("option_a")
                or row.get("A")
                or ""
            ).strip(),

            "option_b": (
                row.get("option_b")
                or row.get("B")
                or ""
            ).strip(),

            "option_c": (
                row.get("option_c")
                or row.get("C")
                or ""
            ).strip(),

            "option_d": (
                row.get("option_d")
                or row.get("D")
                or ""
            ).strip(),

            "correct_answer": (
                row.get("correct_answer")
                or row.get("correct")
                or row.get("Correct")
                or ""
            ).strip().upper(),

            "explanation": (
                row.get("explanation")
                or row.get("Explanation")
                or ""
            ).strip(),
        }

        if q["correct_answer"] in {"A", "B", "C", "D"}:
            if all(
                [
                    q["question"],
                    q["option_a"],
                    q["option_b"],
                    q["option_c"],
                    q["option_d"],
                ]
            ):
                result.append(q)

    return result


def parse_file_questions(filename: str, content: bytes):
    lower = filename.lower()

    text = content.decode(
        "utf-8",
        errors="replace",
    )

    if lower.endswith(".json"):
        return parse_json_questions(text)

    if lower.endswith(".csv"):
        return parse_csv_questions(text)

    return parse_batch_questions(text)


# =========================================================
# CREATION TEXT PROCESSOR
# =========================================================

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message or not message.text:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()
    state = draft["state"]

    # -----------------------------------------------------
    # TITLE
    # -----------------------------------------------------

    if state == STATE_TITLE:

        if len(text) < 2:
            await message.reply_text(
                "❌ Quiz title थोड़ा बड़ा रखें."
            )
            return

        draft["title"] = text
        draft["state"] = STATE_SUBJECT

        await message.reply_text(
            "📚 <b>Subject</b> भेजें.\n\n"
            "Subject केवल database metadata रहेगा.\n"
            "Subject नहीं देना है तो <code>skip</code> लिखें.",
            parse_mode="HTML",
        )
        return

    # -----------------------------------------------------
    # SUBJECT
    # -----------------------------------------------------

    if state == STATE_SUBJECT:

        draft["subject"] = (
            ""
            if text.lower() == "skip"
            else text
        )

        draft["state"] = STATE_TIME

        await message.reply_text(
            "⏱️ <b>Time Limit</b>\n\n"
            "Quiz कितने minutes का होगा?\n\n"
            "Example: <code>30</code>",
            parse_mode="HTML",
        )
        return

    # -----------------------------------------------------
    # TIME
    # -----------------------------------------------------

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
            "➖ <b>Negative Marking</b>\n\n"
            "0 = No negative\n"
            "0.25 = 1/4\n"
            "0.33 = 1/3\n\n"
            "Example: <code>0.25</code>",
            parse_mode="HTML",
        )
        return

    # -----------------------------------------------------
    # NEGATIVE
    # -----------------------------------------------------

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
        draft["state"] = STATE_BATCH

        await message.reply_text(
            "<b>📋 BATCH QUESTION MODE</b>\n\n"
            "अब सारे questions <b>एक ही message</b> में भेजें.\n\n"

            "<code>"
            "Q1. भारत की राजधानी क्या है?\n"
            "A. पटना\n"
            "B. दिल्ली\n"
            "C. मुंबई\n"
            "D. जयपुर\n"
            "Correct: B\n"
            "Explanation: नई दिल्ली भारत की राजधानी है.\n\n"

            "Q2. बिहार की राजधानी क्या है?\n"
            "A. गया\n"
            "B. पटना\n"
            "C. भागलपुर\n"
            "D. दरभंगा\n"
            "Correct: B\n"
            "Explanation: पटना बिहार की राजधानी है."
            "</code>\n\n"

            "💡 1 से 500 तक questions एक batch में भेज सकते हैं.\n"
            "📎 TXT / CSV / JSON file भी upload कर सकते हैं.\n\n"
            "❌ /cancel से process रोकें.",
            parse_mode="HTML",
        )
        return

    # -----------------------------------------------------
    # BATCH
    # -----------------------------------------------------

    if state == STATE_BATCH:

        questions = parse_batch_questions(text)

        if not questions:
            await message.reply_text(
                "❌ कोई valid question नहीं मिला.\n\n"
                "Format check करें और फिर पूरा batch भेजें."
            )
            return

        draft["questions"] = questions
        draft["question_count"] = len(questions)
        draft["state"] = STATE_COMPLETE

        keyboard = InlineKeyboardMarkup(
            [
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
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="draft_cancel",
                    )
                ],
            ]
        )

        await message.reply_text(
            f"✅ <b>{len(questions)} questions detected!</b>\n\n"
            f"<b>Title:</b> {escape(draft['title'])}\n"
            f"<b>Subject:</b> "
            f"{escape(draft['subject'] or 'Not specified')}\n"
            f"<b>Time:</b> {draft['time_limit']} minutes\n"
            f"<b>Negative:</b> {draft['negative_marking']}\n\n"
            "अब Preview या Save Quiz चुनें.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return


# =========================================================
# DOCUMENT UPLOAD
# =========================================================

async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message or not message.document:
        return

    draft = DRAFTS.get(user.id)

    if not draft or draft["state"] != STATE_BATCH:
        await message.reply_text(
            "पहले /create से quiz creation शुरू करें."
        )
        return

    document = message.document
    filename = document.file_name or "questions.txt"

    allowed = (
        filename.lower().endswith(".txt")
        or filename.lower().endswith(".csv")
        or filename.lower().endswith(".json")
    )

    if not allowed:
        await message.reply_text(
            "❌ केवल TXT, CSV या JSON file supported है."
        )
        return

    if document.file_size and document.file_size > 10 * 1024 * 1024:
        await message.reply_text(
            "❌ File maximum 10 MB की हो सकती है."
        )
        return

    try:
        telegram_file = await context.bot.get_file(
            document.file_id
        )

        buffer = io.BytesIO()
        await telegram_file.download_to_memory(
            buffer
        )

        content = buffer.getvalue()

        questions = parse_file_questions(
            filename,
            content,
        )

    except Exception:
        logger.exception("File parsing failed")

        await message.reply_text(
            "❌ File पढ़ने में समस्या आई."
        )
        return

    if not questions:
        await message.reply_text(
            "❌ File में कोई valid question नहीं मिला."
        )
        return

    draft["questions"] = questions
    draft["question_count"] = len(questions)
    draft["state"] = STATE_COMPLETE

    keyboard = InlineKeyboardMarkup(
        [
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
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                )
            ],
        ]
    )

    await message.reply_text(
        f"✅ File imported successfully.\n\n"
        f"📄 File: <code>{escape(filename)}</code>\n"
        f"Questions detected: <b>{len(questions)}</b>\n\n"
        "Preview या Save Quiz चुनें.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# PREVIEW
# =========================================================

def format_preview(draft):
    text = (
        "<b>📋 QUIZ PREVIEW</b>\n\n"
        f"<b>Title:</b> {escape(draft['title'])}\n"
        f"<b>Subject:</b> "
        f"{escape(draft['subject'] or 'Not specified')}\n"
        f"<b>Questions:</b> {len(draft['questions'])}\n"
        f"<b>Time:</b> {draft['time_limit']} minutes\n"
        f"<b>Negative:</b> {draft['negative_marking']}\n\n"
    )

    questions = draft["questions"]

    for index, q in enumerate(questions, 1):

        if index > 30:
            text += (
                f"\n... और {len(questions) - 30} questions हैं."
            )
            break

        text += (
            f"<b>Q{index}.</b> "
            f"{escape(q['question'])}\n"
            f"A. {escape(q['option_a'])}\n"
            f"B. {escape(q['option_b'])}\n"
            f"C. {escape(q['option_c'])}\n"
            f"D. {escape(q['option_d'])}\n"
            f"✅ Correct: {q['correct_answer']}\n"
        )

        if q.get("explanation"):
            text += (
                f"💡 {escape(q['explanation'])}\n"
            )

        text += "\n"

    return text


# =========================================================
# SAVE DRAFT
# =========================================================

async def save_draft(
    query,
    context,
    user_id,
):
    draft = DRAFTS.get(user_id)

    if not draft:
        await query.message.reply_text(
            "❌ Draft नहीं मिला."
        )
        return

    pool = context.bot_data.get(DB_KEY)

    if pool is None:
        await query.message.reply_text(
            "❌ Database connected नहीं है."
        )
        return

    if not draft["questions"]:
        await query.message.reply_text(
            "❌ Quiz में questions नहीं हैं."
        )
        return

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
                settings,
                sections,
                published
            )
            VALUES
            (
                $1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,FALSE
            )
            RETURNING id;
            """,
            user_id,
            draft["title"],
            draft["subject"],
            len(draft["questions"]),
            draft["time_limit"],
            draft["negative_marking"],
            json.dumps(draft["settings"]),
            json.dumps(draft["sections"]),
        )

        for number, q in enumerate(
            draft["questions"],
            start=1,
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
                VALUES
                ($1,$2,$3,$4,$5,$6,$7,$8,$9)
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

    del DRAFTS[user_id]

    await query.message.reply_text(
        f"✅ <b>QUIZ SAVED</b>\n\n"
        f"Quiz ID: <code>{quiz_id}</code>\n"
        f"Title: {escape(draft['title'])}\n"
        f"Questions: {len(draft['questions'])}\n\n"
        f"Status: 🟡 Unpublished\n\n"
        f"Use:\n"
        f"<code>/myquizzes</code>",
        parse_mode="HTML",
    )


# =========================================================
# CALLBACK BUTTONS
# =========================================================

async def quiz_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    if query.data == "draft_preview":

        draft = DRAFTS.get(user.id)

        if not draft:
            await query.message.reply_text(
                "❌ Draft नहीं मिला."
            )
            return

        await query.message.reply_text(
            format_preview(draft),
            parse_mode="HTML",
        )
        return

    if query.data == "draft_save":

        await save_draft(
            query,
            context,
            user.id,
        )
        return

    if query.data == "draft_cancel":

        DRAFTS.pop(user.id, None)

        await query.message.reply_text(
            "❌ Quiz creation cancelled."
        )
        return

    if query.data == "setting_timer":

        session = QUIZ_SESSIONS.setdefault(
            user.id,
            {
                "settings": default_settings(),
                "running": False,
                "paused": False,
                "poll_active": False,
                "current_question": 0,
            },
        )

        current = session["settings"]["question_timer"]

        current -= 5

        if current < 5:
            current = 60

        session["settings"]["question_timer"] = current

        await query.message.reply_text(
            f"⏱ Question timer: {current} seconds"
        )
        return

    if query.data == "setting_explanation":

        session = QUIZ_SESSIONS.setdefault(
            user.id,
            {
                "settings": default_settings(),
                "running": False,
                "paused": False,
                "poll_active": False,
                "current_question": 0,
            },
        )

        s = session["settings"]

        s["show_explanation"] = not s["show_explanation"]

        await query.message.reply_text(
            "💡 Explanation: "
            + (
                "ON"
                if s["show_explanation"]
                else "OFF"
            )
        )
        return

    if query.data == "setting_shuffle_q":

        session = QUIZ_SESSIONS.setdefault(
            user.id,
            {
                "settings": default_settings(),
                "running": False,
                "paused": False,
                "poll_active": False,
                "current_question": 0,
            },
        )

        s = session["settings"]

        s["shuffle_questions"] = not s["shuffle_questions"]

        await query.message.reply_text(
            "🔀 Shuffle questions: "
            + (
                "ON"
                if s["shuffle_questions"]
                else "OFF"
            )
        )
        return

    if query.data == "setting_shuffle_o":

        session = QUIZ_SESSIONS.setdefault(
            user.id,
            {
                "settings": default_settings(),
                "running": False,
                "paused": False,
                "poll_active": False,
                "current_question": 0,
            },
        )

        s = session["settings"]

        s["shuffle_options"] = not s["shuffle_options"]

        await query.message.reply_text(
            "🔀 Shuffle options: "
            + (
                "ON"
                if s["shuffle_options"]
                else "OFF"
            )
        )
        return


# =========================================================
# MY QUIZZES
# =========================================================

async def myquizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message
    pool = context.bot_data.get(DB_KEY)

    if not user or not message:
        return

    if pool is None:
        await message.reply_text(
            "❌ Database connected नहीं है."
        )
        return

    await ensure_quiz_tables(pool)

    rows = await pool.fetch(
        """
        SELECT
            id,
            title,
            subject,
            question_count,
            time_limit,
            negative_marking,
            published
        FROM quizzes
        WHERE creator_id = $1
        ORDER BY id DESC
        LIMIT 50
        """,
        user.id,
    )

    if not rows:
        await message.reply_text(
            "📚 अभी कोई quiz नहीं है.\n\n"
            "/create से पहला quiz बनाएं."
        )
        return

    text = "<b>📚 MY QUIZZES</b>\n\n"

    for row in rows:

        status = (
            "🟢 Published"
            if row["published"]
            else "🟡 Unpublished"
        )

        text += (
            f"<b>#{row['id']} — "
            f"{escape(row['title'])}</b>\n"
            f"Questions: {row['question_count']}\n"
            f"Time: {row['time_limit']} min\n"
            f"Negative: {row['negative_marking']}\n"
            f"Status: {status}\n\n"
        )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# STOP
# =========================================================

async def stop_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.setdefault(
        user.id,
        {
            "settings": default_settings(),
            "running": False,
            "paused": False,
            "poll_active": False,
            "current_question": 0,
        },
    )

    session["running"] = False
    session["paused"] = False

    await message.reply_text(
        "⏹ Current quiz stopped."
    )


# =========================================================
# PAUSE
# =========================================================

async def pause_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.setdefault(
        user.id,
        {
            "settings": default_settings(),
            "running": False,
            "paused": False,
            "poll_active": False,
            "current_question": 0,
        },
    )

    if not session["running"]:
        await message.reply_text(
            "कोई running quiz नहीं है."
        )
        return

    session["paused"] = True

    await message.reply_text(
        "⏸ Quiz paused.\n"
        "/resume से continue करें."
    )


# =========================================================
# RESUME
# =========================================================

async def resume_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.get(user.id)

    if not session or not session["paused"]:
        await message.reply_text(
            "कोई paused quiz नहीं है."
        )
        return

    session["paused"] = False
    session["running"] = True

    await message.reply_text(
        "▶️ Quiz resumed."
    )


# =========================================================
# FAST
# =========================================================

async def fast_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.setdefault(
        user.id,
        {
            "settings": default_settings(),
            "running": False,
            "paused": False,
            "poll_active": False,
            "current_question": 0,
        },
    )

    timer = session["settings"]["question_timer"]

    timer = max(5, timer - 5)

    session["settings"]["question_timer"] = timer

    await message.reply_text(
        f"⚡ Question timer decreased to {timer} seconds."
    )


# =========================================================
# SLOW
# =========================================================

async def slow_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.setdefault(
        user.id,
        {
            "settings": default_settings(),
            "running": False,
            "paused": False,
            "poll_active": False,
            "current_question": 0,
        },
    )

    timer = session["settings"]["question_timer"]

    timer = min(300, timer + 5)

    session["settings"]["question_timer"] = timer

    await message.reply_text(
        f"🐢 Question timer increased to {timer} seconds."
    )


# =========================================================
# STOP POLL
# =========================================================

async def stop_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.setdefault(
        user.id,
        {
            "settings": default_settings(),
            "running": False,
            "paused": False,
            "poll_active": False,
            "current_question": 0,
        },
    )

    session["poll_active"] = False

    await message.reply_text(
        "🛑 Active poll stopped."
    )


# =========================================================
# MISTAKES
# =========================================================

async def mistakes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.get(user.id)

    if not session:
        await message.reply_text(
            "📭 अभी कोई quiz attempt data नहीं है."
        )
        return

    mistakes_list = session.get(
        "mistakes",
        [],
    )

    if not mistakes_list:
        await message.reply_text(
            "✅ अभी कोई incorrect answer नहीं है."
        )
        return

    text = "<b>❌ INCORRECT ANSWERS</b>\n\n"

    for index, item in enumerate(
        mistakes_list[:50],
        1,
    ):
        text += (
            f"<b>{index}.</b> "
            f"{escape(item.get('question', ''))}\n"
            f"Your answer: "
            f"{escape(item.get('your_answer', ''))}\n"
            f"Correct: "
            f"{escape(item.get('correct_answer', ''))}\n\n"
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
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    session = QUIZ_SESSIONS.get(user.id)

    if not session:
        await message.reply_text(
            "🔖 अभी कोई bookmarks नहीं हैं."
        )
        return

    bookmarks_list = session.get(
        "bookmarks",
        [],
    )

    if not bookmarks_list:
        await message.reply_text(
            "🔖 अभी कोई bookmarked question नहीं है."
        )
        return

    text = "<b>🔖 BOOKMARKS</b>\n\n"

    for index, item in enumerate(
        bookmarks_list[:50],
        1,
    ):
        text += (
            f"<b>{index}.</b> "
            f"{escape(item.get('question', ''))}\n\n"
        )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# SECTION CREATOR
# =========================================================

async def section(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    DRAFTS[user.id] = {
        "state": STATE_SECTION_TITLE,
        "title": "",
        "subject": "",
        "question_count": 0,
        "time_limit": 30,
        "negative_marking": 0,
        "questions": [],
        "sections": [],
        "settings": default_settings(),
    }

    await message.reply_text(
        "<b>🧩 TIMED SECTION QUIZ</b>\n\n"
        "Quiz title भेजें.",
        parse_mode="HTML",
    )


# =========================================================
# SECTION CREATION PROCESS
# =========================================================

async def process_section_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message or not message.text:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()

    if draft["state"] == STATE_SECTION_TITLE:

        draft["title"] = text
        draft["state"] = STATE_SECTION_TIME

        await message.reply_text(
            "⏱️ Section का time limit minutes में भेजें.\n"
            "Example: 15"
        )
        return

    if draft["state"] == STATE_SECTION_TIME:

        try:
            minutes = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल number भेजें."
            )
            return

        if not 1 <= minutes <= 180:
            await message.reply_text(
                "❌ 1–180 minutes रखें."
            )
            return

        draft["sections"].append(
            {
                "title": f"Section {len(draft['sections']) + 1}",
                "time_limit": minutes,
            }
        )

        draft["state"] = STATE_SECTION_QUESTIONS

        await message.reply_text(
            "📋 अब इस section के सारे questions "
            "एक message में भेजें.\n\n"
            "Same Q1/A/B/C/D/Correct/Explanation format use करें."
        )
        return

    if draft["state"] == STATE_SECTION_QUESTIONS:

        questions = parse_batch_questions(text)

        if not questions:
            await message.reply_text(
                "❌ Valid questions नहीं मिले."
            )
            return

        draft["questions"].extend(questions)

        draft["question_count"] = len(
            draft["questions"]
        )

        draft["state"] = STATE_COMPLETE

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💾 Save Section Quiz",
                        callback_data="draft_save",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="draft_cancel",
                    )
                ],
            ]
        )

        await message.reply_text(
            f"✅ Section created.\n\n"
            f"Questions: {len(questions)}\n"
            f"Section time: "
            f"{draft['sections'][-1]['time_limit']} min\n\n"
            "Save करें.",
            reply_markup=keyboard,
        )


# =========================================================
# TESTBOOK IMPORT
# =========================================================

def fetch_url_text(url: str):
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Invalid URL")

    request = Request(
        url,
        headers={
            "User-Agent":
                "Mozilla/5.0 "
                "(compatible; QuizCreatorBot/1.0)"
        },
    )

    with urlopen(
        request,
        timeout=15,
    ) as response:
        data = response.read(
            2 * 1024 * 1024
        )

    return data.decode(
        "utf-8",
        errors="ignore",
    )


async def testbook(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
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

    if "testbook.com" not in url.lower():
        await message.reply_text(
            "❌ कृपया valid Testbook URL दें."
        )
        return

    await message.reply_text(
        "🔄 Testbook page पढ़ी जा रही है..."
    )

    try:
        html = fetch_url_text(url)

    except Exception:
        logger.exception(
            "Testbook URL fetch failed"
        )

        await message.reply_text(
            "❌ Testbook page fetch नहीं हो सकी.\n\n"
            "अगर page login/JavaScript protected है, "
            "तो attempted quiz को TXT/CSV/JSON format में "
            "upload करें."
        )
        return

    # Extract visible-looking text from basic HTML.
    clean = re.sub(
        r"<script.*?</script>",
        " ",
        html,
        flags=re.I | re.S,
    )

    clean = re.sub(
        r"<style.*?</style>",
        " ",
        clean,
        flags=re.I | re.S,
    )

    clean = re.sub(
        r"<[^>]+>",
        "\n",
        clean,
    )

    clean = re.sub(
        r"\n{3,}",
        "\n\n",
        clean,
    )

    questions = parse_batch_questions(
        clean
    )

    if not questions:

        await message.reply_text(
            "⚠️ Page मिली, लेकिन standard "
            "question format में questions extract नहीं हुए.\n\n"
            "Testbook का login/JavaScript page होने पर "
            "TXT/CSV/JSON upload method use करें."
        )
        return

    DRAFTS[user.id] = {
        "state": STATE_COMPLETE,
        "title": "Imported Testbook Quiz",
        "subject": "",
        "question_count": len(questions),
        "time_limit": 30,
        "negative_marking": 0,
        "questions": questions,
        "sections": [],
        "settings": default_settings(),
    }

    keyboard = InlineKeyboardMarkup(
        [
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
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                )
            ],
        ]
    )

    await message.reply_text(
        f"✅ <b>{len(questions)} questions imported.</b>\n\n"
        "Preview या Save Quiz चुनें.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    removed = False

    if user.id in DRAFTS:
        del DRAFTS[user.id]
        removed = True

    if user.id in QUIZ_SESSIONS:
        QUIZ_SESSIONS[user.id]["paused"] = False

    if removed:
        await message.reply_text(
            "❌ Current creation/editing process cancelled."
        )
    else:
        await message.reply_text(
            "कोई active creation/editing process नहीं है."
        )


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
        "/help से available commands देखें."
    )


# =========================================================
# TEXT MENU
# =========================================================

async def menu_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()

    if text == "📝 Create Quiz":
        await create_quiz(update, context)

    elif text == "📚 My Quizzes":
        await myquizzes(update, context)

    elif text == "⚙️ Settings":
        await settings(update, context)

    elif text == "📖 Tutorial":
        await tutorial(update, context)


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
            "Transient Telegram error: %s",
            error,
        )
        return

    logger.exception(
        "Error while processing update: %s",
        update,
        exc_info=error,
    )


# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

async def set_bot_commands(
    application: Application,
):
    await application.bot.set_my_commands(
        BOT_COMMANDS
    )


# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_handlers(
    application: Application,
):

    # -----------------------------------------------------
    # EXACT COMMANDS
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("create", create_quiz)
    )

    application.add_handler(
        CommandHandler("myquizzes", myquizzes)
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
        CommandHandler("section", section)
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
        CommandHandler("stoppoll", stop_poll)
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
    # INLINE BUTTONS
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            quiz_buttons,
            pattern=r"^(draft_|setting_)",
        )
    )

    # -----------------------------------------------------
    # BATCH QUIZ TEXT
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=0,
    )

    # -----------------------------------------------------
    # SECTION TEXT
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            process_section_creation,
        ),
        group=1,
    )

    # -----------------------------------------------------
    # FILE UPLOAD
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        ),
        group=0,
    )

    # -----------------------------------------------------
    # REPLY KEYBOARD
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Regex(
                r"^(📝 Create Quiz|📚 My Quizzes|"
                r"⚙️ Settings|📖 Tutorial)$"
            ),
            menu_text,
        ),
        group=2,
    )

    # -----------------------------------------------------
    # UNKNOWN COMMAND
    # -----------------------------------------------------
    # IMPORTANT:
    # Same group, registered AFTER valid commands.
    # Therefore valid commands are not reported as unknown.

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=0,
    )
</textarea>

</div>

<script>
function copyCode() {
    const code = document.getElementById("code").value;

    navigator.clipboard.writeText(code).then(() => {
        alert("handlers.py code copied!");
    });
}
</script>

</body>
</html>
