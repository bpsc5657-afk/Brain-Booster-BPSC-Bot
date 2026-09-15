# =========================================================
# UNKNOWN COMMAND
# =========================================================

# IMPORTANT:
# CommandHandler group 0 में valid command को handle करता है,
# लेकिन generic filters.COMMAND बाद के group में उसी update को
# फिर से देख सकता है।
#
# इसलिए unknown_command को registered commands को explicitly ignore
# करना जरूरी है। इससे /create, /cancel, /help आदि पर
# "Unknown command" दोबारा नहीं आएगा.

KNOWN_COMMANDS = {
    command.lower()
    for command, _description in BOT_COMMANDS
}

# /attempt BOT_COMMANDS menu में नहीं है, लेकिन operational command है.
KNOWN_COMMANDS.add("attempt")


def _extract_command(update: Update) -> str:
    """Extract /command or /command@BotUsername safely."""

    message = get_message(update)

    if not message or not message.text:
        return ""

    token = message.text.split(maxsplit=1)[0]

    if not token.startswith("/"):
        return ""

    # /create
    # /create@MyQuizBot
    return token[1:].split("@", 1)[0].lower()


async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = get_message(update)

    if not message:
        return

    command = _extract_command(update)

    # VALID COMMAND:
    # इसे यहाँ दोबारा message नहीं करना है.
    if command in KNOWN_COMMANDS:
        return

    # केवल genuinely unknown command पर यह message आएगा.
    await message.reply_text(
        "❌ Unknown command.\n\n"
        "/help से available commands देखें."
    )


# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_handlers(
    application: Application,
) -> None:

    # सभी valid commands यहाँ registered हैं.
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

        # Internal/operational command.
        # Public menu में नहीं दिखाया गया है.
        "attempt": attempt_quiz,
    }

    # -----------------------------------------------------
    # COMMAND HANDLERS
    # -----------------------------------------------------

    for command, handler in command_map.items():
        application.add_handler(
            CommandHandler(
                command,
                handler,
            ),
            group=0,
        )

    # -----------------------------------------------------
    # CALLBACK HANDLER
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_router,
            pattern=(
                r"^(draft_|"
                r"setting_|"
                r"answer:|"
                r"skip:|"
                r"bookmark:|"
                r"stored_preview:|"
                r"edit_questions:)"
            ),
        ),
        group=0,
    )

    # -----------------------------------------------------
    # FILE IMPORT
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_quiz_file,
        ),
        group=1,
    )

    # -----------------------------------------------------
    # REPLY KEYBOARD
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Regex(
                r"^(➕ Create Quiz|"
                r"📚 My Quizzes|"
                r"⚙️ Settings|"
                r"📖 Tutorial)$"
            ),
            menu_buttons,
        ),
        group=1,
    )

    # -----------------------------------------------------
    # QUIZ CREATION TEXT
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_quiz_creation,
        ),
        group=2,
    )

    # -----------------------------------------------------
    # UNKNOWN COMMAND FALLBACK
    # -----------------------------------------------------
    #
    # यह handler valid commands को ऊपर वाले
    # KNOWN_COMMANDS check के कारण ignore करेगा.
    #
    # केवल /abcxyz जैसे unknown commands पर response देगा.

    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        ),
        group=3,
    )
