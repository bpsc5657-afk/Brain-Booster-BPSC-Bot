# bot/handlers.py

import csv
import io
import json
import logging
import re
import urllib.request
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    BotCommand,
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
    ConversationHandler,
    filters,
)

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None


logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

try:
    from .config import settings
except Exception:
    settings = None


def get_database_url() -> Optional[str]:
    if settings:
        value = getattr(settings, "database_url", None)
        if value:
            return value

    import os
    return os.getenv("DATABASE_URL")


DATABASE_URL = get_database_url()


# ============================================================
# EXACT TELEGRAM COMMAND MENU
# ============================================================

BOT_COMMANDS = [
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
    ("cancel", "Cancel current creation/editing process"),
]


# ============================================================
# USER STATES
# ============================================================

CREATE_TITLE = "create_title"
CREATE_SUBJECT = "create_subject"
CREATE_TIMER = "create_timer"
CREATE_NEGATIVE = "create_negative"
CREATE_QUESTIONS = "create_questions"


# In-memory workflow state.
# Database stores final quizzes; this stores temporary creation state.
DRAFTS = {}

# Active quiz state per user.
ACTIVE_QUIZZES = {}

# Active polls.
ACTIVE_POLLS = {}


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")

    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed.")

    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Creates/updates tables required by this handler.
    Safe to run at bot startup.
    """

    conn = db_connect()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id SERIAL PRIMARY KEY,
                creator_id BIGINT NOT NULL,
                creator_username TEXT,
                title TEXT NOT NULL,
                subject TEXT,
                timer_seconds INTEGER DEFAULT 30,
                negative_marking NUMERIC DEFAULT 0,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id SERIAL PRIMARY KEY,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id)
                    ON DELETE CASCADE,
                question_no INTEGER NOT NULL,
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option CHAR(1) NOT NULL,
                explanation TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_answers (
                id SERIAL PRIMARY KEY,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id)
                    ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                question_id INTEGER NOT NULL REFERENCES quiz_questions(id)
                    ON DELETE CASCADE,
                selected_option CHAR(1),
                is_correct BOOLEAN DEFAULT FALSE,
                answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_bookmarks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                question_id INTEGER NOT NULL REFERENCES quiz_questions(id)
                    ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, question_id)
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_sections (
                id SERIAL PRIMARY KEY,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id)
                    ON DELETE CASCADE,
                section_no INTEGER NOT NULL,
                section_name TEXT NOT NULL,
                timer_seconds INTEGER NOT NULL,
                question_start INTEGER NOT NULL,
                question_end INTEGER NOT NULL
            );
            """
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# UTILITY
# ============================================================

def user_display(update: Update) -> str:
    user = update.effective_user

    if not user:
        return "User"

    if user.username:
        return f"@{user.username}"

    return user.full_name or "User"


async def safe_reply(update: Update, text: str, **kwargs):
    if update.message:
        return await update.message.reply_text(text, **kwargs)

    if update.callback_query:
        return await update.callback_query.message.reply_text(
            text,
            **kwargs
        )


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    return text.strip()


# ============================================================
# BATCH QUESTION PARSER
# ============================================================

QUESTION_START_RE = re.compile(
    r"^\s*Q\s*(\d+)\s*[\.\:\-]\s*(.*)$",
    re.IGNORECASE,
)

OPTION_RE = re.compile(
    r"^\s*[\(\[]?([ABCD])[\)\]\.\:\-]\s*(.*?)\s*$",
    re.IGNORECASE,
)

CORRECT_RE = re.compile(
    r"^\s*(?:Correct|Correct\s+Answer|Answer|Ans)\s*[:\-]\s*"
    r"\(?([ABCD])\)?\s*$",
    re.IGNORECASE,
)

EXPLANATION_RE = re.compile(
    r"^\s*(?:Ex|Explanation|Explain)\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)


def remove_correct_symbol(option: str):
    """
    Supports:
        B. Option B ✅
        (b) Option B
        [b] Option B
    """

    option = option.strip()

    option = re.sub(
        r"\s*(?:✅|✔️|✓)\s*$",
        "",
        option,
    )

    return option.strip()


def detect_correct_from_options(options):
    """
    If user marks an option with ✅, detect it.
    """

    for letter, text in options.items():

        if re.search(r"(?:✅|✔️|✓)\s*$", text.strip()):
            return letter

    return None


def parse_batch_questions(raw_text: str):
    """
    Robust batch parser.

    Example:

    Q1. What is the capital of Bihar?

    A. Gaya
    B. Patna ✅
    C. Muzaffarpur
    D. Bhagalpur

    Correct: B
    Explanation: Patna is the capital of Bihar.

    It also supports multi-line / statement-based questions.
    """

    text = normalize_text(raw_text)

    if not text:
        return [], ["Input is empty."]

    lines = text.split("\n")

    blocks = []
    current = None

    for line in lines:

        match = QUESTION_START_RE.match(line)

        if match:
            if current:
                blocks.append(current)

            current = {
                "number": int(match.group(1)),
                "lines": [],
            }

            first_line = match.group(2).strip()

            if first_line:
                current["lines"].append(first_line)

            continue

        if current is not None:
            current["lines"].append(line)

    if current:
        blocks.append(current)

    questions = []
    errors = []

    for index, block in enumerate(blocks, start=1):

        q_lines = block["lines"]

        question_lines = []
        explanation_lines = []

        options = {
            "A": None,
            "B": None,
            "C": None,
            "D": None,
        }

        correct = None

        in_explanation = False

        for raw_line in q_lines:

            line = raw_line.strip()

            if not line:
                if in_explanation:
                    explanation_lines.append("")
                elif question_lines:
                    question_lines.append("")

                continue

            explanation_match = EXPLANATION_RE.match(line)

            if explanation_match:
                in_explanation = True

                first_explanation = explanation_match.group(1).strip()

                if first_explanation:
                    explanation_lines.append(
                        first_explanation
                    )

                continue

            if in_explanation:
                explanation_lines.append(line)
                continue

            correct_match = CORRECT_RE.match(line)

            if correct_match:
                correct = correct_match.group(1).upper()
                continue

            option_match = OPTION_RE.match(line)

            if option_match:
                letter = option_match.group(1).upper()
                option_text = option_match.group(2).strip()

                options[letter] = option_text

                continue

            # Anything before options is part of the question.
            question_lines.append(line)

        question_text = "\n".join(question_lines).strip()

        # Remove trailing blank lines.
        question_text = re.sub(
            r"\n{3,}",
            "\n\n",
            question_text,
        )

        explanation = "\n".join(
            explanation_lines
        ).strip()

        # Detect answer from marked option.
        if not correct:
            correct = detect_correct_from_options(options)

        # Remove symbols after detecting.
        for letter in options:
            if options[letter]:
                options[letter] = remove_correct_symbol(
                    options[letter]
                )

        # Validation
        missing = []

        if not question_text:
            missing.append("question")

        for letter in "ABCD":
            if not options[letter]:
                missing.append(f"option {letter}")

        if not correct:
            missing.append("correct answer")

        if correct and correct not in "ABCD":
            missing.append("valid correct option")

        if missing:
            errors.append(
                f"Q{block['number']}: missing "
                + ", ".join(missing)
            )
            continue

        questions.append(
            {
                "question_no": block["number"],
                "question": question_text,
                "option_a": options["A"],
                "option_b": options["B"],
                "option_c": options["C"],
                "option_d": options["D"],
                "correct_option": correct,
                "explanation": explanation,
            }
        )

    return questions, errors


# ============================================================
# JSON / CSV / TXT IMPORT
# ============================================================

def parse_json_questions(content: str):

    data = json.loads(content)

    if isinstance(data, dict):
        data = data.get("questions", [])

    result = []

    for index, item in enumerate(data, start=1):

        result.append(
            {
                "question_no": item.get(
                    "question_no",
                    item.get("number", index),
                ),
                "question": item["question"],
                "option_a": item["option_a"],
                "option_b": item["option_b"],
                "option_c": item["option_c"],
                "option_d": item["option_d"],
                "correct_option": str(
                    item["correct_option"]
                ).upper(),
                "explanation": item.get(
                    "explanation",
                    "",
                ),
            }
        )

    return result, []


def parse_csv_questions(content: str):

    reader = csv.DictReader(
        io.StringIO(content)
    )

    result = []

    for index, row in enumerate(reader, start=1):

        result.append(
            {
                "question_no": row.get(
                    "question_no",
                    index,
                ),
                "question": row["question"],
                "option_a": row["option_a"],
                "option_b": row["option_b"],
                "option_c": row["option_c"],
                "option_d": row["option_d"],
                "correct_option": row[
                    "correct_option"
                ].upper(),
                "explanation": row.get(
                    "explanation",
                    "",
                ),
            }
        )

    return result, []


def parse_uploaded_file(filename: str, content: str):

    lower = filename.lower()

    try:

        if lower.endswith(".json"):
            return parse_json_questions(content)

        if lower.endswith(".csv"):
            return parse_csv_questions(content)

        # TXT / unknown text files use batch parser.
        return parse_batch_questions(content)

    except Exception as exc:

        logger.exception(
            "Question file parsing failed"
        )

        return [], [
            f"File parsing error: {exc}"
        ]


# ============================================================
# QUIZ PREVIEW
# ============================================================

def format_question_preview(q):

    correct = q["correct_option"]

    options = []

    for letter, key in [
        ("A", "option_a"),
        ("B", "option_b"),
        ("C", "option_c"),
        ("D", "option_d"),
    ]:

        mark = " ✅" if letter == correct else ""

        options.append(
            f"{letter}. {q[key]}{mark}"
        )

    explanation = q.get("explanation") or "—"

    return (
        f"Q{q['question_no']}. "
        f"{q['question']}\n\n"
        + "\n".join(options)
        + f"\n\nEx: {explanation}"
    )


def quiz_preview_text(title, subject, timer, negative, questions):

    lines = [
        "📋 QUIZ PREVIEW",
        "",
        f"Title: {title}",
        f"Subject: {subject or 'Not specified'}",
        f"Questions: {len(questions)}",
        f"Timer: {timer} sec/question",
        f"Negative marking: {negative}",
        "",
        "Questions imported successfully.",
    ]

    if questions:

        lines.extend(
            [
                "",
                "First question:",
                "",
                format_question_preview(
                    questions[0]
                ),
            ]
        )

    return "\n".join(lines)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    welcome = (
        "🎯 *Brain Booster Quiz Creator*\n\n"
        "Professional quiz creation and practice bot.\n\n"
        "You can create a complete quiz in one batch using "
        "`/create`.\n\n"
        "📌 *Quick Start*\n"
        "1. Use `/create`\n"
        "2. Enter quiz title\n"
        "3. Optionally add subject\n"
        "4. Set timer and negative marking\n"
        "5. Paste or upload all questions\n"
        "6. Review and save the quiz\n\n"
        "Use `/help` to see all available commands."
    )

    await update.message.reply_text(
        welcome,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "📚 *Quiz Creator — Commands*\n\n"

        "/start — Start the bot and show welcome message\n"
        "/create — Create a new quiz\n"
        "/myquizzes — View quizzes created by the admin\n"
        "/settings — Configure quiz settings\n"
        "/stop — Stop the current quiz\n"
        "/help — Show all commands\n"
        "/section — Create a quiz with multiple timed sections\n"
        "/mistakes — Review incorrectly answered questions\n"
        "/bookmarks — Review bookmarked questions\n"
        "/pause — Pause the current quiz\n"
        "/resume — Resume a paused quiz\n"
        "/fast — Decrease question timer\n"
        "/slow — Increase question timer\n"
        "/stoppoll — Stop an active poll\n"
        "/testbook — Import an attempted quiz from a Testbook URL\n"
        "/tutorial — Show bot tutorials and guides\n"
        "/cancel — Cancel current creation/editing process\n\n"

        "💡 *Quiz format*\n"
        "You can paste many questions in one message "
        "or upload a TXT/CSV/JSON file.\n\n"

        "Each question must contain:\n"
        "• Q<number>.\n"
        "• A/B/C/D options\n"
        "• Correct: A/B/C/D\n"
        "• Explanation:"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# TUTORIAL
# ============================================================

async def tutorial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "📖 *Quiz Creator Tutorial*\n\n"

        "*Step 1 — Create*\n"
        "Use `/create`.\n\n"

        "*Step 2 — Quiz details*\n"
        "Enter title, optional subject, timer and "
        "negative marking.\n\n"

        "*Step 3 — Add questions*\n"
        "Paste the complete question set in one message "
        "or upload a TXT/CSV/JSON file.\n\n"

        "*Step 4 — Correct answer*\n"
        "Use `Correct: B` or mark the correct option with "
        "✅.\n\n"

        "*Example:*\n\n"
        "Q1. What is the capital of Bihar?\n\n"
        "A. Gaya\n"
        "B. Patna ✅\n"
        "C. Muzaffarpur\n"
        "D. Bhagalpur\n\n"
        "Correct: B\n"
        "Explanation: Patna is the capital of Bihar.\n\n"

        "The parser also supports statement-based and "
        "multi-line questions."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# CREATE QUIZ
# ============================================================

async def create_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    DRAFTS[user_id] = {
        "title": None,
        "subject": None,
        "timer": 30,
        "negative": 0,
        "questions": [],
    }

    await update.message.reply_text(
        "📝 *Create New Quiz*\n\n"
        "Enter the quiz title.\n\n"
        "Example:\n"
        "`BPSC Modern History — Practice Test 01`\n\n"
        "Use /cancel to cancel.",
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_TITLE


async def create_title(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    DRAFTS[user_id]["title"] = update.message.text.strip()

    await update.message.reply_text(
        "📚 *Subject (optional)*\n\n"
        "Enter the subject name, or type `skip`.\n\n"
        "Subject is stored only as quiz metadata; "
        "it is NOT a Telegram command.",
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_SUBJECT


async def create_subject(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    value = update.message.text.strip()

    if value.lower() in {
        "skip",
        "none",
        "no",
        "-",
    }:
        value = None

    DRAFTS[user_id]["subject"] = value

    await update.message.reply_text(
        "⏱ *Question Timer*\n\n"
        "Enter timer in seconds.\n"
        "Example: `30`\n\n"
        "Recommended: 15–60 seconds.",
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_TIMER


async def create_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    try:
        timer = int(update.message.text.strip())

        if timer < 1 or timer > 3600:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid timer.\n"
            "Enter a number between 1 and 3600 seconds."
        )

        return CREATE_TIMER

    DRAFTS[user_id]["timer"] = timer

    await update.message.reply_text(
        "➖ *Negative Marking*\n\n"
        "Enter negative marks per wrong answer.\n\n"
        "Examples:\n"
        "`0`\n"
        "`0.25`\n"
        "`0.50`",
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_NEGATIVE


async def create_negative(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    try:
        negative = float(update.message.text.strip())

        if negative < 0 or negative > 10:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid value.\n"
            "Enter a number such as 0, 0.25 or 0.50."
        )

        return CREATE_NEGATIVE

    DRAFTS[user_id]["negative"] = negative

    await update.message.reply_text(
        "📥 *Now send the complete question set.*\n\n"

        "You can paste ALL questions in one message "
        "or upload a `.txt`, `.csv` or `.json` file.\n\n"

        "Required TXT format:\n\n"

        "Q1. What is the capital of Bihar?\n\n"
        "A. Gaya\n"
        "B. Patna ✅\n"
        "C. Muzaffarpur\n"
        "D. Bhagalpur\n\n"
        "Correct: B\n"
        "Explanation: Patna is the capital of Bihar.\n\n"

        "Q2. Your next question...\n\n"

        "You can send 25, 50, 100 or more questions "
        "in one batch.\n\n"

        "Use /cancel to cancel.",
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_QUESTIONS


# ============================================================
# RECEIVE BATCH TEXT
# ============================================================

async def receive_questions_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if user_id not in DRAFTS:

        await update.message.reply_text(
            "No active quiz creation process.\n"
            "Use /create first."
        )

        return ConversationHandler.END

    raw_text = update.message.text

    questions, errors = parse_batch_questions(
        raw_text
    )

    if not questions:

        error_text = (
            "❌ *No valid questions found.*\n\n"
            "Please use this format:\n\n"
            "Q1. Question?\n\n"
            "A. Option A\n"
            "B. Option B ✅\n"
            "C. Option C\n"
            "D. Option D\n\n"
            "Correct: B\n"
            "Explanation: Explanation here."
        )

        if errors:
            error_text += (
                "\n\nParser errors:\n"
                + "\n".join(
                    errors[:10]
                )
            )

        await update.message.reply_text(
            error_text,
            parse_mode=ParseMode.MARKDOWN,
        )

        return CREATE_QUESTIONS

    DRAFTS[user_id]["questions"] = questions

    draft = DRAFTS[user_id]

    preview = quiz_preview_text(
        draft["title"],
        draft["subject"],
        draft["timer"],
        draft["negative"],
        questions,
    )

    if errors:
        preview += (
            "\n\n⚠️ *Skipped/invalid blocks:*\n"
            + "\n".join(errors[:15])
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Save Quiz",
                    callback_data="draft_save",
                ),
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data="draft_preview",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                ),
            ],
        ]
    )

    await update.message.reply_text(
        preview,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_QUESTIONS


# ============================================================
# RECEIVE FILE
# ============================================================

async def receive_questions_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if user_id not in DRAFTS:

        await update.message.reply_text(
            "No active quiz creation process.\n"
            "Use /create first."
        )

        return ConversationHandler.END

    document = update.message.document

    try:

        tg_file = await document.get_file()

        file_bytes = await tg_file.download_as_bytearray()

        content = bytes(file_bytes).decode(
            "utf-8-sig",
            errors="replace",
        )

    except Exception as exc:

        logger.exception(
            "File download failed"
        )

        await update.message.reply_text(
            f"❌ Could not read the file.\n\n{exc}"
        )

        return CREATE_QUESTIONS

    questions, errors = parse_uploaded_file(
        document.file_name or "questions.txt",
        content,
    )

    if not questions:

        await update.message.reply_text(
            "❌ No valid questions were found in the file.\n\n"
            "Supported formats: TXT, CSV, JSON\n\n"
            "For TXT use:\n"
            "Q1. Question?\n"
            "A. Option A\n"
            "B. Option B ✅\n"
            "C. Option C\n"
            "D. Option D\n"
            "Correct: B\n"
            "Explanation: Explanation.",
        )

        if errors:
            await update.message.reply_text(
                "\n".join(errors[:15])
            )

        return CREATE_QUESTIONS

    DRAFTS[user_id]["questions"] = questions

    draft = DRAFTS[user_id]

    preview = quiz_preview_text(
        draft["title"],
        draft["subject"],
        draft["timer"],
        draft["negative"],
        questions,
    )

    if errors:
        preview += (
            "\n\n⚠️ Errors:\n"
            + "\n".join(errors[:15])
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Save Quiz",
                    callback_data="draft_save",
                ),
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data="draft_preview",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="draft_cancel",
                ),
            ],
        ]
    )

    await update.message.reply_text(
        preview,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_QUESTIONS


# ============================================================
# SAVE DRAFT
# ============================================================

def save_quiz_to_database(
    user,
    draft,
):

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO quizzes
            (
                creator_id,
                creator_username,
                title,
                subject,
                timer_seconds,
                negative_marking,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'published')
            RETURNING id
            """,
            (
                user.id,
                user.username,
                draft["title"],
                draft["subject"],
                draft["timer"],
                draft["negative"],
            ),
        )

        quiz_id = cur.fetchone()[0]

        for index, q in enumerate(
            draft["questions"],
            start=1,
        ):

            cur.execute(
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
                    correct_option,
                    explanation
                )
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    quiz_id,
                    index,
                    q["question"],
                    q["option_a"],
                    q["option_b"],
                    q["option_c"],
                    q["option_d"],
                    q["correct_option"],
                    q.get("explanation", ""),
                ),
            )

        conn.commit()

        return quiz_id

    except Exception:

        conn.rollback()
        raise

    finally:

        conn.close()


async def save_draft_callback(
    query,
    context,
):

    user_id = query.from_user.id

    draft = DRAFTS.get(user_id)

    if not draft:

        await query.answer(
            "Draft not found.",
            show_alert=True,
        )

        return

    if not draft.get("questions"):

        await query.answer(
            "No questions found.",
            show_alert=True,
        )

        return

    try:

        quiz_id = save_quiz_to_database(
            query.from_user,
            draft,
        )

        DRAFTS.pop(user_id, None)

        await query.answer(
            "Quiz saved successfully!"
        )

        await query.edit_message_text(
            "✅ *Quiz Created Successfully!*\n\n"
            f"Quiz ID: `{quiz_id}`\n"
            f"Title: {draft['title']}\n"
            f"Questions: {len(draft['questions'])}\n"
            f"Subject: {draft['subject'] or 'Not specified'}\n\n"
            "Use /myquizzes to manage your quizzes.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:

        logger.exception(
            "Quiz save failed"
        )

        await query.answer(
            "Database error.",
            show_alert=True,
        )

        await query.message.reply_text(
            f"❌ Quiz save failed:\n{exc}"
        )


# ============================================================
# PREVIEW CALLBACK
# ============================================================

async def preview_draft_callback(
    query,
    context,
):

    user_id = query.from_user.id

    draft = DRAFTS.get(user_id)

    if not draft:

        await query.answer(
            "Draft not found.",
            show_alert=True,
        )

        return

    preview = quiz_preview_text(
        draft["title"],
        draft["subject"],
        draft["timer"],
        draft["negative"],
        draft["questions"],
    )

    await query.answer()

    await query.message.reply_text(
        preview,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# CANCEL CALLBACK
# ============================================================

async def cancel_draft_callback(
    query,
    context,
):

    user_id = query.from_user.id

    DRAFTS.pop(user_id, None)

    await query.answer(
        "Creation cancelled."
    )

    await query.edit_message_text(
        "❌ Quiz creation cancelled.\n\n"
        "Use /create to start again."
    )


# ============================================================
# MY QUIZZES
# ============================================================

async def myquizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                q.id,
                q.title,
                q.subject,
                q.status,
                q.created_at,
                COUNT(qq.id) AS question_count
            FROM quizzes q
            LEFT JOIN quiz_questions qq
                ON qq.quiz_id = q.id
            WHERE q.creator_id = %s
            GROUP BY
                q.id,
                q.title,
                q.subject,
                q.status,
                q.created_at
            ORDER BY q.created_at DESC
            LIMIT 20
            """,
            (user_id,),
        )

        rows = cur.fetchall()

    finally:

        conn.close()

    if not rows:

        await update.message.reply_text(
            "📭 You have not created any quizzes yet.\n\n"
            "Use /create to create your first quiz."
        )

        return

    text = "📚 *My Quizzes*\n\n"

    buttons = []

    for row in rows:

        quiz_id = row[0]
        title = row[1]
        subject = row[2]
        status = row[3]
        question_count = row[5]

        text += (
            f"🆔 `{quiz_id}`\n"
            f"📌 {title}\n"
            f"📚 {subject or 'No subject'}\n"
            f"❓ {question_count} questions\n"
            f"📊 {status}\n\n"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"👁 Preview #{quiz_id}",
                    callback_data=f"stored_preview:{quiz_id}",
                )
            ]
        )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# STORED QUIZ PREVIEW
# ============================================================

async def stored_quiz_preview(
    query,
    quiz_id: int,
):

    user_id = query.from_user.id

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                title,
                subject,
                timer_seconds,
                negative_marking
            FROM quizzes
            WHERE id = %s
              AND creator_id = %s
            """,
            (
                quiz_id,
                user_id,
            ),
        )

        quiz = cur.fetchone()

        if not quiz:

            await query.answer(
                "Quiz not found or not owned by you.",
                show_alert=True,
            )

            return

        cur.execute(
            """
            SELECT
                question_no,
                question,
                option_a,
                option_b,
                option_c,
                option_d,
                correct_option,
                explanation
            FROM quiz_questions
            WHERE quiz_id = %s
            ORDER BY question_no
            LIMIT 5
            """,
            (quiz_id,),
        )

        questions = cur.fetchall()

    finally:

        conn.close()

    text = (
        f"📋 *{quiz[1]}*\n\n"
        f"Subject: {quiz[2] or 'Not specified'}\n"
        f"Timer: {quiz[3]} sec\n"
        f"Negative: {quiz[4]}\n\n"
        f"Showing first {len(questions)} questions:\n\n"
    )

    for row in questions:

        correct = row[6]

        text += (
            f"Q{row[0]}. {row[1]}\n\n"
            f"A. {row[2]}"
            f"{' ✅' if correct == 'A' else ''}\n"
            f"B. {row[3]}"
            f"{' ✅' if correct == 'B' else ''}\n"
            f"C. {row[4]}"
            f"{' ✅' if correct == 'C' else ''}\n"
            f"D. {row[5]}"
            f"{' ✅' if correct == 'D' else ''}\n\n"
            f"Ex: {row[7] or '—'}\n\n"
        )

    await query.answer()

    await query.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# SETTINGS
# ============================================================

async def settings_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏱ Timer −",
                    callback_data="setting_timer_down",
                ),
                InlineKeyboardButton(
                    "⏱ Timer +",
                    callback_data="setting_timer_up",
                ),
            ],
            [
                InlineKeyboardButton(
                    "➖ Negative Marking",
                    callback_data="setting_negative",
                ),
            ],
        ]
    )

    await update.message.reply_text(
        "⚙️ *Quiz Settings*\n\n"
        "Settings can be adjusted while creating or "
        "running a quiz.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# ACTIVE QUIZ CONTROLS
# ============================================================

async def stop_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    state = ACTIVE_QUIZZES.pop(
        user_id,
        None,
    )

    if not state:

        await update.message.reply_text(
            "ℹ️ No active quiz is running."
        )

        return

    await update.message.reply_text(
        "🛑 Current quiz stopped."
    )


async def pause_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    state = ACTIVE_QUIZZES.get(user_id)

    if not state:

        await update.message.reply_text(
            "ℹ️ No active quiz is running."
        )

        return

    state["paused"] = True

    await update.message.reply_text(
        "⏸ Quiz paused.\n\n"
        "Use /resume to continue."
    )


async def resume_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    state = ACTIVE_QUIZZES.get(user_id)

    if not state:

        await update.message.reply_text(
            "ℹ️ No paused quiz found."
        )

        return

    state["paused"] = False

    await update.message.reply_text(
        "▶️ Quiz resumed."
    )


async def fast_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    state = ACTIVE_QUIZZES.get(user_id)

    if not state:

        await update.message.reply_text(
            "ℹ️ No active quiz.\n"
            "Timer speed can also be configured during creation."
        )

        return

    state["timer"] = max(
        1,
        state.get("timer", 30) - 5,
    )

    await update.message.reply_text(
        f"⚡ Timer decreased to {state['timer']} seconds."
    )


async def slow_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    state = ACTIVE_QUIZZES.get(user_id)

    if not state:

        await update.message.reply_text(
            "ℹ️ No active quiz.\n"
            "Timer speed can also be configured during creation."
        )

        return

    state["timer"] = min(
        3600,
        state.get("timer", 30) + 5,
    )

    await update.message.reply_text(
        f"🐢 Timer increased to {state['timer']} seconds."
    )


# ============================================================
# MISTAKES
# ============================================================

async def mistakes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                q.question,
                q.option_a,
                q.option_b,
                q.option_c,
                q.option_d,
                q.correct_option,
                q.explanation,
                a.selected_option
            FROM quiz_answers a
            JOIN quiz_questions q
                ON q.id = a.question_id
            WHERE a.user_id = %s
              AND a.is_correct = FALSE
            ORDER BY a.answered_at DESC
            LIMIT 20
            """,
            (user_id,),
        )

        rows = cur.fetchall()

    finally:

        conn.close()

    if not rows:

        await update.message.reply_text(
            "✅ No incorrect answers found."
        )

        return

    text = "❌ *Incorrect Questions*\n\n"

    for index, row in enumerate(rows, 1):

        text += (
            f"{index}. {row[0]}\n"
            f"Your answer: {row[7] or 'Not answered'}\n"
            f"Correct answer: {row[5]}\n"
            f"Ex: {row[6] or '—'}\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# BOOKMARKS
# ============================================================

async def bookmarks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                q.question,
                q.option_a,
                q.option_b,
                q.option_c,
                q.option_d,
                q.correct_option,
                q.explanation
            FROM quiz_bookmarks b
            JOIN quiz_questions q
                ON q.id = b.question_id
            WHERE b.user_id = %s
            ORDER BY b.created_at DESC
            LIMIT 20
            """,
            (user_id,),
        )

        rows = cur.fetchall()

    finally:

        conn.close()

    if not rows:

        await update.message.reply_text(
            "🔖 No bookmarked questions."
        )

        return

    text = "🔖 *Bookmarked Questions*\n\n"

    for index, row in enumerate(rows, 1):

        text += (
            f"*{index}.* {row[0]}\n\n"
            f"A. {row[1]}\n"
            f"B. {row[2]}\n"
            f"C. {row[3]}\n"
            f"D. {row[4]}\n\n"
            f"Correct: {row[5]}\n"
            f"Ex: {row[6] or '—'}\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# SECTION QUIZ
# ============================================================

async def section_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    DRAFTS[user_id] = {
        "title": "Section Quiz",
        "subject": None,
        "timer": 30,
        "negative": 0,
        "questions": [],
        "sections": [],
    }

    await update.message.reply_text(
        "🧩 *Multiple Timed Sections*\n\n"
        "Section mode is prepared through the same batch "
        "question workflow.\n\n"
        "First enter the quiz title.",
        parse_mode=ParseMode.MARKDOWN,
    )

    return CREATE_TITLE


# ============================================================
# STOP POLL
# ============================================================

async def stop_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    poll = ACTIVE_POLLS.pop(
        user_id,
        None,
    )

    if not poll:

        await update.message.reply_text(
            "ℹ️ No active poll found."
        )

        return

    await update.message.reply_text(
        "🛑 Active poll stopped."
    )


# ============================================================
# TESTBOOK IMPORT
# ============================================================

def fetch_url_text(url: str):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64)"
            )
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=20,
    ) as response:

        data = response.read()

    text = data.decode(
        "utf-8",
        errors="ignore",
    )

    # Basic HTML cleanup.
    text = re.sub(
        r"<script.*?>.*?</script>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<style.*?>.*?</style>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<[^>]+>",
        "\n",
        text,
    )

    text = re.sub(
        r"\n\s*\n+",
        "\n\n",
        text,
    )

    return text.strip()


async def testbook(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🔗 *Testbook Import*\n\n"
        "Send the attempted Testbook quiz URL.\n\n"
        "Example:\n"
        "`https://...`\n\n"
        "Note: Some Testbook pages are dynamically rendered. "
        "If the questions cannot be extracted from the page, "
        "upload the question set as TXT/CSV/JSON instead.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def testbook_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    url = update.message.text.strip()

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):

        await update.message.reply_text(
            "❌ Please send a valid HTTP/HTTPS URL."
        )

        return

    await update.message.reply_text(
        "⏳ Attempting to import the quiz..."
    )

    try:

        text = fetch_url_text(url)

        questions, errors = parse_batch_questions(
            text
        )

        if not questions:

            await update.message.reply_text(
                "⚠️ The page was reached, but "
                "questions could not be reliably extracted.\n\n"
                "Please upload TXT/CSV/JSON instead."
            )

            return

        user_id = update.effective_user.id

        DRAFTS[user_id] = {
            "title": "Imported Testbook Quiz",
            "subject": None,
            "timer": 30,
            "negative": 0,
            "questions": questions,
        }

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Save Imported Quiz",
                        callback_data="draft_save",
                    ),
                    InlineKeyboardButton(
                        "👁 Preview",
                        callback_data="draft_preview",
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

        await update.message.reply_text(
            f"✅ Imported {len(questions)} questions.\n\n"
            "Review and save the quiz.",
            reply_markup=keyboard,
        )

    except Exception as exc:

        logger.exception(
            "Testbook import failed"
        )

        await update.message.reply_text(
            "❌ Testbook import failed.\n\n"
            "This can happen when the page uses "
            "JavaScript rendering or requires login.\n\n"
            "Recommended: upload TXT/CSV/JSON."
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    DRAFTS.pop(
        user_id,
        None,
    )

    await update.message.reply_text(
        "❌ Current creation/editing process cancelled.\n\n"
        "Use /create to start a new quiz."
    )

    return ConversationHandler.END


# ============================================================
# UNKNOWN COMMAND
# ============================================================

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "❌ Unknown command.\n\n"
        "Use /help to see the available commands."
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data or ""

    if data == "draft_save":
        await save_draft_callback(
            query,
            context,
        )
        return

    if data == "draft_preview":
        await preview_draft_callback(
            query,
            context,
        )
        return

    if data == "draft_cancel":
        await cancel_draft_callback(
            query,
            context,
        )
        return

    if data.startswith(
        "stored_preview:"
    ):

        try:
            quiz_id = int(
                data.split(":")[1]
            )

            await stored_quiz_preview(
                query,
                quiz_id,
            )

        except Exception:

            await query.message.reply_text(
                "❌ Invalid quiz ID."
            )

        return

    if data == "setting_timer_down":

        await query.message.reply_text(
            "Use /fast during an active quiz "
            "to decrease the timer."
        )

        return

    if data == "setting_timer_up":

        await query.message.reply_text(
            "Use /slow during an active quiz "
            "to increase the timer."
        )

        return

    if data == "setting_negative":

        await query.message.reply_text(
            "Negative marking is configured during "
            "quiz creation."
        )

        return


# ============================================================
# COMMAND REGISTRATION
# ============================================================

def register_handlers(application: Application):

    # --------------------------------------------------------
    # CREATE CONVERSATION
    # --------------------------------------------------------

    create_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "create",
                create_quiz,
            )
        ],

        states={

            CREATE_TITLE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    create_title,
                )
            ],

            CREATE_SUBJECT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    create_subject,
                )
            ],

            CREATE_TIMER: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    create_timer,
                )
            ],

            CREATE_NEGATIVE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    create_negative,
                )
            ],

            CREATE_QUESTIONS: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_questions_text,
                ),

                MessageHandler(
                    filters.Document.ALL,
                    receive_questions_file,
                ),
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],

        allow_reentry=True,
    )

    application.add_handler(
        create_conversation,
        group=0,
    )

    # --------------------------------------------------------
    # EXACT PUBLIC COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "myquizzes",
            myquizzes,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "settings",
            settings_command,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "stop",
            stop_quiz,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "section",
            section_command,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "mistakes",
            mistakes,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "bookmarks",
            bookmarks,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "pause",
            pause_quiz,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "resume",
            resume_quiz,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "fast",
            fast_timer,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "slow",
            slow_timer,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "stoppoll",
            stop_poll,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "testbook",
            testbook,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "tutorial",
            tutorial,
        ),
        group=0,
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        ),
        group=0,
    )

    # --------------------------------------------------------
    # TESTBOOK URL
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            testbook_url,
        ),
        group=2,
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_router,
        ),
        group=1,
    )

    # --------------------------------------------------------
    # UNKNOWN COMMAND
    #
    # IMPORTANT:
    # This must be AFTER all valid CommandHandlers
    # and in the SAME group.
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=0,
    )


# ============================================================
# BOT COMMAND MENU
# ============================================================

async def set_bot_commands(application: Application):

    commands = [
        BotCommand(
            command,
            description,
        )
        for command, description in BOT_COMMANDS
    ]

    await application.bot.set_my_commands(
        commands
    )

    logger.info(
        "Telegram command menu registered: %d commands",
        len(commands),
    )


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    application: Application,
):

    try:

        init_db()

        logger.info(
            "Database initialized successfully."
        )

    except Exception:

        logger.exception(
            "Database initialization failed."
        )

        # Do not silently hide DB problems.
        # Bot can still start so Railway logs show the error.

    try:

        await set_bot_commands(
            application
        )

    except Exception:

        logger.exception(
            "Failed to register Telegram commands."
        )


# ============================================================
# PUBLIC SETUP FUNCTION
# ============================================================

def setup_handlers(
    application: Application,
):

    register_handlers(
        application
    )

    return application
