"""Brain Booster BPSC Quiz Bot - professional Telegram handlers.

Features:
- Exactly the 17 requested public Telegram commands.
- /create creates a quiz from one batch message or TXT/CSV/JSON file.
- Multi-line MCQs are preserved, including Statement/Assertion/Reason/Match/Chronology.
- Correct answer can be marked with ✅ and/or "Correct: B".
- Subjects are optional database metadata, never commands.
- Every user can create and edit their own quizzes.
- Admin can manage all quizzes.
- Inline quiz attempts, bookmarks, mistakes, pause/resume and timer controls.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from html import escape
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Temporary per-user workflows. Quiz/attempt data is persisted in PostgreSQL.
DRAFTS: dict[int, dict] = {}
ACTIVE: dict[int, dict] = {}

# EXACTLY the commands requested by the user.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS quizzes (
    id SERIAL PRIMARY KEY,
    creator_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    subject TEXT,
    question_count INTEGER NOT NULL DEFAULT 0,
    time_limit INTEGER NOT NULL DEFAULT 30,
    negative_marking DOUBLE PRECISION NOT NULL DEFAULT 0,
    published BOOLEAN NOT NULL DEFAULT TRUE,
    sections JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    question_no INTEGER NOT NULL,
    question TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    option_d TEXT NOT NULL,
    correct_answer CHAR(1) NOT NULL,
    explanation TEXT DEFAULT '',
    image_file_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    score DOUBLE PRECISION DEFAULT 0,
    correct_count INTEGER DEFAULT 0,
    wrong_count INTEGER DEFAULT 0,
    unanswered_count INTEGER DEFAULT 0,
    completed BOOLEAN DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS quiz_answers (
    id SERIAL PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    selected_answer CHAR(1) NOT NULL,
    is_correct BOOLEAN NOT NULL,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(attempt_id, question_id)
);

CREATE TABLE IF NOT EXISTS quiz_bookmarks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, question_id)
);
"""


async def ensure_tables(pool) -> None:
    await pool.execute(SCHEMA)


def get_pool(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get(DB_KEY)


def is_admin(user_id: int) -> bool:
    return ADMIN_ID > 0 and user_id == ADMIN_ID


def parse_correct_letter(value: str) -> str | None:
    match = re.search(r"\b([ABCD])\b", value.upper())
    return match.group(1) if match else None


def clean_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("&nbsp;", " ")
    return re.sub(r"[ \t]+", " ", value).strip()


def option_match(line: str):
    """Supports A. / (a) / A: / a- formats and a trailing ✅."""
    match = re.match(
        r"^\s*\(?([ABCDabcd])\)?[\.\:\-]\s*(.*?)\s*$",
        line,
    )
    if not match:
        return None
    letter = match.group(1).upper()
    text = match.group(2).strip()
    marked = "✅" in text or "✔️" in text
    text = re.sub(r"\s*(?:✅|✔️)\s*$", "", text).strip()
    return letter, text, marked


def parse_batch_text(text: str) -> tuple[list[dict], list[str]]:
    """Robust batch parser.

    Accepted example:

    Q1. What is the capital of Bihar? / बिहार की राजधानी क्या है?

    👉 Choose the correct option / सही विकल्प चुनें

    (a) Gaya / गया
    (b) Patna / पटना ✅
    (c) Muzaffarpur / मुजफ्फरपुर
    (d) Bhagalpur / भागलपुर

    Ex: Patna is the capital of Bihar. / पटना बिहार की राजधानी है.
    Correct: B

    Multi-line question text is preserved.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [], ["Input is empty."]

    lines = text.split("\n")
    starts = [
        i for i, line in enumerate(lines)
        if re.match(r"^\s*Q\s*\d+\s*[\.\:\-]\s*", line, re.I)
    ]
    if not starts:
        return [], ["No Q1/Q2/... question headers were found."]

    questions: list[dict] = []
    errors: list[str] = []

    for block_index, start in enumerate(starts):
        end = starts[block_index + 1] if block_index + 1 < len(starts) else len(lines)
        block = [x.rstrip() for x in lines[start:end]]

        header = re.match(
            r"^\s*Q\s*(\d+)\s*[\.\:\-]\s*(.*)$",
            block[0],
            re.I,
        )
        if not header:
            continue

        qno = header.group(1)
        q_lines: list[str] = []
        first = header.group(2).strip()
        if first:
            q_lines.append(first)

        options: dict[str, str] = {}
        marked_correct: set[str] = set()
        correct: str | None = None
        explanation_lines: list[str] = []
        mode = "question"
        last_option: str | None = None

        for raw in block[1:]:
            line = raw.strip()

            if not line:
                # Blank lines inside question/explanation are meaningful.
                if mode == "question" and q_lines and q_lines[-1] != "":
                    q_lines.append("")
                elif mode == "explanation" and explanation_lines and explanation_lines[-1] != "":
                    explanation_lines.append("")
                continue

            # CTA line is informational and should not enter the question.
            if re.match(
                r"^(?:👉\s*)?(?:choose the correct option|select the correct option|सही विकल्प चुनें)",
                line,
                re.I,
            ):
                continue

            exp = re.match(
                r"^(?:Ex|Explanation|Explain|व्याख्या)\s*:\s*(.*)$",
                line,
                re.I,
            )
            if exp:
                mode = "explanation"
                if exp.group(1).strip():
                    explanation_lines.append(exp.group(1).strip())
                continue

            corr = re.match(
                r"^(?:Correct(?:\s+Answer)?|Answer|Ans|सही\s*उत्तर)\s*:\s*(.*)$",
                line,
                re.I,
            )
            if corr:
                parsed = parse_correct_letter(corr.group(1))
                if parsed:
                    correct = parsed
                else:
                    errors.append(f"Q{qno}: invalid Correct/Answer line.")
                mode = "after_answer"
                continue

            opt = option_match(line)
            if opt:
                letter, value, marked = opt
                options[letter] = value
                last_option = letter
                if marked:
                    marked_correct.add(letter)
                mode = "options"
                continue

            if mode == "question":
                q_lines.append(line)
            elif mode == "options" and last_option:
                # Preserve wrapped option text.
                options[last_option] += "\n" + line
            elif mode == "explanation":
                explanation_lines.append(line)

        if correct is None and len(marked_correct) == 1:
            correct = next(iter(marked_correct))
        elif correct and marked_correct and correct not in marked_correct:
            errors.append(f"Q{qno}: Correct: {correct} conflicts with the ✅ marker.")

        q_text = "\n".join(q_lines).strip()
        explanation = "\n".join(explanation_lines).strip()
        missing = [letter for letter in "ABCD" if not options.get(letter)]

        if not q_text:
            errors.append(f"Q{qno}: question text missing.")
        if missing:
            errors.append(f"Q{qno}: missing option(s): {', '.join(missing)}.")
        if correct not in "ABCD":
            errors.append(f"Q{qno}: correct answer missing.")

        if not q_text or missing or correct not in "ABCD":
            continue

        questions.append({
            "question": q_text,
            "option_a": options["A"],
            "option_b": options["B"],
            "option_c": options["C"],
            "option_d": options["D"],
            "correct_answer": correct,
            "explanation": explanation,
            "image_file_id": None,
        })

    if not questions:
        errors.append("No valid question block could be imported.")

    return questions, errors


def parse_csv_text(text: str) -> tuple[list[dict], list[str]]:
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as exc:
        return [], [f"CSV error: {exc}"]

    aliases = {
        "question": "question", "q": "question",
        "option_a": "option_a", "a": "option_a",
        "option_b": "option_b", "b": "option_b",
        "option_c": "option_c", "c": "option_c",
        "option_d": "option_d", "d": "option_d",
        "correct": "correct_answer", "correct_answer": "correct_answer",
        "answer": "correct_answer",
        "explanation": "explanation",
    }

    result: list[dict] = []
    errors: list[str] = []

    for index, row in enumerate(rows, 1):
        normalized = {}
        for key, value in row.items():
            canonical = aliases.get((key or "").strip().lower())
            if canonical:
                normalized[canonical] = (value or "").strip()

        correct = parse_correct_letter(normalized.get("correct_answer", ""))
        item = {
            "question": normalized.get("question", ""),
            "option_a": normalized.get("option_a", ""),
            "option_b": normalized.get("option_b", ""),
            "option_c": normalized.get("option_c", ""),
            "option_d": normalized.get("option_d", ""),
            "correct_answer": correct,
            "explanation": normalized.get("explanation", ""),
            "image_file_id": None,
        }
        if (
            not item["question"]
            or any(not item[x] for x in ("option_a", "option_b", "option_c", "option_d"))
            or correct not in "ABCD"
        ):
            errors.append(f"CSV row {index}: invalid fields.")
            continue
        result.append(item)

    return result, errors


def parse_json_text(text: str) -> tuple[list[dict], list[str]]:
    try:
        data = json.loads(text)
    except Exception as exc:
        return [], [f"JSON error: {exc}"]

    if isinstance(data, dict):
        data = data.get("questions", data.get("data", []))
    if not isinstance(data, list):
        return [], ["JSON must contain a question list."]

    result: list[dict] = []
    errors: list[str] = []

    for index, row in enumerate(data, 1):
        if not isinstance(row, dict):
            errors.append(f"JSON item {index}: object expected.")
            continue

        def get(*keys):
            for key in keys:
                if row.get(key) is not None:
                    return str(row[key]).strip()
            return ""

        options = row.get("options")
        if isinstance(options, list) and len(options) >= 4:
            a, b, c, d = [str(x).strip() for x in options[:4]]
        elif isinstance(options, dict):
            a = str(options.get("A", options.get("a", ""))).strip()
            b = str(options.get("B", options.get("b", ""))).strip()
            c = str(options.get("C", options.get("c", ""))).strip()
            d = str(options.get("D", options.get("d", ""))).strip()
        else:
            a = get("option_a", "a", "A")
            b = get("option_b", "b", "B")
            c = get("option_c", "c", "C")
            d = get("option_d", "d", "D")

        correct = parse_correct_letter(get("correct_answer", "correct", "answer"))
        item = {
            "question": get("question", "q", "text"),
            "option_a": a,
            "option_b": b,
            "option_c": c,
            "option_d": d,
            "correct_answer": correct,
            "explanation": get("explanation", "ex"),
            "image_file_id": None,
        }

        if (
            not item["question"]
            or any(not item[x] for x in ("option_a", "option_b", "option_c", "option_d"))
            or correct not in "ABCD"
        ):
            errors.append(f"JSON item {index}: invalid fields.")
            continue
        result.append(item)

    return result, errors


def parse_uploaded_file(filename: str, raw: bytes) -> tuple[list[dict], list[str]]:
    extension = os.path.splitext(filename.lower())[1]
    text = raw.decode("utf-8-sig", errors="replace")
    if extension == ".csv":
        return parse_csv_text(text)
    if extension == ".json":
        return parse_json_text(text)
    if extension in (".txt", ".text", ""):
        return parse_batch_text(text)
    return [], ["Supported file types: .txt, .csv, .json"]


def draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁 Preview", callback_data="draft:preview"),
            InlineKeyboardButton("💾 Save Quiz", callback_data="draft:save"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="draft:cancel")],
    ])


def preview_text(draft: dict, limit: int = 8) -> str:
    output = [
        "<b>QUIZ PREVIEW</b>",
        "",
        f"<b>Title:</b> {escape(draft['title'])}",
        f"<b>Subject:</b> {escape(draft.get('subject') or '—')}",
        f"<b>Questions:</b> {len(draft['questions'])}",
        f"<b>Time:</b> {draft['time_limit']} min",
        f"<b>Negative:</b> {draft['negative_marking']}",
        "",
    ]

    for index, q in enumerate(draft["questions"][:limit], 1):
        output += [
            f"<b>Q{index}.</b> {escape(q['question'])}",
            f"(a) {escape(q['option_a'])}",
            f"(b) {escape(q['option_b'])}",
            f"(c) {escape(q['option_c'])}",
            f"(d) {escape(q['option_d'])}",
            f"Correct: <b>{q['correct_answer']}</b>",
        ]
        if q.get("explanation"):
            output.append(f"Ex: {escape(q['explanation'])}")
        output.append("")

    remaining = len(draft["questions"]) - min(limit, len(draft["questions"]))
    if remaining:
        output.append(f"... और {remaining} questions हैं.")

    return "\n".join(output)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    pool = get_pool(context)
    if pool:
        try:
            await db.upsert_user(pool, user.id, user.username, user.first_name)
            await ensure_tables(pool)
        except Exception:
            logger.exception("Database initialization failed")

    await message.reply_text(
        f"<b>Brain Booster BPSC Quiz Bot</b>\n\n"
        f"Welcome, {escape(user.first_name or 'Learner')}!\n\n"
        "Professional MCQ creation, batch import, quiz attempts, "
        "bookmarks and mistake review — all in one bot.\n\n"
        "👉 <b>/create</b> से नया quiz बनाइए.",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    command_text = "\n".join(
        f"/{command} — {description}"
        for command, description in BOT_COMMANDS
    )

    await message.reply_text(
        "<b>Brain Booster BPSC Quiz Bot</b>\n\n"
        f"{command_text}\n\n"
        "<b>Batch Question Format</b>\n\n"
        "<pre>"
        "Q1. Question / प्रश्न\n"
        "👉 Choose the correct option / सही विकल्प चुनें\n"
        "(a) Option 1\n"
        "(b) Option 2 ✅\n"
        "(c) Option 3\n"
        "(d) Option 4\n"
        "Ex: Explanation / व्याख्या\n"
        "Correct: B\n\n"
        "Q2. Next question...\n"
        "</pre>\n"
        "एक ही message/file में जितने questions भेजेंगे, "
        "सभी उसी quiz में import होंगे.",
        parse_mode="HTML",
    )


async def tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "<b>📘 QUIZ CREATOR TUTORIAL</b>\n\n"
        "<b>Step 1:</b> /create\n"
        "<b>Step 2:</b> Title दें.\n"
        "<b>Step 3:</b> Subject optional है — skip कर सकते हैं.\n"
        "<b>Step 4:</b> Total time और negative marking दें.\n"
        "<b>Step 5:</b> सारे questions एक ही message में paste करें.\n"
        "<b>Alternative:</b> TXT / CSV / JSON file upload करें.\n"
        "<b>Step 6:</b> Preview → Save Quiz.\n\n"
        "<b>Correct answer:</b> option के अंत में ✅ लगाएँ; "
        "maximum reliability के लिए <code>Correct: B</code> भी रखें.\n\n"
        "<b>Important:</b> Statement/Assertion/Reason/Match/Chronology "
        "जैसे multi-line questions भी preserve किए जाते हैं.",
        parse_mode="HTML",
    )


async def create_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if user.id in DRAFTS:
        await message.reply_text(
            "एक creation/editing process पहले से चल रहा है.\n/cancel करके नया शुरू करें."
        )
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
        "<b>CREATE NEW QUIZ</b>\n\n"
        "Step 1/5 — Quiz title भेजें.\n\n"
        "Example: <code>Modern History Mega Test</code>\n"
        "Cancel: /cancel",
        parse_mode="HTML",
    )


async def section_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if user.id in DRAFTS:
        await message.reply_text("/cancel करके current process बंद करें.")
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
        "<b>MULTI-SECTION QUIZ</b>\n\n"
        "Quiz title भेजें.",
        parse_mode="HTML",
    )


async def process_creation_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return

    draft = DRAFTS.get(user.id)
    if not draft:
        return

    value = message.text.strip()
    state = draft["state"]

    # Existing quiz editing.
    if state == "edit_questions":
        questions, errors = parse_batch_text(value)
        if not questions:
            await message.reply_text(
                "❌ New batch में कोई valid question नहीं मिला.\n\n" +
                "\n".join(errors[:15])
            )
            return
        draft["questions"] = questions
        draft["state"] = "edit_complete"
        await send_import_result(message, draft, errors)
        return

    # Multi-section wizard.
    if draft.get("section_mode"):
        if state == "section_title":
            draft["title"] = value[:200]
            draft["state"] = "section_subject"
            await message.reply_text(
                "Optional subject भेजें या <code>skip</code> लिखें.",
                parse_mode="HTML",
            )
            return

        if state == "section_subject":
            draft["subject"] = "" if value.lower() in {"skip", "-", "none"} else value[:100]
            draft["state"] = "section_definition"
            await message.reply_text(
                "हर section एक line में दें:\n\n"
                "<code>Section 1 | Modern History | 10 min</code>\n"
                "<code>Section 2 | Polity | 15 min</code>\n\n"
                "सभी sections के बाद <code>done</code> लिखें.",
                parse_mode="HTML",
            )
            return

        if state == "section_definition":
            if value.lower() == "done":
                if not draft["sections"]:
                    await message.reply_text("कम से कम 1 section जरूरी है.")
                    return
                draft["state"] = "time"
                await message.reply_text("Total quiz time minutes दें.")
                return

            parts = [part.strip() for part in value.split("|")]
            if len(parts) != 3:
                await message.reply_text(
                    "Format सही रखें:\nSection 1 | Modern History | 10 min"
                )
                return

            time_match = re.search(r"\d+", parts[2])
            if not time_match:
                await message.reply_text("Section time valid number में दें.")
                return

            draft["sections"].append({
                "name": parts[1][:100],
                "time": int(time_match.group()),
            })
            await message.reply_text(
                f"✅ Section added: <b>{escape(parts[1])}</b>",
                parse_mode="HTML",
            )
            return

    # Normal /create wizard.
    if state == "title":
        if len(value) < 2:
            await message.reply_text("Valid quiz title दें.")
            return
        draft["title"] = value[:200]
        draft["state"] = "subject"
        await message.reply_text(
            "Step 2/5 — Subject optional है.\n"
            "Subject भेजें या <code>skip</code> लिखें.",
            parse_mode="HTML",
        )
        return

    if state == "subject":
        draft["subject"] = "" if value.lower() in {"skip", "-", "none", "no"} else value[:100]
        draft["state"] = "time"
        await message.reply_text(
            "Step 3/5 — Total quiz time minutes में दें.\n"
            "Example: <code>30</code>",
            parse_mode="HTML",
        )
        return

    if state == "time":
        try:
            minutes = int(value)
            if not 1 <= minutes <= 1440:
                raise ValueError
        except ValueError:
            await message.reply_text("1–1440 के बीच valid minutes दें.")
            return

        draft["time_limit"] = minutes
        draft["state"] = "negative"
        await message.reply_text(
            "Step 4/5 — Negative marking दें.\n"
            "Examples: <code>0</code>, <code>0.25</code>, <code>0.33</code>",
            parse_mode="HTML",
        )
        return

    if state == "negative":
        try:
            negative = float(value)
            if not 0 <= negative <= 10:
                raise ValueError
        except ValueError:
            await message.reply_text("0–10 के बीच valid negative marking दें.")
            return

        draft["negative_marking"] = negative
        draft["state"] = "questions"
        await message.reply_text(
            "<b>Step 5/5 — अब सारे questions एक साथ भेजें.</b>\n\n"
            "<pre>"
            "Q1. Question / प्रश्न\n"
            "👉 Choose the correct option / सही विकल्प चुनें\n"
            "(a) Option 1\n"
            "(b) Option 2 ✅\n"
            "(c) Option 3\n"
            "(d) Option 4\n"
            "Ex: Explanation / व्याख्या\n"
            "Correct: B\n\n"
            "Q2. Next question..."
            "</pre>\n"
            "TXT/CSV/JSON file भी इसी step पर upload कर सकते हैं.",
            parse_mode="HTML",
        )
        return

    if state == "questions":
        questions, errors = parse_batch_text(value)
        if not questions:
            await message.reply_text(
                "❌ कोई valid question import नहीं हुआ.\n\n"
                + "\n".join(errors[:20])
                + "\n\n/tutorial से format देखें."
            )
            return

        draft["questions"] = questions
        draft["state"] = "complete"
        await send_import_result(message, draft, errors)
        return


async def send_import_result(message, draft: dict, errors: list[str]) -> None:
    text = f"✅ <b>{len(draft['questions'])} questions imported.</b>\n\n"

    if errors:
        text += "⚠️ <b>Warnings:</b>\n"
        text += "\n".join(f"• {escape(error)}" for error in errors[:15])
        if len(errors) > 15:
            text += f"\n• ...and {len(errors) - 15} more"
        text += "\n\n"

    text += "Preview या Save Quiz चुनें."
    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=draft_keyboard(),
    )


async def process_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.document:
        return

    draft = DRAFTS.get(user.id)
    if not draft or draft.get("state") not in {"questions", "complete", "edit_questions"}:
        await message.reply_text("/create से quiz शुरू करें, फिर TXT/CSV/JSON upload करें.")
        return

    filename = message.document.file_name or "questions.txt"

    try:
        tg_file = await message.document.get_file()
        data = await tg_file.download_as_bytearray()
        questions, errors = parse_uploaded_file(filename, bytes(data))
    except Exception:
        logger.exception("File import failed")
        await message.reply_text(
            "❌ File पढ़ने में समस्या हुई.\n"
            "UTF-8 TXT, CSV या JSON file इस्तेमाल करें."
        )
        return

    if not questions:
        await message.reply_text(
            "❌ File में कोई valid question नहीं मिला.\n\n"
            + "\n".join(errors[:20])
        )
        return

    draft["questions"] = questions
    draft["state"] = "edit_complete" if draft.get("state") == "edit_questions" else "complete"
    await send_import_result(message, draft, errors)


async def insert_questions(conn, quiz_id: int, questions: list[dict]) -> None:
    for number, question in enumerate(questions, 1):
        await conn.execute(
            """
            INSERT INTO quiz_questions
            (quiz_id,question_no,question,option_a,option_b,option_c,option_d,
             correct_answer,explanation,image_file_id)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
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


async def save_new_quiz(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    pool = get_pool(context)
    draft = DRAFTS.get(user_id)
    if not pool or not draft or not draft["questions"]:
        return None

    await ensure_tables(pool)

    async with pool.acquire() as conn:
        async with conn.transaction():
            quiz_id = await conn.fetchval(
                """
                INSERT INTO quizzes
                (creator_id,title,subject,question_count,time_limit,negative_marking,published,sections)
                VALUES($1,$2,$3,$4,$5,$6,TRUE,$7::jsonb)
                RETURNING id
                """,
                user_id,
                draft["title"],
                draft.get("subject") or None,
                len(draft["questions"]),
                draft["time_limit"],
                draft["negative_marking"],
                json.dumps(draft.get("sections", []), ensure_ascii=False),
            )
            await insert_questions(conn, quiz_id, draft["questions"])

    return int(quiz_id)


async def update_existing_quiz(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    draft: dict,
) -> bool:
    pool = get_pool(context)
    if not pool or not draft.get("quiz_id"):
        return False

    quiz_id = int(draft["quiz_id"])
    await ensure_tables(pool)

    async with pool.acquire() as conn:
        async with conn.transaction():
            allowed = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM quizzes
                    WHERE id=$1 AND (creator_id=$2 OR $2=$3)
                )
                """,
                quiz_id,
                user_id,
                ADMIN_ID,
            )
            if not allowed:
                return False

            await conn.execute(
                "DELETE FROM quiz_questions WHERE quiz_id=$1",
                quiz_id,
            )
            await conn.execute(
                """
                UPDATE quizzes
                SET title=$1,subject=$2,question_count=$3,
                    time_limit=$4,negative_marking=$5,
                    published=TRUE,sections=$6::jsonb
                WHERE id=$7
                """,
                draft["title"],
                draft.get("subject") or None,
                len(draft["questions"]),
                draft["time_limit"],
                draft["negative_marking"],
                json.dumps(draft.get("sections", []), ensure_ascii=False),
                quiz_id,
            )
            await insert_questions(conn, quiz_id, draft["questions"])

    return True


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user
    draft = DRAFTS.get(user.id)

    if not draft:
        await query.message.reply_text("Draft नहीं मिला. /create से शुरू करें.")
        return

    action = query.data.split(":", 1)[1]

    if action == "cancel":
        DRAFTS.pop(user.id, None)
        await query.message.reply_text("❌ Draft cancelled.")
        return

    if action == "preview":
        await query.message.reply_text(
            preview_text(draft),
            parse_mode="HTML",
        )
        return

    if action != "save":
        return

    if not draft.get("questions"):
        await query.message.reply_text("❌ कम से कम 1 valid question जरूरी है.")
        return

    try:
        if draft.get("state") == "edit_complete":
            ok = await update_existing_quiz(user.id, context, draft)
            if not ok:
                await query.message.reply_text(
                    "❌ Quiz update failed या edit permission नहीं है."
                )
                return

            quiz_id = draft["quiz_id"]
            count = len(draft["questions"])
            DRAFTS.pop(user.id, None)
            await query.message.reply_text(
                f"✅ <b>Quiz #{quiz_id} updated successfully.</b>\n\n"
                f"Questions: <b>{count}</b>\n"
                "Status: <b>Published</b>",
                parse_mode="HTML",
            )
            return

        quiz_id = await save_new_quiz(user.id, context)

    except Exception:
        logger.exception("Quiz save/update failed")
        await query.message.reply_text(
            "⚠️ Database error आया. Quiz save/update नहीं हुआ."
        )
        return

    if quiz_id is None:
        await query.message.reply_text("⚠️ Database connected नहीं है.")
        return

    count = len(draft["questions"])
    DRAFTS.pop(user.id, None)

    await query.message.reply_text(
        f"✅ <b>Quiz created successfully!</b>\n\n"
        f"Quiz ID: <code>{quiz_id}</code>\n"
        f"Questions: <b>{count}</b>\n"
        "Status: <b>Published</b>\n\n"
        "/myquizzes से Start/Edit करें.",
        parse_mode="HTML",
    )


async def myquizzes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    pool = get_pool(context)
    if not user or not message:
        return

    if not pool:
        await message.reply_text("⚠️ Database connected नहीं है.")
        return

    await ensure_tables(pool)

    if is_admin(user.id):
        rows = await pool.fetch(
            """
            SELECT id,title,subject,question_count,time_limit,negative_marking,published
            FROM quizzes ORDER BY id DESC LIMIT 50
            """
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id,title,subject,question_count,time_limit,negative_marking,published
            FROM quizzes
            WHERE published=TRUE AND (creator_id=$1 OR creator_id=$2)
            ORDER BY id DESC LIMIT 50
            """,
            user.id,
            ADMIN_ID,
        )

    if not rows:
        await message.reply_text("अभी कोई quiz उपलब्ध नहीं है.")
        return

    for row in rows:
        buttons = []

        if row["published"]:
            buttons.append([
                InlineKeyboardButton(
                    "▶️ Start",
                    callback_data=f"start:{row['id']}",
                )
            ])

        # Callback itself checks ownership/admin permission.
        buttons.append([
            InlineKeyboardButton(
                "✏️ Edit",
                callback_data=f"edit:{row['id']}",
            ),
            InlineKeyboardButton(
                "🗑 Delete",
                callback_data=f"delete:{row['id']}",
            ),
        ])

        await message.reply_text(
            f"<b>#{row['id']} — {escape(row['title'])}</b>\n"
            f"Subject: {escape(row['subject'] or '—')}\n"
            f"Questions: {row['question_count']}\n"
            f"Time: {row['time_limit']} min\n"
            f"Negative: {row['negative_marking']}\n"
            f"Status: {'🟢 Published' if row['published'] else '🟡 Draft'}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    draft = DRAFTS.get(user.id)
    active = ACTIVE.get(user.id)

    if draft:
        await message.reply_text(
            f"<b>Draft Settings</b>\n\n"
            f"Title: {escape(draft.get('title') or '—')}\n"
            f"Subject: {escape(draft.get('subject') or '—')}\n"
            f"Time: {draft.get('time_limit', 30)} min\n"
            f"Negative: {draft.get('negative_marking', 0)}\n"
            f"Questions: {len(draft.get('questions', []))}",
            parse_mode="HTML",
        )
        return

    if active:
        await message.reply_text(
            f"<b>Active Quiz Settings</b>\n\n"
            f"Timer: {active['timer_seconds']} sec/question\n"
            f"Paused: {'Yes' if active['paused'] else 'No'}",
            parse_mode="HTML",
        )
        return

    await message.reply_text(
        "कोई active quiz या draft नहीं है.\n/create से नया quiz बनाएं."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    active = ACTIVE.pop(user.id, None)
    if not active:
        await message.reply_text("कोई active quiz नहीं है.")
        return

    await message.reply_text("⏹️ Current quiz stopped.")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    active = ACTIVE.get(user.id)
    if not active:
        await message.reply_text("कोई active quiz नहीं है.")
        return

    active["paused"] = True
    await message.reply_text("⏸️ Quiz paused.\n/resume से जारी करें.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    active = ACTIVE.get(user.id)
    if not active:
        await message.reply_text("कोई paused quiz नहीं है.")
        return

    active["paused"] = False
    await message.reply_text("▶️ Quiz resumed.")
    await send_current_question(user.id, context, message)


async def fast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    active = ACTIVE.get(user.id)
    if not active:
        await message.reply_text("कोई active quiz नहीं है.")
        return

    active["timer_seconds"] = max(10, active["timer_seconds"] - 10)
    await message.reply_text(
        f"⚡ Timer decreased: {active['timer_seconds']} sec/question"
    )


async def slow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    active = ACTIVE.get(user.id)
    if not active:
        await message.reply_text("कोई active quiz नहीं है.")
        return

    active["timer_seconds"] = min(3600, active["timer_seconds"] + 10)
    await message.reply_text(
        f"🐢 Timer increased: {active['timer_seconds']} sec/question"
    )


async def stoppoll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    active = ACTIVE.get(user.id)
    if active and active.get("poll_message_id"):
        try:
            await context.bot.stop_poll(
                chat_id=user.id,
                message_id=active["poll_message_id"],
            )
            active["poll_message_id"] = None
            await message.reply_text("🛑 Active poll stopped.")
            return
        except Exception:
            logger.exception("stop_poll failed")

    await message.reply_text("इस समय कोई active Telegram poll नहीं है.")


async def mistakes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    pool = get_pool(context)
    if not user or not message:
        return

    if not pool:
        await message.reply_text("⚠️ Database connected नहीं है.")
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT q.question,a.selected_answer,q.correct_answer,q.explanation
        FROM quiz_answers a
        JOIN quiz_questions q ON q.id=a.question_id
        JOIN quiz_attempts t ON t.id=a.attempt_id
        WHERE t.user_id=$1 AND a.is_correct=FALSE
        ORDER BY a.answered_at DESC LIMIT 30
        """,
        user.id,
    )

    if not rows:
        await message.reply_text("कोई incorrect answer record नहीं मिला.")
        return

    output = ["<b>❌ MISTAKES</b>", ""]

    for index, row in enumerate(rows, 1):
        output.append(f"<b>{index}.</b> {escape(row['question'])}")
        output.append(
            f"Your answer: {escape(row['selected_answer'] or '—')}"
        )
        output.append(
            f"Correct: {escape(row['correct_answer'])}"
        )
        if row["explanation"]:
            output.append(
                f"Ex: {escape(row['explanation'])}"
            )
        output.append("")

    await message.reply_text(
        "\n".join(output),
        parse_mode="HTML",
    )


async def bookmarks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    pool = get_pool(context)
    if not user or not message:
        return

    if not pool:
        await message.reply_text("⚠️ Database connected नहीं है.")
        return

    await ensure_tables(pool)

    rows = await pool.fetch(
        """
        SELECT q.question,q.option_a,q.option_b,q.option_c,q.option_d,
               q.correct_answer,q.explanation
        FROM quiz_bookmarks b
        JOIN quiz_questions q ON q.id=b.question_id
        WHERE b.user_id=$1
        ORDER BY b.created_at DESC LIMIT 30
        """,
        user.id,
    )

    if not rows:
        await message.reply_text("कोई bookmarked question नहीं है.")
        return

    output = ["<b>🔖 BOOKMARKS</b>", ""]

    for index, row in enumerate(rows, 1):
        output += [
            f"<b>{index}. {escape(row['question'])}</b>",
            f"(a) {escape(row['option_a'])}",
            f"(b) {escape(row['option_b'])}",
            f"(c) {escape(row['option_c'])}",
            f"(d) {escape(row['option_d'])}",
            f"Correct: {escape(row['correct_answer'])}",
            "",
        ]

    await message.reply_text(
        "\n".join(output),
        parse_mode="HTML",
    )


async def start_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user
    pool = get_pool(context)

    if not pool:
        await query.message.reply_text("⚠️ Database connected नहीं है.")
        return

    try:
        quiz_id = int(query.data.split(":", 1)[1])
    except ValueError:
        await query.message.reply_text("Invalid quiz ID.")
        return

    await ensure_tables(pool)

    quiz = await pool.fetchrow(
        """
        SELECT id,title,time_limit,negative_marking
        FROM quizzes
        WHERE id=$1 AND published=TRUE
        """,
        quiz_id,
    )

    if not quiz:
        await query.message.reply_text("Quiz नहीं मिला या published नहीं है.")
        return

    questions = await pool.fetch(
        """
        SELECT id,question_no,question,option_a,option_b,option_c,option_d,
               correct_answer,explanation
        FROM quiz_questions
        WHERE quiz_id=$1
        ORDER BY question_no
        """,
        quiz_id,
    )

    if not questions:
        await query.message.reply_text("इस quiz में questions नहीं हैं.")
        return

    ACTIVE.pop(user.id, None)

    attempt_id = await pool.fetchval(
        """
        INSERT INTO quiz_attempts(quiz_id,user_id)
        VALUES($1,$2)
        RETURNING id
        """,
        quiz_id,
        user.id,
    )

    per_question = max(
        10,
        int((int(quiz["time_limit"]) * 60) / max(1, len(questions))),
    )

    ACTIVE[user.id] = {
        "quiz_id": quiz_id,
        "attempt_id": int(attempt_id),
        "questions": [dict(q) for q in questions],
        "index": 0,
        "timer_seconds": per_question,
        "paused": False,
        "poll_message_id": None,
    }

    await query.message.reply_text(
        f"<b>▶️ {escape(quiz['title'])}</b>\n\n"
        f"Questions: {len(questions)}\n"
        f"Total time: {quiz['time_limit']} min\n\n"
        "Option button दबाकर answer करें.",
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
        await message.reply_text("⏸️ Quiz paused. /resume दबाएँ.")
        return

    if active["index"] >= len(active["questions"]):
        await complete_attempt(user_id, context, message)
        return

    question = active["questions"][active["index"]]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"A. {question['option_a'][:32]}",
                callback_data=f"ans:A:{question['id']}",
            ),
            InlineKeyboardButton(
                f"B. {question['option_b'][:32]}",
                callback_data=f"ans:B:{question['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                f"C. {question['option_c'][:32]}",
                callback_data=f"ans:C:{question['id']}",
            ),
            InlineKeyboardButton(
                f"D. {question['option_d'][:32]}",
                callback_data=f"ans:D:{question['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔖 Bookmark",
                callback_data=f"bookmark:{question['id']}",
            ),
        ],
    ])

    await message.reply_text(
        f"<b>Q{active['index'] + 1}/{len(active['questions'])}</b>\n\n"
        f"{escape(question['question'])}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    active = ACTIVE.get(user.id)
    pool = get_pool(context)

    if not active or not pool:
        await query.answer("No active quiz.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[0]

    try:
        question_id = int(parts[-1])
    except ValueError:
        await query.answer("Invalid question.", show_alert=True)
        return

    if action == "bookmark":
        await pool.execute(
            """
            INSERT INTO quiz_bookmarks(user_id,question_id)
            VALUES($1,$2)
            ON CONFLICT DO NOTHING
            """,
            user.id,
            question_id,
        )
        await query.answer("🔖 Bookmarked")
        return

    if action != "ans" or len(parts) != 3:
        return

    selected = parts[1]
    current = active["questions"][active["index"]]

    if question_id != current["id"]:
        await query.answer(
            "This question is no longer active.",
            show_alert=True,
        )
        return

    correct = selected == current["correct_answer"]

    await pool.execute(
        """
        INSERT INTO quiz_answers
        (attempt_id,question_id,selected_answer,is_correct)
        VALUES($1,$2,$3,$4)
        ON CONFLICT(attempt_id,question_id)
        DO UPDATE SET
            selected_answer=EXCLUDED.selected_answer,
            is_correct=EXCLUDED.is_correct,
            answered_at=now()
        """,
        active["attempt_id"],
        question_id,
        selected,
        correct,
    )

    await query.answer("Correct!" if correct else "Wrong!")

    if correct:
        await query.message.reply_text("✅ Correct!")
    else:
        explanation = (
            f"\n💡 {current['explanation']}"
            if current.get("explanation")
            else ""
        )
        await query.message.reply_text(
            f"❌ Wrong.\n"
            f"Correct answer: {current['correct_answer']}"
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
    active = ACTIVE.pop(user_id, None)
    if not active:
        return

    pool = get_pool(context)

    if not pool:
        await message.reply_text("Quiz finished.")
        return

    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER(WHERE is_correct=TRUE) AS correct,
            COUNT(*) FILTER(WHERE is_correct=FALSE) AS wrong
        FROM quiz_answers
        WHERE attempt_id=$1
        """,
        active["attempt_id"],
    )

    correct = int(row["correct"] or 0)
    wrong = int(row["wrong"] or 0)
    total = len(active["questions"])
    unanswered = max(0, total - correct - wrong)

    quiz = await pool.fetchrow(
        "SELECT negative_marking FROM quizzes WHERE id=$1",
        active["quiz_id"],
    )

    negative = float(quiz["negative_marking"] or 0) if quiz else 0
    score = correct - wrong * negative

    await pool.execute(
        """
        UPDATE quiz_attempts
        SET score=$1,correct_count=$2,wrong_count=$3,
            unanswered_count=$4,completed=TRUE,completed_at=now()
        WHERE id=$5
        """,
        score,
        correct,
        wrong,
        unanswered,
        active["attempt_id"],
    )

    await message.reply_text(
        f"<b>🏁 Quiz Completed</b>\n\n"
        f"Correct: {correct}\n"
        f"Wrong: {wrong}\n"
        f"Unanswered: {unanswered}\n"
        f"Score: <b>{score:.2f}</b>",
        parse_mode="HTML",
    )


async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user
    pool = get_pool(context)

    if not pool:
        await query.message.reply_text("⚠️ Database connected नहीं है.")
        return

    quiz_id = int(query.data.split(":", 1)[1])
    await ensure_tables(pool)

    row = await pool.fetchrow(
        """
        SELECT id,title,subject,time_limit,negative_marking
        FROM quizzes
        WHERE id=$1 AND (creator_id=$2 OR $2=$3)
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
        SELECT question,option_a,option_b,option_c,option_d,
               correct_answer,explanation,image_file_id
        FROM quiz_questions
        WHERE quiz_id=$1
        ORDER BY question_no
        """,
        quiz_id,
    )

    DRAFTS[user.id] = {
        "state": "edit_questions",
        "quiz_id": quiz_id,
        "title": row["title"],
        "subject": row["subject"] or "",
        "time_limit": int(row["time_limit"]),
        "negative_marking": float(row["negative_marking"] or 0),
        "questions": [dict(q) for q in questions],
        "sections": [],
        "section_mode": False,
        "created_by": user.id,
    }

    await query.message.reply_text(
        f"<b>✏️ Edit Quiz #{quiz_id}</b>\n\n"
        f"Current questions: {len(questions)}\n\n"
        "नया पूरा question batch भेजें.\n"
        "यह existing question set को replace करेगा.\n\n"
        "/cancel",
        parse_mode="HTML",
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    quiz_id = int(query.data.split(":", 1)[1])

    await query.message.reply_text(
        f"Quiz #{quiz_id} delete करना है?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Confirm Delete",
                    callback_data=f"confirm_delete:{quiz_id}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data="delete_cancel",
                ),
            ]
        ]),
    )


async def confirm_delete_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user
    pool = get_pool(context)

    if query.data == "delete_cancel":
        await query.message.reply_text("Delete cancelled.")
        return

    if not pool:
        await query.message.reply_text("⚠️ Database connected नहीं है.")
        return

    quiz_id = int(query.data.split(":", 1)[1])

    result = await pool.execute(
        """
        DELETE FROM quizzes
        WHERE id=$1 AND (creator_id=$2 OR $2=$3)
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


async def testbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    if not context.args:
        await message.reply_text(
            "Testbook URL दें:\n"
            "<code>/testbook https://...</code>",
            parse_mode="HTML",
        )
        return

    url = context.args[0].strip()
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        await message.reply_text("❌ Valid http/https URL दें.")
        return

    try:
        request = Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urlopen(request, timeout=12) as response:
            html = response.read(2_000_000).decode(
                "utf-8",
                errors="ignore",
            )

        plain = re.sub(
            r"<script.*?</script>",
            " ",
            html,
            flags=re.I | re.S,
        )
        plain = re.sub(
            r"<style.*?</style>",
            " ",
            plain,
            flags=re.I | re.S,
        )
        plain = re.sub(r"<[^>]+>", "\n", plain)
        plain = clean_markup(plain)

        questions, errors = parse_batch_text(plain)

        if questions and message.from_user:
            user = message.from_user
            DRAFTS[user.id] = {
                "state": "complete",
                "title": "Imported Testbook Quiz",
                "subject": "Imported",
                "time_limit": 30,
                "negative_marking": 0.0,
                "questions": questions,
                "sections": [],
                "section_mode": False,
                "created_by": user.id,
            }
            await send_import_result(
                message,
                DRAFTS[user.id],
                errors,
            )
            return

    except Exception:
        logger.exception("Testbook import failed")

    await message.reply_text(
        "⚠️ URL valid है, लेकिन इस Testbook page से questions सीधे extract नहीं हो सके.\n\n"
        "Modern JavaScript pages ऐसा कर सकते हैं.\n"
        "Page का TXT/CSV/JSON export /create के दौरान upload करें."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if DRAFTS.pop(user.id, None) is not None:
        await message.reply_text(
            "❌ Current creation/editing process cancelled."
        )
    else:
        await message.reply_text(
            "कोई creation/editing process active नहीं है."
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "❌ Unknown command.\n/help से available commands देखें."
        )


def register_handlers(application: Application) -> None:
    """Register exactly the requested 17 commands plus required callbacks."""
    command_handlers = {
        "start": start,
        "create": create_quiz,
        "myquizzes": myquizzes,
        "settings": settings,
        "stop": stop,
        "help": help_command,
        "section": section_command,
        "mistakes": mistakes,
        "bookmarks": bookmarks,
        "pause": pause,
        "resume": resume,
        "fast": fast,
        "slow": slow,
        "stoppoll": stoppoll,
        "testbook": testbook,
        "tutorial": tutorial,
        "cancel": cancel,
    }

    for command, callback in command_handlers.items():
        application.add_handler(
            CommandHandler(command, callback),
            group=0,
        )

    # Text is routed only when a creation/editing workflow is active.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_creation_text,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_document,
        ),
        group=0,
    )

    application.add_handler(
        CallbackQueryHandler(
            draft_callback,
            pattern=r"^draft:(preview|save|cancel)$",
        ),
        group=0,
    )

    application.add_handler(
        CallbackQueryHandler(
            start_quiz_callback,
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
            confirm_delete_callback,
            pattern=r"^(confirm_delete:\d+|delete_cancel)$",
        ),
        group=0,
    )

    application.add_handler(
        CallbackQueryHandler(
            answer_callback,
            pattern=r"^(ans|bookmark):",
        ),
        group=0,
    )

    # Must be in a later group so valid commands do not trigger Unknown command.
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=1,
    )


async def set_bot_commands(application: Application) -> None:
    """Set exactly the requested commands in Telegram's command menu."""
    await application.bot.set_my_commands(BOT_COMMANDS)
