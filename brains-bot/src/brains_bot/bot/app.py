"""Builds the Telegram application and registers handlers."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from brains_bot.bot import analysis_handlers, handlers, project_handlers
from brains_bot.config import Settings
from brains_bot.database.session import make_engine, make_session_factory, session_scope
from brains_bot.services.analysis import ClaudeAnalyzer, make_client
from brains_bot.services.categories import sync_categories

log = logging.getLogger(__name__)

# The bot only works in one-to-one chats, so private data never lands in a group.
PRIVATE_CHAT = filters.ChatType.PRIVATE


async def _post_init(application: Application) -> None:
    # Only public commands go in Telegram's command menu, which every user can see.
    await application.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc in handlers.PUBLIC_COMMANDS.items()]
    )
    factory = application.bot_data.get("db")
    if factory is not None:
        # Keep the category table in step with data/categories.json.
        def sync() -> int:
            with session_scope(factory) as session:
                return sync_categories(session)

        try:
            count = await asyncio.to_thread(sync)
            log.info(
                "categories synced", extra={"event": "categories_synced", "command": str(count)}
            )
        except Exception:
            log.exception("category sync failed", extra={"event": "db_error"})


def _make_analyzer(settings: Settings) -> ClaudeAnalyzer | None:
    if not (settings.anthropic_key_present and settings.claude_model):
        return None
    return ClaudeAnalyzer(make_client(), settings.claude_model)


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
    application.bot_data["analyzer"] = _make_analyzer(settings)

    commands = {
        "start": handlers.start,
        "help": handlers.help_command,
        "myid": handlers.myid,
        "health": handlers.health,
        "projects": project_handlers.projects,
        "search": project_handlers.search,
        "project": project_handlers.project,
        "categories": analysis_handlers.categories,
        "analyze": analysis_handlers.analyze,
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
