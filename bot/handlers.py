"""
Brain Booster BPSC Quiz Bot
Professional Generic Quiz Creator

IMPORTANT:
- No subject-specific Telegram commands.
- Subjects are optional quiz metadata only.
- Any user can create/edit quizzes.
- Batch import supported: text / TXT / JSON / CSV.
- Correct option can contain ✅.
- Supports (a)-(d) and A-D option formats.
- Preserves multi-line / statement-based questions.
"""

import csv
import io
import json
import re
import html
import urllib.request
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
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

from .db import db_pool


# ============================================================
# COMMAND MENU
# EXACTLY THE REQUESTED PUBLIC COMMANDS
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
    ("cancel", "Cancel the current creation/editing process"),
]


# ============================================================
# CONVERSATION STATES
# ============================================================

(
    CREATE_TITLE,
    CREATE_SUBJECT,
    CREATE_TIMER,
    CREATE_NEGATIVE,
    CREATE_QUESTIONS,
    SECTION_DATA,
    EDIT_DATA,
) = range(7)


# ============================================================
# IN-MEMORY USER STATE
# ============================================================

DRAFTS = {}
ACTIVE_QUIZZES = {}
PAUSED_QUIZZES = {}


# ============================================================
# DATABASE HELPERS
# ============================================================

async def execute(query: str, *args):
    async with db_pool.acquire() as conn:
        return await conn.execute(query, *args)


async def fetch(query: str, *args):
    async with db_pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

async def ensure_quiz_tables():
    if not db_pool:
        return

    await execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id SERIAL PRIMARY KEY,
            creator_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            subject TEXT,
            timer_seconds INTEGER DEFAULT 30,
            negative_marking NUMERIC DEFAULT 0,
            is_published BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS quiz_questions (
            id SERIAL PRIMARY KEY,
            quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
            question_no INTEGER NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_option CHAR(1) NOT NULL,
            explanation TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS quiz_answers (
            id SERIAL PRIMARY KEY,
            quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
            question_id INTEGER REFERENCES quiz_questions(id)
                ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            selected_option CHAR(1),
            is_correct BOOLEAN DEFAULT FALSE,
            answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS quiz_bookmarks (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            question_id INTEGER REFERENCES quiz_questions(id)
                ON DELETE CASCADE,
            UNIQUE(user_id, question_id)
        )
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS quiz_sections (
            id SERIAL PRIMARY KEY,
            quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
            section_name TEXT NOT NULL,
            timer_seconds INTEGER NOT NULL,
            question_start INTEGER,
            question_end INTEGER
        )
    """)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    text = (
        "🎯 <b>Welcome to Quiz Creator Bot</b>\n\n"
        f"Hello <b>{html.escape(user.first_name or 'User')}</b>!\n\n"
        "यह bot आपको professional quiz बनाने, edit करने "
        "और attempt करने की सुविधा देता है।\n\n"
        "<b>मुख्य सुविधाएँ:</b>\n"
        "• Batch में unlimited-style question import\n"
        "• Text / TXT / JSON / CSV support\n"
        "• Correct answer + ✅ marking\n"
        "• Explanation support\n"
        "• Bilingual questions\n"
        "• Statement-based questions\n"
        "• Timer & negative marking\n"
        "• Bookmark & mistake review\n\n"
        "Quiz बनाने के लिए:\n"
        "👉 /create\n\n"
        "सभी commands देखने के लिए:\n"
        "👉 /help"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    lines = ["<b>📚 Quiz Creator Bot — Commands</b>\n"]

    for command, description in BOT_COMMANDS:
        lines.append(f"/{command} — {description}")

    lines.append(
        "\n<b>Quiz Format</b>\n"
        "आप एक ही message/file में जितने questions देंगे, "
        "bot उन्हें एक ही quiz में import करेगा।"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML
    )


# ============================================================
# QUIZ FORMAT
# ============================================================

QUIZ_FORMAT_HELP = """
<b>Recommended Quiz Format</b>

<pre>
Q1. भारत का संविधान कब लागू हुआ?

(a) 15 अगस्त 1947
(b) 26 जनवरी 1950
(c) 26 नवंबर 1949
(d) 15 जनवरी 1950

👉 सही विकल्प चुनें / Choose the correct option.

Correct: B

Ex: भारतीय संविधान 26 जनवरी 1950 को लागू हुआ।
The Constitution of India came into force on 26 January 1950.
</pre>

<b>Multiple Question Example</b>

<pre>
Q2. निम्नलिखित में कौन-सा सही है?

Statement 1: भारत एक गणराज्य है।

Statement 2: भारत में संसदीय शासन प्रणाली है।

(a) केवल Statement 1
(b) केवल Statement 2
(c) दोनों 1 और 2
(d) इनमें से कोई नहीं

Correct: C

Ex: दोनों कथन सही हैं।
Both statements are correct.
</pre>

<b>Important:</b>
Correct option के साथ ✅ लगाया जा सकता है:

(b) 26 जनवरी 1950 ✅

लेकिन import reliability के लिए
<b>Correct: B</b> भी देना recommended है।
"""


# ============================================================
# CREATE QUIZ
# ============================================================

async def create_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    DRAFTS[user_id] = {
        "title": None,
        "subject": None,
        "timer": 30,
        "negative": 0,
        "questions": [],
        "sections": [],
    }

    await update.message.reply_text(
        "📝 <b>Create New Quiz</b>\n\n"
        "सबसे पहले quiz का <b>Title</b> भेजें।\n\n"
        "Example:\n"
        "<code>BPSC History Mega Test</code>\n\n"
        "Cancel करने के लिए /cancel",
        parse_mode=ParseMode.HTML
    )

    return CREATE_TITLE


async def create_title(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if user_id not in DRAFTS:
        await update.message.reply_text(
            "कोई active quiz creation नहीं है। /create दबाएँ।"
        )
        return ConversationHandler.END

    DRAFTS[user_id]["title"] = update.message.text.strip()

    await update.message.reply_text(
        "📌 <b>Subject</b> optional है।\n\n"
        "आप subject लिख सकते हैं, जैसे:\n"
        "History\n"
        "Polity\n"
        "Geography\n"
        "Science\n"
        "Current Affairs\n"
        "Bihar\n\n"
        "या <b>skip</b> लिखकर आगे बढ़ें।",
        parse_mode=ParseMode.HTML
    )

    return CREATE_SUBJECT


async def create_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    value = update.message.text.strip()

    if value.lower() not in ("skip", "none", "no", "-"):
        DRAFTS[user_id]["subject"] = value

    await update.message.reply_text(
        "⏱ <b>Question Timer</b>\n\n"
        "हर question के लिए कितने seconds चाहिए?\n\n"
        "Example: <code>30</code>\n\n"
        "Default = 30 seconds",
        parse_mode=ParseMode.HTML
    )

    return CREATE_TIMER


async def create_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    try:
        timer = int(update.message.text.strip())
        if timer < 5 or timer > 3600:
            raise ValueError

        DRAFTS[user_id]["timer"] = timer

    except ValueError:
        await update.message.reply_text(
            "❌ Timer 5 से 3600 seconds के बीच होना चाहिए।"
        )
        return CREATE_TIMER

    await update.message.reply_text(
        "➖ <b>Negative Marking</b>\n\n"
        "गलत answer पर कितने marks deduct हों?\n\n"
        "Example:\n"
        "<code>0.25</code>\n\n"
        "No negative marking के लिए <code>0</code> भेजें।",
        parse_mode=ParseMode.HTML
    )

    return CREATE_NEGATIVE


async def create_negative(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    try:
        negative = float(update.message.text.strip())

        if negative < 0 or negative > 100:
            raise ValueError

        DRAFTS[user_id]["negative"] = negative

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid negative marking value."
        )
        return CREATE_NEGATIVE

    await update.message.reply_text(
        "📥 <b>अब Questions भेजें</b>\n\n"
        "आप एक ही message में जितने questions देंगे, "
        "सभी questions एक ही quiz में बन जाएंगे।\n\n"
        + QUIZ_FORMAT_HELP,
        parse_mode=ParseMode.HTML
    )

    return CREATE_QUESTIONS


# ============================================================
# ROBUST BATCH QUESTION PARSER
# ============================================================

QUESTION_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*)?(\d+)\s*[\.\):\-]\s*(.*)$",
    re.IGNORECASE
)

OPTION_RE = re.compile(
    r"^\s*(?:\(([a-dA-D])\)|([a-dA-D])[\.\):\-])\s*(.*)$"
)

CORRECT_RE = re.compile(
    r"^\s*(?:Correct|Answer|Correct Answer)\s*:\s*"
    r"\(?([A-Da-d])\)?\s*$",
    re.IGNORECASE
)

EXPLANATION_RE = re.compile(
    r"^\s*(?:Ex|Explanation)\s*:\s*(.*)$",
    re.IGNORECASE
)


def clean_option(text: str) -> str:
    return re.sub(r"\s*✅\s*$", "", text).strip()


def parse_quiz_text(raw_text: str):

    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    questions = []
    errors = []

    current = None
    mode = "question"

    def finish_question():

        nonlocal current

        if not current:
            return

        number = current["number"]

        question_text = "\n".join(
            line.rstrip()
            for line in current["question_lines"]
        ).strip()

        options = current["options"]

        correct = current.get("correct")

        # अगर Correct line नहीं है तो ✅ से answer detect करें
        if not correct:
            for letter, value in options.items():
                if "✅" in value:
                    correct = letter
                    break

        if not question_text:
            errors.append(f"Q{number}: Question text missing.")
            current = None
            return

        if len(options) != 4:
            errors.append(
                f"Q{number}: Exactly 4 options required."
            )
            current = None
            return

        if correct not in ("A", "B", "C", "D"):
            errors.append(
                f"Q{number}: Correct answer missing/invalid."
            )
            current = None
            return

        questions.append({
            "question_no": number,
            "question": question_text,
            "option_a": clean_option(options["A"]),
            "option_b": clean_option(options["B"]),
            "option_c": clean_option(options["C"]),
            "option_d": clean_option(options["D"]),
            "correct_option": correct,
            "explanation": current.get(
                "explanation", ""
            ).strip(),
        })

        current = None

    for line in lines:

        # New question
        qmatch = QUESTION_RE.match(line)

        if qmatch:
            finish_question()

            current = {
                "number": int(qmatch.group(1)),
                "question_lines": [],
                "options": {},
                "correct": None,
                "explanation": "",
            }

            first_text = qmatch.group(2).strip()

            if first_text:
                current["question_lines"].append(first_text)

            mode = "question"
            continue

        if not current:
            continue

        # Correct
        cmatch = CORRECT_RE.match(line)

        if cmatch:
            current["correct"] = cmatch.group(1).upper()
            mode = "correct"
            continue

        # Explanation
        ematch = EXPLANATION_RE.match(line)

        if ematch:
            current["explanation"] = ematch.group(1).strip()
            mode = "explanation"
            continue

        # Option
        omatch = OPTION_RE.match(line)

        if omatch:
            letter = (
                omatch.group(1) or omatch.group(2)
            ).upper()

            current["options"][letter] = omatch.group(3).strip()

            mode = "options"
            continue

        # Preserve question context / statements
        if mode == "question":
            current["question_lines"].append(line)

        # Explanation continuation
        elif mode == "explanation":
            if line.strip():
                current["explanation"] += "\n" + line.strip()

    finish_question()

    questions.sort(
        key=lambda x: x["question_no"]
    )

    return questions, errors


# ============================================================
# SAVE QUIZ TO DATABASE
# ============================================================

async def save_quiz_to_database(
    user_id: int,
    draft: dict,
):

    if not db_pool:
        raise RuntimeError(
            "DATABASE_URL / database connection unavailable."
        )

    async with db_pool.acquire() as conn:

        async with conn.transaction():

            quiz = await conn.fetchrow(
                """
                INSERT INTO quizzes
                (
                    creator_id,
                    title,
                    subject,
                    timer_seconds,
                    negative_marking
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                user_id,
                draft["title"],
                draft.get("subject"),
                draft.get("timer", 30),
                draft.get("negative", 0),
            )

            quiz_id = quiz["id"]

            for q in draft["questions"]:

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
                        correct_option,
                        explanation
                    )
                    VALUES
                    (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9
                    )
                    """,
                    quiz_id,
                    q["question_no"],
                    q["question"],
                    q["option_a"],
                    q["option_b"],
                    q["option_c"],
                    q["option_d"],
                    q["correct_option"],
                    q["explanation"],
                )

    return quiz_id


# ============================================================
# HANDLE BATCH QUESTIONS
# ============================================================

async def create_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if user_id not in DRAFTS:
        await update.message.reply_text(
            "❌ कोई active quiz creation नहीं है। /create करें।"
        )
        return ConversationHandler.END

    raw = update.message.text or ""

    questions, errors = parse_quiz_text(raw)

    if not questions:

        await update.message.reply_text(
            "❌ कोई valid question नहीं मिला।\n\n"
            "Format check करें:\n\n"
            + QUIZ_FORMAT_HELP,
            parse_mode=ParseMode.HTML
        )

        return CREATE_QUESTIONS

    DRAFTS[user_id]["questions"] = questions

    preview = build_quiz_preview(
        DRAFTS[user_id],
        limit=5
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "💾 Save Quiz",
                callback_data="draft_save"
            ),
            InlineKeyboardButton(
                "✏️ Edit",
                callback_data="draft_edit"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="draft_cancel"
            )
        ],
    ]

    error_text = ""

    if errors:
        error_text = (
            "\n\n⚠️ <b>Import warnings:</b>\n"
            + "\n".join(
                f"• {html.escape(e)}"
                for e in errors[:20]
            )
        )

    await update.message.reply_text(
        preview + error_text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_QUESTIONS


# ============================================================
# PREVIEW
# ============================================================

def build_quiz_preview(draft: dict, limit=5):

    questions = draft.get("questions", [])

    text = (
        "📋 <b>Quiz Preview</b>\n\n"
        f"<b>Title:</b> "
        f"{html.escape(draft.get('title') or '')}\n"
        f"<b>Subject:</b> "
        f"{html.escape(draft.get('subject') or 'Not specified')}\n"
        f"<b>Questions:</b> {len(questions)}\n"
        f"<b>Timer:</b> {draft.get('timer', 30)} sec\n"
        f"<b>Negative:</b> {draft.get('negative', 0)}\n\n"
    )

    for q in questions[:limit]:

        text += (
            f"<b>Q{q['question_no']}.</b> "
            f"{html.escape(q['question'])}\n"
            f"(a) {html.escape(q['option_a'])}\n"
            f"(b) {html.escape(q['option_b'])}\n"
            f"(c) {html.escape(q['option_c'])}\n"
            f"(d) {html.escape(q['option_d'])}\n"
            f"<b>Correct:</b> {q['correct_option']}\n"
            f"<b>Ex:</b> "
            f"{html.escape(q['explanation'] or '—')}\n\n"
        )

    if len(questions) > limit:
        text += (
            f"… और {len(questions) - limit} questions।\n"
        )

    return text


# ============================================================
# FILE IMPORT
# ============================================================

async def handle_quiz_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id not in DRAFTS:
        await update.message.reply_text(
            "पहले /create से quiz creation शुरू करें।"
        )
        return

    document = update.message.document

    if not document:
        return

    filename = document.file_name or "quiz.txt"

    allowed = (
        ".txt",
        ".json",
        ".csv",
    )

    if not filename.lower().endswith(allowed):
        await update.message.reply_text(
            "❌ Supported files:\n"
            "• .txt\n"
            "• .json\n"
            "• .csv"
        )
        return

    tg_file = await document.get_file()

    data = await tg_file.download_as_bytearray()

    try:
        content = bytes(data).decode(
            "utf-8",
            errors="replace"
        )

    except Exception:
        await update.message.reply_text(
            "❌ File read नहीं हो सकी।"
        )
        return

    # JSON
    if filename.lower().endswith(".json"):

        try:
            parsed = json.loads(content)

            if isinstance(parsed, dict):
                parsed = parsed.get(
                    "questions",
                    []
                )

            converted = []

            for i, q in enumerate(parsed, 1):

                options = q.get(
                    "options",
                    {}
                )

                converted.append(
                    f"Q{i}. {q.get('question', '')}\n"
                    f"(a) {options.get('A', '')}\n"
                    f"(b) {options.get('B', '')}\n"
                    f"(c) {options.get('C', '')}\n"
                    f"(d) {options.get('D', '')}\n"
                    f"Correct: {q.get('correct', '')}\n"
                    f"Ex: {q.get('explanation', '')}\n"
                )

            content = "\n".join(converted)

        except Exception as exc:
            await update.message.reply_text(
                f"❌ JSON error: {exc}"
            )
            return

    # CSV
    elif filename.lower().endswith(".csv"):

        try:

            reader = csv.DictReader(
                io.StringIO(content)
            )

            converted = []

            for i, row in enumerate(reader, 1):

                converted.append(
                    f"Q{i}. {row.get('question', '')}\n"
                    f"(a) {row.get('A', '')}\n"
                    f"(b) {row.get('B', '')}\n"
                    f"(c) {row.get('C', '')}\n"
                    f"(d) {row.get('D', '')}\n"
                    f"Correct: {row.get('correct', '')}\n"
                    f"Ex: {row.get('explanation', '')}\n"
                )

            content = "\n".join(converted)

        except Exception as exc:
            await update.message.reply_text(
                f"❌ CSV error: {exc}"
            )
            return

    questions, errors = parse_quiz_text(content)

    if not questions:

        await update.message.reply_text(
            "❌ File में कोई valid question नहीं मिला।\n\n"
            + QUIZ_FORMAT_HELP,
            parse_mode=ParseMode.HTML
        )

        return

    DRAFTS[user_id]["questions"] = questions

    text = build_quiz_preview(
        DRAFTS[user_id],
        limit=5
    )

    if errors:
        text += (
            "\n⚠️ <b>Warnings:</b>\n"
            + "\n".join(
                f"• {html.escape(e)}"
                for e in errors[:20]
            )
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "💾 Save Quiz",
                callback_data="draft_save"
            ),
            InlineKeyboardButton(
                "✏️ Edit",
                callback_data="draft_edit"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="draft_cancel"
            )
        ],
    ]

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============================================================
# DRAFT CALLBACKS
# ============================================================

async def draft_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    draft = DRAFTS.get(user_id)

    if not draft:
        await query.edit_message_text(
            "❌ Draft नहीं मिला। /create से फिर शुरू करें।"
        )
        return

    if query.data == "draft_cancel":

        DRAFTS.pop(user_id, None)

        await query.edit_message_text(
            "❌ Quiz creation cancelled."
        )

        return

    if query.data == "draft_edit":

        await query.edit_message_text(
            "✏️ <b>Edit Mode</b>\n\n"
            "पूरा question set दोबारा भेजें।\n\n"
            "आप questions, options, correct answers "
            "और explanations बदल सकते हैं।\n\n"
            "Format:\n\n"
            + QUIZ_FORMAT_HELP,
            parse_mode=ParseMode.HTML
        )

        return

    if query.data == "draft_save":

        try:

            quiz_id = await save_quiz_to_database(
                user_id,
                draft
            )

            DRAFTS.pop(user_id, None)

            await query.edit_message_text(
                "✅ <b>Quiz Created Successfully!</b>\n\n"
                f"Quiz ID: <code>{quiz_id}</code>\n"
                f"Questions: <b>{len(draft['questions'])}</b>\n\n"
                "आप इसे /myquizzes से देख सकते हैं।",
                parse_mode=ParseMode.HTML
            )

        except Exception as exc:

            await query.edit_message_text(
                "❌ Quiz save नहीं हो सका।\n\n"
                f"<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML
            )


# ============================================================
# MY QUIZZES
# ============================================================

async def myquizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not db_pool:
        await update.message.reply_text(
            "Database unavailable."
        )
        return

    rows = await fetch(
        """
        SELECT
            q.id,
            q.title,
            q.subject,
            q.is_published,
            COUNT(qq.id) AS question_count
        FROM quizzes q
        LEFT JOIN quiz_questions qq
            ON qq.quiz_id = q.id
        WHERE q.creator_id = $1
        GROUP BY q.id
        ORDER BY q.created_at DESC
        LIMIT 30
        """,
        user_id
    )

    if not rows:

        await update.message.reply_text(
            "📚 अभी आपने कोई quiz create नहीं किया है।\n\n"
            "👉 /create"
        )

        return

    for row in rows:

        status = (
            "🟢 Published"
            if row["is_published"]
            else "🟡 Draft"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data=f"stored_preview:{row['id']}"
                ),
                InlineKeyboardButton(
                    "✏️ Edit",
                    callback_data=f"edit_questions:{row['id']}"
                ),
            ]
        ])

        await update.message.reply_text(
            f"📝 <b>{html.escape(row['title'])}</b>\n\n"
            f"Quiz ID: <code>{row['id']}</code>\n"
            f"Subject: {html.escape(row['subject'] or '—')}\n"
            f"Questions: {row['question_count']}\n"
            f"Status: {status}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# ============================================================
# SETTINGS
# ============================================================

async def settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏱ Timer",
                callback_data="setting_timer"
            ),
            InlineKeyboardButton(
                "➖ Negative",
                callback_data="setting_negative"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Close",
                callback_data="setting_close"
            )
        ],
    ])

    await update.message.reply_text(
        "⚙️ <b>Quiz Settings</b>\n\n"
        "Timer और negative marking configure करें।",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def settings_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    if query.data == "setting_close":
        await query.edit_message_text(
            "⚙️ Settings closed."
        )
        return

    if query.data == "setting_timer":
        await query.edit_message_text(
            "⏱ Timer setting:\n\n"
            "नई quiz बनाते समय timer configure करें।"
        )

    elif query.data == "setting_negative":
        await query.edit_message_text(
            "➖ Negative marking setting:\n\n"
            "नई quiz बनाते समय negative marking configure करें।"
        )


# ============================================================
# STOP
# ============================================================

async def stop_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id in ACTIVE_QUIZZES:
        ACTIVE_QUIZZES.pop(user_id, None)

        await update.message.reply_text(
            "🛑 Current quiz stopped."
        )
        return

    await update.message.reply_text(
        "कोई active quiz नहीं चल रहा है।"
    )


# ============================================================
# PAUSE
# ============================================================

async def pause_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id not in ACTIVE_QUIZZES:

        await update.message.reply_text(
            "कोई active quiz नहीं है।"
        )
        return

    PAUSED_QUIZZES[user_id] = ACTIVE_QUIZZES[user_id]

    await update.message.reply_text(
        "⏸ Quiz paused.\n\n"
        "Resume करने के लिए /resume"
    )


# ============================================================
# RESUME
# ============================================================

async def resume_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id not in PAUSED_QUIZZES:

        await update.message.reply_text(
            "कोई paused quiz नहीं है।"
        )
        return

    ACTIVE_QUIZZES[user_id] = PAUSED_QUIZZES.pop(
        user_id
    )

    await update.message.reply_text(
        "▶️ Quiz resumed."
    )


# ============================================================
# FAST
# ============================================================

async def fast_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    quiz = ACTIVE_QUIZZES.get(user_id)

    if not quiz:

        await update.message.reply_text(
            "कोई active quiz नहीं है।"
        )
        return

    current = quiz.get("timer", 30)

    quiz["timer"] = max(
        5,
        current - 5
    )

    await update.message.reply_text(
        f"⚡ Timer decreased: "
        f"<b>{quiz['timer']} sec</b>",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# SLOW
# ============================================================

async def slow_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    quiz = ACTIVE_QUIZZES.get(user_id)

    if not quiz:

        await update.message.reply_text(
            "कोई active quiz नहीं है।"
        )
        return

    current = quiz.get("timer", 30)

    quiz["timer"] = min(
        3600,
        current + 5
    )

    await update.message.reply_text(
        f"🐢 Timer increased: "
        f"<b>{quiz['timer']} sec</b>",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# STOP POLL
# ============================================================

async def stop_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🛑 Active poll stop request received.\n\n"
        "यह feature Telegram poll message के "
        "active poll ID के साथ integrate किया जा सकता है।"
    )


# ============================================================
# MISTAKES
# ============================================================

async def mistakes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not db_pool:
        await update.message.reply_text(
            "Database unavailable."
        )
        return

    rows = await fetch(
        """
        SELECT
            qq.question,
            qq.option_a,
            qq.option_b,
            qq.option_c,
            qq.option_d,
            qq.correct_option,
            qq.explanation
        FROM quiz_answers qa
        JOIN quiz_questions qq
            ON qq.id = qa.question_id
        WHERE qa.user_id = $1
          AND qa.is_correct = FALSE
        ORDER BY qa.answered_at DESC
        LIMIT 20
        """,
        user_id
    )

    if not rows:

        await update.message.reply_text(
            "✅ अभी कोई incorrectly answered question नहीं है।"
        )
        return

    text = "❌ <b>Your Mistakes</b>\n\n"

    for i, row in enumerate(rows, 1):

        text += (
            f"<b>{i}. {html.escape(row['question'])}</b>\n"
            f"(a) {html.escape(row['option_a'])}\n"
            f"(b) {html.escape(row['option_b'])}\n"
            f"(c) {html.escape(row['option_c'])}\n"
            f"(d) {html.escape(row['option_d'])}\n"
            f"Correct: <b>{row['correct_option']}</b>\n"
            f"Ex: {html.escape(row['explanation'] or '—')}\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


# ============================================================
# BOOKMARKS
# ============================================================

async def bookmarks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not db_pool:
        await update.message.reply_text(
            "Database unavailable."
        )
        return

    rows = await fetch(
        """
        SELECT
            qq.question,
            qq.option_a,
            qq.option_b,
            qq.option_c,
            qq.option_d,
            qq.correct_option,
            qq.explanation
        FROM quiz_bookmarks qb
        JOIN quiz_questions qq
            ON qq.id = qb.question_id
        WHERE qb.user_id = $1
        ORDER BY qb.id DESC
        LIMIT 20
        """,
        user_id
    )

    if not rows:

        await update.message.reply_text(
            "🔖 अभी कोई bookmarked question नहीं है।"
        )
        return

    text = "🔖 <b>Bookmarked Questions</b>\n\n"

    for i, row in enumerate(rows, 1):

        text += (
            f"<b>{i}. {html.escape(row['question'])}</b>\n"
            f"(a) {html.escape(row['option_a'])}\n"
            f"(b) {html.escape(row['option_b'])}\n"
            f"(c) {html.escape(row['option_c'])}\n"
            f"(d) {html.escape(row['option_d'])}\n"
            f"Correct: <b>{row['correct_option']}</b>\n"
            f"Ex: {html.escape(row['explanation'] or '—')}\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


# ============================================================
# SECTION QUIZ
# ============================================================

async def section(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    DRAFTS[user_id] = {
        "title": None,
        "subject": None,
        "timer": 30,
        "negative": 0,
        "questions": [],
        "sections": [],
    }

    await update.message.reply_text(
        "📚 <b>Multi-Section Quiz</b>\n\n"
        "Section format भेजें:\n\n"
        "<pre>"
        "Section 1 | History | 300\n"
        "Section 2 | Polity | 240\n"
        "Section 3 | Geography | 180"
        "</pre>\n\n"
        "Format:\n"
        "<code>Section Name | Metadata | Timer Seconds</code>",
        parse_mode=ParseMode.HTML
    )

    return SECTION_DATA


async def section_data(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    sections = []

    for line in update.message.text.splitlines():

        parts = [
            x.strip()
            for x in line.split("|")
        ]

        if len(parts) != 3:
            continue

        try:
            timer = int(parts[2])
        except ValueError:
            continue

        sections.append({
            "name": parts[0],
            "metadata": parts[1],
            "timer": timer,
        })

    if not sections:

        await update.message.reply_text(
            "❌ Valid section नहीं मिली।"
        )

        return SECTION_DATA

    DRAFTS[user_id]["sections"] = sections

    await update.message.reply_text(
        f"✅ {len(sections)} sections configured.\n\n"
        "अब /create से questions वाले quiz flow का "
        "इस्तेमाल करके questions import कर सकते हैं।"
    )

    return ConversationHandler.END


# ============================================================
# TESTBOOK IMPORT
# ============================================================

def html_to_text(page: str) -> str:

    page = re.sub(
        r"<script.*?</script>",
        " ",
        page,
        flags=re.I | re.S
    )

    page = re.sub(
        r"<style.*?</style>",
        " ",
        page,
        flags=re.I | re.S
    )

    page = re.sub(
        r"<[^>]+>",
        "\n",
        page
    )

    page = html.unescape(page)

    return re.sub(
        r"\n{3,}",
        "\n\n",
        page
    )


async def testbook_import(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    args = context.args

    if not args:

        await update.message.reply_text(
            "📥 <b>Testbook Import</b>\n\n"
            "Usage:\n"
            "<code>/testbook https://...</code>\n\n"
            "Note: Modern JavaScript-rendered pages "
            "may not expose questions directly.",
            parse_mode=ParseMode.HTML
        )

        return

    url = args[0].strip()

    if not (
        url.startswith("https://")
        or url.startswith("http://")
    ):

        await update.message.reply_text(
            "❌ Valid URL भेजें।"
        )
        return

    await update.message.reply_text(
        "⏳ Testbook page पढ़ी जा रही है..."
    )

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                "Mozilla/5.0"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="ignore"
            )

        text = html_to_text(raw)

        questions, errors = parse_quiz_text(text)

        if not questions:

            await update.message.reply_text(
                "⚠️ इस Testbook URL से questions "
                "directly extract नहीं हो पाए।\n\n"
                "ऐसे case में Testbook content को "
                ".txt/.json/.csv format में import करें।"
            )

            return

        user_id = update.effective_user.id

        DRAFTS[user_id] = {
            "title": "Imported Testbook Quiz",
            "subject": None,
            "timer": 30,
            "negative": 0,
            "questions": questions,
            "sections": [],
        }

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💾 Save Quiz",
                    callback_data="draft_save"
                ),
                InlineKeyboardButton(
                    "✏️ Edit",
                    callback_data="draft_edit"
                )
            ]
        ])

        await update.message.reply_text(
            build_quiz_preview(
                DRAFTS[user_id],
                limit=5
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )

    except Exception as exc:

        await update.message.reply_text(
            "❌ Testbook import failed.\n\n"
            "यदि page JavaScript-rendered है, "
            "तो direct import संभव नहीं हो सकता।\n\n"
            f"<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML
        )


# ============================================================
# TUTORIAL
# ============================================================

async def tutorial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🎓 <b>Quiz Creator Tutorial</b>\n\n"
        "<b>Step 1</b>\n"
        "/create दबाएँ।\n\n"
        "<b>Step 2</b>\n"
        "Quiz title दें।\n\n"
        "<b>Step 3</b>\n"
        "Optional subject metadata दें या skip करें।\n\n"
        "<b>Step 4</b>\n"
        "Timer और negative marking configure करें।\n\n"
        "<b>Step 5</b>\n"
        "एक ही message या TXT/JSON/CSV file में "
        "सारे questions भेजें।\n\n"
        "<b>Step 6</b>\n"
        "Preview check करें।\n\n"
        "<b>Step 7</b>\n"
        "Save Quiz दबाएँ।\n\n"
        "👉 /create\n"
        "👉 /myquizzes\n"
        "👉 /settings",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    DRAFTS.pop(user_id, None)

    await update.message.reply_text(
        "❌ Current creation/editing process cancelled."
    )

    return ConversationHandler.END


# ============================================================
# UNKNOWN COMMAND
# IMPORTANT:
# Must be registered AFTER valid command handlers
# in the same handler group.
# ============================================================

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "❓ Unknown command.\n\n"
        "Available commands देखने के लिए:\n"
        "👉 /help"
    )


# ============================================================
# STORED QUIZ PREVIEW
# ============================================================

async def stored_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    quiz_id = int(
        query.data.split(":")[1]
    )

    quiz = await fetchrow(
        """
        SELECT *
        FROM quizzes
        WHERE id = $1
          AND creator_id = $2
        """,
        quiz_id,
        user_id
    )

    if not quiz:

        await query.edit_message_text(
            "❌ Quiz नहीं मिला।"
        )
        return

    questions = await fetch(
        """
        SELECT *
        FROM quiz_questions
        WHERE quiz_id = $1
        ORDER BY question_no
        LIMIT 5
        """,
        quiz_id
    )

    text = (
        f"📋 <b>{html.escape(quiz['title'])}</b>\n\n"
        f"Questions: {len(questions)}\n\n"
    )

    for q in questions:

        text += (
            f"<b>Q{q['question_no']}.</b> "
            f"{html.escape(q['question'])}\n"
            f"(a) {html.escape(q['option_a'])}\n"
            f"(b) {html.escape(q['option_b'])}\n"
            f"(c) {html.escape(q['option_c'])}\n"
            f"(d) {html.escape(q['option_d'])}\n"
            f"Correct: {q['correct_option']}\n\n"
        )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML
    )


# ============================================================
# EDIT STORED QUESTIONS
# ============================================================

async def edit_questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    quiz_id = int(
        query.data.split(":")[1]
    )

    quiz = await fetchrow(
        """
        SELECT *
        FROM quizzes
        WHERE id = $1
          AND creator_id = $2
        """,
        quiz_id,
        user_id
    )

    if not quiz:

        await query.edit_message_text(
            "❌ Quiz नहीं मिला।"
        )
        return

    questions = await fetch(
        """
        SELECT *
        FROM quiz_questions
        WHERE quiz_id = $1
        ORDER BY question_no
        """,
        quiz_id
    )

    draft_questions = []

    for q in questions:

        draft_questions.append({
            "question_no": q["question_no"],
            "question": q["question"],
            "option_a": q["option_a"],
            "option_b": q["option_b"],
            "option_c": q["option_c"],
            "option_d": q["option_d"],
            "correct_option": q["correct_option"],
            "explanation": q["explanation"] or "",
        })

    DRAFTS[user_id] = {
        "quiz_id": quiz_id,
        "title": quiz["title"],
        "subject": quiz["subject"],
        "timer": quiz["timer_seconds"],
        "negative": float(
            quiz["negative_marking"] or 0
        ),
        "questions": draft_questions,
        "sections": [],
    }

    await query.edit_message_text(
        "✏️ <b>Edit Mode Activated</b>\n\n"
        "अब पूरा question set भेजें।\n"
        "पुराने questions replace होकर नया batch save होगा।\n\n"
        + QUIZ_FORMAT_HELP,
        parse_mode=ParseMode.HTML
    )


# ============================================================
# UPDATE EXISTING QUIZ
# ============================================================

async def save_existing_quiz(
    user_id: int,
    draft: dict
):

    quiz_id = draft["quiz_id"]

    async with db_pool.acquire() as conn:

        async with conn.transaction():

            await conn.execute(
                """
                UPDATE quizzes
                SET title = $1,
                    subject = $2,
                    timer_seconds = $3,
                    negative_marking = $4
                WHERE id = $5
                  AND creator_id = $6
                """,
                draft["title"],
                draft["subject"],
                draft["timer"],
                draft["negative"],
                quiz_id,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM quiz_questions
                WHERE quiz_id = $1
                """,
                quiz_id
            )

            for q in draft["questions"]:

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
                        correct_option,
                        explanation
                    )
                    VALUES
                    ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    """,
                    quiz_id,
                    q["question_no"],
                    q["question"],
                    q["option_a"],
                    q["option_b"],
                    q["option_c"],
                    q["option_d"],
                    q["correct_option"],
                    q["explanation"],
                )


# ============================================================
# HANDLE NORMAL TEXT AFTER EDIT
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    draft = DRAFTS.get(user_id)

    if not draft:
        return

    questions, errors = parse_quiz_text(
        update.message.text
    )

    if not questions:

        await update.message.reply_text(
            "❌ Valid questions नहीं मिले।\n\n"
            + QUIZ_FORMAT_HELP,
            parse_mode=ParseMode.HTML
        )

        return

    draft["questions"] = questions

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💾 Save Changes",
                callback_data="draft_save"
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="draft_cancel"
            )
        ]
    ])

    await update.message.reply_text(
        build_quiz_preview(
            draft,
            limit=5
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


# ============================================================
# FINAL DRAFT SAVE OVERRIDE
# ============================================================

async def final_draft_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    draft = DRAFTS.get(user_id)

    if not draft:

        await query.edit_message_text(
            "❌ Draft not found."
        )
        return

    if query.data == "draft_cancel":

        DRAFTS.pop(user_id, None)

        await query.edit_message_text(
            "❌ Cancelled."
        )

        return

    if query.data == "draft_edit":

        await query.edit_message_text(
            "✏️ पूरा question batch दोबारा भेजें।\n\n"
            + QUIZ_FORMAT_HELP,
            parse_mode=ParseMode.HTML
        )

        return

    if query.data == "draft_save":

        try:

            if "quiz_id" in draft:

                await save_existing_quiz(
                    user_id,
                    draft
                )

                message = (
                    "✅ <b>Quiz Updated Successfully!</b>\n\n"
                    f"Questions: {len(draft['questions'])}"
                )

            else:

                quiz_id = await save_quiz_to_database(
                    user_id,
                    draft
                )

                message = (
                    "✅ <b>Quiz Created Successfully!</b>\n\n"
                    f"Quiz ID: <code>{quiz_id}</code>\n"
                    f"Questions: {len(draft['questions'])}"
                )

            DRAFTS.pop(user_id, None)

            await query.edit_message_text(
                message,
                parse_mode=ParseMode.HTML
            )

        except Exception as exc:

            await query.edit_message_text(
                "❌ Database error:\n\n"
                f"<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML
            )


# ============================================================
# REGISTER COMMANDS / HANDLERS
# ============================================================

def register_handlers(application: Application):

    # --------------------------------------------------------
    # Conversation: /create
    # --------------------------------------------------------

    create_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "create",
                create_quiz
            )
        ],

        states={

            CREATE_TITLE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_title
                )
            ],

            CREATE_SUBJECT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_subject
                )
            ],

            CREATE_TIMER: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_timer
                )
            ],

            CREATE_NEGATIVE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_negative
                )
            ],

            CREATE_QUESTIONS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    create_questions
                ),
                MessageHandler(
                    filters.Document.ALL,
                    handle_quiz_file
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel
            )
        ],

        allow_reentry=True,
    )

    application.add_handler(
        create_conversation
    )

    # --------------------------------------------------------
    # SECTION
    # --------------------------------------------------------

    section_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "section",
                section
            )
        ],

        states={
            SECTION_DATA: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    section_data
                )
            ]
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel
            )
        ],

        allow_reentry=True,
    )

    application.add_handler(
        section_conversation
    )

    # --------------------------------------------------------
    # MAIN COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
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
        CommandHandler("testbook", testbook_import)
    )

    application.add_handler(
        CommandHandler("tutorial", tutorial)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            settings_callback,
            pattern=r"^setting_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            final_draft_callback,
            pattern=r"^draft_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            stored_preview,
            pattern=r"^stored_preview:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            edit_questions,
            pattern=r"^edit_questions:"
        )
    )

    # --------------------------------------------------------
    # UNKNOWN COMMAND
    # MUST BE LAST
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command
        )
    )


# ============================================================
# SET TELEGRAM COMMAND MENU
# ============================================================

async def set_bot_commands(application: Application):

    await application.bot.set_my_commands(
        [
            BotCommand(
                command=command,
                description=description
            )
            for command, description in BOT_COMMANDS
        ]
    )


# ============================================================
# STARTUP FUNCTION
# ============================================================

async def post_init(application: Application):

    await ensure_quiz_tables()

    await set_bot_commands(
        application
    )
