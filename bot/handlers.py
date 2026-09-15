"""Professional generic Quiz Creator Bot handlers."""

import asyncio
import csv
import io
import json
import logging
import re
from html import escape
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import NetworkError, TimedOut, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import db

logger = logging.getLogger(__name__)

DB_KEY = "db"

# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

BOT_COMMANDS = (
    ("start", "Start the bot"),
    ("create", "Create a new quiz"),
    ("myquizzes", "View your quizzes"),
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
    ("testbook", "Import an attempted Testbook quiz"),
    ("tutorial", "Show tutorials and guides"),
    ("cancel", "Cancel current process"),
)

# =========================================================
# USER STATES
# =========================================================

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

DRAFTS: dict[int, dict] = {}

# Current quiz session settings/state.
SESSIONS: dict[int, dict] = {}

# Active Telegram polls.
ACTIVE_POLLS: dict[int, int] = {}


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

CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_settings (
    user_id BIGINT PRIMARY KEY,
    default_time_limit INTEGER NOT NULL DEFAULT 30,
    default_negative_marking DOUBLE PRECISION NOT NULL DEFAULT 0,
    default_question_timer INTEGER NOT NULL DEFAULT 30,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_SECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_sections (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    section_no INTEGER NOT NULL,
    section_name TEXT NOT NULL,
    time_limit INTEGER NOT NULL DEFAULT 10,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    score DOUBLE PRECISION NOT NULL DEFAULT 0
);
"""

CREATE_ANSWERS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_answers (
    id SERIAL PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    selected_answer TEXT,
    is_correct BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_BOOKMARKS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_bookmarks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, question_id)
);
"""


async def ensure_tables(pool) -> None:
    """Create required tables if they don't exist."""

    if pool is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(CREATE_QUIZZES_TABLE)
        await conn.execute(CREATE_QUESTIONS_TABLE)
        await conn.execute(CREATE_SETTINGS_TABLE)
        await conn.execute(CREATE_SECTIONS_TABLE)
        await conn.execute(CREATE_ATTEMPTS_TABLE)
        await conn.execute(CREATE_ANSWERS_TABLE)
        await conn.execute(CREATE_BOOKMARKS_TABLE)


# =========================================================
# HELPERS
# =========================================================

def get_pool(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get(DB_KEY)


async def cancel_user_state(user_id: int) -> None:
    DRAFTS.pop(user_id, None)
    SESSIONS.pop(user_id, None)


def main_help_text() -> str:
    return """
<b>🧠 Brain Booster Quiz Creator</b>

<b>Main Commands</b>

/start — Start the bot
/create — Create a new quiz
/myquizzes — View your quizzes
/settings — Configure quiz settings
/stop — Stop the current quiz
/help — Show all commands
/section — Create a quiz with timed sections
/mistakes — Review incorrect answers
/bookmarks — Review bookmarked questions
/pause — Pause current quiz
/resume — Resume paused quiz
/fast — Decrease question timer
/slow — Increase question timer
/stoppoll — Stop active poll
/testbook — Import an attempted Testbook quiz
/tutorial — Tutorials and guides
/cancel — Cancel creation/editing

Subjects are quiz metadata only. They are not commands.
"""


def question_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data="draft_preview",
                )
            ],
            [
                InlineKeyboardButton(
                    "➕ Add Next Question",
                    callback_data="draft_add",
                ),
                InlineKeyboardButton(
                    "💾 Finish",
                    callback_data="draft_finish",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                )
            ],
        ]
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    pool = get_pool(context)

    if pool is not None:
        await db.upsert_user(
            pool,
            user.id,
            user.username,
            user.first_name,
        )

        await ensure_tables(pool)

    await message.reply_text(
        f"👋 Welcome to <b>Brain Booster Quiz Creator</b>, "
        f"{escape(user.first_name or 'User')}!\n\n"
        "Create professional quizzes, manage questions, "
        "configure timers and review your quiz activity.\n\n"
        "Use /help to see all commands.",
        parse_mode="HTML",
    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        main_help_text(),
        parse_mode="HTML",
    )


# =========================================================
# SETTINGS
# =========================================================

async def settings_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user
    pool = get_pool(context)

    if message is None or user is None:
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connection available नहीं है।"
        )
        return

    await ensure_tables(pool)

    row = await pool.fetchrow(
        """
        SELECT default_time_limit,
               default_negative_marking,
               default_question_timer
        FROM quiz_settings
        WHERE user_id = $1;
        """,
        user.id,
    )

    if row is None:
        await pool.execute(
            """
            INSERT INTO quiz_settings
            (user_id, default_time_limit,
             default_negative_marking,
             default_question_timer)
            VALUES ($1, 30, 0, 30)
            ON CONFLICT (user_id) DO NOTHING;
            """,
            user.id,
        )

        time_limit = 30
        negative = 0
        timer = 30

    else:
        time_limit = row["default_time_limit"]
        negative = row["default_negative_marking"]
        timer = row["default_question_timer"]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏱ Time 30m",
                    callback_data="setting_time",
                ),
                InlineKeyboardButton(
                    "➖ Negative",
                    callback_data="setting_negative",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚡ Timer",
                    callback_data="setting_timer",
                )
            ],
        ]
    )

    await message.reply_text(
        "<b>⚙️ Quiz Settings</b>\n\n"
        f"Default quiz time: <b>{time_limit} min</b>\n"
        f"Negative marking: <b>{negative}</b>\n"
        f"Question timer: <b>{timer} sec</b>\n\n"
        "Use the buttons to change defaults.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================================================
# CREATE
# =========================================================

async def create_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
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
        "current_question": {},
    }

    await message.reply_text(
        "<b>📝 Create New Quiz</b>\n\n"
        "<b>Step 1/6</b>\n"
        "Quiz का title भेजें।",
        parse_mode="HTML",
    )


# =========================================================
# CREATE TEXT PROCESSOR
# =========================================================

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if (
        user is None
        or message is None
        or not message.text
    ):
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()

    state = draft["state"]

    # TITLE
    if state == STATE_TITLE:

        if not text:
            await message.reply_text(
                "❌ Quiz title खाली नहीं हो सकता।"
            )
            return

        draft["title"] = text
        draft["state"] = STATE_SUBJECT

        await message.reply_text(
            "<b>Step 2/6</b>\n\n"
            "Subject metadata भेजें।\n"
            "नहीं देना है तो <b>skip</b> लिखें।",
            parse_mode="HTML",
        )
        return

    # SUBJECT
    if state == STATE_SUBJECT:

        draft["subject"] = (
            "" if text.lower() == "skip" else text
        )

        draft["state"] = STATE_COUNT

        await message.reply_text(
            "<b>Step 3/6</b>\n\n"
            "कितने questions चाहिए?\n"
            "उदाहरण: <b>20</b>",
            parse_mode="HTML",
        )
        return

    # COUNT
    if state == STATE_COUNT:

        try:
            count = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल संख्या भेजें।"
            )
            return

        if not 1 <= count <= 500:
            await message.reply_text(
                "❌ Questions 1 से 500 के बीच रखें।"
            )
            return

        draft["question_count"] = count
        draft["state"] = STATE_TIME

        await message.reply_text(
            "<b>Step 4/6</b>\n\n"
            "Quiz time limit कितने मिनट का होगा?\n"
            "उदाहरण: <b>30</b>",
            parse_mode="HTML",
        )
        return

    # TIME
    if state == STATE_TIME:

        try:
            minutes = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल मिनट की संख्या भेजें।"
            )
            return

        if not 1 <= minutes <= 300:
            await message.reply_text(
                "❌ Time 1–300 मिनट के बीच रखें।"
            )
            return

        draft["time_limit"] = minutes
        draft["state"] = STATE_NEGATIVE

        await message.reply_text(
            "<b>Step 5/6</b>\n\n"
            "Negative marking भेजें:\n\n"
            "0 = No negative\n"
            "0.25 = 1/4\n"
            "0.33 = 1/3",
            parse_mode="HTML",
        )
        return

    # NEGATIVE
    if state == STATE_NEGATIVE:

        try:
            negative = float(text)
        except ValueError:
            await message.reply_text(
                "❌ उदाहरण: 0, 0.25 या 0.33"
            )
            return

        if not 0 <= negative <= 1:
            await message.reply_text(
                "❌ Value 0 से 1 के बीच रखें।"
            )
            return

        draft["negative_marking"] = negative
        draft["state"] = STATE_QUESTION

        await message.reply_text(
            "<b>Step 6/6</b>\n\n"
            "अब Question 1 भेजें:",
            parse_mode="HTML",
        )
        return

    # QUESTION
    if state == STATE_QUESTION:

        draft["current_question"] = {
            "question": text
        }

        draft["state"] = STATE_OPTION_A

        await message.reply_text(
            "🔹 Option A भेजें:"
        )
        return

    # A
    if state == STATE_OPTION_A:

        draft["current_question"]["option_a"] = text
        draft["state"] = STATE_OPTION_B

        await message.reply_text(
            "🔹 Option B भेजें:"
        )
        return

    # B
    if state == STATE_OPTION_B:

        draft["current_question"]["option_b"] = text
        draft["state"] = STATE_OPTION_C

        await message.reply_text(
            "🔹 Option C भेजें:"
        )
        return

    # C
    if state == STATE_OPTION_C:

        draft["current_question"]["option_c"] = text
        draft["state"] = STATE_OPTION_D

        await message.reply_text(
            "🔹 Option D भेजें:"
        )
        return

    # D
    if state == STATE_OPTION_D:

        draft["current_question"]["option_d"] = text
        draft["state"] = STATE_CORRECT

        await message.reply_text(
            "✅ Correct answer भेजें:\n\n"
            "<b>A / B / C / D</b>",
            parse_mode="HTML",
        )
        return

    # CORRECT
    if state == STATE_CORRECT:

        answer = text.upper()

        if answer not in {"A", "B", "C", "D"}:
            await message.reply_text(
                "❌ केवल A, B, C या D भेजें।"
            )
            return

        draft["current_question"]["correct_answer"] = answer
        draft["state"] = STATE_EXPLANATION

        await message.reply_text(
            "💡 Explanation भेजें।\n\n"
            "Explanation नहीं देना है तो <b>skip</b> लिखें।",
            parse_mode="HTML",
        )
        return

    # EXPLANATION
    if state == STATE_EXPLANATION:

        draft["current_question"]["explanation"] = (
            ""
            if text.lower() == "skip"
            else text
        )

        draft["questions"].append(
            draft["current_question"].copy()
        )

        draft["current_question"] = {}

        number = len(draft["questions"])
        required = draft["question_count"]

        if number >= required:

            draft["state"] = "complete"

            await message.reply_text(
                f"✅ <b>{number}/{required}</b> questions ready.\n\n"
                "Choose an action:",
                parse_mode="HTML",
                reply_markup=question_keyboard(),
            )

        else:

            draft["state"] = STATE_QUESTION

            await message.reply_text(
                f"✅ Question {number} saved.\n\n"
                f"अब Question {number + 1} भेजें:"
            )


# =========================================================
# FILE IMPORT
# =========================================================

def parse_uploaded_content(
    filename: str,
    content: bytes,
) -> list[dict]:

    name = filename.lower()

    if name.endswith(".json"):

        data = json.loads(
            content.decode("utf-8-sig")
        )

        if isinstance(data, dict):
            data = data.get("questions", [])

        if not isinstance(data, list):
            raise ValueError(
                "JSON format invalid है।"
            )

        return data

    if name.endswith(".csv"):

        text = content.decode(
            "utf-8-sig"
        )

        rows = csv.DictReader(
            io.StringIO(text)
        )

        questions = []

        for row in rows:

            questions.append(
                {
                    "question": row.get("question", ""),
                    "option_a": row.get("option_a", ""),
                    "option_b": row.get("option_b", ""),
                    "option_c": row.get("option_c", ""),
                    "option_d": row.get("option_d", ""),
                    "correct_answer": row.get(
                        "correct_answer",
                        "",
                    ),
                    "explanation": row.get(
                        "explanation",
                        "",
                    ),
                }
            )

        return questions

    # TXT format
    text = content.decode(
        "utf-8-sig"
    )

    blocks = re.split(
        r"\n\s*\n",
        text.strip(),
    )

    questions = []

    for block in blocks:

        lines = [
            x.strip()
            for x in block.splitlines()
            if x.strip()
        ]

        if len(lines) < 6:
            continue

        question = lines[0]

        options = {}

        for line in lines[1:]:
            match = re.match(
                r"^([ABCD])[\.\)\-:]\s*(.+)$",
                line,
                re.IGNORECASE,
            )

            if match:
                options[
                    match.group(1).lower()
                ] = match.group(2)

        correct = ""

        for line in lines:

            match = re.search(
                r"(?:correct|answer)\s*[:\-]\s*([ABCD])",
                line,
                re.IGNORECASE,
            )

            if match:
                correct = match.group(1).upper()

        questions.append(
            {
                "question": question,
                "option_a": options.get("a", ""),
                "option_b": options.get("b", ""),
                "option_c": options.get("c", ""),
                "option_d": options.get("d", ""),
                "correct_answer": correct,
                "explanation": "",
            }
        )

    return questions


async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user

    if (
        message is None
        or user is None
        or message.document is None
    ):
        return

    document = message.document
    filename = document.file_name or ""

    allowed = (
        ".txt",
        ".csv",
        ".json",
    )

    if not filename.lower().endswith(allowed):
        await message.reply_text(
            "❌ Supported files:\n"
            "• .txt\n"
            "• .csv\n"
            "• .json"
        )
        return

    try:

        telegram_file = await context.bot.get_file(
            document.file_id
        )

        data = await telegram_file.download_as_bytearray()

        questions = parse_uploaded_content(
            filename,
            bytes(data),
        )

    except Exception as exc:

        logger.exception(
            "File import failed",
            exc_info=exc,
        )

        await message.reply_text(
            "❌ File पढ़ने में समस्या हुई।\n"
            "Format check करें।"
        )
        return

    valid = []

    for q in questions:

        required = (
            "question",
            "option_a",
            "option_b",
            "option_c",
            "option_d",
            "correct_answer",
        )

        if not all(q.get(x) for x in required):
            continue

        if q["correct_answer"].upper() not in {
            "A",
            "B",
            "C",
            "D",
        }:
            continue

        valid.append(
            {
                **q,
                "correct_answer":
                    q["correct_answer"].upper(),
            }
        )

    if not valid:

        await message.reply_text(
            "❌ कोई valid question नहीं मिला।"
        )
        return

    DRAFTS[user.id] = {
        "state": "complete",
        "title": filename.rsplit(".", 1)[0],
        "subject": "",
        "question_count": len(valid),
        "time_limit": 30,
        "negative_marking": 0,
        "questions": valid,
        "sections": [],
        "current_question": {},
    }

    await message.reply_text(
        f"📥 <b>{len(valid)} questions imported.</b>\n\n"
        "Quiz title और settings बाद में edit किए जा सकते हैं।\n\n"
        "Preview या Finish करें:",
        parse_mode="HTML",
        reply_markup=question_keyboard(),
    )


# =========================================================
# PREVIEW
# =========================================================

def format_preview(
    draft: dict,
) -> str:

    text = (
        "<b>📋 QUIZ PREVIEW</b>\n\n"
        f"<b>Title:</b> "
        f"{escape(draft['title'])}\n"
        f"<b>Subject:</b> "
        f"{escape(draft['subject'] or 'Not specified')}\n"
        f"<b>Questions:</b> "
        f"{len(draft['questions'])}\n"
        f"<b>Time:</b> "
        f"{draft['time_limit']} minutes\n"
        f"<b>Negative:</b> "
        f"{draft['negative_marking']}\n\n"
    )

    for i, q in enumerate(
        draft["questions"],
        start=1,
    ):

        text += (
            f"<b>Q{i}.</b> "
            f"{escape(q['question'])}\n"
            f"A. {escape(q['option_a'])}\n"
            f"B. {escape(q['option_b'])}\n"
            f"C. {escape(q['option_c'])}\n"
            f"D. {escape(q['option_d'])}\n"
            f"✅ Correct: "
            f"{q['correct_answer']}\n"
        )

        if q.get("explanation"):
            text += (
                f"💡 "
                f"{escape(q['explanation'])}\n"
            )

        text += "\n"

        # Telegram message limit protection
        if len(text) > 3800:
            text += "\n…Preview truncated."
            break

    return text


async def preview_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        await message.reply_text(
            "कोई active draft नहीं है।\n"
            "/create से शुरू करें।"
        )
        return

    await message.reply_text(
        format_preview(draft),
        parse_mode="HTML",
    )


# =========================================================
# DRAFT CALLBACKS
# =========================================================

async def draft_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if query is None:
        return

    await query.answer()

    user = query.from_user
    draft = DRAFTS.get(user.id)

    if not draft:

        await query.message.reply_text(
            "Draft नहीं मिला। /create से नया quiz बनाएं।"
        )
        return

    action = query.data

    if action == "draft_preview":

        await query.message.reply_text(
            format_preview(draft),
            parse_mode="HTML",
        )
        return

    if action == "draft_add":

        draft["state"] = STATE_QUESTION

        await query.message.reply_text(
            f"➕ Question "
            f"{len(draft['questions']) + 1} भेजें:"
        )
        return

    if action == "draft_cancel":

        DRAFTS.pop(user.id, None)

        await query.message.reply_text(
            "❌ Quiz creation cancelled."
        )
        return

    if action == "draft_finish":

        pool = get_pool(context)

        if pool is None:

            await query.message.reply_text(
                "⚠️ Database connected नहीं है।"
            )
            return

        if not draft["questions"]:

            await query.message.reply_text(
                "❌ Quiz में कम से कम 1 question होना चाहिए।"
            )
            return

        await ensure_tables(pool)

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
                VALUES
                ($1,$2,$3,$4,$5,$6,FALSE)
                RETURNING id;
                """,
                user.id,
                draft["title"],
                draft["subject"],
                len(draft["questions"]),
                draft["time_limit"],
                draft["negative_marking"],
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
                    ($1,$2,$3,$4,$5,$6,$7,$8,$9);
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

        DRAFTS.pop(user.id, None)

        await query.message.reply_text(
            "✅ <b>Quiz created successfully!</b>\n\n"
            f"Quiz ID: <code>{quiz_id}</code>\n"
            f"Questions: {len(draft['questions'])}\n"
            f"Status: Draft\n\n"
            f"Quiz ID: <code>{quiz_id}</code> "
            "को /myquizzes में manage करें।",
            parse_mode="HTML",
        )


# =========================================================
# MY QUIZZES
# =========================================================

async def my_quizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user
    pool = get_pool(context)

    if message is None or user is None:
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है।"
        )
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT id,
               title,
               subject,
               question_count,
               time_limit,
               negative_marking,
               published
        FROM quizzes
        WHERE creator_id = $1
        ORDER BY id DESC
        LIMIT 50;
        """,
        user.id,
    )

    if not rows:

        await message.reply_text(
            "आपने अभी कोई quiz create नहीं किया है।\n\n"
            "/create से पहला quiz बनाएं।"
        )
        return

    text = "<b>📚 My Quizzes</b>\n\n"

    for row in rows:

        status = (
            "🟢 Published"
            if row["published"]
            else "🟡 Draft"
        )

        text += (
            f"<b>#{row['id']} "
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
# SECTION QUIZ
# =========================================================

async def section_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
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
        "current_question": {},
        "section_mode": True,
    }

    await message.reply_text(
        "⏱️ <b>Section Quiz</b>\n\n"
        "पहले quiz title भेजें।\n"
        "इसके बाद questions और sections configure होंगे।",
        parse_mode="HTML",
    )


# =========================================================
# STOP / PAUSE / RESUME
# =========================================================

async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    if user.id in SESSIONS:

        SESSIONS.pop(user.id, None)

        await message.reply_text(
            "⛔ Current quiz session stopped."
        )
        return

    if user.id in DRAFTS:

        DRAFTS.pop(user.id, None)

        await message.reply_text(
            "⛔ Current creation process stopped."
        )
        return

    await message.reply_text(
        "कोई active quiz/process नहीं है।"
    )


async def pause_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    session = SESSIONS.get(user.id)

    if not session:

        await message.reply_text(
            "कोई active quiz session नहीं है।"
        )
        return

    session["paused"] = True

    await message.reply_text(
        "⏸ Quiz paused.\n\n"
        "/resume से जारी करें।"
    )


async def resume_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    session = SESSIONS.get(user.id)

    if not session:

        await message.reply_text(
            "कोई paused quiz session नहीं है।"
        )
        return

    session["paused"] = False

    await message.reply_text(
        "▶️ Quiz resumed."
    )


# =========================================================
# TIMER SPEED
# =========================================================

async def fast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    session = SESSIONS.setdefault(
        user.id,
        {
            "paused": False,
            "timer": 30,
        },
    )

    session["timer"] = max(
        5,
        int(session.get("timer", 30)) - 5,
    )

    await message.reply_text(
        f"⚡ Question timer: "
        f"<b>{session['timer']} sec</b>",
        parse_mode="HTML",
    )


async def slow_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    session = SESSIONS.setdefault(
        user.id,
        {
            "paused": False,
            "timer": 30,
        },
    )

    session["timer"] = min(
        600,
        int(session.get("timer", 30)) + 5,
    )

    await message.reply_text(
        f"🐢 Question timer: "
        f"<b>{session['timer']} sec</b>",
        parse_mode="HTML",
    )


# =========================================================
# MISTAKES
# =========================================================

async def mistakes_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user
    pool = get_pool(context)

    if message is None or user is None:
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है।"
        )
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT q.question,
               a.selected_answer,
               q.correct_answer,
               q.explanation
        FROM quiz_answers a
        JOIN quiz_questions q
          ON q.id = a.question_id
        JOIN quiz_attempts at
          ON at.id = a.attempt_id
        WHERE at.user_id = $1
          AND a.is_correct = FALSE
        ORDER BY a.id DESC
        LIMIT 30;
        """,
        user.id,
    )

    if not rows:

        await message.reply_text(
            "✅ अभी कोई incorrectly answered question नहीं है।"
        )
        return

    text = "<b>❌ Incorrect Answers</b>\n\n"

    for i, row in enumerate(rows, 1):

        text += (
            f"<b>{i}. "
            f"{escape(row['question'])}</b>\n"
            f"Your answer: "
            f"{escape(row['selected_answer'] or '-')}\n"
            f"Correct: "
            f"{escape(row['correct_answer'])}\n"
        )

        if row["explanation"]:
            text += (
                f"💡 {escape(row['explanation'])}\n"
            )

        text += "\n"

    await message.reply_text(
        text[:4000],
        parse_mode="HTML",
    )


# =========================================================
# BOOKMARKS
# =========================================================

async def bookmarks_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user
    pool = get_pool(context)

    if message is None or user is None:
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है।"
        )
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT q.question,
               q.option_a,
               q.option_b,
               q.option_c,
               q.option_d
        FROM quiz_bookmarks b
        JOIN quiz_questions q
          ON q.id = b.question_id
        WHERE b.user_id = $1
        ORDER BY b.id DESC
        LIMIT 30;
        """,
        user.id,
    )

    if not rows:

        await message.reply_text(
            "🔖 अभी कोई bookmarked question नहीं है।"
        )
        return

    text = "<b>🔖 Bookmarked Questions</b>\n\n"

    for i, row in enumerate(rows, 1):

        text += (
            f"<b>{i}. "
            f"{escape(row['question'])}</b>\n"
            f"A. {escape(row['option_a'])}\n"
            f"B. {escape(row['option_b'])}\n"
            f"C. {escape(row['option_c'])}\n"
            f"D. {escape(row['option_d'])}\n\n"
        )

    await message.reply_text(
        text[:4000],
        parse_mode="HTML",
    )


# =========================================================
# STOP POLL
# =========================================================

async def stoppoll_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    poll_id = ACTIVE_POLLS.get(user.id)

    if poll_id is None:

        await message.reply_text(
            "कोई active poll registered नहीं है।"
        )
        return

    try:

        await context.bot.stop_poll(
            chat_id=message.chat_id,
            message_id=poll_id,
        )

        ACTIVE_POLLS.pop(user.id, None)

        await message.reply_text(
            "🛑 Active poll stopped."
        )

    except Exception as exc:

        logger.exception(
            "Unable to stop poll",
            exc_info=exc,
        )

        await message.reply_text(
            "❌ Active poll stop नहीं हो पाया।"
        )


# =========================================================
# TESTBOOK IMPORT
# =========================================================

def fetch_url_sync(url: str) -> str:

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
        timeout=20,
    ) as response:

        raw = response.read()

    return raw.decode(
        "utf-8",
        errors="ignore",
    )


def extract_visible_text(html: str) -> str:

    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        html,
        flags=re.I | re.S,
    )

    html = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        html,
        flags=re.I | re.S,
    )

    html = re.sub(
        r"<[^>]+>",
        " ",
        html,
    )

    html = re.sub(
        r"\s+",
        " ",
        html,
    )

    return html.strip()


async def testbook_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if not context.args:

        await message.reply_text(
            "Usage:\n"
            "/testbook TESTBOOK_URL"
        )
        return

    url = context.args[0].strip()

    if not re.match(
        r"^https?://(?:www\.)?testbook\.com/",
        url,
        re.IGNORECASE,
    ):

        await message.reply_text(
            "❌ केवल valid Testbook URL दें।"
        )
        return

    await message.reply_text(
        "🔄 Testbook page पढ़ी जा रही है..."
    )

    try:

        html = await asyncio.to_thread(
            fetch_url_sync,
            url,
        )

        text = extract_visible_text(
            html
        )

        if not text:

            raise ValueError(
                "Page content unavailable"
            )

        # Store source information in a draft.
        # We do not fabricate questions when Testbook
        # content cannot be parsed reliably.

        DRAFTS[user.id] = {
            "state": "complete",
            "title": "Testbook Import",
            "subject": "",
            "question_count": 0,
            "time_limit": 30,
            "negative_marking": 0,
            "questions": [],
            "sections": [],
            "current_question": {},
            "source_url": url,
            "source_text": text[:20000],
        }

        await message.reply_text(
            "✅ Testbook page successfully fetched.\n\n"
            "Page को source के रूप में draft में रखा गया है।\n"
            "Automatic question extraction तभी किया जाएगा "
            "जब page का question structure reliably available हो।\n\n"
            "इस version में unsupported/ambiguous HTML से "
            "गलत questions generate नहीं किए जाएंगे।"
        )

    except (
        HTTPError,
        URLError,
        TimeoutError,
        ValueError,
    ) as exc:

        logger.warning(
            "Testbook import failed: %s",
            exc,
        )

        await message.reply_text(
            "❌ Testbook page fetch नहीं हो पाया।\n"
            "URL check करें या quiz को /create से manually बनाएं।"
        )


# =========================================================
# TUTORIAL
# =========================================================

async def tutorial_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "<b>📖 Quiz Creator Tutorial</b>\n\n"
        "<b>1. New Quiz</b>\n"
        "/create → title → subject → questions → timer → "
        "negative marking → questions.\n\n"
        "<b>2. File Import</b>\n"
        "TXT, CSV या JSON file भेजें।\n\n"
        "<b>3. Preview</b>\n"
        "Questions save करने के बाद Preview देखें।\n\n"
        "<b>4. Manage</b>\n"
        "/myquizzes से अपने quizzes देखें।\n\n"
        "<b>5. Process Control</b>\n"
        "/pause, /resume, /stop और /cancel इस्तेमाल करें।\n\n"
        "<b>6. Timer</b>\n"
        "/fast और /slow से current timer बदलें।",
        parse_mode="HTML",
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    had_state = (
        user.id in DRAFTS
        or user.id in SESSIONS
    )

    await cancel_user_state(
        user.id
    )

    if had_state:

        await message.reply_text(
            "❌ Current process cancelled."
        )

    else:

        await message.reply_text(
            "कोई active creation/editing process नहीं है।"
        )


# =========================================================
# CALLBACK SETTINGS
# =========================================================

async def settings_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if query is None:
        return

    await query.answer()

    user = query.from_user
    message = query.message
    pool = get_pool(context)

    if message is None or pool is None:
        return

    await ensure_tables(pool)

    if query.data == "setting_time":

        await pool.execute(
            """
            INSERT INTO quiz_settings
            (user_id, default_time_limit)
            VALUES ($1, 60)
            ON CONFLICT (user_id)
            DO UPDATE SET
                default_time_limit = 60,
                updated_at = now();
            """,
            user.id,
        )

        await message.reply_text(
            "⏱ Default quiz time set to 60 minutes."
        )

    elif query.data == "setting_negative":

        await pool.execute(
            """
            INSERT INTO quiz_settings
            (user_id, default_negative_marking)
            VALUES ($1, 0.25)
            ON CONFLICT (user_id)
            DO UPDATE SET
                default_negative_marking = 0.25,
                updated_at = now();
            """,
            user.id,
        )

        await message.reply_text(
            "➖ Default negative marking set to 0.25."
        )

    elif query.data == "setting_timer":

        await pool.execute(
            """
            INSERT INTO quiz_settings
            (user_id, default_question_timer)
            VALUES ($1, 30)
            ON CONFLICT (user_id)
            DO UPDATE SET
                default_question_timer = 30,
                updated_at = now();
            """,
            user.id,
        )

        await message.reply_text(
            "⏱ Default question timer set to 30 seconds."
        )


# =========================================================
# UNKNOWN COMMAND
# =========================================================

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "❓ Unknown command.\n\n"
        "Use /help to see available commands."
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    error = context.error

    if isinstance(
        error,
        (
            Conflict,
            NetworkError,
            TimedOut,
        ),
    ):

        logger.warning(
            "Telegram transient error: %s",
            error,
        )
        return

    logger.exception(
        "Unhandled update error",
        exc_info=error,
    )


# =========================================================
# TELEGRAM COMMAND REGISTRATION
# =========================================================

async def set_bot_commands(
    application: Application,
) -> None:

    await application.bot.set_my_commands(
        BOT_COMMANDS
    )


# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_handlers(
    application: Application,
) -> None:

    # -----------------------------------------------------
    # REQUIRED COMMANDS ONLY
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
        CommandHandler("settings", settings_command)
    )

    application.add_handler(
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("section", section_command)
    )

    application.add_handler(
        CommandHandler("mistakes", mistakes_command)
    )

    application.add_handler(
        CommandHandler("bookmarks", bookmarks_command)
    )

    application.add_handler(
        CommandHandler("pause", pause_command)
    )

    application.add_handler(
        CommandHandler("resume", resume_command)
    )

    application.add_handler(
        CommandHandler("fast", fast_command)
    )

    application.add_handler(
        CommandHandler("slow", slow_command)
    )

    application.add_handler(
        CommandHandler("stoppoll", stoppoll_command)
    )

    application.add_handler(
        CommandHandler("testbook", testbook_command)
    )

    application.add_handler(
        CommandHandler("tutorial", tutorial_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
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
            document_handler,
        )
    )

    # -----------------------------------------------------
    # QUIZ CREATION TEXT
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=0,
    )

    # -----------------------------------------------------
    # UNKNOWN COMMAND
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=1,
    )
