"""Builds the Telegram application and registers handlers."""

from __future__ import annotations

import time

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from brains_bot.bot import handlers, project_handlers
from brains_bot.config import Settings
from brains_bot.database.session import make_engine, make_session_factory

# The bot only works in one-to-one chats, so private data never lands in a group.
PRIVATE_CHAT = filters.ChatType.PRIVATE


async def _post_init(application: Application) -> None:
    # Only public commands go in Telegram's command menu, which every user can see.
    await application.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc in handlers.PUBLIC_COMMANDS.items()]
    )


def build_application(settings: Settings) -> Application:
    application = (
        Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["started_at"] = time.monotonic()
    # The engine connects lazily, so a database that is down does not stop the bot starting.
    application.bot_data["db"] = (
        make_session_factory(make_engine(settings.database_url)) if settings.database_url else None
    )

    commands = {
        "start": handlers.start,
        "help": handlers.help_command,
        "myid": handlers.myid,
        "health": handlers.health,
        "projects": project_handlers.projects,
        "search": project_handlers.search,
        "project": project_handlers.project,
    }
    for name, callback in commands.items():
        application.add_handler(CommandHandler(name, callback, filters=PRIVATE_CHAT))
    application.add_handler(
        CallbackQueryHandler(
            project_handlers.projects_page_callback, pattern=project_handlers.PAGE_CALLBACK
        )
    )
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.COMMAND, handlers.unknown_command)
    )
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.TEXT & ~filters.COMMAND, handlers.text_message)
    )
    application.add_error_handler(handlers.error_handler)
    return application
