"""Telegram handlers for Brain Booster BPSC Quiz Bot."""

import logging
import os
from html import escape

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

# ---------------------------------------------------------
# ADMIN
# ---------------------------------------------------------

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id == ADMIN_ID


async def admin_only(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message

    if not is_admin(user.id if user else None):
        if message:
            await message.reply_text(
                "⛔ यह command केवल bot administrator के लिए है।"
            )
        return False

    return True


# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------

BOT_COMMANDS = (
    ("start", "Start the bot"),
    ("create", "Create a new quiz"),
    ("myquizzes", "View your quizzes"),
    ("editquiz", "Edit a quiz"),
    ("deletequiz", "Delete a quiz"),
    ("publish", "Publish a quiz"),
    ("preview", "Preview a quiz"),
    ("addquestion", "Add a question"),
    ("cancel", "Cancel current process"),
    ("help", "Show help"),
)

# ---------------------------------------------------------
# NORMAL USER MENU
# ---------------------------------------------------------

MENU_HELP = "Help"
MENU_ABOUT = "About"
MENU_PING = "Ping"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_HELP, MENU_ABOUT], [MENU_PING]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Choose a menu item",
)

# ---------------------------------------------------------
# QUIZ CREATION STATES
# ---------------------------------------------------------

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

# ---------------------------------------------------------
# DATABASE SCHEMA
# ---------------------------------------------------------

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


async def ensure_quiz_tables(pool) -> None:
    if pool is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(CREATE_QUIZZES_TABLE)
        await conn.execute(CREATE_QUESTIONS_TABLE)


# ---------------------------------------------------------
# START
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    pool = context.bot_data.get(DB_KEY)

    if pool is not None:
        await db.upsert_user(
            pool,
            user.id,
            user.username,
            user.first_name,
        )

        await ensure_quiz_tables(pool)

    name = user.first_name or "friend"

    await message.reply_text(
        f"Welcome to Brain Booster BPSC Quiz Bot, {name}!\n\n"
        "Choose a practice mode or use /help to see commands.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


# ---------------------------------------------------------
# HELP
# ---------------------------------------------------------

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    text = """
<b>Brain Booster BPSC Quiz Bot</b>

Available commands:

/start - Start the bot
/help - Show help
/about - Bot information
/ping - Check bot status

<b>Admin Commands</b>

/create - Create a new quiz
/myquizzes - View created quizzes
/editquiz - Edit a quiz
/deletequiz - Delete a quiz
/publish - Publish a quiz
/preview - Preview a quiz
/addquestion - Add question
/cancel - Cancel current process
"""

    await message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------
# ABOUT
# ---------------------------------------------------------

async def about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "Brain Booster BPSC Quiz Bot\n\n"
        "A BPSC-focused quiz platform with timed tests, "
        "question explanations, scoring and admin quiz creation."
    )


# ---------------------------------------------------------
# PING
# ---------------------------------------------------------

async def ping(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    client = context.bot_data.get(REDIS_KEY)

    if client is None:
        await message.reply_text("pong")
        return

    cached = await cache.get_or_set_ping(client)
    source = "cached" if cached else "fresh"

    await message.reply_text(f"pong ({source})")


# ---------------------------------------------------------
# CREATE QUIZ
# ---------------------------------------------------------

async def create_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

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
        "current_question": {},
    }

    await message.reply_text(
        "📝 <b>Create New Quiz</b>\n\n"
        "Step 1/6\n"
        "Quiz का title भेजें:",
        parse_mode="HTML",
    )


# ---------------------------------------------------------
# CANCEL
# ---------------------------------------------------------

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    if user.id in DRAFTS:
        del DRAFTS[user.id]
        await message.reply_text("❌ Current quiz creation cancelled.")
    else:
        await message.reply_text("कोई active process नहीं है।")


# ---------------------------------------------------------
# CREATE QUIZ TEXT PROCESSOR
# ---------------------------------------------------------

async def process_quiz_creation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None or not message.text:
        return

    if not is_admin(user.id):
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        return

    text = message.text.strip()
    state = draft["state"]

    # ---------------- TITLE ----------------

    if state == STATE_TITLE:

        draft["title"] = text
        draft["state"] = STATE_SUBJECT

        await message.reply_text(
            "📚 Step 2/6\n\n"
            "Subject भेजें।\n"
            "अगर subject नहीं देना है तो <b>skip</b> लिखें।",
            parse_mode="HTML",
        )
        return

    # ---------------- SUBJECT ----------------

    if state == STATE_SUBJECT:

        draft["subject"] = "" if text.lower() == "skip" else text
        draft["state"] = STATE_COUNT

        await message.reply_text(
            "🔢 Step 3/6\n\n"
            "कितने questions बनाने हैं?\n\n"
            "उदाहरण: <b>10</b>",
            parse_mode="HTML",
        )
        return

    # ---------------- COUNT ----------------

    if state == STATE_COUNT:

        try:
            count = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल संख्या भेजें। उदाहरण: 10"
            )
            return

        if count < 1 or count > 500:
            await message.reply_text(
                "❌ Questions की संख्या 1 से 500 के बीच रखें।"
            )
            return

        draft["question_count"] = count
        draft["state"] = STATE_TIME

        await message.reply_text(
            "⏱️ Step 4/6\n\n"
            "Time limit कितने मिनट का होगा?\n\n"
            "उदाहरण: <b>30</b>",
            parse_mode="HTML",
        )
        return

    # ---------------- TIME ----------------

    if state == STATE_TIME:

        try:
            minutes = int(text)
        except ValueError:
            await message.reply_text(
                "❌ केवल मिनट की संख्या भेजें।"
            )
            return

        if minutes < 1 or minutes > 300:
            await message.reply_text(
                "❌ Time limit 1 से 300 मिनट के बीच रखें।"
            )
            return

        draft["time_limit"] = minutes
        draft["state"] = STATE_NEGATIVE

        await message.reply_text(
            "➖ Step 5/6\n\n"
            "Negative marking कितनी होगी?\n\n"
            "उदाहरण:\n"
            "0 = No negative marking\n"
            "0.25 = 1/4 negative\n"
            "0.33 = 1/3 negative\n\n"
            "Default: <b>0</b>",
            parse_mode="HTML",
        )
        return

    # ---------------- NEGATIVE MARKING ----------------

    if state == STATE_NEGATIVE:

        try:
            negative = float(text)
        except ValueError:
            await message.reply_text(
                "❌ उदाहरण के लिए 0, 0.25 या 0.33 भेजें।"
            )
            return

        if negative < 0 or negative > 1:
            await message.reply_text(
                "❌ Negative marking 0 से 1 के बीच रखें।"
            )
            return

        draft["negative_marking"] = negative
        draft["state"] = STATE_QUESTION

        await message.reply_text(
            "📌 Step 6/6\n\n"
            "अब Question 1 भेजें:",
            parse_mode="HTML",
        )
        return

    # ---------------- QUESTION ----------------

    if state == STATE_QUESTION:

        draft["current_question"] = {
            "question": text,
        }

        draft["state"] = STATE_OPTION_A

        await message.reply_text(
            "🔹 Option A भेजें:"
        )
        return

    # ---------------- OPTION A ----------------

    if state == STATE_OPTION_A:

        draft["current_question"]["option_a"] = text
        draft["state"] = STATE_OPTION_B

        await message.reply_text("🔹 Option B भेजें:")
        return

    # ---------------- OPTION B ----------------

    if state == STATE_OPTION_B:

        draft["current_question"]["option_b"] = text
        draft["state"] = STATE_OPTION_C

        await message.reply_text("🔹 Option C भेजें:")
        return

    # ---------------- OPTION C ----------------

    if state == STATE_OPTION_C:

        draft["current_question"]["option_c"] = text
        draft["state"] = STATE_OPTION_D

        await message.reply_text("🔹 Option D भेजें:")
        return

    # ---------------- OPTION D ----------------

    if state == STATE_OPTION_D:

        draft["current_question"]["option_d"] = text
        draft["state"] = STATE_CORRECT

        await message.reply_text(
            "✅ Correct answer कौन सा है?\n\n"
            "केवल भेजें:\n"
            "<b>A</b> / <b>B</b> / <b>C</b> / <b>D</b>",
            parse_mode="HTML",
        )
        return

    # ---------------- CORRECT ANSWER ----------------

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
            "अगर explanation नहीं देना है तो <b>skip</b> लिखें。",
            parse_mode="HTML",
        )
        return

    # ---------------- EXPLANATION ----------------

    if state == STATE_EXPLANATION:

        draft["current_question"]["explanation"] = (
            "" if text.lower() == "skip" else text
        )

        draft["questions"].append(
            draft["current_question"].copy()
        )

        draft["current_question"] = {}

        question_number = len(draft["questions"])
        required = draft["question_count"]

        if question_number >= required:

            draft["state"] = "complete"

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👁 Preview Quiz",
                            callback_data="quiz_preview",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "➕ Add Next Question",
                            callback_data="quiz_add",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "💾 Finish Quiz",
                            callback_data="quiz_finish",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Cancel",
                            callback_data="quiz_cancel",
                        )
                    ],
                ]
            )

            await message.reply_text(
                f"✅ {question_number}/{required} questions तैयार हैं।\n\n"
                "अब क्या करना है?",
                reply_markup=keyboard,
            )

        else:

            draft["state"] = STATE_QUESTION

            await message.reply_text(
                f"✅ Question {question_number} saved.\n\n"
                f"अब Question {question_number + 1} भेजें:"
            )

        return


# ---------------------------------------------------------
# QUIZ PREVIEW
# ---------------------------------------------------------

def format_preview(draft: dict) -> str:

    title = escape(draft["title"])
    subject = escape(draft["subject"] or "Not specified")

    text = (
        "<b>📋 QUIZ PREVIEW</b>\n\n"
        f"<b>Title:</b> {title}\n"
        f"<b>Subject:</b> {subject}\n"
        f"<b>Questions:</b> {len(draft['questions'])}/{draft['question_count']}\n"
        f"<b>Time:</b> {draft['time_limit']} minutes\n"
        f"<b>Negative:</b> {draft['negative_marking']}\n\n"
    )

    for index, q in enumerate(draft["questions"], 1):

        text += (
            f"<b>Q{index}.</b> {escape(q['question'])}\n"
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


async def preview_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        await message.reply_text(
            "कोई active quiz draft नहीं मिला। /create से शुरू करें।"
        )
        return

    await message.reply_text(
        format_preview(draft),
        parse_mode="HTML",
    )


# ---------------------------------------------------------
# CALLBACK BUTTONS
# ---------------------------------------------------------

async def quiz_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if query is None:
        return

    await query.answer()

    user = query.from_user

    if not is_admin(user.id):
        await query.answer(
            "Only admin can use this button.",
            show_alert=True,
        )
        return

    draft = DRAFTS.get(user.id)

    if not draft:
        await query.message.reply_text(
            "Draft नहीं मिला। /create से नया quiz बनाएं।"
        )
        return

    action = query.data

    # ---------------- PREVIEW ----------------

    if action == "quiz_preview":

        await query.message.reply_text(
            format_preview(draft),
            parse_mode="HTML",
        )
        return

    # ---------------- ADD QUESTION ----------------

    if action == "quiz_add":

        draft["state"] = STATE_QUESTION

        await query.message.reply_text(
            f"➕ Question {len(draft['questions']) + 1} भेजें:"
        )
        return

    # ---------------- CANCEL ----------------

    if action == "quiz_cancel":

        del DRAFTS[user.id]

        await query.message.reply_text(
            "❌ Quiz draft cancelled."
        )
        return

    # ---------------- FINISH ----------------

    if action == "quiz_finish":

        pool = context.bot_data.get(DB_KEY)

        if pool is None:
            await query.message.reply_text(
                "⚠️ PostgreSQL database connected नहीं है।\n"
                "पहले DATABASE_URL configure करें।"
            )
            return

        if not draft["questions"]:
            await query.message.reply_text(
                "❌ Quiz में कम से कम 1 question होना जरूरी है।"
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
                    published
                )
                VALUES ($1, $2, $3, $4, $5, $6, FALSE)
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

        del DRAFTS[user.id]

        await query.message.reply_text(
            f"✅ <b>Quiz saved successfully!</b>\n\n"
            f"Quiz ID: <code>{quiz_id}</code>\n"
            f"Title: {escape(draft['title'])}\n"
            f"Questions: {len(draft['questions'])}\n\n"
            f"Quiz अभी unpublished है।\n"
            f"/publish {quiz_id} से publish करें।",
            parse_mode="HTML",
        )


# ---------------------------------------------------------
# MY QUIZZES
# ---------------------------------------------------------

async def my_quizzes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    message = update.effective_message
    user = update.effective_user
    pool = context.bot_data.get(DB_KEY)

    if message is None or user is None:
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है।"
        )
        return

    await ensure_quiz_tables(pool)

    rows = await pool.fetch(
        """
        SELECT id, title, subject, question_count,
               time_limit, negative_marking, published
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

    text = "<b>📚 Your Quizzes</b>\n\n"

    for row in rows:

        status = "🟢 Published" if row["published"] else "🟡 Draft"

        text += (
            f"<b>ID:</b> {row['id']}\n"
            f"<b>{escape(row['title'])}</b>\n"
            f"Questions: {row['question_count']}\n"
            f"Time: {row['time_limit']} min\n"
            f"Negative: {row['negative_marking']}\n"
            f"Status: {status}\n\n"
        )

    await message.reply_text(
        text,
        parse_mode="HTML",
    )


# ---------------------------------------------------------
# PUBLISH
# ---------------------------------------------------------

async def publish_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    message = update.effective_message
    user = update.effective_user
    pool = context.bot_data.get(DB_KEY)

    if message is None or user is None:
        return

    if not context.args:
        await message.reply_text(
            "Usage:\n/publish QUIZ_ID\n\n"
            "Example:\n/publish 1"
        )
        return

    try:
        quiz_id = int(context.args[0])
    except ValueError:
        await message.reply_text(
            "❌ Quiz ID number होना चाहिए।"
        )
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है।"
        )
        return

    result = await pool.execute(
        """
        UPDATE quizzes
        SET published = TRUE
        WHERE id = $1
          AND creator_id = $2
          AND question_count > 0;
        """,
        quiz_id,
        user.id,
    )

    if result.endswith("0"):
        await message.reply_text(
            "❌ Quiz नहीं मिला या publish नहीं किया जा सकता।"
        )
    else:
        await message.reply_text(
            f"🚀 Quiz <b>#{quiz_id}</b> successfully published!",
            parse_mode="HTML",
        )


# ---------------------------------------------------------
# DELETE QUIZ
# ---------------------------------------------------------

async def delete_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    message = update.effective_message
    user = update.effective_user
    pool = context.bot_data.get(DB_KEY)

    if message is None or user is None:
        return

    if not context.args:
        await message.reply_text(
            "Usage:\n/deletequiz QUIZ_ID"
        )
        return

    try:
        quiz_id = int(context.args[0])
    except ValueError:
        await message.reply_text(
            "❌ Invalid Quiz ID."
        )
        return

    if pool is None:
        await message.reply_text(
            "⚠️ Database connected नहीं है।"
        )
        return

    result = await pool.execute(
        """
        DELETE FROM quizzes
        WHERE id = $1 AND creator_id = $2;
        """,
        quiz_id,
        user.id,
    )

    if result.endswith("0"):
        await message.reply_text(
            "❌ Quiz नहीं मिला।"
        )
    else:
        await message.reply_text(
            f"🗑 Quiz #{quiz_id} deleted successfully."
        )


# ---------------------------------------------------------
# ADD QUESTION
# ---------------------------------------------------------

async def add_question_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    await message.reply_text(
        "➕ New question जोड़ने के लिए /create इस्तेमाल करें।\n\n"
        "Quiz creation में आप जितने चाहें questions add कर सकते हैं।"
    )


# ---------------------------------------------------------
# EDIT QUIZ
# ---------------------------------------------------------

async def edit_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "✏️ Quiz editing system तैयार है।\n\n"
        "Existing quiz देखने के लिए:\n"
        "/myquizzes\n\n"
        "नया quiz बनाने के लिए:\n"
        "/create"
    )


# ---------------------------------------------------------
# UNKNOWN COMMAND
# ---------------------------------------------------------

async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    await message.reply_text(
        "Unknown command.\n"
        "/help लिखकर available commands देखें।"
    )


# ---------------------------------------------------------
# MENU BUTTONS
# ---------------------------------------------------------

async def menu_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None or not message.text:
        return

    text = message.text.strip()

    if text == MENU_HELP:
        await help_command(update, context)

    elif text == MENU_ABOUT:
        await about(update, context)

    elif text == MENU_PING:
        await ping(update, context)


# ---------------------------------------------------------
# ERROR HANDLER
# ---------------------------------------------------------

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    error = context.error

    if isinstance(error, (Conflict, NetworkError, TimedOut)):
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


# ---------------------------------------------------------
# SET TELEGRAM COMMANDS
# ---------------------------------------------------------

async def set_bot_commands(
    application: Application,
) -> None:

    await application.bot.set_my_commands(
        BOT_COMMANDS
    )


# ---------------------------------------------------------
# REGISTER HANDLERS
# ---------------------------------------------------------

def register_handlers(
    application: Application,
) -> None:

    # Normal commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("about", about)
    )

    application.add_handler(
        CommandHandler("ping", ping)
    )

    # Admin commands
    application.add_handler(
        CommandHandler("create", create_quiz)
    )

    application.add_handler(
        CommandHandler("myquizzes", my_quizzes)
    )

    application.add_handler(
        CommandHandler("editquiz", edit_quiz)
    )

    application.add_handler(
        CommandHandler("deletequiz", delete_quiz)
    )

    application.add_handler(
        CommandHandler("publish", publish_quiz)
    )

    application.add_handler(
        CommandHandler("preview", preview_quiz)
    )

    application.add_handler(
        CommandHandler("addquestion", add_question_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    # Inline buttons
    application.add_handler(
        CallbackQueryHandler(
            quiz_buttons,
            pattern=r"^quiz_"
        )
    )

    # Quiz creation text must come BEFORE echo
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=0,
    )

    # Menu buttons
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"
            ),
            menu_button,
        ),
        group=1,
    )

    # Unknown commands
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=1,
    )
