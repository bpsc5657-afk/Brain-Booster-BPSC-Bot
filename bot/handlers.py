"""Professional Telegram Quiz Creator Bot handlers.

Enhanced version:
- Persistent quiz drafts and active sessions in PostgreSQL
- Quiz create/edit/delete/publish workflows
- Batch TXT/JSON/CSV import
- Section quiz configuration
- Real quiz attempt engine
- Pause/resume/stop
- Fast/slow question timer
- Active Telegram poll tracking
- Mistakes and bookmarks
- Telegram-safe message splitting
- Safer Testbook URL validation/import fallback
- 20 public commands
- Startup schema initialization helper

NOTE:
This module is designed for python-telegram-bot v20+ and asyncpg-style
PostgreSQL pools.
"""

import asyncio
import csv
import io
import json
import logging
import re
from html import escape
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
# PUBLIC TELEGRAM COMMANDS — 20 commands
# =========================================================

BOT_COMMANDS = (
    ("start", "Start the bot"),
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
    ("testbook", "Import quiz from Testbook URL"),
    ("tutorial", "Show tutorials and guides"),
    ("cancel", "Cancel current process"),
    ("editquiz", "Edit a quiz"),
    ("deletequiz", "Delete a quiz"),
    ("publish", "Publish a quiz"),
)

# =========================================================
# USER STATES
# =========================================================

STATE_TITLE = "title"
STATE_SUBJECT = "subject"
STATE_TIME = "time"
STATE_NEGATIVE = "negative"
STATE_BATCH = "batch_questions"
STATE_COMPLETE = "complete"
STATE_SECTION_CONFIG = "section_config"

DRAFTS: dict[int, dict] = {}
USER_SESSIONS: dict[int, dict] = {}

TELEGRAM_TEXT_LIMIT = 3900
DEFAULT_TIMER = 30
MIN_TIMER = 5
MAX_TIMER = 300


# =========================================================
# DATABASE SCHEMA
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
    quiz_id INTEGER NOT NULL
        REFERENCES quizzes(id)
        ON DELETE CASCADE,
    question_no INTEGER NOT NULL,
    question TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    option_d TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    explanation TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(quiz_id, question_no)
);
"""

CREATE_BOOKMARKS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_bookmarks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    question_id INTEGER NOT NULL
        REFERENCES quiz_questions(id)
        ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, question_id)
);
"""

CREATE_ANSWERS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_answers (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    quiz_id INTEGER NOT NULL
        REFERENCES quizzes(id)
        ON DELETE CASCADE,
    question_id INTEGER NOT NULL
        REFERENCES quiz_questions(id)
        ON DELETE CASCADE,
    selected_answer TEXT,
    is_correct BOOLEAN NOT NULL DEFAULT FALSE,
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_SECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_sections (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL
        REFERENCES quizzes(id)
        ON DELETE CASCADE,
    section_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    time_limit INTEGER NOT NULL DEFAULT 10,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(quiz_id, section_no)
);
"""

CREATE_DRAFTS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_drafts (
    user_id BIGINT PRIMARY KEY,
    draft JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def ensure_quiz_tables(pool) -> None:
    """Create required tables. Safe to call repeatedly."""
    if pool is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(CREATE_QUIZZES_TABLE)
        await conn.execute(CREATE_QUESTIONS_TABLE)
        await conn.execute(CREATE_BOOKMARKS_TABLE)
        await conn.execute(CREATE_ANSWERS_TABLE)
        await conn.execute(CREATE_SECTIONS_TABLE)
        await conn.execute(CREATE_DRAFTS_TABLE)

        await conn.execute(
            """
            ALTER TABLE quiz_answers
            ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION
            NOT NULL DEFAULT 0;
            """
        )


# =========================================================
# GENERIC HELPERS
# =========================================================

def get_user_id(update: Update) -> int | None:
    user = update.effective_user
    return user.id if user else None


def get_message(update: Update):
    return update.effective_message


def get_pool(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get(DB_KEY)


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split long Telegram messages without exceeding safe limits."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []

    for line in text.splitlines(True):
        if sum(len(x) for x in current) + len(line) <= limit:
            current.append(line)
            continue

        if current:
            chunks.append("".join(current))
            current = []

        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]

        if line:
            current.append(line)

    if current:
        chunks.append("".join(current))

    return chunks or [""]


async def safe_reply(
    message,
    text: str,
    **kwargs,
) -> None:
    for chunk in split_text(text):
        await message.reply_text(chunk, **kwargs)


def quiz_creation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                "👁 Preview",
                callback_data="draft_preview",
            )],
            [InlineKeyboardButton(
                "💾 Save Quiz",
                callback_data="draft_save",
            )],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data="draft_cancel",
            )],
        ]
    )


def edit_keyboard(quiz_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                "✏️ Edit Questions",
                callback_data=f"edit_questions:{quiz_id}",
            )],
            [InlineKeyboardButton(
                "👁 Preview",
                callback_data=f"stored_preview:{quiz_id}",
            )],
        ]
    )


def attempt_keyboard(
    quiz_id: int,
    question_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "A",
                    callback_data=f"answer:{quiz_id}:{question_id}:A",
                ),
                InlineKeyboardButton(
                    "B",
                    callback_data=f"answer:{quiz_id}:{question_id}:B",
                ),
                InlineKeyboardButton(
                    "C",
                    callback_data=f"answer:{quiz_id}:{question_id}:C",
                ),
                InlineKeyboardButton(
                    "D",
                    callback_data=f"answer:{quiz_id}:{question_id}:D",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔖 Bookmark",
                    callback_data=f"bookmark:{question_id}",
                ),
                InlineKeyboardButton(
                    "⏭ Skip",
                    callback_data=f"skip:{quiz_id}:{question_id}",
                ),
            ],
        ]
    )


# =========================================================
# DRAFT PERSISTENCE
# =========================================================

async def persist_draft(
    user_id: int,
    draft: dict,
    pool,
) -> None:
    if pool is None:
        return

    await ensure_quiz_tables(pool)

    await pool.execute(
        """
        INSERT INTO quiz_drafts(user_id, draft, updated_at)
        VALUES($1, $2::jsonb, now())
        ON CONFLICT(user_id)
        DO UPDATE SET
            draft = EXCLUDED.draft,
            updated_at = now();
        """,
        user_id,
        json.dumps(draft, ensure_ascii=False),
    )


async def delete_persisted_draft(
    user_id: int,
    pool,
) -> None:
    if pool is None:
        return

    await pool.execute(
        "DELETE FROM quiz_drafts WHERE user_id = $1",
        user_id,
    )


async def restore_draft(
    user_id: int,
    pool,
) -> dict | None:
    if pool is None:
        return None

    await ensure_quiz_tables(pool)

    row = await pool.fetchrow(
        """
        SELECT draft
        FROM quiz_drafts
        WHERE user_id = $1
        """,
        user_id,
    )

    if not row:
        return None

    draft = row["draft"]

    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except json.JSONDecodeError:
            return None

    if not isinstance(draft, dict):
        return None

    DRAFTS[user_id] = draft
    return draft


# =========================================================
# START / HELP / TUTORIAL
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    pool = get_pool(context)

    if pool is not None:
        try:
            await ensure_quiz_tables(pool)
        except Exception:
            logger.exception("Failed to initialize quiz tables")

        try:
            await db.upsert_user(
                pool,
                user.id,
                user.username,
                user.first_name,
            )
        except Exception:
            logger.exception("Failed to upsert user")

        if user.id not in DRAFTS:
            try:
                await restore_draft(user.id, pool)
            except Exception:
                logger.exception("Failed to restore draft")

    keyboard = ReplyKeyboardMarkup(
        [
            ["➕ Create Quiz", "📚 My Quizzes"],
            ["⚙️ Settings", "📖 Tutorial"],
        ],
        resize_keyboard=True,
    )

    name = escape(user.first_name or "Student")

    await message.reply_text(
        f"<b>🎯 BPSC Quiz Bot</b>\n\n"
        f"Welcome, {name}!\n\n"
        "Create, manage and attempt professional quizzes.\n\n"
        "➕ /create — Create quiz\n"
        "📚 /myquizzes — Your quizzes\n"
        "🎯 /help — All commands",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)

    if not message:
        return

    text = """
<b>🎯 Professional Quiz Creator Bot</b>

<b>Quiz Creation</b>
/create — Create a new quiz
/myquizzes — View your quizzes
/settings — Configure quiz settings
/section — Create timed sections
/editquiz ID — Edit your quiz
/deletequiz ID — Delete your quiz
/publish ID — Publish your quiz

<b>Quiz Control</b>
/stop — Stop current quiz
/pause — Pause current quiz
/resume — Resume paused quiz
/fast — Decrease timer
/slow — Increase timer
/stoppoll — Stop active poll

<b>Review</b>
/mistakes — Review incorrect answers
/bookmarks — Review bookmarks

<b>Import & Learning</b>
/testbook URL — Import from Testbook
/tutorial — Tutorials and guides

<b>General</b>
/start — Start bot
/help — Show all commands
/cancel — Cancel current process

<b>Attempt</b>
/attempt ID — Attempt a published quiz
"""

    await safe_reply(
        message,
        text,
        parse_mode="HTML",
    )


async def tutorial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)

    if not message:
        return

    text = """
<b>📖 Quiz Creator Tutorial</b>

<b>1. Create</b>
<code>/create</code>

Enter:
• Title
• Subject or skip
• Time limit
• Negative marking
• Questions

<b>2. Batch format</b>

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

<b>3. Files</b>
Supported: .txt, .json, .csv

<b>4. Save / Publish</b>
Preview → Save Quiz → Publish ID

<b>5. Attempt</b>
Published quiz का ID मिलने पर:

<code>/attempt ID</code>

Use the buttons to answer, bookmark, pause or stop.
"""

    await safe_reply(
        message,
        text,
        parse_mode="HTML",
    )


# =========================================================
# CREATE / SECTION CREATION
# =========================================================

async def create_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    draft = {
        "state": STATE_TITLE,
        "title": "",
        "subject": "",
        "question_count": 0,
        "time_limit": DEFAULT_TIMER,
        "negative_marking": 0,
        "questions": [],
        "sections": [],
        "section_mode": False,
    }

    DRAFTS[user.id] = draft

    await persist_draft(
        user.id,
        draft,
        get_pool(context),
    )

    await message.reply_text(
        "<b>📝 CREATE NEW QUIZ</b>\n\n"
        "Quiz का title भेजें.\n\n"
        "Example:\n"
        "<code>BPSC General Studies Mock Test 01</code>\n\n"
        "❌ /cancel",
        parse_mode="HTML",
    )


async def section_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    draft = {
        "state": STATE_TITLE,
        "title": "",
        "subject": "",
        "question_count": 0,
        "time_limit": DEFAULT_TIMER,
        "negative_marking": 0,
        "questions": [],
        "sections": [],
        "section_mode": True,
    }

    DRAFTS[user.id] = draft

    await persist_draft(
        user.id,
        draft,
        get_pool(context),
    )

    await message.reply_text(
        "<b>🧩 SECTION QUIZ</b>\n\n"
        "Quiz title भेजें.\n\n"
        "फिर subject, total time और negative marking आएगी।\n"
        "उसके बाद section configuration भेजें:\n\n"
        "<code>Section 1 | Ancient History | 10</code>\n"
        "<code>Section 2 | Polity | 15</code>\n"
        "<code>Section 3 | Bihar Special | 10</code>\n\n"
        "आखिर में सभी questions import करें.\n\n"
        "❌ /cancel",
        parse_mode="HTML",
    )


# =========================================================
# BATCH QUESTION PARSER
# =========================================================

def parse_batch_questions(
    text: str,
) -> tuple[list[dict], list[str]]:
    text = (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )

    if not text:
        return [], ["Empty input"]

    blocks = re.split(
        r"(?im)(?=^\s*(?:Q(?:uestion)?\s*\d+\s*[\.\):\-]))",
        text,
    )

    questions = []
    errors = []

    for block_number, block in enumerate(
        blocks,
        start=1,
    ):
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

        question = re.sub(
            r"(?i)^question\s*:\s*",
            "",
            question,
        ).strip()

        options = {
            "A": None,
            "B": None,
            "C": None,
            "D": None,
        }

        correct = None
        explanation_parts = []
        reading_explanation = False

        for line in lines[1:]:
            match = re.match(
                r"(?i)^explanation\s*:\s*(.*)$",
                line,
            )

            if match:
                explanation_parts.append(
                    match.group(1).strip()
                )
                reading_explanation = True
                continue

            if reading_explanation:
                explanation_parts.append(line)
                continue

            match = re.match(
                r"^([ABCD])\s*[\.\):\-]\s*(.+)$",
                line,
                re.IGNORECASE,
            )

            if match:
                options[
                    match.group(1).upper()
                ] = match.group(2).strip()
                continue

            match = re.match(
                r"(?i)^(?:correct\s*answer|correct|answer)"
                r"\s*:\s*([ABCD])\s*$",
                line,
            )

            if match:
                correct = match.group(1).upper()
                continue

        missing = []

        for key in ("A", "B", "C", "D"):
            if not options[key]:
                missing.append(key)

        if not correct:
            missing.append("correct answer")

        if missing:
            errors.append(
                f"Question block {block_number}: "
                f"missing {', '.join(missing)}"
            )
            continue

        questions.append(
            {
                "question": question,
                "option_a": options["A"],
                "option_b": options["B"],
                "option_c": options["C"],
                "option_d": options["D"],
                "correct_answer": correct,
                "explanation": " ".join(
                    explanation_parts
                ).strip(),
            }
        )

    return questions, errors


def parse_json_questions(
    text: str,
) -> tuple[list[dict], list[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"Invalid JSON: {exc}"]

    if isinstance(data, dict):
        data = data.get("questions", [])

    if not isinstance(data, list):
        return [], [
            "JSON must contain a questions list."
        ]

    questions = []
    errors = []

    for index, item in enumerate(
        data,
        start=1,
    ):
        if not isinstance(item, dict):
            errors.append(
                f"Question {index}: object expected."
            )
            continue

        question = str(
            item.get("question", "")
        ).strip()

        options = item.get("options")

        if isinstance(options, dict):
            option_a = str(
                options.get("A", "")
            ).strip()
            option_b = str(
                options.get("B", "")
            ).strip()
            option_c = str(
                options.get("C", "")
            ).strip()
            option_d = str(
                options.get("D", "")
            ).strip()
        else:
            option_a = str(
                item.get("option_a", "")
            ).strip()
            option_b = str(
                item.get("option_b", "")
            ).strip()
            option_c = str(
                item.get("option_c", "")
            ).strip()
            option_d = str(
                item.get("option_d", "")
            ).strip()

        correct = str(
            item.get(
                "correct_answer",
                item.get("answer", ""),
            )
        ).strip().upper()

        explanation = str(
            item.get("explanation", "")
        ).strip()

        if not all(
            [
                question,
                option_a,
                option_b,
                option_c,
                option_d,
                correct in {"A", "B", "C", "D"},
            ]
        ):
            errors.append(
                f"Question {index}: invalid or incomplete."
            )
            continue

        questions.append(
            {
                "question": question,
                "option_a": option_a,
                "option_b": option_b,
                "option_c": option_c,
                "option_d": option_d,
                "correct_answer": correct,
                "explanation": explanation,
            }
        )

    return questions, errors


def parse_csv_questions(
    text: str,
) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(
        io.StringIO(text)
    )

    questions = []
    errors = []

    for index, row in enumerate(
        reader,
        start=1,
    ):
        def value(*keys):
            for key in keys:
                if key in row and row[key]:
                    return row[key].strip()
            return ""

        question = value(
            "question",
            "Question",
        )

        option_a = value(
            "option_a",
            "A",
            "a",
        )

        option_b = value(
            "option_b",
            "B",
            "b",
        )

        option_c = value(
            "option_c",
            "C",
            "c",
        )

        option_d = value(
            "option_d",
            "D",
            "d",
        )

        correct = value(
            "correct_answer",
            "correct",
            "answer",
        ).upper()

        explanation = value(
            "explanation",
            "Explanation",
        )

        if not all(
            [
                question,
                option_a,
                option_b,
                option_c,
                option_d,
                correct in {"A", "B", "C", "D"},
            ]
        ):
            errors.append(
                f"CSV row {index}: incomplete."
            )
            continue

        questions.append(
            {
                "question": question,
                "option_a": option_a,
                "option_b": option_b,
                "option_c": option_c,
                "option_d": option_d,
                "correct_answer": correct,
                "explanation": explanation,
            }
        )

    return questions, errors


# =========================================================
# SECTION PARSER
# =========================================================

def parse_section_line(
    line: str,
) -> dict | None:
    match = re.match(
        r"(?i)^\s*section\s+(\d+)\s*\|\s*"
        r"(.+?)\s*\|\s*(\d+)\s*$",
        line.strip(),
    )

    if not match:
        return None

    section_no = int(match.group(1))
    title = match.group(2).strip()
    time_limit = int(match.group(3))

    if section_no <= 0:
        return None

    if not title:
        return None

    if not (
        MIN_TIMER <= time_limit <= MAX_TIMER
    ):
        return None

    return {
        "section_no": section_no,
        "title": title,
        "time_limit": time_limit,
    }


# =========================================================
# DRAFT PREVIEW
# =========================================================

def format_draft_preview(
    draft: dict,
) -> str:
    title = escape(
        str(draft.get("title", "Untitled"))
    )

    subject = escape(
        str(draft.get("subject", ""))
        or "General"
    )

    questions = draft.get(
        "questions",
        [],
    )

    sections = draft.get(
        "sections",
        [],
    )

    text = (
        "<b>📋 QUIZ PREVIEW</b>\n\n"
        f"<b>Title:</b> {title}\n"
        f"<b>Subject:</b> {subject}\n"
        f"<b>Questions:</b> {len(questions)}\n"
        f"<b>Total Time:</b> "
        f"{draft.get('time_limit', DEFAULT_TIMER)} minutes\n"
        f"<b>Negative:</b> "
        f"{draft.get('negative_marking', 0)}\n"
    )

    if draft.get("section_mode"):
        text += "\n<b>Sections:</b>\n"

        for section in sections:
            text += (
                f"• Section "
                f"{section['section_no']}: "
                f"{escape(section['title'])} — "
                f"{section['time_limit']} min\n"
            )

    text += "\n"

    for index, question in enumerate(
        questions[:10],
        start=1,
    ):
        text += (
            f"<b>Q{index}.</b> "
            f"{escape(question['question'])}\n"
            f"A. {escape(question['option_a'])}\n"
            f"B. {escape(question['option_b'])}\n"
            f"C. {escape(question['option_c'])}\n"
            f"D. {escape(question['option_d'])}\n"
            f"<b>Correct:</b> "
            f"{escape(question['correct_answer'])}\n\n"
        )

    if len(questions) > 10:
        text += (
            f"... और {len(questions) - 10} questions.\n"
        )

    return text


# =========================================================
# PROCESS QUIZ CREATION
# =========================================================

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message or not message.text:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()
    state = draft.get("state")

    if state == STATE_TITLE:
        if len(text) < 3:
            await message.reply_text(
                "❌ Title कम से कम 3 characters का होना चाहिए."
            )
            return

        draft["title"] = text
        draft["state"] = STATE_SUBJECT

        await persist_draft(
            user.id,
            draft,
            get_pool(context),
        )

        await message.reply_text(
            "📚 Subject भेजें.\n\n"
            "अगर subject नहीं रखना है तो <code>skip</code> भेजें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_SUBJECT:
        draft["subject"] = (
            "" if text.lower() == "skip" else text
        )

        draft["state"] = STATE_TIME

        await persist_draft(
            user.id,
            draft,
            get_pool(context),
        )

        await message.reply_text(
            "⏱ Quiz का total time minutes में भेजें.\n"
            "Example: <code>30</code>",
            parse_mode="HTML",
        )
        return

    if state == STATE_TIME:
        try:
            time_limit = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल number भेजें. Example: 30"
            )
            return

        if not (
            MIN_TIMER <= time_limit <= 1440
        ):
            await message.reply_text(
                "❌ Time 5 से 1440 minutes के बीच होना चाहिए."
            )
            return

        draft["time_limit"] = time_limit
        draft["state"] = STATE_NEGATIVE

        await persist_draft(
            user.id,
            draft,
            get_pool(context),
        )

        await message.reply_text(
            "➖ Negative marking भेजें.\n\n"
            "Examples:\n"
            "<code>0</code> = No negative marking\n"
            "<code>0.25</code> = -0.25 per wrong answer\n"
            "<code>0.33</code> = -0.33 per wrong answer",
            parse_mode="HTML",
        )
        return

    if state == STATE_NEGATIVE:
        try:
            negative = float(text)
        except ValueError:
            await message.reply_text(
                "❌ Valid number भेजें. Example: 0.25"
            )
            return

        if negative < 0 or negative > 10:
            await message.reply_text(
                "❌ Negative marking 0 से 10 के बीच रखें."
            )
            return

        draft["negative_marking"] = negative

        if draft.get("section_mode"):
            draft["state"] = STATE_SECTION_CONFIG

            await persist_draft(
                user.id,
                draft,
                get_pool(context),
            )

            await message.reply_text(
                "<b>🧩 Section configuration</b>\n\n"
                "एक line में एक section भेजें:\n\n"
                "<code>Section 1 | Ancient History | 10</code>\n"
                "<code>Section 2 | Polity | 15</code>\n"
                "<code>Section 3 | Bihar Special | 10</code>\n\n"
                "सभी sections भेजने के बाद "
                "<code>done</code> भेजें.",
                parse_mode="HTML",
            )
        else:
            draft["state"] = STATE_BATCH

            await persist_draft(
                user.id,
                draft,
                get_pool(context),
            )

            await message.reply_text(
                "<b>📝 Questions भेजें</b>\n\n"
                "Batch format:\n\n"
                "<code>"
                "Q1. Question?\n"
                "A. Option A\n"
                "B. Option B\n"
                "C. Option C\n"
                "D. Option D\n"
                "Correct: B\n"
                "Explanation: Explanation"
                "</code>\n\n"
                "Multiple batches भेज सकते हैं.\n"
                "हर batch automatically append होगा.\n\n"
                "File भी भेज सकते हैं: TXT / JSON / CSV",
                parse_mode="HTML",
            )

        return

    if state == STATE_SECTION_CONFIG:
        if text.lower() == "done":
            if not draft.get("sections"):
                await message.reply_text(
                    "❌ कम से कम एक section add करें."
                )
                return

            draft["state"] = STATE_BATCH

            await persist_draft(
                user.id,
                draft,
                get_pool(context),
            )

            await message.reply_text(
                "✅ Sections saved.\n\n"
                "अब questions भेजें.\n"
                "TXT / JSON / CSV भी भेज सकते हैं.",
            )
            return

        section = parse_section_line(text)

        if section is None:
            await message.reply_text(
                "❌ Invalid format.\n\n"
                "Use:\n"
                "<code>Section 1 | Ancient History | 10</code>",
                parse_mode="HTML",
            )
            return

        sections = draft.setdefault(
            "sections",
            [],
        )

        existing = next(
            (
                item
                for item in sections
                if item["section_no"]
                == section["section_no"]
            ),
            None,
        )

        if existing:
            existing.update(section)
        else:
            sections.append(section)

        sections.sort(
            key=lambda item: item["section_no"]
        )

        await persist_draft(
            user.id,
            draft,
            get_pool(context),
        )

        await message.reply_text(
            f"✅ Section {section['section_no']} saved.\n"
            f"📚 {section['title']}\n"
            f"⏱ {section['time_limit']} minutes\n\n"
            "More sections भेजें या <code>done</code> भेजें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_BATCH:
        questions, errors = parse_batch_questions(text)

        if not questions:
            await message.reply_text(
                "❌ कोई valid question detect नहीं हुआ.\n\n"
                + "\n".join(errors[:10])
            )
            return

        draft.setdefault(
            "questions",
            []
        ).extend(questions)

        draft["question_count"] = len(
            draft["questions"]
        )

        await persist_draft(
            user.id,
            draft,
            get_pool(context),
        )

        response = (
            f"✅ <b>{len(questions)} questions added.</b>\n"
            f"📚 Total questions: "
            f"<b>{len(draft['questions'])}</b>\n\n"
            "आप और batch भेज सकते हैं.\n"
            "या Preview / Save Quiz चुनें."
        )

        if errors:
            response += (
                "\n\n⚠️ Skipped blocks:\n"
                + "\n".join(errors[:10])
            )

        await message.reply_text(
            response,
            parse_mode="HTML",
            reply_markup=quiz_creation_keyboard(),
        )
        return

    if state == STATE_COMPLETE:
        await message.reply_text(
            "Quiz draft complete है.\n"
            "Preview या Save Quiz button इस्तेमाल करें."
        )


# =========================================================
# FILE IMPORT
# =========================================================

async def process_quiz_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message or not message.document:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        await message.reply_text(
            "❌ पहले /create या /section से quiz शुरू करें."
        )
        return

    document = message.document
    filename = (
        document.file_name or ""
    ).lower()

    if not filename.endswith(
        (".txt", ".json", ".csv")
    ):
        await message.reply_text(
            "❌ केवल TXT, JSON या CSV files supported हैं."
        )
        return

    try:
        telegram_file = await document.get_file()
        content = await telegram_file.download_as_bytearray()
        text = bytes(content).decode(
            "utf-8",
            errors="ignore",
        )
    except Exception as exc:
        logger.exception(
            "File download failed: %s",
            exc,
        )
        await message.reply_text(
            "❌ File read नहीं हो सकी."
        )
        return

    if filename.endswith(".json"):
        questions, errors = parse_json_questions(text)
    elif filename.endswith(".csv"):
        questions, errors = parse_csv_questions(text)
    else:
        questions, errors = parse_batch_questions(text)

    if not questions:
        await message.reply_text(
            "❌ File में valid questions नहीं मिले.\n\n"
            + "\n".join(errors[:10])
        )
        return

    draft.setdefault(
        "questions",
        []
    ).extend(questions)

    draft["question_count"] = len(
        draft["questions"]
    )

    if draft.get("state") != STATE_COMPLETE:
        draft["state"] = STATE_BATCH

    await persist_draft(
        user.id,
        draft,
        get_pool(context),
    )

    response = (
        f"✅ <b>{len(questions)} questions imported.</b>\n"
        f"📚 Total questions: "
        f"<b>{len(draft['questions'])}</b>\n\n"
        "और batch/file भेज सकते हैं.\n"
        "या Preview / Save Quiz चुनें."
    )

    if errors:
        response += (
            "\n\n⚠️ Skipped:\n"
            + "\n".join(errors[:10])
        )

    await message.reply_text(
        response,
        parse_mode="HTML",
        reply_markup=quiz_creation_keyboard(),
    )


# =========================================================
# DRAFT BUTTONS
# =========================================================

async def draft_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user
    draft = DRAFTS.get(user.id)

    if not draft:
        await query.message.reply_text(
            "❌ Draft नहीं मिला."
        )
        return

    data = query.data or ""

    if data == "draft_cancel":
        DRAFTS.pop(user.id, None)

        await delete_persisted_draft(
            user.id,
            get_pool(context),
        )

        await query.message.reply_text(
            "❌ Quiz draft cancelled."
        )
        return

    if data == "draft_preview":
        preview = format_draft_preview(draft)

        await safe_reply(
            query.message,
            preview,
            parse_mode="HTML",
            reply_markup=quiz_creation_keyboard(),
        )
        return

    if data == "draft_save":
        await save_draft_to_database(
            query.message,
            user.id,
            draft,
            context,
        )
        return


# =========================================================
# SAVE QUIZ
# =========================================================

async def save_draft_to_database(
    message,
    user_id: int,
    draft: dict,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    pool = get_pool(context)

    if pool is None:
        await message.reply_text(
            "❌ Database pool available नहीं है."
        )
        return None

    questions = draft.get(
        "questions",
        [],
    )

    if not questions:
        await message.reply_text(
            "❌ Quiz में कम से कम एक question होना चाहिए."
        )
        return None

    try:
        await ensure_quiz_tables(pool)

        async with pool.acquire() as conn:
            async with conn.transaction():
                quiz_id = await conn.fetchval(
                    """
                    INSERT INTO quizzes(
                        creator_id,
                        title,
                        subject,
                        question_count,
                        time_limit,
                        negative_marking,
                        published
                    )
                    VALUES($1, $2, $3, $4, $5, $6, FALSE)
                    RETURNING id
                    """,
                    user_id,
                    draft.get(
                        "title",
                        "Untitled Quiz",
                    ),
                    draft.get(
                        "subject",
                        "",
                    ),
                    len(questions),
                    int(
                        draft.get(
                            "time_limit",
                            DEFAULT_TIMER,
                        )
                    ),
                    float(
                        draft.get(
                            "negative_marking",
                            0,
                        )
                    ),
                )

                for index, question in enumerate(
                    questions,
                    start=1,
                ):
                    await conn.execute(
                        """
                        INSERT INTO quiz_questions(
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
                        VALUES(
                            $1,$2,$3,$4,$5,$6,$7,$8,$9
                        )
                        """,
                        quiz_id,
                        index,
                        question["question"],
                        question["option_a"],
                        question["option_b"],
                        question["option_c"],
                        question["option_d"],
                        question["correct_answer"],
                        question.get(
                            "explanation",
                            "",
                        ),
                    )

                for section in draft.get(
                    "sections",
                    [],
                ):
                    await conn.execute(
                        """
                        INSERT INTO quiz_sections(
                            quiz_id,
                            section_no,
                            title,
                            time_limit
                        )
                        VALUES($1,$2,$3,$4)
                        ON CONFLICT(
                            quiz_id,
                            section_no
                        )
                        DO UPDATE SET
                            title = EXCLUDED.title,
                            time_limit = EXCLUDED.time_limit
                        """,
                        quiz_id,
                        section["section_no"],
                        section["title"],
                        section["time_limit"],
                    )

        DRAFTS.pop(user_id, None)

        await delete_persisted_draft(
            user_id,
            pool,
        )

        await message.reply_text(
            "✅ <b>Quiz saved successfully!</b>\n\n"
            f"🆔 Quiz ID: <code>{quiz_id}</code>\n"
            f"📚 Questions: {len(questions)}\n\n"
            f"Publish करने के लिए:\n"
            f"<code>/publish {quiz_id}</code>",
            parse_mode="HTML",
        )

        return quiz_id

    except Exception as exc:
        logger.exception(
            "Failed to save quiz: %s",
            exc,
        )

        await message.reply_text(
            "❌ Quiz save नहीं हो सका.\n"
            "Database error check करें."
        )

        return None
