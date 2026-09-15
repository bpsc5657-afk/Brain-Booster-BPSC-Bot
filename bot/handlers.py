
"""Professional Telegram handlers for Brain Booster BPSC Quiz Bot.

- Exactly the requested Telegram command menu.
- No subject-specific commands.
- Normal users can create and edit their own quizzes.
- Batch quiz creation: paste many questions in ONE message.
- TXT / CSV / JSON batch file import.
- Optional subject metadata is stored in PostgreSQL.
- Preview, save, edit, delete and automatic publish.
- Inline quiz taking with pause/resume/fast/slow.
- Mistakes and bookmarks are persisted in PostgreSQL.
- Multi-section quiz creation.
"""

import csv
import io
import json
import logging
import os
import re
from html import escape
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# Admin ID is optional for access control; normal users can create/edit.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Session state is kept in memory. Quiz/question records are persisted in PostgreSQL.
DRAFTS: dict[int, dict] = {}
ACTIVE: dict[int, dict] = {}

# EXACT command menu requested by the user.
BOT_COMMANDS = (
    ("start", "Start the bot and show welcome message"),
    ("create", "Create a new quiz"),
    ("myquizzes", "View quizzes created by the admin"),
    ("settings", "Configure quiz settings"),
    ("stop", "Stop the current quiz"),
    ("help", "Show all commands"),
    ("section", "Create a quiz with multiple timed sections"),
    ("mistakes", "Review incorrectly answered questions"),
    ("bookmarks", "Review bookmarked questions"),
    ("pause", "Pause the current quiz"),
    ("resume", "Resume a paused quiz"),
    ("fast", "Decrease question timer"),
    ("slow", "Increase question timer"),
    ("stoppoll", "Stop an active poll"),
    ("testbook", "Import an attempted quiz from a Testbook URL"),
    ("tutorial", "Show bot tutorials and guides"),
    ("cancel", "Cancel the current creation/editing process"),
)

# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------

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
    sections JSONB,
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
    image_file_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    wrong_count INTEGER NOT NULL DEFAULT 0,
    unanswered_count INTEGER NOT NULL DEFAULT 0,
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
"""

CREATE_ANSWERS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_answers (
    id SERIAL PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    selected_answer TEXT,
    is_correct BOOLEAN,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(attempt_id, question_id)
);
"""

CREATE_BOOKMARKS_TABLE = """
CREATE TABLE IF NOT EXISTS quiz_bookmarks (
    user_id BIGINT NOT NULL,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id, question_id)
);
"""


async def ensure_tables(pool) -> None:
    if pool is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(CREATE_QUIZZES_TABLE)
        await conn.execute(CREATE_QUESTIONS_TABLE)
        await conn.execute(CREATE_ATTEMPTS_TABLE)
        await conn.execute(CREATE_ANSWERS_TABLE)
        await conn.execute(CREATE_BOOKMARKS_TABLE)


# ---------------------------------------------------------------------
# BATCH PARSER
# ---------------------------------------------------------------------

def parse_batch_questions(text: str) -> list[dict]:
    """
    Accepted format:

    Q1. Question text
    A. Option A
    B. Option B
    C. Option C
    D. Option D
    Correct: B
    Explanation: Explanation text

    Q2. ...
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not text:
        return []

    starts = list(
        re.finditer(
            r"(?im)^\s*(?:Q(?:uestion)?\s*\d+\s*[\.\):\-])\s*",
            text,
        )
    )

    if not starts:
        return []

    blocks = []

    for i, match in enumerate(starts):
        end = (
            starts[i + 1].start()
            if i + 1 < len(starts)
            else len(text)
        )
        blocks.append(text[match.end():end].strip())

    questions = []

    for block in blocks:
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

        options = {}
        correct = None
        explanation_parts = []
        reading_explanation = False

        for line in lines[1:]:

            option_match = re.match(
                r"^([ABCD])\s*[\.\):\-]\s*(.+)$",
                line,
                re.IGNORECASE,
            )

            if option_match and not reading_explanation:
                options[option_match.group(1).upper()] = (
                    option_match.group(2).strip()
                )
                continue

            correct_match = re.match(
                r"(?i)^(?:correct\s*answer|correct|answer)"
                r"\s*[:\-]\s*([ABCD])\s*$",
                line,
            )

            if correct_match and not reading_explanation:
                correct = correct_match.group(1).upper()
                continue

            explanation_match = re.match(
                r"(?i)^explanation\s*[:\-]\s*(.*)$",
                line,
            )

            if explanation_match:
                reading_explanation = True

                if explanation_match.group(1).strip():
                    explanation_parts.append(
                        explanation_match.group(1).strip()
                    )
                continue

            if reading_explanation:
                explanation_parts.append(line)

        if (
            question
            and all(letter in options for letter in "ABCD")
            and correct in {"A", "B", "C", "D"}
        ):
            questions.append(
                {
                    "question": question,
                    "option_a": options["A"],
                    "option_b": options["B"],
                    "option_c": options["C"],
                    "option_d": options["D"],
                    "correct_answer": correct,
                    "explanation": " ".join(explanation_parts).strip(),
                    "image_file_id": None,
                }
            )

    return questions


def parse_uploaded_document(
    filename: str,
    raw: bytes,
) -> list[dict]:

    filename = (filename or "").lower()
    text = raw.decode("utf-8-sig", errors="replace")

    # TXT
    if filename.endswith(".txt"):
        return parse_batch_questions(text)

    # CSV
    if filename.endswith(".csv"):

        rows = csv.DictReader(io.StringIO(text))
        questions = []

        for row in rows:

            data = {
                str(key).strip().lower(): (
                    value or ""
                ).strip()
                for key, value in row.items()
            }

            correct = data.get(
                "correct_answer",
                data.get(
                    "correct",
                    data.get("answer", ""),
                ),
            ).upper()

            if (
                data.get("question")
                and all(
                    data.get(f"option_{letter}")
                    for letter in "abcd"
                )
                and correct in {"A", "B", "C", "D"}
            ):
                questions.append(
                    {
                        "question": data["question"],
                        "option_a": data["option_a"],
                        "option_b": data["option_b"],
                        "option_c": data["option_c"],
                        "option_d": data["option_d"],
                        "correct_answer": correct,
                        "explanation": data.get(
                            "explanation",
                            "",
                        ),
                        "image_file_id": None,
                    }
                )

        return questions

    # JSON
    if filename.endswith(".json"):

        data = json.loads(text)

        if isinstance(data, dict):
            data = data.get("questions", [])

        questions = []

        if not isinstance(data, list):
            return []

        for item in data:

            if not isinstance(item, dict):
                continue

            correct = str(
                item.get(
                    "correct_answer",
                    item.get("correct", ""),
                )
            ).upper()

            if (
                item.get("question")
                and all(
                    item.get(f"option_{letter}")
                    for letter in "abcd"
                )
                and correct in {"A", "B", "C", "D"}
            ):
                questions.append(
                    {
                        "question": str(item["question"]),
                        "option_a": str(item["option_a"]),
                        "option_b": str(item["option_b"]),
                        "option_c": str(item["option_c"]),
                        "option_d": str(item["option_d"]),
                        "correct_answer": correct,
                        "explanation": str(
                            item.get("explanation", "")
                        ),
                        "image_file_id": item.get(
                            "image_file_id"
                        ),
                    }
                )

        return questions

    raise ValueError(
        "Supported files: .txt, .csv and .json"
    )


# ---------------------------------------------------------------------
# UI HELPERS
# ---------------------------------------------------------------------

def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
        )
    except Exception:
        return False


def preview_text(
    draft: dict,
    limit: int = 20,
) -> str:

    questions = draft["questions"][:limit]

    text = (
        "<b>📋 QUIZ PREVIEW</b>\n\n"
        f"<b>Title:</b> "
        f"{escape(draft['title'])}\n"
        f"<b>Subject:</b> "
        f"{escape(draft['subject'] or 'Not specified')}\n"
        f"<b>Questions:</b> "
        f"{len(draft['questions'])}\n"
        f"<b>Time:</b> "
        f"{draft['time_limit']} min\n"
        f"<b>Negative:</b> "
        f"{draft['negative_marking']}\n\n"
    )

    for number, question in enumerate(
        questions,
        1,
    ):

        text += (
            f"<b>Q{number}.</b> "
            f"{escape(question['question'])}\n"
            f"A. {escape(question['option_a'])}\n"
            f"B. {escape(question['option_b'])}\n"
            f"C. {escape(question['option_c'])}\n"
            f"D. {escape(question['option_d'])}\n"
            f"✅ Correct: "
            f"{question['correct_answer']}\n"
        )

        if question.get("explanation"):
            text += (
                f"💡 "
                f"{escape(question['explanation'])}\n"
            )

        text += "\n"

    remaining = (
        len(draft["questions"]) - limit
    )

    if remaining > 0:
        text += (
            f"... और {remaining} questions हैं.\n"
        )

    return text


def draft_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
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
        ]
    )


# ---------------------------------------------------------------------
# START / HELP / TUTORIAL
# ---------------------------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
        await ensure_tables(pool)

    await message.reply_text(
        f"Welcome to <b>Brain Booster BPSC Quiz Bot</b>, "
        f"{escape(user.first_name or 'there')}!\n\n"
        "Create, manage and attempt professional quizzes "
        "from one place.\n\n"
        "Use /create to create a quiz.",
        parse_mode="HTML",
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if not message:
        return

    text = (
        "<b>Brain Booster BPSC Quiz Bot</b>\n\n"
        "/start — Start the bot\n"
        "/create — Create a new quiz\n"
        "/myquizzes — View quizzes\n"
        "/settings — Configure quiz settings\n"
        "/stop — Stop current quiz\n"
        "/help — Show all commands\n"
        "/section — Create timed sections\n"
        "/mistakes — Review wrong answers\n"
        "/bookmarks — Review bookmarks\n"
        "/pause — Pause current quiz\n"
        "/resume — Resume paused quiz\n"
        "/fast — Decrease question timer\n"
        "/slow — Increase question timer\n"
        "/stoppoll — Stop active poll\n"
        "/testbook — Import from Testbook URL\n"
        "/tutorial — Tutorials and guides\n"
        "/cancel — Cancel creation/editing\n\n"
        "<b>Batch Quiz</b>\n\n"
        "एक quiz में सारे questions एक ही message में भेजें.\n\n"
        "<code>"
        "Q1. Question text\n"
        "A. Option A\n"
        "B. Option B\n"
        "C. Option C\n"
        "D. Option D\n"
        "Correct: B\n"
        "Explanation: Explanation text\n\n"
        "Q2. Question text\n"
        "A. Option A\n"
        "B. Option B\n"
        "C. Option C\n"
        "D. Option D\n"
        "Correct: C\n"
        "Explanation: Explanation text"
        "</code>"
    )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


async def tutorial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        "<b>📘 QUIZ CREATOR TUTORIAL</b>\n\n"
        "<b>Step 1</b>\n"
        "/create भेजें.\n\n"
        "<b>Step 2</b>\n"
        "Title, optional subject, time और negative marking दें.\n\n"
        "<b>Step 3</b>\n"
        "सारे questions ONE message में paste करें.\n\n"
        "<b>Step 4</b>\n"
        "Preview → Save.\n\n"
        "<b>Step 5</b>\n"
        "Saved published quiz को /myquizzes से Start करें.\n\n"
        "<b>File import</b>\n"
        ".txt, .csv या .json file भी /create के batch step पर upload कर सकते हैं.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------

async def create_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    DRAFTS[user.id] = {
        "state": "title",
        "title": "",
        "subject": "",
        "time_limit": 30,
        "negative_marking": 0.0,
        "questions": [],
        "sections": [],
        "section_mode": False,
        "created_by": user.id,
    }

    await message.reply_text(
        "<b>📝 CREATE NEW QUIZ</b>\n\n"
        "Quiz का title भेजें.\n\n"
        "फिर optional subject, time और negative marking "
        "पूछी जाएगी.\n\n"
        "उसके बाद सारे questions एक ही message में "
        "भेज सकते हैं.\n\n"
        "❌ रोकने के लिए /cancel",
        parse_mode="HTML",
    )


async def section_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    DRAFTS[user.id] = {
        "state": "section_title",
        "title": "",
        "subject": "",
        "time_limit": 30,
        "negative_marking": 0.0,
        "questions": [],
        "sections": [],
        "section_mode": True,
        "created_by": user.id,
    }

    await message.reply_text(
        "<b>📚 MULTI-SECTION QUIZ</b>\n\n"
        "Quiz title भेजें.\n\n"
        "फिर sections की definition और सभी questions "
        "एक batch में लिए जाएंगे.\n\n"
        "❌ रोकने के लिए /cancel",
        parse_mode="HTML",
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    if DRAFTS.pop(user.id, None):
        await message.reply_text(
            "❌ Current creation/editing process cancelled."
        )
    else:
        await message.reply_text(
            "कोई active creation/editing process नहीं है."
        )


# ---------------------------------------------------------------------
# CREATION PROCESSOR
# ---------------------------------------------------------------------

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message or not message.text:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()
    state = draft["state"]

    # TITLE
    if state in {"title", "section_title"}:

        if len(text) < 2:
            await message.reply_text(
                "❌ Title थोड़ा बड़ा रखें."
            )
            return

        draft["title"] = text
        draft["state"] = "subject"

        await message.reply_text(
            "📚 Subject भेजें.\n\n"
            "यह केवल database metadata रहेगा.\n"
            "नहीं देना है तो <code>skip</code> लिखें.",
            parse_mode="HTML",
        )
        return

    # SUBJECT
    if state == "subject":

        draft["subject"] = (
            ""
            if text.lower() == "skip"
            else text
        )

        draft["state"] = "time"

        await message.reply_text(
            "⏱️ Time limit कितने minutes का होगा?\n\n"
            "Example: <code>30</code>",
            parse_mode="HTML",
        )
        return

    # TIME
    if state == "time":

        try:
            minutes = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल minutes की संख्या भेजें."
            )
            return

        if not 1 <= minutes <= 300:
            await message.reply_text(
                "❌ Time 1–300 minutes के बीच रखें."
            )
            return

        draft["time_limit"] = minutes
        draft["state"] = "negative"

        await message.reply_text(
            "➖ Negative marking भेजें.\n\n"
            "<code>0</code> = none\n"
            "<code>0.25</code> = 1/4\n"
            "<code>0.33</code> = 1/3",
            parse_mode="HTML",
        )
        return

    # NEGATIVE
    if state == "negative":

        try:
            negative = float(text)
        except ValueError:
            await message.reply_text(
                "❌ Example: 0, 0.25 या 0.33"
            )
            return

        if not 0 <= negative <= 1:
            await message.reply_text(
                "❌ Negative marking 0–1 के बीच रखें."
            )
            return

        draft["negative_marking"] = negative

        if draft.get("section_mode"):

            draft["state"] = "section_definition"

            await message.reply_text(
                "📑 Sections इस format में भेजें:\n\n"
                "<code>"
                "Section 1 | History | 15\n"
                "Section 2 | Polity | 20"
                "</code>\n\n"
                "फिर सभी questions एक ही message में भेजें.\n"
                "Questions section order में assign होंगे.",
                parse_mode="HTML",
            )

        else:

            draft["state"] = "batch_questions"

            await message.reply_text(
                "📋 <b>अब सारे questions एक ही message में भेजें.</b>\n\n"
                "<code>"
                "Q1. Question text\n"
                "A. Option A\n"
                "B. Option B\n"
                "C. Option C\n"
                "D. Option D\n"
                "Correct: B\n"
                "Explanation: Explanation text\n\n"
                "Q2. Question text\n"
                "A. Option A\n"
                "B. Option B\n"
                "C. Option C\n"
                "D. Option D\n"
                "Correct: C\n"
                "Explanation: Explanation text"
                "</code>\n\n"
                "या .txt/.csv/.json file upload करें.",
                parse_mode="HTML",
            )

        return

    # SECTION DEFINITION
    if state == "section_definition":

        sections = []

        for line in text.splitlines():

            match = re.match(
                r"^\s*Section\s+(\d+)\s*\|\s*(.*?)\s*\|\s*(\d+)"
                r"\s*(?:min|minutes)?\s*$",
                line,
                re.IGNORECASE,
            )

            if match:

                sections.append(
                    {
                        "number": int(match.group(1)),
                        "name": match.group(2).strip(),
                        "minutes": int(match.group(3)),
                    }
                )

        if not sections:
            await message.reply_text(
                "❌ Format नहीं मिला.\n\n"
                "Example:\n"
                "Section 1 | History | 15\n"
                "Section 2 | Polity | 20"
            )
            return

        draft["sections"] = sections
        draft["state"] = "batch_questions"

        await message.reply_text(
            "✅ Sections saved.\n\n"
            "अब सभी questions एक ही message में भेजें.\n"
            "Questions section order में assign होंगे."
        )
        return

    # EDIT: replace entire question set with one batch
    if state == "edit_wait":

        questions = parse_batch_questions(text)

        if not questions:
            await message.reply_text(
                "❌ Valid batch नहीं मिला.\n"
                "Q1 + A/B/C/D + Correct format रखें."
            )
            return

        draft["questions"] = questions
        draft["state"] = "edit_complete"

        await message.reply_text(
            f"✅ {len(questions)} questions loaded for replacement.\n\n"
            "Preview या Save चुनें.",
            reply_markup=draft_keyboard(),
        )
        return

    # BATCH QUESTIONS
    if state == "batch_questions":

        questions = parse_batch_questions(text)

        if not questions:
            await message.reply_text(
                "❌ कोई valid question नहीं मिला.\n\n"
                "हर question में Q-number, A/B/C/D और "
                "Correct जरूरी है."
            )
            return

        draft["questions"] = questions
        draft["state"] = "complete"

        await message.reply_text(
            f"✅ <b>{len(questions)} questions detected.</b>\n\n"
            "Preview या Save चुनें.",
            parse_mode="HTML",
            reply_markup=draft_keyboard(),
        )


# ---------------------------------------------------------------------
# FILE IMPORT
# ---------------------------------------------------------------------

async def process_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message or not message.document:
        return

    draft = DRAFTS.get(user.id)

    if not draft or draft.get("state") not in {
        "batch_questions",
        "edit_wait",
    }:
        await message.reply_text(
            "पहले /create शुरू करें, फिर question file भेजें."
        )
        return

    document = message.document
    filename = document.file_name or "questions.txt"

    try:

        telegram_file = await document.get_file()

        raw = bytes(
            await telegram_file.download_as_bytearray()
        )

        questions = parse_uploaded_document(
            filename,
            raw,
        )

    except ValueError as exc:

        await message.reply_text(
            f"❌ {exc}"
        )
        return

    except Exception:

        logger.exception(
            "Document import failed"
        )

        await message.reply_text(
            "⚠️ File पढ़ने में समस्या हुई."
        )
        return

    if not questions:
        await message.reply_text(
            "❌ File में कोई valid question नहीं मिला."
        )
        return

    draft["questions"] = questions

    if draft.get("state") == "edit_wait":
        draft["state"] = "edit_complete"
    else:
        draft["state"] = "complete"

    await message.reply_text(
        f"✅ <b>{len(questions)} questions imported.</b>\n\n"
        "Preview या Save चुनें.",
        parse_mode="HTML",
        reply_markup=draft_keyboard(),
    )


# ---------------------------------------------------------------------
# SAVE / EDIT
# ---------------------------------------------------------------------

async def save_new_quiz(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:

    pool = context.bot_data.get(DB_KEY)
    draft = DRAFTS.get(user_id)

    if not pool or not draft or not draft["questions"]:
        return None

    await ensure_tables(pool)

    sections_json = json.dumps(
        draft.get("sections", []),
        ensure_ascii=False,
    )

    async with pool.acquire() as conn:

        # Saved quizzes are immediately published because /publish
        # is intentionally NOT one of the requested Telegram commands.
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
                published,
                sections
            )
            VALUES
            ($1,$2,$3,$4,$5,$6,TRUE,$7::jsonb)
            RETURNING id
            """,
            user_id,
            draft["title"],
            draft["subject"] or None,
            len(draft["questions"]),
            draft["time_limit"],
            draft["negative_marking"],
            sections_json,
        )

        for number, question in enumerate(
            draft["questions"],
            1,
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
                    explanation,
                    image_file_id
                )
                VALUES
                ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                quiz_id,
                number,
                question["question"],
                question["option_a"],
                question["option_b"],
                question["option_c"],
                question["option_d"],
                question["correct_answer"],
                question.get("explanation", ""),
                question.get("image_file_id"),
            )

    return int(quiz_id)


async def finish_new_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:

    message = update.effective_message
    draft = DRAFTS.get(user_id)

    if not message or not draft:
        return

    if not draft["questions"]:
        await message.reply_text(
            "❌ कम से कम 1 valid question जरूरी है."
        )
        return

    try:

        quiz_id = await save_new_quiz(
            user_id,
            context,
        )

    except Exception:

        logger.exception(
            "Could not save quiz"
        )

        await message.reply_text(
            "⚠️ Quiz save करते समय database error आया."
        )
        return

    if quiz_id is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    count = len(draft["questions"])

    DRAFTS.pop(user_id, None)

    await message.reply_text(
        f"✅ <b>Quiz saved successfully!</b>\n\n"
        f"Quiz ID: <code>{quiz_id}</code>\n"
        f"Questions: <b>{count}</b>\n\n"
        "🚀 Quiz automatically published है.\n"
        "/myquizzes से quiz Start करें.",
        parse_mode="HTML",
    )


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
            "Draft नहीं मिला. /create से शुरू करें."
        )
        return

    if query.data == "draft_preview":

        await query.message.reply_text(
            preview_text(draft),
            parse_mode="HTML",
        )
        return

    if query.data == "draft_cancel":

        DRAFTS.pop(user.id, None)

        await query.message.reply_text(
            "❌ Draft cancelled."
        )
        return

    if query.data != "draft_save":
        return

    # EDIT EXISTING QUIZ
    if (
        draft.get("state") == "edit_complete"
        and draft.get("quiz_id")
    ):

        pool = context.bot_data.get(DB_KEY)

        if not pool:
            await query.message.reply_text(
                "⚠️ Database connected नहीं है."
            )
            return

        quiz_id = int(draft["quiz_id"])

        allowed = await pool.fetchval(
            """
            SELECT EXISTS(
                SELECT 1
                FROM quizzes
                WHERE id=$1
                AND (creator_id=$2 OR $2=$3)
            )
            """,
            quiz_id,
            user.id,
            ADMIN_ID,
        )

        if not allowed:
            await query.message.reply_text(
                "❌ Edit permission denied."
            )
            return

        try:

            async with pool.acquire() as conn:

                await conn.execute(
                    """
                    DELETE FROM quiz_questions
                    WHERE quiz_id=$1
                    """,
                    quiz_id,
                )

                await conn.execute(
                    """
                    UPDATE quizzes
                    SET
                        title=$1,
                        subject=$2,
                        question_count=$3,
                        time_limit=$4,
                        negative_marking=$5,
                        published=TRUE
                    WHERE id=$6
                    """,
                    draft["title"],
                    draft["subject"] or None,
                    len(draft["questions"]),
                    draft["time_limit"],
                    draft["negative_marking"],
                    quiz_id,
                )

                for number, question in enumerate(
                    draft["questions"],
                    1,
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
                            explanation,
                            image_file_id
                        )
                        VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
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
                        question.get(
                            "image_file_id"
                        ),
                    )

        except Exception:

            logger.exception(
                "Quiz update failed"
            )

            await query.message.reply_text(
                "⚠️ Quiz update करते समय database error आया."
            )
            return

        DRAFTS.pop(user.id, None)

        await query.message.reply_text(
            f"✅ Quiz #{quiz_id} updated and published."
        )
        return

    # NEW QUIZ
    await finish_new_quiz(
        update,
        context,
        user.id,
    )


# ---------------------------------------------------------------------
# MY QUIZZES
# ---------------------------------------------------------------------

async def myquizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message
    pool = context.bot_data.get(DB_KEY)

    if not user or not message:
        return

    if not pool:
        await message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    await ensure_tables(pool)

    # Normal users see admin-created quizzes + their own.
    # Admin sees all quizzes.
    if is_admin(user.id):
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
            ORDER BY id DESC
            LIMIT 50
            """
        )
    else:
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
            WHERE creator_id=$1 OR creator_id=$2
            ORDER BY id DESC
            LIMIT 50
            """,
            user.id,
            ADMIN_ID,
        )

    if not rows:
        await message.reply_text(
            "अभी कोई quiz उपलब्ध नहीं है."
        )
        return

    for row in rows:

        status = (
            "🟢 Published"
            if row["published"]
            else "🟡 Draft"
        )

        buttons = []

        if row["published"]:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"▶️ Start #{row['id']}",
                        callback_data=(
                            f"start:{row['id']}"
                        ),
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"✏️ Edit #{row['id']}",
                    callback_data=(
                        f"edit:{row['id']}"
                    ),
                ),
                InlineKeyboardButton(
                    f"🗑 Delete #{row['id']}",
                    callback_data=(
                        f"delete:{row['id']}"
                    ),
                ),
            ]
        )

        await message.reply_text(
            f"<b>#{row['id']} — "
            f"{escape(row['title'])}</b>\n"
            f"Subject: "
            f"{escape(row['subject'] or '—')}\n"
            f"Questions: "
            f"{row['question_count']}\n"
            f"Time: "
            f"{row['time_limit']} min\n"
            f"Negative: "
            f"{row['negative_marking']}\n"
            f"Status: {status}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )


# ---------------------------------------------------------------------
# SETTINGS / QUIZ CONTROL
# ---------------------------------------------------------------------

async def settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    draft = DRAFTS.get(user.id)
    active = ACTIVE.get(user.id)

    if draft:

        await message.reply_text(
            "⚙️ <b>Draft Settings</b>\n\n"
            f"Time: {draft['time_limit']} min\n"
            f"Negative: {draft['negative_marking']}\n\n"
            "Creation wizard के दौरान इन settings को configure किया जाता है.",
            parse_mode="HTML",
        )
        return

    if active:

        await message.reply_text(
            "⚙️ <b>Active Quiz Settings</b>\n\n"
            f"Timer: {active['timer_seconds']} sec/question\n"
            f"Paused: "
            f"{'Yes' if active['paused'] else 'No'}",
            parse_mode="HTML",
        )
        return

    await message.reply_text(
        "कोई active quiz या draft नहीं है.\n"
        "/create से नया quiz बनाएं."
    )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    active = ACTIVE.pop(user.id, None)

    if not active:
        await message.reply_text(
            "कोई active quiz नहीं है."
        )
        return

    await message.reply_text(
        "⏹️ Current quiz stopped."
    )


async def pause(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    active = ACTIVE.get(user.id)

    if not active:
        await message.reply_text(
            "कोई active quiz नहीं है."
        )
        return

    active["paused"] = True

    await message.reply_text(
        "⏸️ Quiz paused.\n"
        "/resume से जारी करें."
    )


async def resume(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    active = ACTIVE.get(user.id)

    if not active:
        await message.reply_text(
            "कोई paused quiz नहीं है."
        )
        return

    active["paused"] = False

    await send_current_question(
        user.id,
        context,
        message,
    )


async def fast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    active = ACTIVE.get(user.id)

    if not active:
        await message.reply_text(
            "कोई active quiz नहीं है."
        )
        return

    active["timer_seconds"] = max(
        10,
        active["timer_seconds"] - 10,
    )

    await message.reply_text(
        f"⚡ Timer decreased to "
        f"{active['timer_seconds']} sec/question."
    )


async def slow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    active = ACTIVE.get(user.id)

    if not active:
        await message.reply_text(
            "कोई active quiz नहीं है."
        )
        return

    active["timer_seconds"] = min(
        3600,
        active["timer_seconds"] + 10,
    )

    await message.reply_text(
        f"🐢 Timer increased to "
        f"{active['timer_seconds']} sec/question."
    )


async def stoppoll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    active = ACTIVE.get(user.id)

    if active and active.get("poll_message_id"):

        try:

            await context.bot.stop_poll(
                chat_id=user.id,
                message_id=active[
                    "poll_message_id"
                ],
            )

            active["poll_message_id"] = None

            await message.reply_text(
                "🛑 Active poll stopped."
            )
            return

        except Exception:

            logger.exception(
                "stop_poll failed"
            )

    await message.reply_text(
        "इस समय कोई active poll नहीं है."
    )


# ---------------------------------------------------------------------
# TESTBOOK URL
# ---------------------------------------------------------------------

async def testbook(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if not message:
        return

    if not context.args:

        await message.reply_text(
            "Testbook URL भेजें:\n\n"
            "<code>/testbook https://...</code>",
            parse_mode="HTML",
        )
        return

    url = context.args[0].strip()

    if not valid_url(url):

        await message.reply_text(
            "❌ Valid http/https URL भेजें."
        )
        return

    # URL validation is real. External Testbook page parsing is not
    # hard-coded here because their page structure/API can change.
    await message.reply_text(
        "🔗 Testbook URL received and validated.\n\n"
        "इस bot में सुरक्षित batch import के लिए "
        ".txt/.csv/.json export को /create के दौरान upload करें.\n\n"
        "Direct Testbook webpage parsing के लिए अलग importer/API "
        "integration चाहिए."
    )


# ---------------------------------------------------------------------
# MISTAKES / BOOKMARKS
# ---------------------------------------------------------------------

async def mistakes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message
    pool = context.bot_data.get(DB_KEY)

    if not user or not message:
        return

    if not pool:
        await message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT
            q.question,
            a.selected_answer,
            q.correct_answer,
            q.explanation
        FROM quiz_answers a
        JOIN quiz_questions q
            ON q.id=a.question_id
        JOIN quiz_attempts attempt
            ON attempt.id=a.attempt_id
        WHERE attempt.user_id=$1
          AND a.is_correct=FALSE
        ORDER BY a.answered_at DESC
        LIMIT 30
        """,
        user.id,
    )

    if not rows:
        await message.reply_text(
            "कोई incorrect answer record नहीं मिला."
        )
        return

    text = "<b>❌ MISTAKES</b>\n\n"

    for number, row in enumerate(
        rows,
        1,
    ):

        text += (
            f"<b>{number}.</b> "
            f"{escape(row['question'])}\n"
            f"Your answer: "
            f"{escape(row['selected_answer'] or '—')}\n"
            f"Correct: "
            f"{escape(row['correct_answer'])}\n"
        )

        if row["explanation"]:
            text += (
                f"💡 "
                f"{escape(row['explanation'])}\n"
            )

        text += "\n"

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


async def bookmarks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message
    pool = context.bot_data.get(DB_KEY)

    if not user or not message:
        return

    if not pool:
        await message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT
            q.question,
            q.option_a,
            q.option_b,
            q.option_c,
            q.option_d,
            q.correct_answer,
            q.explanation
        FROM quiz_bookmarks b
        JOIN quiz_questions q
            ON q.id=b.question_id
        WHERE b.user_id=$1
        ORDER BY b.created_at DESC
        LIMIT 30
        """,
        user.id,
    )

    if not rows:
        await message.reply_text(
            "कोई bookmarked question नहीं है."
        )
        return

    text = "<b>🔖 BOOKMARKS</b>\n\n"

    for number, row in enumerate(
        rows,
        1,
    ):

        text += (
            f"<b>{number}. "
            f"{escape(row['question'])}</b>\n"
            f"A. {escape(row['option_a'])}\n"
            f"B. {escape(row['option_b'])}\n"
            f"C. {escape(row['option_c'])}\n"
            f"D. {escape(row['option_d'])}\n"
            f"Correct: "
            f"{escape(row['correct_answer'])}\n\n"
        )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------
# QUIZ TAKING
# ---------------------------------------------------------------------

async def start_published_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user
    quiz_id = int(
        query.data.split(":", 1)[1]
    )

    pool = context.bot_data.get(DB_KEY)

    if not pool:
        await query.message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    await ensure_tables(pool)

    quiz = await pool.fetchrow(
        """
        SELECT
            id,
            title,
            time_limit,
            negative_marking
        FROM quizzes
        WHERE id=$1
          AND published=TRUE
        """,
        quiz_id,
    )

    if not quiz:
        await query.message.reply_text(
            "Quiz नहीं मिला या published नहीं है."
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
        WHERE quiz_id=$1
        ORDER BY question_no
        """,
        quiz_id,
    )

    if not questions:
        await query.message.reply_text(
            "इस quiz में questions नहीं हैं."
        )
        return

    attempt_id = await pool.fetchval(
        """
        INSERT INTO quiz_attempts
        (quiz_id,user_id)
        VALUES($1,$2)
        RETURNING id
        """,
        quiz_id,
        user.id,
    )

    per_question = int(
        quiz["time_limit"] * 60
        / max(1, len(questions))
    )

    ACTIVE[user.id] = {
        "quiz_id": quiz_id,
        "attempt_id": int(attempt_id),
        "questions": [
            dict(question)
            for question in questions
        ],
        "index": 0,
        "timer_seconds": max(
            10,
            per_question,
        ),
        "paused": False,
        "poll_message_id": None,
    }

    await query.message.reply_text(
        f"▶️ <b>{escape(quiz['title'])}</b>\n\n"
        f"{len(questions)} questions | "
        f"{quiz['time_limit']} min\n\n"
        "Answer button से answer चुनें.\n"
        "🔖 Bookmark भी कर सकते हैं.",
        parse_mode="HTML",
    )

    await send_current_question(
        user.id,
        context,
        query.message,
    )


async def send_current_question(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    message,
) -> None:

    active = ACTIVE.get(user_id)

    if not active:
        return

    if active["paused"]:

        await message.reply_text(
            "⏸️ Quiz paused. /resume दबाएँ."
        )
        return

    if active["index"] >= len(
        active["questions"]
    ):

        await complete_attempt(
            user_id,
            context,
            message,
        )
        return

    question = active["questions"][
        active["index"]
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"A. {question['option_a'][:35]}",
                    callback_data=(
                        f"ans:A:{question['id']}"
                    ),
                ),
                InlineKeyboardButton(
                    f"B. {question['option_b'][:35]}",
                    callback_data=(
                        f"ans:B:{question['id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    f"C. {question['option_c'][:35]}",
                    callback_data=(
                        f"ans:C:{question['id']}"
                    ),
                ),
                InlineKeyboardButton(
                    f"D. {question['option_d'][:35]}",
                    callback_data=(
                        f"ans:D:{question['id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔖 Bookmark",
                    callback_data=(
                        f"bookmark:{question['id']}"
                    ),
                )
            ],
        ]
    )

    await message.reply_text(
        f"<b>Q{active['index'] + 1}/"
        f"{len(active['questions'])}</b>\n\n"
        f"{escape(question['question'])}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def answer_or_bookmark(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query:
        return

    user = query.from_user
    active = ACTIVE.get(user.id)

    if not active:
        await query.answer(
            "No active quiz.",
            show_alert=True,
        )
        return

    parts = query.data.split(":")
    action = parts[0]

    pool = context.bot_data.get(DB_KEY)

    if not pool:
        await query.answer(
            "Database unavailable.",
            show_alert=True,
        )
        return

    question_id = int(parts[-1])

    if action == "bookmark":

        await pool.execute(
            """
            INSERT INTO quiz_bookmarks
            (user_id,question_id)
            VALUES($1,$2)
            ON CONFLICT DO NOTHING
            """,
            user.id,
            question_id,
        )

        await query.answer(
            "🔖 Bookmarked."
        )
        return

    if action != "ans":
        return

    selected = parts[1]

    current = active["questions"][
        active["index"]
    ]

    if question_id != current["id"]:
        await query.answer(
            "This question is no longer active.",
            show_alert=True,
        )
        return

    correct = (
        selected
        == current["correct_answer"]
    )

    await pool.execute(
        """
        INSERT INTO quiz_answers
        (
            attempt_id,
            question_id,
            selected_answer,
            is_correct
        )
        VALUES($1,$2,$3,$4)
        ON CONFLICT(attempt_id,question_id)
        DO UPDATE SET
            selected_answer=EXCLUDED.selected_answer,
            is_correct=EXCLUDED.is_correct
        """,
        active["attempt_id"],
        question_id,
        selected,
        correct,
    )

    await query.answer(
        "Correct!" if correct else "Wrong!"
    )

    if correct:

        await query.message.reply_text(
            "✅ Correct!"
        )

    else:

        explanation = (
            f"\n💡 {current['explanation']}"
            if current.get("explanation")
            else ""
        )

        await query.message.reply_text(
            f"❌ Wrong.\n"
            f"Correct answer: "
            f"{current['correct_answer']}"
            f"{explanation}"
        )

    active["index"] += 1

    await send_current_question(
        user.id,
        context,
        query.message,
    )


async def complete_attempt(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    message,
) -> None:

    active = ACTIVE.pop(
        user_id,
        None,
    )

    if not active:
        return

    pool = context.bot_data.get(DB_KEY)

    if not pool:
        await message.reply_text(
            "Quiz finished."
        )
        return

    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE is_correct=TRUE
            ) AS correct,
            COUNT(*) FILTER (
                WHERE is_correct=FALSE
            ) AS wrong
        FROM quiz_answers
        WHERE attempt_id=$1
        """,
        active["attempt_id"],
    )

    correct = int(
        row["correct"] or 0
    )

    wrong = int(
        row["wrong"] or 0
    )

    total = len(
        active["questions"]
    )

    unanswered = max(
        0,
        total - correct - wrong,
    )

    quiz = await pool.fetchrow(
        """
        SELECT negative_marking
        FROM quizzes
        WHERE id=$1
        """,
        active["quiz_id"],
    )

    negative = float(
        quiz["negative_marking"] or 0
    ) if quiz else 0

    score = (
        correct
        - wrong * negative
    )

    await pool.execute(
        """
        UPDATE quiz_attempts
        SET
            score=$1,
            correct_count=$2,
            wrong_count=$3,
            unanswered_count=$4,
            completed=TRUE,
            completed_at=now()
        WHERE id=$5
        """,
        score,
        correct,
        wrong,
        unanswered,
        active["attempt_id"],
    )

    await message.reply_text(
        f"🏁 <b>Quiz Completed</b>\n\n"
        f"Correct: {correct}\n"
        f"Wrong: {wrong}\n"
        f"Unanswered: {unanswered}\n"
        f"Score: <b>{score:.2f}</b>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------
# EDIT / DELETE CALLBACKS
# ---------------------------------------------------------------------

async def edit_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user
    quiz_id = int(
        query.data.split(":", 1)[1]
    )

    pool = context.bot_data.get(DB_KEY)

    if not pool:
        await query.message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    row = await pool.fetchrow(
        """
        SELECT
            id,
            title,
            subject,
            time_limit,
            negative_marking
        FROM quizzes
        WHERE id=$1
          AND (creator_id=$2 OR $2=$3)
        """,
        quiz_id,
        user.id,
        ADMIN_ID,
    )

    if not row:
        await query.message.reply_text(
            "❌ यह quiz आपके edit अधिकार में नहीं है."
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
            explanation,
            image_file_id
        FROM quiz_questions
        WHERE quiz_id=$1
        ORDER BY question_no
        """,
        quiz_id,
    )

    DRAFTS[user.id] = {
        "state": "edit_wait",
        "quiz_id": quiz_id,
        "title": row["title"],
        "subject": row["subject"] or "",
        "time_limit": int(
            row["time_limit"]
        ),
        "negative_marking": float(
            row["negative_marking"] or 0
        ),
        "questions": [
            dict(question)
            for question in questions
        ],
        "sections": [],
        "section_mode": False,
        "created_by": user.id,
    }

    await query.message.reply_text(
        f"✏️ <b>Edit Quiz #{quiz_id}</b>\n\n"
        f"Current title: "
        f"<b>{escape(row['title'])}</b>\n"
        f"Current questions: "
        f"{len(questions)}\n\n"
        "नया पूरा question batch भेजें. "
        "यह existing question set को replace करेगा.\n\n"
        "❌ /cancel",
        parse_mode="HTML",
    )


async def delete_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user
    quiz_id = int(
        query.data.split(":", 1)[1]
    )

    pool = context.bot_data.get(DB_KEY)

    if not pool:
        await query.message.reply_text(
            "⚠️ Database connected नहीं है."
        )
        return

    result = await pool.execute(
        """
        DELETE FROM quizzes
        WHERE id=$1
          AND (creator_id=$2 OR $2=$3)
        """,
        quiz_id,
        user.id,
        ADMIN_ID,
    )

    if result.endswith("1"):

        await query.message.reply_text(
            f"🗑 Quiz #{quiz_id} deleted."
        )

    else:

        await query.message.reply_text(
            "❌ Quiz नहीं मिला या permission नहीं है."
        )


# ---------------------------------------------------------------------
# UNKNOWN COMMAND
# ---------------------------------------------------------------------

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message:

        await message.reply_text(
            "❌ Unknown command.\n"
            "/help से available commands देखें."
        )


# ---------------------------------------------------------------------
# TELEGRAM COMMAND MENU
# ---------------------------------------------------------------------

async def set_bot_commands(
    application: Application,
) -> None:

    await application.bot.set_my_commands(
        BOT_COMMANDS
    )


# ---------------------------------------------------------------------
# REGISTER HANDLERS
# ---------------------------------------------------------------------

def register_handlers(
    application: Application,
) -> None:

    # EXACT requested commands.
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
        CommandHandler("stop", stop)
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
        CommandHandler("pause", pause)
    )

    application.add_handler(
        CommandHandler("resume", resume)
    )

    application.add_handler(
        CommandHandler("fast", fast)
    )

    application.add_handler(
        CommandHandler("slow", slow)
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

    # Text batch creation/editing.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=0,
    )

    # TXT/CSV/JSON batch file import.
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_document,
        ),
        group=0,
    )

    # Draft buttons.
    application.add_handler(
        CallbackQueryHandler(
            draft_buttons,
            pattern=r"^draft_",
        ),
        group=0,
    )

    # Quiz buttons.
    application.add_handler(
        CallbackQueryHandler(
            start_published_quiz,
            pattern=r"^start:\d+$",
        ),
        group=0,
    )

    application.add_handler(
        CallbackQueryHandler(
            edit_callback,
            pattern=r"^edit:\d+$",
        ),
        group=0,
    )

    application.add_handler(
        CallbackQueryHandler(
            delete_callback,
            pattern=r"^delete:\d+$",
        ),
        group=0,
    )

    application.add_handler(
        CallbackQueryHandler(
            answer_or_bookmark,
            pattern=r"^(ans|bookmark):",
        ),
        group=0,
    )

    # IMPORTANT:
    # This is in a later group, so valid commands do NOT
    # also trigger "Unknown command".
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=1,
    )
