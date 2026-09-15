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
- Startup schema initialization helper

Designed for python-telegram-bot v20+ and asyncpg-style PostgreSQL pools.
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
# BOT COMMANDS
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
# STATES
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
# HELPERS
# =========================================================

def get_user_id(update: Update) -> int | None:
    user = update.effective_user
    return user.id if user else None


def get_message(update: Update):
    return update.effective_message


def get_pool(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get(DB_KEY)


def split_text(
    text: str,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []

    for line in text.splitlines(True):
        current_length = sum(len(x) for x in current)

        if current_length + len(line) <= limit:
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


async def safe_bot_reply(
    bot,
    chat_id: int,
    text: str,
    **kwargs,
) -> None:
    for chunk in split_text(text):
        await bot.send_message(chat_id, chunk, **kwargs)


def quiz_creation_keyboard():
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


def attempt_keyboard(
    quiz_id: int,
    question_id: int,
):
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
        draft = json.loads(draft)

    if isinstance(draft, dict):
        DRAFTS[user_id] = draft
        return draft

    return None


# =========================================================
# START / HELP
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)
    user = update.effective_user

    if not message or not user:
        return

    pool = get_pool(context)

    if pool is not None:
        try:
            await db.upsert_user(
                pool,
                user.id,
                user.username,
                user.first_name,
            )
        except Exception:
            logger.exception("Unable to upsert user")

        await ensure_quiz_tables(pool)
        await restore_draft(user.id, pool)

    name = escape(user.first_name or "User")

    keyboard = ReplyKeyboardMarkup(
        [
            ["➕ Create Quiz", "📚 My Quizzes"],
            ["⚙️ Settings", "📖 Tutorial"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option",
    )

    await message.reply_text(
        f"<b>Welcome to Brain Booster BPSC Quiz Bot, {name}!</b>\n\n"
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
• Subject
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
Supported:
• TXT
• JSON
• CSV

<b>4. Save / Publish</b>
Preview → Save Quiz → Publish ID

<b>5. Attempt</b>
<code>/attempt ID</code>

Use the buttons to answer, bookmark,
skip, pause or stop.
"""

    await safe_reply(
        message,
        text,
        parse_mode="HTML",
    )


# =========================================================
# CREATE QUIZ
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
        "फिर subject, total time और negative marking आएगी.\n\n"
        "Section format:\n"
        "<code>Section 1 | Ancient History | 10</code>\n"
        "<code>Section 2 | Polity | 15</code>\n"
        "<code>Section 3 | Bihar Special | 10</code>\n\n"
        "अंत में <code>done</code> लिखें.",
        parse_mode="HTML",
    )


# =========================================================
# QUESTION PARSERS
# =========================================================

def parse_batch_questions(
    text: str,
) -> tuple[list[dict], list[str]]:
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = text.strip()

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
    text: str,
) -> dict | None:
    match = re.match(
        r"(?i)^\s*section\s*(\d+)\s*\|\s*(.+?)"
        r"\s*\|\s*(\d+)\s*$",
        text,
    )

    if not match:
        return None

    section_no = int(
        match.group(1)
    )

    title = match.group(2).strip()

    minutes = int(
        match.group(3)
    )

    if (
        section_no < 1
        or minutes < 1
        or minutes > 300
        or not title
    ):
        return None

    return {
        "section_no": section_no,
        "title": title,
        "time_limit": minutes,
    }


# =========================================================
# CREATE PROCESSOR
# =========================================================

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if (
        not user
        or not message
        or not message.text
    ):
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()
    state = draft.get("state")
    pool = get_pool(context)

    if text.lower() == "/cancel":
        await cancel(update, context)
        return

    if state == STATE_TITLE:
        if len(text) < 2:
            await message.reply_text(
                "❌ Quiz title बहुत छोटा है."
            )
            return

        draft["title"] = text
        draft["state"] = STATE_SUBJECT

        await persist_draft(
            user.id,
            draft,
            pool,
        )

        await message.reply_text(
            "📚 <b>Subject</b> भेजें.\n"
            "Subject नहीं देना है तो "
            "<code>skip</code> लिखें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_SUBJECT:
        draft["subject"] = (
            ""
            if text.lower() == "skip"
            else text
        )

        draft["state"] = STATE_TIME

        await persist_draft(
            user.id,
            draft,
            pool,
        )

        await message.reply_text(
            "⏱️ <b>Time Limit</b>\n\n"
            "Quiz कितने minutes का होगा?\n"
            "Example: <code>30</code>",
            parse_mode="HTML",
        )
        return

    if state == STATE_TIME:
        try:
            minutes = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल number भेजें. Example: 30"
            )
            return

        if not 1 <= minutes <= 300:
            await message.reply_text(
                "❌ Time 1 से 300 minutes के बीच रखें."
            )
            return

        draft["time_limit"] = minutes

        if draft.get("section_mode"):
            draft["state"] = STATE_SECTION_CONFIG

            await persist_draft(
                user.id,
                draft,
                pool,
            )

            await message.reply_text(
                "🧩 <b>Sections configure करें</b>\n\n"
                "हर section अलग line में भेजें:\n\n"
                "<code>Section 1 | Ancient History | 10</code>\n"
                "<code>Section 2 | Polity | 15</code>\n"
                "<code>Section 3 | Bihar Special | 10</code>\n\n"
                "जब sections पूरे हो जाएँ तो "
                "<code>done</code> लिखें.",
                parse_mode="HTML",
            )
        else:
            draft["state"] = STATE_NEGATIVE

            await persist_draft(
                user.id,
                draft,
                pool,
            )

            await message.reply_text(
                "➖ <b>Negative Marking</b>\n\n"
                "0 = No negative\n"
                "0.25 = 1/4\n"
                "0.33 = 1/3\n\n"
                "Example: <code>0.25</code>",
                parse_mode="HTML",
            )

        return

    if state == STATE_SECTION_CONFIG:
        if text.lower() == "done":
            if not draft.get("sections"):
                await message.reply_text(
                    "❌ कम से कम 1 section जोड़ें."
                )
                return

            draft["state"] = STATE_NEGATIVE

            await persist_draft(
                user.id,
                draft,
                pool,
            )

            await message.reply_text(
                "➖ Negative marking भेजें.\n"
                "Example: <code>0.25</code>",
                parse_mode="HTML",
            )
            return

        section = parse_section_line(text)

        if not section:
            await message.reply_text(
                "❌ Format गलत है.\n\n"
                "Example:\n"
                "<code>Section 1 | Ancient History | 10</code>",
                parse_mode="HTML",
            )
            return

        existing = {
            s["section_no"]
            for s in draft.get(
                "sections",
                [],
            )
        }

        if section["section_no"] in existing:
            await message.reply_text(
                "❌ Section number already exists."
            )
            return

        draft.setdefault(
            "sections",
            [],
        ).append(section)

        draft["sections"].sort(
            key=lambda x: x["section_no"]
        )

        await persist_draft(
            user.id,
            draft,
            pool,
        )

        await message.reply_text(
            f"✅ Section {section['section_no']} added: "
            f"{escape(section['title'])} — "
            f"{section['time_limit']} min",
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
        draft["state"] = STATE_BATCH

        await persist_draft(
            user.id,
            draft,
            pool,
        )

        await message.reply_text(
            "<b>📋 अब सारे questions एक साथ भेजें.</b>\n\n"
            "<code>"
            "Q1. भारतीय संविधान का अनुच्छेद 32 किससे संबंधित है?\n"
            "A. समानता का अधिकार\n"
            "B. संवैधानिक उपचार का अधिकार\n"
            "C. स्वतंत्रता का अधिकार\n"
            "D. शिक्षा का अधिकार\n"
            "Correct: B\n"
            "Explanation: अनुच्छेद 32 संवैधानिक उपचार के अधिकार से संबंधित है.\n\n"
            "Q2. बिहार की राजधानी क्या है?\n"
            "A. गया\n"
            "B. पटना\n"
            "C. भागलपुर\n"
            "D. दरभंगा\n"
            "Correct: B\n"
            "Explanation: पटना बिहार की राजधानी है."
            "</code>\n\n"
            "या .txt / .json / .csv file भेजें.",
            parse_mode="HTML",
        )
        return

    if state == STATE_BATCH:
        questions, errors = parse_batch_questions(text)

        if not questions:
            await message.reply_text(
                "❌ कोई valid question नहीं मिला."
            )
            return

        draft.setdefault(
            "questions",
            [],
        ).extend(questions)

        draft["question_count"] = len(
            draft["questions"]
        )

        draft["state"] = STATE_COMPLETE

        await persist_draft(
            user.id,
            draft,
            pool,
        )

        result = (
            f"✅ <b>{len(questions)} questions added.</b>\n"
            f"📚 Total: <b>{draft['question_count']}</b>"
        )

        if errors:
            result += (
                "\n\n⚠️ Errors:\n"
                + "\n".join(
                    f"• {escape(x)}"
                    for x in errors[:10]
                )
            )

        result += "\n\nPreview या Save Quiz चुनें."

        await message.reply_text(
            result,
            parse_mode="HTML",
            reply_markup=quiz_creation_keyboard(),
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
            "❌ पहले /create या /section शुरू करें."
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
            "❌ केवल .txt, .json या .csv files supported हैं."
        )
        return

    if document.file_size and document.file_size > 10 * 1024 * 1024:
        await message.reply_text(
            "❌ File 10 MB से छोटी रखें."
        )
        return

    await message.reply_text(
        "⏳ File process कर रहा हूँ..."
    )

    try:
        telegram_file = await document.get_file()
        raw = await telegram_file.download_as_bytearray()
        text = bytes(raw).decode(
            "utf-8",
            errors="replace",
        )
    except Exception:
        logger.exception(
            "File download failed"
        )
        await message.reply_text(
            "❌ File read नहीं हो सकी."
        )
        return

    if filename.endswith(".json"):
        questions, errors = parse_json_questions(
            text
        )
    elif filename.endswith(".csv"):
        questions, errors = parse_csv_questions(
            text
        )
    else:
        questions, errors = parse_batch_questions(
            text
        )

    if not questions:
        await message.reply_text(
            "❌ File में valid questions नहीं मिले."
        )
        return

    draft.setdefault(
        "questions",
        [],
    ).extend(questions)

    draft["question_count"] = len(
        draft["questions"]
    )

    draft["state"] = STATE_COMPLETE

    await persist_draft(
        user.id,
        draft,
        get_pool(context),
    )

    response = (
        f"✅ <b>{len(questions)} questions imported.</b>\n"
        f"📚 Total questions: "
        f"<b>{draft['question_count']}</b>"
    )

    if errors:
        response += (
            "\n\n⚠️ कुछ rows/blocks reject हुए:\n"
            + "\n".join(
                f"• {escape(x)}"
                for x in errors[:10]
            )
        )

    response += "\n\nPreview या Save Quiz चुनें."

    await message.reply_text(
        response,
        parse_mode="HTML",
        reply_markup=quiz_creation_keyboard(),
    )


# =========================================================
# DRAFT PREVIEW
# =========================================================

def format_draft_preview(
    draft: dict,
) -> str:
    lines = [
        "<b>📋 QUIZ PREVIEW</b>",
        "",
        f"<b>Title:</b> "
        f"{escape(draft.get('title', ''))}",
        f"<b>Subject:</b> "
        f"{escape(draft.get('subject') or '—')}",
        f"<b>Questions:</b> "
        f"{draft.get('question_count', 0)}",
        f"<b>Time:</b> "
        f"{draft.get('time_limit', 30)} min",
        f"<b>Negative:</b> "
        f"{draft.get('negative_marking', 0)}",
    ]

    sections = draft.get(
        "sections",
        [],
    )

    if sections:
        lines.extend(
            [
                "",
                "<b>Sections:</b>",
            ]
        )

        for section in sections:
            lines.append(
                f"• {section['section_no']}. "
                f"{escape(section['title'])} "
                f"({section['time_limit']} min)"
            )

    lines.extend(
        [
            "",
            "<b>Question Preview:</b>",
        ]
    )

    questions = draft.get(
        "questions",
        [],
    )

    for index, q in enumerate(
        questions[:5],
        start=1,
    ):
        lines.extend(
            [
                "",
                f"<b>Q{index}.</b> "
                f"{escape(q['question'])}",
                f"A. {escape(q['option_a'])}",
                f"B. {escape(q['option_b'])}",
                f"C. {escape(q['option_c'])}",
                f"D. {escape(q['option_d'])}",
                f"Correct: <b>{q['correct_answer']}</b>",
            ]
        )

    if len(questions) > 5:
        lines.extend(
            [
                "",
                f"... और "
                f"{len(questions) - 5} questions.",
            ]
        )

    return "\n".join(lines)


async def draft_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        await query.answer(
            "Draft नहीं मिला.",
            show_alert=True,
        )
        return

    data = query.data or ""

    if data == "draft_preview":
        await query.answer()

        await safe_bot_reply(
            context.bot,
            user.id,
            format_draft_preview(draft),
            parse_mode="HTML",
        )
        return

    if data == "draft_cancel":
        await query.answer(
            "Draft cancelled."
        )

        DRAFTS.pop(
            user.id,
            None,
        )

        await delete_persisted_draft(
            user.id,
            get_pool(context),
        )

        await query.message.reply_text(
            "❌ Current quiz process cancelled."
        )
        return

    if data == "draft_save":
        await query.answer(
            "Saving..."
        )

        quiz_id = await save_draft_to_database(
            user.id,
            draft,
            get_pool(context),
        )

        if quiz_id is None:
            await query.message.reply_text(
                "❌ Quiz save नहीं हो सका."
            )
            return

        DRAFTS.pop(
            user.id,
            None,
        )

        await delete_persisted_draft(
            user.id,
            get_pool(context),
        )

        await query.message.reply_text(
            f"✅ Quiz saved successfully.\n\n"
            f"🆔 Quiz ID: <b>{quiz_id}</b>\n\n"
            f"Publish करने के लिए:\n"
            f"<code>/publish {quiz_id}</code>",
            parse_mode="HTML",
        )


# =========================================================
# SAVE QUIZ
# =========================================================

async def save_draft_to_database(
    user_id: int,
    draft: dict,
    pool,
) -> int | None:
    if pool is None:
        return None

    await ensure_quiz_tables(pool)

    questions = draft.get(
        "questions",
        [],
    )

    if not questions:
        return None

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
                VALUES($1,$2,$3,$4,$5,$6,FALSE)
                RETURNING id
                """,
                user_id,
                draft.get("title", "Untitled Quiz"),
                draft.get("subject", ""),
                len(questions),
                draft.get(
                    "time_limit",
                    DEFAULT_TIMER,
                ),
                draft.get(
                    "negative_marking",
                    0,
                ),
            )

            for number, question in enumerate(
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
                    number,
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
                    """,
                    quiz_id,
                    section["section_no"],
                    section["title"],
                    section["time_limit"],
                )

    return quiz_id


# =========================================================
# MY QUIZZES
# =========================================================

async def my_quizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    rows = await pool.fetch(
        """
        SELECT
            id,
            title,
            subject,
            question_count,
            time_limit,
            negative_marking,
            published,
            created_at
        FROM quizzes
        WHERE creator_id = $1
        ORDER BY id DESC
        LIMIT 50
        """,
        user.id,
    )

    if not rows:
        await message.reply_text(
            "📚 अभी आपका कोई quiz नहीं है.\n\n"
            "/create से नया quiz बनाइए."
        )
        return

    lines = [
        "<b>📚 YOUR QUIZZES</b>",
        "",
    ]

    for row in rows:
        status = (
            "🟢 Published"
            if row["published"]
            else "🟡 Draft"
        )

        lines.extend(
            [
                f"<b>#{row['id']} "
                f"{escape(row['title'])}</b>",
                f"📚 Questions: "
                f"{row['question_count']}",
                f"⏱ Time: "
                f"{row['time_limit']} min",
                f"➖ Negative: "
                f"{row['negative_marking']}",
                f"Status: {status}",
                f"🎯 Attempt: "
                f"<code>/attempt {row['id']}</code>",
                "",
            ]
        )

    await safe_reply(
        message,
        "\n".join(lines),
        parse_mode="HTML",
    )


# =========================================================
# SETTINGS
# =========================================================

async def settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)

    if not message:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏱ Timer 30s",
                    callback_data="setting_timer:30",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ Timer 60s",
                    callback_data="setting_timer:60",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ Timer 90s",
                    callback_data="setting_timer:90",
                )
            ],
        ]
    )

    await message.reply_text(
        "<b>⚙️ Settings</b>\n\n"
        "Default question timer चुनें.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def settings_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    data = query.data or ""

    if not data.startswith(
        "setting_timer:"
    ):
        await query.answer()
        return

    try:
        timer = int(
            data.split(":", 1)[1]
        )
    except ValueError:
        await query.answer(
            "Invalid timer.",
            show_alert=True,
        )
        return

    timer = max(
        MIN_TIMER,
        min(MAX_TIMER, timer),
    )

    session = USER_SESSIONS.get(user.id)

    if session:
        session["question_timer"] = timer

    await query.answer(
        f"Timer set to {timer}s."
    )

    await query.message.reply_text(
        f"✅ Question timer: {timer} seconds."
    )


# =========================================================
# ATTEMPT ENGINE
# =========================================================

async def attempt_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if not context.args:
        await message.reply_text(
            "Usage:\n"
            "<code>/attempt QUIZ_ID</code>",
            parse_mode="HTML",
        )
        return

    try:
        quiz_id = int(
            context.args[0]
        )
    except ValueError:
        await message.reply_text(
            "❌ Invalid quiz ID."
        )
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    quiz = await pool.fetchrow(
        """
        SELECT
            id,
            title,
            subject,
            question_count,
            time_limit,
            negative_marking
        FROM quizzes
        WHERE id = $1
          AND published = TRUE
        """,
        quiz_id,
    )

    if not quiz:
        await message.reply_text(
            "❌ Published quiz नहीं मिला."
        )
        return

    questions = await pool.fetch(
        """
        SELECT
            id,
            question_no,
            question,
            option_a,
            option_b,
            option_c,
            option_d,
            correct_answer,
            explanation
        FROM quiz_questions
        WHERE quiz_id = $1
        ORDER BY question_no
        """,
        quiz_id,
    )

    if not questions:
        await message.reply_text(
            "❌ Quiz में questions नहीं हैं."
        )
        return

    old_session = USER_SESSIONS.get(
        user.id
    )

    if old_session:
        timer_task = old_session.get(
            "timer_task"
        )

        if timer_task:
            timer_task.cancel()

    USER_SESSIONS[user.id] = {
        "quiz_id": quiz_id,
        "questions": [
            dict(q)
            for q in questions
        ],
        "index": 0,
        "score": 0.0,
        "correct": 0,
        "wrong": 0,
        "skipped": 0,
        "answered": set(),
        "status": "running",
        "question_timer": DEFAULT_TIMER,
        "negative_marking": float(
            quiz["negative_marking"] or 0
        ),
        "started_at": asyncio.get_running_loop().time(),
        "timer_task": None,
        "active_poll_id": None,
        "active_poll_chat_id": None,
        "active_poll_message_id": None,
        "title": quiz["title"],
        "total_time": quiz["time_limit"],
    }

    await message.reply_text(
        f"🎯 <b>{escape(quiz['title'])}</b>\n\n"
        f"Questions: {len(questions)}\n"
        f"Negative: {quiz['negative_marking']}\n\n"
        "Quiz शुरू हो गया.",
        parse_mode="HTML",
    )

    await send_current_question(
        update,
        context,
    )


async def send_current_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user

    if not user:
        return

    await send_current_question_to_user(
        user.id,
        context,
    )


async def send_current_question_to_user(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    session = USER_SESSIONS.get(
        user_id
    )

    if not session:
        return

    if session.get("status") == "paused":
        return

    questions = session["questions"]
    index = session["index"]

    if index >= len(questions):
        await finish_quiz_to_user(
            user_id,
            context,
        )
        return

    question = questions[index]
    question_id = question["id"]

    timer_task = session.get(
        "timer_task"
    )

    if timer_task:
        timer_task.cancel()

    text = (
        f"<b>Question {index + 1}/"
        f"{len(questions)}</b>\n\n"
        f"{escape(question['question'])}\n\n"
        f"A. {escape(question['option_a'])}\n"
        f"B. {escape(question['option_b'])}\n"
        f"C. {escape(question['option_c'])}\n"
        f"D. {escape(question['option_d'])}\n\n"
        f"⏱ {session['question_timer']} seconds"
    )

    await safe_bot_reply(
        context.bot,
        user_id,
        text,
        parse_mode="HTML",
        reply_markup=attempt_keyboard(
            session["quiz_id"],
            question_id,
        ),
    )

    session["timer_task"] = asyncio.create_task(
        question_timeout(
            user_id,
            session["quiz_id"],
            question_id,
            session["question_timer"],
            context,
        )
    )


async def question_timeout(
    user_id: int,
    quiz_id: int,
    question_id: int,
    seconds: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return

    session = USER_SESSIONS.get(
        user_id
    )

    if not session:
        return

    if session.get("status") != "running":
        return

    if session.get("quiz_id") != quiz_id:
        return

    index = session.get("index", 0)

    if index >= len(
        session["questions"]
    ):
        return

    current = session["questions"][index]

    if current["id"] != question_id:
        return

    if question_id in session["answered"]:
        return

    session["answered"].add(
        question_id
    )

    session["skipped"] += 1
    session["index"] += 1

    await safe_bot_reply(
        context.bot,
        user_id,
        "⏰ Time up! Question skipped.",
    )

    await send_current_question_to_user(
        user_id,
        context,
    )


# =========================================================
# ANSWER CALLBACK
# =========================================================

async def handle_answer_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    pool = get_pool(context)

    if not query or not user:
        return

    try:
        _, quiz_id, question_id, answer = (
            query.data.split(":")
        )

        quiz_id = int(quiz_id)
        question_id = int(question_id)
        answer = answer.upper()

    except Exception:
        await query.answer(
            "Invalid answer.",
            show_alert=True,
        )
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await query.answer(
            "No active quiz.",
            show_alert=True,
        )
        return

    if session["quiz_id"] != quiz_id:
        await query.answer(
            "This is not your current quiz.",
            show_alert=True,
        )
        return

    index = session["index"]

    if index >= len(
        session["questions"]
    ):
        await query.answer()
        return

    question = session["questions"][index]

    if question["id"] != question_id:
        await query.answer(
            "This question is no longer active.",
            show_alert=True,
        )
        return

    if question_id in session["answered"]:
        await query.answer(
            "Already answered.",
            show_alert=True,
        )
        return

    session["answered"].add(
        question_id
    )

    timer_task = session.get(
        "timer_task"
    )

    if timer_task:
        timer_task.cancel()

    correct_answer = (
        question["correct_answer"]
        .upper()
    )

    is_correct = (
        answer == correct_answer
    )

    if is_correct:
        score = 1.0
        session["correct"] += 1
        result = "✅ Correct!"
    else:
        negative = float(
            session.get(
                "negative_marking",
                0,
            )
        )

        score = -negative
        session["wrong"] += 1
        result = (
            f"❌ Wrong!\n"
            f"Correct answer: "
            f"<b>{correct_answer}</b>"
        )

    session["score"] += score

    if pool is not None:
        try:
            await pool.execute(
                """
                INSERT INTO quiz_answers(
                    user_id,
                    quiz_id,
                    question_id,
                    selected_answer,
                    is_correct,
                    score
                )
                VALUES($1,$2,$3,$4,$5,$6)
                """,
                user.id,
                quiz_id,
                question_id,
                answer,
                is_correct,
                score,
            )
        except Exception:
            logger.exception(
                "Unable to save answer"
            )

    await query.answer(
        "Correct!" if is_correct else "Wrong!"
    )

    explanation = question.get(
        "explanation",
        "",
    )

    response = (
        f"{result}\n\n"
        f"Score: <b>{score:+.2f}</b>"
    )

    if explanation:
        response += (
            f"\n\n<b>Explanation:</b>\n"
            f"{escape(explanation)}"
        )

    try:
        await query.message.reply_text(
            response,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "Unable to send answer result"
        )

    session["index"] += 1

    await send_current_question_to_user(
        user.id,
        context,
    )


# =========================================================
# SKIP
# =========================================================

async def skip_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    try:
        _, quiz_id, question_id = (
            query.data.split(":")
        )

        quiz_id = int(quiz_id)
        question_id = int(question_id)

    except Exception:
        await query.answer(
            "Invalid skip.",
            show_alert=True,
        )
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await query.answer(
            "No active quiz.",
            show_alert=True,
        )
        return

    if session["quiz_id"] != quiz_id:
        await query.answer(
            "Invalid quiz.",
            show_alert=True,
        )
        return

    index = session["index"]

    if index >= len(
        session["questions"]
    ):
        return

    question = session["questions"][index]

    if question["id"] != question_id:
        await query.answer(
            "Question expired.",
            show_alert=True,
        )
        return

    if question_id in session["answered"]:
        await query.answer(
            "Already processed.",
            show_alert=True,
        )
        return

    session["answered"].add(
        question_id
    )

    timer_task = session.get(
        "timer_task"
    )

    if timer_task:
        timer_task.cancel()

    session["skipped"] += 1
    session["index"] += 1

    await query.answer(
        "Question skipped."
    )

    await query.message.reply_text(
        "⏭ Question skipped."
    )

    await send_current_question_to_user(
        user.id,
        context,
    )


# =========================================================
# FINISH QUIZ
# =========================================================

async def finish_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user

    if not user:
        return

    await finish_quiz_to_user(
        user.id,
        context,
    )


async def finish_quiz_to_user(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    session = USER_SESSIONS.get(
        user_id
    )

    if not session:
        return

    timer_task = session.get(
        "timer_task"
    )

    if timer_task:
        timer_task.cancel()

    session["status"] = "finished"

    total = len(
        session["questions"]
    )

    await safe_bot_reply(
        context.bot,
        user_id,
        (
            "<b>🏁 QUIZ FINISHED</b>\n\n"
            f"📚 {escape(session['title'])}\n"
            f"Total: {total}\n"
            f"✅ Correct: {session['correct']}\n"
            f"❌ Wrong: {session['wrong']}\n"
            f"⏭ Skipped: {session['skipped']}\n"
            f"🏆 Score: "
            f"<b>{session['score']:.2f}</b>"
        ),
        parse_mode="HTML",
    )


# =========================================================
# STOP / PAUSE / RESUME
# =========================================================

async def stop_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await message.reply_text(
            "ℹ️ कोई active quiz नहीं है."
        )
        return

    timer_task = session.get(
        "timer_task"
    )

    if timer_task:
        timer_task.cancel()

    session["status"] = "stopped"

    await message.reply_text(
        "🛑 Current quiz stopped."
    )


async def pause_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await message.reply_text(
            "ℹ️ कोई active quiz नहीं है."
        )
        return

    if session.get("status") != "running":
        await message.reply_text(
            "ℹ️ Quiz running नहीं है."
        )
        return

    timer_task = session.get(
        "timer_task"
    )

    if timer_task:
        timer_task.cancel()

    session["status"] = "paused"

    await message.reply_text(
        "⏸ Quiz paused.\n"
        "/resume से जारी करें."
    )


async def resume_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await message.reply_text(
            "ℹ️ कोई paused quiz नहीं है."
        )
        return

    if session.get("status") != "paused":
        await message.reply_text(
            "ℹ️ Quiz paused नहीं है."
        )
        return

    session["status"] = "running"

    await message.reply_text(
        "▶️ Quiz resumed."
    )

    await send_current_question_to_user(
        user.id,
        context,
    )


# =========================================================
# TIMER CONTROL
# =========================================================

async def fast_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await message.reply_text(
            "ℹ️ कोई active quiz नहीं है."
        )
        return

    current = int(
        session.get(
            "question_timer",
            DEFAULT_TIMER,
        )
    )

    current = max(
        MIN_TIMER,
        current - 5,
    )

    session["question_timer"] = current

    await message.reply_text(
        f"⚡ Question timer अब "
        f"<b>{current}s</b> है.",
        parse_mode="HTML",
    )


async def slow_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await message.reply_text(
            "ℹ️ कोई active quiz नहीं है."
        )
        return

    current = int(
        session.get(
            "question_timer",
            DEFAULT_TIMER,
        )
    )

    current = min(
        MAX_TIMER,
        current + 5,
    )

    session["question_timer"] = current

    await message.reply_text(
        f"🐢 Question timer अब "
        f"<b>{current}s</b> है.",
        parse_mode="HTML",
    )


# =========================================================
# BOOKMARKS
# =========================================================

async def bookmark_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    pool = get_pool(context)

    if not query or not user:
        return

    try:
        question_id = int(
            query.data.split(":", 1)[1]
        )
    except Exception:
        await query.answer(
            "Invalid question.",
            show_alert=True,
        )
        return

    if pool is None:
        await query.answer(
            "Database unavailable.",
            show_alert=True,
        )
        return

    try:
        await pool.execute(
            """
            INSERT INTO quiz_bookmarks(
                user_id,
                question_id
            )
            VALUES($1,$2)
            ON CONFLICT(user_id, question_id)
            DO NOTHING
            """,
            user.id,
            question_id,
        )

        await query.answer(
            "🔖 Bookmarked."
        )

    except Exception:
        logger.exception(
            "Bookmark failed"
        )
        await query.answer(
            "Bookmark failed.",
            show_alert=True,
        )


async def bookmarks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    rows = await pool.fetch(
        """
        SELECT
            q.id,
            q.question,
            q.option_a,
            q.option_b,
            q.option_c,
            q.option_d,
            q.correct_answer
        FROM quiz_bookmarks b
        JOIN quiz_questions q
          ON q.id = b.question_id
        WHERE b.user_id = $1
        ORDER BY b.created_at DESC
        LIMIT 50
        """,
        user.id,
    )

    if not rows:
        await message.reply_text(
            "🔖 कोई bookmarked question नहीं है."
        )
        return

    lines = [
        "<b>🔖 BOOKMARKS</b>",
        "",
    ]

    for index, row in enumerate(
        rows,
        start=1,
    ):
        lines.extend(
            [
                f"<b>{index}. "
                f"{escape(row['question'])}</b>",
                f"A. {escape(row['option_a'])}",
                f"B. {escape(row['option_b'])}",
                f"C. {escape(row['option_c'])}",
                f"D. {escape(row['option_d'])}",
                f"Correct: "
                f"<b>{row['correct_answer']}</b>",
                "",
            ]
        )

    await safe_reply(
        message,
        "\n".join(lines),
        parse_mode="HTML",
    )


# =========================================================
# MISTAKES
# =========================================================

async def mistakes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (q.id)
            q.id,
            q.question,
            q.option_a,
            q.option_b,
            q.option_c,
            q.option_d,
            q.correct_answer
        FROM quiz_answers a
        JOIN quiz_questions q
          ON q.id = a.question_id
        WHERE a.user_id = $1
          AND a.is_correct = FALSE
        ORDER BY q.id, a.answered_at DESC
        LIMIT 50
        """,
        user.id,
    )

    if not rows:
        await message.reply_text(
            "🎯 अभी कोई mistake नहीं मिली."
        )
        return

    lines = [
        "<b>❌ MISTAKES</b>",
        "",
    ]

    for index, row in enumerate(
        rows,
        start=1,
    ):
        lines.extend(
            [
                f"<b>{index}. "
                f"{escape(row['question'])}</b>",
                f"A. {escape(row['option_a'])}",
                f"B. {escape(row['option_b'])}",
                f"C. {escape(row['option_c'])}",
                f"D. {escape(row['option_d'])}",
                f"Correct: "
                f"<b>{row['correct_answer']}</b>",
                "",
            ]
        )

    await safe_reply(
        message,
        "\n".join(lines),
        parse_mode="HTML",
    )


# =========================================================
# EDIT QUIZ
# =========================================================

async def edit_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if not context.args:
        await message.reply_text(
            "Usage:\n"
            "<code>/editquiz QUIZ_ID</code>",
            parse_mode="HTML",
        )
        return

    try:
        quiz_id = int(
            context.args[0]
        )
    except ValueError:
        await message.reply_text(
            "❌ Invalid quiz ID."
        )
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    quiz = await pool.fetchrow(
        """
        SELECT *
        FROM quizzes
        WHERE id = $1
          AND creator_id = $2
        """,
        quiz_id,
        user.id,
    )

    if not quiz:
        await message.reply_text(
            "❌ Quiz नहीं मिला या यह आपका quiz नहीं है."
        )
        return

    questions = await pool.fetch(
        """
        SELECT
            question,
            option_a,
            option_b,
            option_c,
            option_d,
            correct_answer,
            explanation
        FROM quiz_questions
        WHERE quiz_id = $1
        ORDER BY question_no
        """,
        quiz_id,
    )

    sections = await pool.fetch(
        """
        SELECT
            section_no,
            title,
            time_limit
        FROM quiz_sections
        WHERE quiz_id = $1
        ORDER BY section_no
        """,
        quiz_id,
    )

    draft = {
        "state": STATE_COMPLETE,
        "title": quiz["title"],
        "subject": quiz["subject"] or "",
        "question_count": len(questions),
        "time_limit": quiz["time_limit"],
        "negative_marking": quiz["negative_marking"],
        "questions": [
            dict(q)
            for q in questions
        ],
        "sections": [
            dict(s)
            for s in sections
        ],
        "section_mode": bool(sections),
        "editing_quiz_id": quiz_id,
    }

    DRAFTS[user.id] = draft

    await persist_draft(
        user.id,
        draft,
        pool,
    )

    await message.reply_text(
        f"✏️ Quiz <b>#{quiz_id}</b> loaded.\n\n"
        f"<b>{escape(quiz['title'])}</b>\n"
        f"Questions: {len(questions)}\n\n"
        "अब नया question batch/file भेजें.\n"
        "Save Changes workflow आगे जारी रहेगा.",
        parse_mode="HTML",
        reply_markup=quiz_creation_keyboard(),
    )


# =========================================================
# DELETE QUIZ
# =========================================================

async def delete_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if not context.args:
        await message.reply_text(
            "Usage:\n"
            "<code>/deletequiz QUIZ_ID</code>",
            parse_mode="HTML",
        )
        return

    try:
        quiz_id = int(
            context.args[0]
        )
    except ValueError:
        await message.reply_text(
            "❌ Invalid quiz ID."
        )
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    result = await pool.execute(
        """
        DELETE FROM quizzes
        WHERE id = $1
          AND creator_id = $2
        """,
        quiz_id,
        user.id,
    )

    if result.endswith("1"):
        await message.reply_text(
            f"🗑 Quiz #{quiz_id} deleted."
        )
    else:
        await message.reply_text(
            "❌ Quiz नहीं मिला."
        )


# =========================================================
# PUBLISH
# =========================================================

async def publish_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)
    pool = get_pool(context)

    if not user or not message:
        return

    if not context.args:
        await message.reply_text(
            "Usage:\n"
            "<code>/publish QUIZ_ID</code>",
            parse_mode="HTML",
        )
        return

    try:
        quiz_id = int(
            context.args[0]
        )
    except ValueError:
        await message.reply_text(
            "❌ Invalid quiz ID."
        )
        return

    if pool is None:
        await message.reply_text(
            "❌ Database unavailable."
        )
        return

    count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM quiz_questions
        WHERE quiz_id = $1
        """,
        quiz_id,
    )

    if not count:
        await message.reply_text(
            "❌ Quiz में कोई question नहीं है."
        )
        return

    result = await pool.execute(
        """
        UPDATE quizzes
        SET published = TRUE
        WHERE id = $1
          AND creator_id = $2
        """,
        quiz_id,
        user.id,
    )

    if result.endswith("1"):
        await message.reply_text(
            f"🟢 Quiz #{quiz_id} published successfully.\n\n"
            f"Attempt:\n"
            f"<code>/attempt {quiz_id}</code>",
            parse_mode="HTML",
        )
    else:
        await message.reply_text(
            "❌ Quiz नहीं मिला."
        )


# =========================================================
# STOP POLL
# =========================================================

async def stop_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    session = USER_SESSIONS.get(
        user.id
    )

    if not session:
        await message.reply_text(
            "ℹ️ कोई active poll नहीं है."
        )
        return

    poll_id = session.get(
        "active_poll_id"
    )

    chat_id = session.get(
        "active_poll_chat_id"
    )

    if not poll_id or not chat_id:
        await message.reply_text(
            "ℹ️ कोई active poll नहीं है."
        )
        return

    try:
        await context.bot.stop_poll(
            chat_id=chat_id,
            message_id=session[
                "active_poll_message_id"
            ],
        )

        session["active_poll_id"] = None
        session["active_poll_chat_id"] = None
        session["active_poll_message_id"] = None

        await message.reply_text(
            "🛑 Active poll stopped."
        )

    except Exception:
        logger.exception(
            "Unable to stop poll"
        )
        await message.reply_text(
            "❌ Poll stop नहीं हो सका."
        )


# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = get_message(update)

    if not user or not message:
        return

    draft = DRAFTS.pop(
        user.id,
        None,
    )

    await delete_persisted_draft(
        user.id,
        get_pool(context),
    )

    session = USER_SESSIONS.get(
        user.id
    )

    if session:
        timer_task = session.get(
            "timer_task"
        )

        if timer_task:
            timer_task.cancel()

        USER_SESSIONS.pop(
            user.id,
            None,
        )

    if draft:
        await message.reply_text(
            "❌ Current quiz draft cancelled."
        )
    else:
        await message.reply_text(
            "ℹ️ Nothing to cancel."
        )


# =========================================================
# TESTBOOK IMPORT
# =========================================================

ALLOWED_TESTBOOK_HOSTS = {
    "testbook.com",
    "www.testbook.com",
}


def fetch_url_text(
    url: str,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; QuizBot/1.0)"
            )
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


def html_to_text(
    html: str,
) -> str:
    html = re.sub(
        r"(?is)<script.*?</script>",
        " ",
        html,
    )

    html = re.sub(
        r"(?is)<style.*?</style>",
        " ",
        html,
    )

    html = re.sub(
        r"(?i)<br\s*/?>",
        "\n",
        html,
    )

    html = re.sub(
        r"(?i)</(?:p|div|li|h1|h2|h3|h4|tr)>",
        "\n",
        html,
    )

    html = re.sub(
        r"(?s)<[^>]+>",
        " ",
        html,
    )

    replacements = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
    }

    for old, new in replacements.items():
        html = html.replace(
            old,
            new,
        )

    lines = [
        re.sub(
            r"\s+",
            " ",
            line,
        ).strip()
        for line in html.splitlines()
    ]

    return "\n".join(
        line
        for line in lines
        if line
    )


async def testbook_import(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)
    user = update.effective_user

    if not message or not user:
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
    parsed = urlparse(url)

    if parsed.scheme not in {
        "http",
        "https",
    }:
        await message.reply_text(
            "❌ Valid HTTP/HTTPS URL भेजें."
        )
        return

    hostname = (
        parsed.hostname or ""
    ).lower()

    if hostname not in ALLOWED_TESTBOOK_HOSTS:
        await message.reply_text(
            "❌ केवल Testbook domain का URL accepted है."
        )
        return

    await message.reply_text(
        "⏳ Testbook page fetch कर रहा हूँ..."
    )

    try:
        html = await asyncio.to_thread(
            fetch_url_text,
            url,
        )

        text = html_to_text(
            html
        )

        questions, errors = (
            parse_batch_questions(text)
        )

    except Exception:
        logger.exception(
            "Testbook import failed"
        )

        await message.reply_text(
            "❌ Testbook page import नहीं हो सका.\n\n"
            "Dynamic page होने पर .txt/.json/.csv "
            "export करके /create से import करें."
        )
        return

    if not questions:
        await message.reply_text(
            "❌ इस URL से structured questions detect नहीं हुए.\n\n"
            "Testbook page JavaScript से questions "
            "load करता हो तो direct HTML fetch पर्याप्त नहीं होगा."
        )
        return

    pool = get_pool(context)

    draft = DRAFTS.get(
        user.id
    )

    if not draft:
        draft = {
            "state": STATE_COMPLETE,
            "title": "Imported Testbook Quiz",
            "subject": "",
            "question_count": 0,
            "time_limit": DEFAULT_TIMER,
            "negative_marking": 0,
            "questions": [],
            "sections": [],
            "section_mode": False,
        }

    draft.setdefault(
        "questions",
        []
    ).extend(questions)

    draft["question_count"] = len(
        draft["questions"]
    )

    draft["state"] = STATE_COMPLETE
    draft["source_url"] = url

    DRAFTS[user.id] = draft

    await persist_draft(
        user.id,
        draft,
        pool,
    )

    await message.reply_text(
        f"✅ <b>{len(questions)} questions imported.</b>\n"
        f"📚 Total questions: "
        f"<b>{len(draft['questions'])}</b>\n\n"
        "Preview या Save Quiz चुनें.",
        parse_mode="HTML",
        reply_markup=quiz_creation_keyboard(),
    )


# =========================================================
# MENU BUTTONS
# =========================================================

async def menu_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)

    if not message or not message.text:
        return

    text = message.text.strip()

    if text == "➕ Create Quiz":
        await create_quiz(
            update,
            context,
        )

    elif text == "📚 My Quizzes":
        await my_quizzes(
            update,
            context,
        )

    elif text == "⚙️ Settings":
        await settings(
            update,
            context,
        )

    elif text == "📖 Tutorial":
        await tutorial(
            update,
            context,
        )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    if data.startswith("draft_"):
        await draft_buttons(
            update,
            context,
        )
        return

    if data.startswith("setting_"):
        await settings_buttons(
            update,
            context,
        )
        return

    if data.startswith("answer:"):
        await handle_answer_callback(
            update,
            context,
        )
        return

    if data.startswith("skip:"):
        await skip_callback(
            update,
            context,
        )
        return

    if data.startswith("bookmark:"):
        await bookmark_callback(
            update,
            context,
        )
        return

    await query.answer()


# =========================================================
# UNKNOWN COMMAND
# =========================================================

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)

    if not message:
        return

    await message.reply_text(
        "❌ Unknown command.\n\n"
        "/help से available commands देखें."
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
# BOT COMMAND MENU
# =========================================================

async def set_bot_commands(
    application: Application,
) -> None:
    await application.bot.set_my_commands(
        list(BOT_COMMANDS)
    )


# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_handlers(
    application: Application,
) -> None:
    command_map = {
        "start": start,
        "create": create_quiz,
        "myquizzes": my_quizzes,
        "settings": settings,
        "stop": stop_quiz,
        "help": help_command,
        "section": section_command,
        "mistakes": mistakes,
        "bookmarks": bookmarks,
        "pause": pause_quiz,
        "resume": resume_quiz,
        "fast": fast_timer,
        "slow": slow_timer,
        "stoppoll": stop_poll,
        "testbook": testbook_import,
        "tutorial": tutorial,
        "cancel": cancel,
        "editquiz": edit_quiz,
        "deletequiz": delete_quiz,
        "publish": publish_quiz,
        "attempt": attempt_quiz,
    }

    for command, handler in command_map.items():
        application.add_handler(
            CommandHandler(
                command,
                handler,
            )
        )

    application.add_handler(
        CallbackQueryHandler(
            callback_router,
            pattern=(
                r"^(draft_|setting_|answer:|"
                r"skip:|bookmark:)"
            ),
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_quiz_file,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.Regex(
                r"^(➕ Create Quiz|📚 My Quizzes|"
                r"⚙️ Settings|📖 Tutorial)$"
            ),
            menu_buttons,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=1,
    )

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=2,
    )


# =========================================================
# INITIALIZE SYSTEM
# =========================================================

async def initialize_quiz_system(
    application: Application,
) -> None:
    pool = application.bot_data.get(
        DB_KEY
    )

    if pool is not None:
        await ensure_quiz_tables(
            pool
        )

    await set_bot_commands(
        application
    )

    logger.info(
        "Quiz system initialized. Commands: %d",
        len(BOT_COMMANDS),
    )
    application.add_handler(
    MessageHandler(
            filters.COMMAND,
              unknown_command,
         ),
        group=0,
    )
