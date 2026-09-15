"""Professional Telegram Quiz Creator Bot handlers."""

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

logger = logging.getLogger(name)

DB_KEY = "db"
REDIS_KEY = "redis"

=========================================================

TELEGRAM COMMANDS

EXACTLY THESE COMMANDS ARE EXPOSED

=========================================================

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
("testbook", "Import quiz from Testbook URL"),
("tutorial", "Show tutorials and guides"),
("cancel", "Cancel current process"),
)

=========================================================

USER STATES

=========================================================

STATE_TITLE = "title"
STATE_SUBJECT = "subject"
STATE_TIME = "time"
STATE_NEGATIVE = "negative"
STATE_BATCH = "batch_questions"
STATE_COMPLETE = "complete"

DRAFTS: dict[int, dict] = {}

Active quiz/session state

USER_SESSIONS: dict[int, dict] = {}

=========================================================

DATABASE SCHEMA

=========================================================

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
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

async def ensure_quiz_tables(pool) -> None:
"""Create required tables if they don't exist."""

if pool is None:  
    return  

async with pool.acquire() as conn:  
    await conn.execute(CREATE_QUIZZES_TABLE)  
    await conn.execute(CREATE_QUESTIONS_TABLE)  
    await conn.execute(CREATE_BOOKMARKS_TABLE)  
    await conn.execute(CREATE_ANSWERS_TABLE)  
    await conn.execute(CREATE_SECTIONS_TABLE)

=========================================================

HELPERS

=========================================================

def get_user_id(update: Update) -> int | None:
user = update.effective_user
return user.id if user else None

def get_message(update: Update):
return update.effective_message

def get_pool(context: ContextTypes.DEFAULT_TYPE):
return context.bot_data.get(DB_KEY)

def quiz_creation_keyboard() -> InlineKeyboardMarkup:
return InlineKeyboardMarkup(
[
[
InlineKeyboardButton(
"👁 Preview",
callback_data="draft_preview",
),
],
[
InlineKeyboardButton(
"💾 Save Quiz",
callback_data="draft_save",
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

def edit_keyboard(quiz_id: int) -> InlineKeyboardMarkup:
return InlineKeyboardMarkup(
[
[
InlineKeyboardButton(
"✏️ Edit Questions",
callback_data=f"edit_questions:{quiz_id}",
)
],
[
InlineKeyboardButton(
"👁 Preview",
callback_data=f"stored_preview:{quiz_id}",
)
],
]
)

=========================================================

START

=========================================================

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
    await db.upsert_user(  
        pool,  
        user.id,  
        user.username,  
        user.first_name,  
    )  
    await ensure_quiz_tables(pool)  

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
    "Create, manage and attempt professional quizzes "  
    "from one place.\n\n"  
    "➕ /create — Create a quiz\n"  
    "📚 /myquizzes — Your quizzes\n"  
    "📖 /help — All commands",  
    parse_mode="HTML",  
    reply_markup=keyboard,  
)

=========================================================

HELP

=========================================================

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
/section — Create timed quiz sections
/cancel — Cancel current process

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

<b>Import & Learning</b>

/testbook — Import quiz from Testbook URL
/tutorial — Tutorials and guides

<b>General</b>

/start — Start bot
/help — Show all commands
"""

await message.reply_text(  
    text,  
    parse_mode="HTML",  
)

=========================================================

TUTORIAL

=========================================================

async def tutorial(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

message = get_message(update)  

if not message:  
    return  

text = """

<b>📖 Quiz Creator Tutorial</b>

<b>1. Create Quiz</b>

Send:

<code>/create</code>

Then enter:

• Quiz title
• Optional subject
• Time limit
• Negative marking
• All questions in one message

<b>2. Batch Question Format</b>

<code>  
Q1. भारत की राजधानी क्या है?  
A. पटना  
B. दिल्ली  
C. मुंबई  
D. जयपुर  
Correct: B  
Explanation: नई दिल्ली भारत की राजधानी है.  Q2. बिहार की राजधानी क्या है?
A. गया
B. पटना
C. भागलपुर
D. दरभंगा
Correct: B
Explanation: पटना बिहार की राजधानी है.
</code>

<b>3. Save</b>

Questions parse होने के बाद:

👁 Preview

या

💾 Save Quiz

<b>4. Manage</b>

<code>/myquizzes</code>

से अपने quizzes देखें।

<b>5. Publish</b>

Quiz save होने के बाद bot Quiz ID देगा।
उस ID से publish किया जा सकता है।
"""

await message.reply_text(  
    text,  
    parse_mode="HTML",  
)

=========================================================

CREATE QUIZ

=========================================================

async def create_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

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
}  

await message.reply_text(  
    "<b>📝 CREATE NEW QUIZ</b>\n\n"  
    "Quiz का title भेजें.\n\n"  
    "Example:\n"  
    "<code>BPSC General Studies Mock Test 01</code>\n\n"  
    "❌ रोकने के लिए /cancel",  
    parse_mode="HTML",  
)

=========================================================

BATCH QUESTION PARSER

=========================================================

def parse_batch_questions(text: str) -> tuple[list[dict], list[str]]:
"""
Parse multiple questions from one text block.

Supported:  

Q1. Question  
A. Option A  
B. Option B  
C. Option C  
D. Option D  
Correct: B  
Explanation: Explanation  

Also supports:  
Question 1:  
Answer: B  
Correct Answer: B  
"""  

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

for block_number, block in enumerate(blocks, start=1):  

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

    option_a = None  
    option_b = None  
    option_c = None  
    option_d = None  
    correct = None  
    explanation_parts = []  

    reading_explanation = False  

    for line in lines[1:]:  

        # ---------------------------------------------  
        # EXPLANATION  
        # ---------------------------------------------  

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

        # ---------------------------------------------  
        # OPTIONS  
        # ---------------------------------------------  

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

            continue  

        # ---------------------------------------------  
        # CORRECT ANSWER  
        # ---------------------------------------------  

        match = re.match(  
            r"(?i)^(?:correct\s*answer|correct|answer)"  
            r"\s*:\s*([ABCD])\s*$",  
            line,  
        )  

        if match:  
            correct = match.group(1).upper()  
            continue  

    missing = []  

    if not question:  
        missing.append("question")  

    if not option_a:  
        missing.append("A")  

    if not option_b:  
        missing.append("B")  

    if not option_c:  
        missing.append("C")  

    if not option_d:  
        missing.append("D")  

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
            "option_a": option_a,  
            "option_b": option_b,  
            "option_c": option_c,  
            "option_d": option_d,  
            "correct_answer": correct,  
            "explanation": " ".join(  
                explanation_parts  
            ).strip(),  
        }  
    )  

return questions, errors

=========================================================

FILE PARSERS

=========================================================

def parse_json_questions(text: str) -> tuple[list[dict], list[str]]:

try:  
    data = json.loads(text)  
except json.JSONDecodeError as exc:  
    return [], [f"Invalid JSON: {exc}"]  

if isinstance(data, dict):  
    data = data.get("questions", [])  

if not isinstance(data, list):  
    return [], ["JSON must contain a questions list."]  

questions = []  
errors = []  

for index, item in enumerate(data, start=1):  

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

def parse_csv_questions(text: str) -> tuple[list[dict], list[str]]:

reader = csv.DictReader(  
    io.StringIO(text)  
)  

questions = []  
errors = []  

for index, row in enumerate(reader, start=1):  

    def value(*keys):  
        for key in keys:  
            if key in row and row[key]:  
                return row[key].strip()  
        return ""  

    question = value("question", "Question")  
    option_a = value("option_a", "A", "a")  
    option_b = value("option_b", "B", "b")  
    option_c = value("option_c", "C", "c")  
    option_d = value("option_d", "D", "d")  
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

=========================================================

SAVE DRAFT TO DATABASE

=========================================================

async def save_draft_to_database(
draft: dict,
user_id: int,
pool,
) -> int:

await ensure_quiz_tables(pool)  

async with pool.acquire() as conn:  

    async with conn.transaction():  

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
            user_id,  
            draft["title"],  
            draft["subject"] or None,  
            len(draft["questions"]),  
            draft["time_limit"],  
            draft["negative_marking"],  
        )  

        for number, question in enumerate(  
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

return int(quiz_id)

=========================================================

PREVIEW FORMAT

=========================================================

def format_draft_preview(
draft: dict,
max_questions: int = 20,
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

for index, question in enumerate(  
    draft["questions"][:max_questions],  
    start=1,  
):  

    text += (  
        f"<b>Q{index}.</b> "  
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
            "💡 "  
            f"{escape(question['explanation'])}\n"  
        )  

    text += "\n"  

if len(draft["questions"]) > max_questions:  
    text += (  
        f"<i>Showing first {max_questions} "  
        f"of {len(draft['questions'])} questions.</i>\n"  
    )  

return text

=========================================================

CREATE TEXT PROCESSOR

=========================================================

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
state = draft["state"]  

# -----------------------------------------------------  
# TITLE  
# -----------------------------------------------------  

if state == STATE_TITLE:  

    if len(text) < 2:  
        await message.reply_text(  
            "❌ Quiz title बहुत छोटा है."  
        )  
        return  

    draft["title"] = text  
    draft["state"] = STATE_SUBJECT  

    await message.reply_text(  
        "📚 <b>Subject</b> भेजें.\n\n"  
        "यह केवल quiz metadata रहेगा.\n"  
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
            "❌ केवल number भेजें.\n"  
            "Example: 30"  
        )  
        return  

    if not 1 <= minutes <= 300:  
        await message.reply_text(  
            "❌ Time 1 से 300 minutes के बीच रखें."  
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
# NEGATIVE MARKING  
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
        "<b>📋 अब सारे questions एक साथ भेजें.</b>\n\n"  
        "Example:\n\n"  
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
        "💡 10, 50, 100 या जितने questions "  
        "चाहें एक ही message में भेजें.\n\n"  
        "या .txt / .json / .csv file भेजें.\n\n"  
        "❌ /cancel",  
        parse_mode="HTML",  
    )  
    return  

# -----------------------------------------------------  
# BATCH QUESTIONS  
# -----------------------------------------------------  

if state == STATE_BATCH:  

    questions, errors = parse_batch_questions(text)  

    if not questions:  

        await message.reply_text(  
            "❌ कोई valid question नहीं मिला.\n\n"  
            "Format check करें:\n\n"  
            "<code>"  
            "Q1. Question\n"  
            "A. Option A\n"  
            "B. Option B\n"  
            "C. Option C\n"  
            "D. Option D\n"  
            "Correct: B\n"  
            "Explanation: Explanation"  
            "</code>",  
            parse_mode="HTML",  
        )  
        return  

    draft["questions"] = questions  
    draft["question_count"] = len(questions)  
    draft["state"] = STATE_COMPLETE  

    response = (  
        f"✅ <b>{len(questions)} questions detected!</b>\n\n"  
        f"<b>Quiz:</b> "  
        f"{escape(draft['title'])}\n"  
        f"<b>Subject:</b> "  
        f"{escape(draft['subject'] or 'Not specified')}\n"  
        f"<b>Time:</b> "  
        f"{draft['time_limit']} minutes\n"  
        f"<b>Negative:</b> "  
        f"{draft['negative_marking']}\n\n"  
    )  

    if errors:  
        response += (  
            f"⚠️ {len(errors)} question blocks "  
            "were skipped because they were incomplete.\n\n"  
        )  

    response += (  
        "अब Preview करें या Quiz Save करें."  
    )  

    await message.reply_text(  
        response,  
        parse_mode="HTML",  
        reply_markup=quiz_creation_keyboard(),  
    )  

    return  

if state == STATE_COMPLETE:  

    await message.reply_text(  
        "Quiz तैयार है.\n\n"  
        "ऊपर दिए Preview या Save button का उपयोग करें."  
    )

=========================================================

DOCUMENT / FILE IMPORT

=========================================================

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
        "पहले /create शुरू करें, फिर question file भेजें."  
    )  
    return  

if draft["state"] != STATE_BATCH:  
    await message.reply_text(  
        "अभी file लेने का चरण नहीं है.\n"  
        "पहले /create पूरा करें."  
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
        "❌ केवल .txt, .json या .csv file supported है."  
    )  
    return  

telegram_file = await document.get_file()  

data = await telegram_file.download_as_bytearray()  

try:  
    content = bytes(data).decode(  
        "utf-8-sig"  
    )  
except UnicodeDecodeError:  
    await message.reply_text(  
        "❌ File UTF-8 text में होनी चाहिए."  
    )  
    return  

if filename.endswith(".json"):  

    questions, errors = parse_json_questions(  
        content  
    )  

elif filename.endswith(".csv"):  

    questions, errors = parse_csv_questions(  
        content  
    )  

else:  

    questions, errors = parse_batch_questions(  
        content  
    )  

if not questions:  

    await message.reply_text(  
        "❌ File से कोई valid question नहीं मिला."  
    )  
    return  

draft["questions"] = questions  
draft["question_count"] = len(questions)  
draft["state"] = STATE_COMPLETE  

await message.reply_text(  
    f"✅ File imported successfully.\n\n"  
    f"📚 Questions detected: "  
    f"<b>{len(questions)}</b>\n\n"  
    "अब Preview या Save Quiz चुनें.",  
    parse_mode="HTML",  
    reply_markup=quiz_creation_keyboard(),  
)

=========================================================

CANCEL

=========================================================

async def cancel(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

removed = False  

if user.id in DRAFTS:  
    del DRAFTS[user.id]  
    removed = True  

if user.id in USER_SESSIONS:  
    del USER_SESSIONS[user.id]  
    removed = True  

if removed:  
    await message.reply_text(  
        "❌ Current process cancelled."  
    )  
else:  
    await message.reply_text(  
        "कोई active process नहीं है."  
    )

=========================================================

DRAFT CALLBACKS

=========================================================

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
        "❌ Draft नहीं मिला.\n/create से नया quiz बनाएं."  
    )  
    return  

action = query.data  

# -----------------------------------------------------  
# PREVIEW  
# -----------------------------------------------------  

if action == "draft_preview":  

    await query.message.reply_text(  
        format_draft_preview(draft),  
        parse_mode="HTML",  
    )  
    return  

# -----------------------------------------------------  
# SAVE  
# -----------------------------------------------------  

if action == "draft_save":  

    pool = get_pool(context)  

    if pool is None:  
        await query.message.reply_text(  
            "⚠️ PostgreSQL database connected नहीं है."  
        )  
        return  

    if not draft["questions"]:  
        await query.message.reply_text(  
            "❌ Quiz में कम से कम 1 question जरूरी है."  
        )  
        return  

    quiz_id = await save_draft_to_database(  
        draft,  
        user.id,  
        pool,  
    )  

    title = escape(  
        draft["title"]  
    )  

    question_count = len(  
        draft["questions"]  
    )  

    del DRAFTS[user.id]  

    await query.message.reply_text(  
        f"✅ <b>Quiz Saved Successfully!</b>\n\n"  
        f"<b>Quiz ID:</b> "  
        f"<code>{quiz_id}</code>\n"  
        f"<b>Title:</b> {title}\n"  
        f"<b>Questions:</b> "  
        f"{question_count}\n\n"  
        f"Quiz अभी unpublished है.",  
        parse_mode="HTML",  
    )  
    return  

# -----------------------------------------------------  
# CANCEL  
# -----------------------------------------------------  

if action == "draft_cancel":  

    del DRAFTS[user.id]  

    await query.message.reply_text(  
        "❌ Quiz draft cancelled."  
    )

=========================================================

MY QUIZZES

=========================================================

async def my_quizzes(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

message = get_message(update)  
user = update.effective_user  
pool = get_pool(context)  

if not message or not user:  
    return  

if pool is None:  
    await message.reply_text(  
        "⚠️ Database connected नहीं है."  
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
    LIMIT 50;  
    """,  
    user.id,  
)  

if not rows:  

    await message.reply_text(  
        "📚 आपने अभी कोई quiz create नहीं किया है.\n\n"  
        "/create से पहला quiz बनाएं."  
    )  
    return  

for row in rows:  

    status = (  
        "🟢 Published"  
        if row["published"]  
        else "🟡 Unpublished"  
    )  

    text = (  
        "<b>📚 QUIZ</b>\n\n"  
        f"<b>ID:</b> <code>{row['id']}</code>\n"  
        f"<b>Title:</b> "  
        f"{escape(row['title'])}\n"  
        f"<b>Subject:</b> "  
        f"{escape(row['subject'] or 'Not specified')}\n"  
        f"<b>Questions:</b> "  
        f"{row['question_count']}\n"  
        f"<b>Time:</b> "  
        f"{row['time_limit']} min\n"  
        f"<b>Negative:</b> "  
        f"{row['negative_marking']}\n"  
        f"<b>Status:</b> {status}"  
    )  

    await message.reply_text(  
        text,  
        parse_mode="HTML",  
        reply_markup=edit_keyboard(  
            row["id"]  
        ),  
    )

=========================================================

SETTINGS

=========================================================

async def settings(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

message = get_message(update)  
user = update.effective_user  

if not message or not user:  
    return  

session = USER_SESSIONS.setdefault(  
    user.id,  
    {},  
)  

timer = session.get(  
    "question_timer",  
    30,  
)  

negative = session.get(  
    "negative_marking",  
    0.25,  
)  

keyboard = InlineKeyboardMarkup(  
    [  
        [  
            InlineKeyboardButton(  
                "⏱ 15 sec",  
                callback_data="setting_timer:15",  
            ),  
            InlineKeyboardButton(  
                "⏱ 30 sec",  
                callback_data="setting_timer:30",  
            ),  
        ],  
        [  
            InlineKeyboardButton(  
                "⏱ 60 sec",  
                callback_data="setting_timer:60",  
            ),  
            InlineKeyboardButton(  
                "⏱ 120 sec",  
                callback_data="setting_timer:120",  
            ),  
        ],  
        [  
            InlineKeyboardButton(  
                "➖ No Negative",  
                callback_data="setting_negative:0",  
            ),  
        ],  
        [  
            InlineKeyboardButton(  
                "➖ 0.25 Negative",  
                callback_data="setting_negative:0.25",  
            ),  
            InlineKeyboardButton(  
                "➖ 0.33 Negative",  
                callback_data="setting_negative:0.33",  
            ),  
        ],  
    ]  
)  

await message.reply_text(  
    "<b>⚙️ QUIZ SETTINGS</b>\n\n"  
    f"Question timer: <b>{timer} sec</b>\n"  
    f"Negative marking: <b>{negative}</b>\n\n"  
    "अपनी preferred settings चुनें.",  
    parse_mode="HTML",  
    reply_markup=keyboard,  
)

=========================================================

SETTINGS CALLBACK

=========================================================

async def settings_buttons(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

query = update.callback_query  

if not query:  
    return  

await query.answer()  

user = query.from_user  

session = USER_SESSIONS.setdefault(  
    user.id,  
    {},  
)  

data = query.data  

if data.startswith("setting_timer:"):  

    value = int(  
        data.split(":", 1)[1]  
    )  

    session["question_timer"] = value  

    await query.message.reply_text(  
        f"⏱ Question timer set to {value} seconds."  
    )  
    return  

if data.startswith("setting_negative:"):  

    value = float(  
        data.split(":", 1)[1]  
    )  

    session["negative_marking"] = value  

    await query.message.reply_text(  
        f"➖ Negative marking set to {value}."  
    )

=========================================================

STOP

=========================================================

async def stop_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

session = USER_SESSIONS.get(user.id)  

if not session:  
    await message.reply_text(  
        "कोई active quiz नहीं है."  
    )  
    return  

session["status"] = "stopped"  

await message.reply_text(  
    "⏹ Current quiz stopped."  
)

=========================================================

PAUSE

=========================================================

async def pause_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

session = USER_SESSIONS.get(user.id)  

if not session:  
    await message.reply_text(  
        "कोई active quiz नहीं है."  
    )  
    return  

session["status"] = "paused"  

await message.reply_text(  
    "⏸ Quiz paused.\n\n"  
    "/resume से continue करें."  
)

=========================================================

RESUME

=========================================================

async def resume_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

session = USER_SESSIONS.get(user.id)  

if not session:  
    await message.reply_text(  
        "कोई paused quiz नहीं मिला."  
    )  
    return  

if session.get("status") != "paused":  
    await message.reply_text(  
        "कोई paused quiz नहीं है."  
    )  
    return  

session["status"] = "running"  

await message.reply_text(  
    "▶️ Quiz resumed."  
)

=========================================================

FAST

=========================================================

async def fast_timer(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

session = USER_SESSIONS.setdefault(  
    user.id,  
    {},  
)  

current = session.get(  
    "question_timer",  
    30,  
)  

new_value = max(  
    5,  
    current - 5,  
)  

session["question_timer"] = new_value  

await message.reply_text(  
    f"⚡ Question timer decreased to "  
    f"<b>{new_value} seconds</b>.",  
    parse_mode="HTML",  
)

=========================================================

SLOW

=========================================================

async def slow_timer(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

session = USER_SESSIONS.setdefault(  
    user.id,  
    {},  
)  

current = session.get(  
    "question_timer",  
    30,  
)  

new_value = min(  
    300,  
    current + 5,  
)  

session["question_timer"] = new_value  

await message.reply_text(  
    f"🐢 Question timer increased to "  
    f"<b>{new_value} seconds</b>.",  
    parse_mode="HTML",  
)

=========================================================

STOP POLL

=========================================================

async def stop_poll(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

if not user or not message:  
    return  

session = USER_SESSIONS.get(user.id)  

if not session or not session.get("poll_message_id"):  
    await message.reply_text(  
        "कोई active poll नहीं है."  
    )  
    return  

chat_id = session.get("poll_chat_id")  
message_id = session.get("poll_message_id")  

try:  

    await context.bot.stop_poll(  
        chat_id=chat_id,  
        message_id=message_id,  
    )  

    session.pop(  
        "poll_message_id",  
        None,  
    )  

    session.pop(  
        "poll_chat_id",  
        None,  
    )  

    await message.reply_text(  
        "⏹ Active poll stopped."  
    )  

except Exception as exc:  

    logger.exception(  
        "Unable to stop poll: %s",  
        exc,  
    )  

    await message.reply_text(  
        "⚠️ Poll stop नहीं हो सका."  
    )

=========================================================

MISTAKES

=========================================================

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
        "⚠️ Database connected नहीं है."  
    )  
    return  

await ensure_quiz_tables(pool)  

rows = await pool.fetch(  
    """  
    SELECT  
        q.question,  
        q.option_a,  
        q.option_b,  
        q.option_c,  
        q.option_d,  
        q.correct_answer,  
        a.selected_answer,  
        q.explanation  
    FROM quiz_answers a  
    JOIN quiz_questions q  
      ON q.id = a.question_id  
    WHERE a.user_id = $1  
      AND a.is_correct = FALSE  
    ORDER BY a.answered_at DESC  
    LIMIT 20;  
    """,  
    user.id,  
)  

if not rows:  

    await message.reply_text(  
        "🎯 कोई incorrect answer record नहीं मिला."  
    )  
    return  

for index, row in enumerate(  
    rows,  
    start=1,  
):  

    text = (  
        f"<b>❌ Mistake #{index}</b>\n\n"  
        f"<b>{escape(row['question'])}</b>\n\n"  
        f"A. {escape(row['option_a'])}\n"  
        f"B. {escape(row['option_b'])}\n"  
        f"C. {escape(row['option_c'])}\n"  
        f"D. {escape(row['option_d'])}\n\n"  
        f"Your answer: "  
        f"<b>{escape(row['selected_answer'] or '-')}</b>\n"  
        f"Correct: "  
        f"<b>{escape(row['correct_answer'])}</b>\n"  
    )  

    if row["explanation"]:  
        text += (  
            f"\n💡 {escape(row['explanation'])}"  
        )  

    await message.reply_text(  
        text,  
        parse_mode="HTML",  
    )

=========================================================

BOOKMARKS

=========================================================

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
        "⚠️ Database connected नहीं है."  
    )  
    return  

await ensure_quiz_tables(pool)  

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
      ON q.id = b.question_id  
    WHERE b.user_id = $1  
    ORDER BY b.created_at DESC  
    LIMIT 50;  
    """,  
    user.id,  
)  

if not rows:  

    await message.reply_text(  
        "🔖 कोई bookmarked question नहीं है."  
    )  
    return  

for index, row in enumerate(  
    rows,  
    start=1,  
):  

    text = (  
        f"<b>🔖 Bookmark #{index}</b>\n\n"  
        f"<b>{escape(row['question'])}</b>\n\n"  
        f"A. {escape(row['option_a'])}\n"  
        f"B. {escape(row['option_b'])}\n"  
        f"C. {escape(row['option_c'])}\n"  
        f"D. {escape(row['option_d'])}\n\n"  
        f"Correct: <b>{row['correct_answer']}</b>"  
    )  

    if row["explanation"]:  
        text += (  
            f"\n\n💡 "  
            f"{escape(row['explanation'])}"  
        )  

    await message.reply_text(  
        text,  
        parse_mode="HTML",  
    )

=========================================================

SECTION CREATION

=========================================================

async def section_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

user = update.effective_user  
message = get_message(update)  

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
    "section_mode": True,  
}  

await message.reply_text(  
    "<b>🧩 SECTION QUIZ</b>\n\n"  
    "पहले quiz title भेजें.\n\n"  
    "इसके बाद आप section configuration "  
    "और questions add कर सकते हैं.\n\n"  
    "❌ /cancel",  
    parse_mode="HTML",  
)

=========================================================

TESTBOOK IMPORT

=========================================================

def fetch_url_text(url: str) -> str:

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

def html_to_text(html: str) -> str:

# Remove scripts/styles  
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

# Convert common breaks  
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

# Remove remaining tags  
html = re.sub(  
    r"(?s)<[^>]+>",  
    " ",  
    html,  
)  

# Decode common entities  
html = (  
    html.replace("&nbsp;", " ")  
    .replace("&amp;", "&")  
    .replace("&lt;", "<")  
    .replace("&gt;", ">")  
    .replace("&quot;", '"')  
    .replace("&#39;", "'")  
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

if not message:  
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

await message.reply_text(  
    "⏳ Testbook page fetch कर रहा हूँ..."  
)  

try:  

    html = await asyncio.to_thread(  
        fetch_url_text,  
        url,  
    )  

    text = html_to_text(html)  

    questions, errors = parse_batch_questions(  
        text  
    )  

except Exception as exc:  

    logger.exception(  
        "Testbook import failed: %s",  
        exc,  
    )  

    await message.reply_text(  
        "❌ Testbook page import नहीं हो सका.\n\n"  
        "अगर page dynamically loaded है, "  
        "तो questions को .txt/.json/.csv में "  
        "export करके /create से import करें."  
    )  
    return  

if not questions:  

    await message.reply_text(  
        "❌ इस URL से structured questions "  
        "detect नहीं हुए.\n\n"  
        "आप /create में question text "  
        "या supported file भेज सकते हैं."  
    )  
    return  

user = update.effective_user  

if not user:  
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
    "source_url": url,  
}  

await message.reply_text(  
    f"✅ <b>{len(questions)} questions imported.</b>\n\n"  
    "अब Preview या Save Quiz करें.",  
    parse_mode="HTML",  
    reply_markup=quiz_creation_keyboard(),  
)

=========================================================

EDIT QUIZ

=========================================================

async def edit_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

message = get_message(update)  
user = update.effective_user  
pool = get_pool(context)  

if not message or not user:  
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
        "❌ Invalid Quiz ID."  
    )  
    return  

if pool is None:  
    await message.reply_text(  
        "⚠️ Database connected नहीं है."  
    )  
    return  

quiz = await pool.fetchrow(  
    """  
    SELECT *  
    FROM quizzes  
    WHERE id = $1  
      AND creator_id = $2;  
    """,  
    quiz_id,  
    user.id,  
)  

if not quiz:  

    await message.reply_text(  
        "❌ Quiz नहीं मिला."  
    )  
    return  

questions = await pool.fetch(  
    """  
    SELECT  
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
    ORDER BY question_no;  
    """,  
    quiz_id,  
)  

DRAFTS[user.id] = {  
    "state": STATE_COMPLETE,  
    "title": quiz["title"],  
    "subject": quiz["subject"] or "",  
    "question_count": len(questions),  
    "time_limit": quiz["time_limit"],  
    "negative_marking": quiz["negative_marking"],  
    "questions": [  
        {  
            "question": row["question"],  
            "option_a": row["option_a"],  
            "option_b": row["option_b"],  
            "option_c": row["option_c"],  
            "option_d": row["option_d"],  
            "correct_answer": row["correct_answer"],  
            "explanation": row["explanation"] or "",  
        }  
        for row in questions  
    ],  
    "editing_quiz_id": quiz_id,  
    "sections": [],  
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
                "💾 Save Changes",  
                callback_data="draft_update",  
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
    f"✏️ <b>Editing Quiz #{quiz_id}</b>\n\n"  
    f"{escape(quiz['title'])}\n"  
    f"Questions: {len(questions)}\n\n"  
    "Preview या Save Changes चुनें.",  
    parse_mode="HTML",  
    reply_markup=keyboard,  
)

=========================================================

UPDATE EXISTING QUIZ

=========================================================

async def update_existing_quiz(
draft: dict,
user_id: int,
pool,
) -> int:

quiz_id = draft.get(  
    "editing_quiz_id"  
)  

if not quiz_id:  
    raise ValueError(  
        "Missing editing quiz ID."  
    )  

async with pool.acquire() as conn:  

    async with conn.transaction():  

        result = await conn.execute(  
            """  
            UPDATE quizzes  
            SET  
                title = $1,  
                subject = $2,  
                question_count = $3,  
                time_limit = $4,  
                negative_marking = $5,  
                published = FALSE  
            WHERE id = $6  
              AND creator_id = $7;  
            """,  
            draft["title"],  
            draft["subject"] or None,  
            len(draft["questions"]),  
            draft["time_limit"],  
            draft["negative_marking"],  
            quiz_id,  
            user_id,  
        )  

        if result.endswith("0"):  
            raise ValueError(  
                "Quiz not found."  
            )  

        await conn.execute(  
            """  
            DELETE FROM quiz_questions  
            WHERE quiz_id = $1;  
            """,  
            quiz_id,  
        )  

        for number, question in enumerate(  
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

return int(quiz_id)

=========================================================

DELETE QUIZ

=========================================================

async def delete_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

message = get_message(update)  
user = update.effective_user  
pool = get_pool(context)  

if not message or not user:  
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
        "❌ Invalid Quiz ID."  
    )  
    return  

if pool is None:  

    await message.reply_text(  
        "⚠️ Database connected नहीं है."  
    )  
    return  

result = await pool.execute(  
    """  
    DELETE FROM quizzes  
    WHERE id = $1  
      AND creator_id = $2;  
    """,  
    quiz_id,  
    user.id,  
)  

if result.endswith("0"):  

    await message.reply_text(  
        "❌ Quiz नहीं मिला."  
    )  
    return  

await message.reply_text(  
    f"🗑 Quiz #{quiz_id} deleted successfully."  
)

=========================================================

PUBLISH QUIZ

=========================================================

async def publish_quiz(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

message = get_message(update)  
user = update.effective_user  
pool = get_pool(context)  

if not message or not user:  
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
        "❌ Invalid Quiz ID."  
    )  
    return  

if pool is None:  

    await message.reply_text(  
        "⚠️ Database connected नहीं है."  
    )  
    return  

question_count = await pool.fetchval(  
    """  
    SELECT COUNT(*)  
    FROM quiz_questions q  
    JOIN quizzes z  
      ON z.id = q.quiz_id  
    WHERE z.id = $1  
      AND z.creator_id = $2;  
    """,  
    quiz_id,  
    user.id,  
)  

if not question_count:  

    await message.reply_text(  
        "❌ Quiz में कोई question नहीं है."  
    )  
    return  

result = await pool.execute(  
    """  
    UPDATE quizzes  
    SET  
        published = TRUE,  
        question_count = $1  
    WHERE id = $2  
      AND creator_id = $3;  
    """,  
    question_count,  
    quiz_id,  
    user.id,  
)  

if result.endswith("0"):  

    await message.reply_text(  
        "❌ Quiz नहीं मिला."  
    )  
    return  

await message.reply_text(  
    f"🚀 Quiz <b>#{quiz_id}</b> published successfully.",  
    parse_mode="HTML",  
)

=========================================================

STORED QUIZ PREVIEW

=========================================================

async def stored_preview(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
quiz_id: int,
) -> None:

message = get_message(update)  
user = update.effective_user  
pool = get_pool(context)  

if not message or not user or pool is None:  
    return  

quiz = await pool.fetchrow(  
    """  
    SELECT *  
    FROM quizzes  
    WHERE id = $1  
      AND creator_id = $2;  
    """,  
    quiz_id,  
    user.id,  
)  

if not quiz:  

    await message.reply_text(  
        "❌ Quiz नहीं मिला."  
    )  
    return  

questions = await pool.fetch(  
    """  
    SELECT *  
    FROM quiz_questions  
    WHERE quiz_id = $1  
    ORDER BY question_no;  
    """,  
    quiz_id,  
)  

text = (  
    "<b>📋 QUIZ PREVIEW</b>\n\n"  
    f"<b>Title:</b> "  
    f"{escape(quiz['title'])}\n"  
    f"<b>Subject:</b> "  
    f"{escape(quiz['subject'] or 'Not specified')}\n"  
    f"<b>Questions:</b> "  
    f"{len(questions)}\n"  
    f"<b>Time:</b> "  
    f"{quiz['time_limit']} minutes\n"  
    f"<b>Negative:</b> "  
    f"{quiz['negative_marking']}\n\n"  
)  

for index, row in enumerate(  
    questions[:20],  
    start=1,  
):  

    text += (  
        f"<b>Q{index}.</b> "  
        f"{escape(row['question'])}\n"  
        f"A. {escape(row['option_a'])}\n"  
        f"B. {escape(row['option_b'])}\n"  
        f"C. {escape(row['option_c'])}\n"  
        f"D. {escape(row['option_d'])}\n"  
        f"Correct: "  
        f"{row['correct_answer']}\n\n"  
    )  

await message.reply_text(  
    text,  
    parse_mode="HTML",  
)

=========================================================

CALLBACK ROUTER

=========================================================

async def callback_router(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
) -> None:

query = update.callback_query  

if not query:  
    return  

data = query.data or ""  

# Draft buttons  
if data.startswith("draft_"):  

    await draft_buttons(  
        update,  
        context,  
    )  
    return  

# Settings  
if data.startswith("setting_"):  

    await settings_buttons(  
        update,  
        context,  
    )  
    return  

# Stored quiz preview  
if data.startswith("stored_preview:"):  

    await query.answer()  

    try:  
        quiz_id = int(  
            data.split(":", 1)[1]  
        )  
    except ValueError:  

        await query.message.reply_text(  
            "❌ Invalid quiz ID."  
        )  
        return  

    await stored_preview(  
        update,  
        context,  
        quiz_id,  
    )  
    return  

# Edit questions  
if data.startswith("edit_questions:"):  

    await query.answer()  

    try:  
        quiz_id = int(  
            data.split(":", 1)[1]  
        )  
    except ValueError:  

        await query.message.reply_text(  
            "❌ Invalid quiz ID."  
        )  
        return  

    await query.message.reply_text(  
        f"✏️ Quiz #{quiz_id} को edit करने के लिए:\n\n"  
        f"<code>/editquiz {quiz_id}</code>\n\n"  
        "इसके बाद पूरा question set "  
        "दोबारा import करके Save Changes करें.",  
        parse_mode="HTML",  
    )  
    return  

await query.answer()

=========================================================

UNKNOWN COMMAND

=========================================================

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

=========================================================

MENU TEXT BUTTONS

=========================================================

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

=========================================================

ERROR HANDLER

=========================================================

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

=========================================================

SET TELEGRAM COMMAND MENU

=========================================================

async def set_bot_commands(
application: Application,
) -> None:

await application.bot.set_my_commands(  
    list(BOT_COMMANDS)  
)

=========================================================

REGISTER HANDLERS

=========================================================

def register_handlers(
application: Application,
) -> None:

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

# -----------------------------------------------------  
# IMPORTANT:  
# The following are NOT Telegram commands.  
# They are internal compatibility handlers for  
# existing quiz-management functionality.  
# -----------------------------------------------------  

# Edit/delete/publish are intentionally NOT exposed  
# in the Telegram command menu, per the requested  
# exact 19-command public command system.  
#  
# They can be reached through normal quiz-management  
# buttons/workflows.  

# -----------------------------------------------------  
# CALLBACKS  
# -----------------------------------------------------  

application.add_handler(  
    CallbackQueryHandler(  
        callback_router,  
        pattern=r"^(draft_|setting_|stored_preview:|edit_questions:)",  
    )  
)  

# -----------------------------------------------------  
# DOCUMENT IMPORT  
# -----------------------------------------------------  

application.add_handler(  
    MessageHandler(  
        filters.Document.ALL,  
        process_quiz_file,  
    ),  
    group=0,  
)  

# -----------------------------------------------------  
# BATCH TEXT PROCESSOR  
# -----------------------------------------------------  

application.add_handler(  
    MessageHandler(  
        filters.TEXT & ~filters.COMMAND,  
        process_quiz_creation,  
    ),  
    group=0,  
)  

# -----------------------------------------------------  
# REPLY KEYBOARD  
# -----------------------------------------------------  

application.add_handler(  
    MessageHandler(  
        filters.Regex(  
            r"^(➕ Create Quiz|📚 My Quizzes|"  
            r"⚙️ Settings|📖 Tutorial)$"  
        ),  
        menu_buttons,  
    ),  
    group=1,  
)  

# -----------------------------------------------------  
# UNKNOWN COMMAND  
# -----------------------------------------------------  
#  
# IMPORTANT:  
# Same group as CommandHandlers and registered LAST.  
# Therefore valid commands are handled first and do  
# NOT produce "Unknown command".  
#  

application.add_handler(  
    MessageHandler(  
        filters.COMMAND,  
        unknown_command,  
    ),  
    group=0,  
)
