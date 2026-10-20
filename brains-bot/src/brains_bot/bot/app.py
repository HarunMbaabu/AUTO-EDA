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

from brains_bot.bot import (
    analysis_handlers,
    chat_handlers,
    contact_handlers,
    followup_handlers,
    handlers,
    import_handlers,
    outreach_handlers,
    project_handlers,
    site_handlers,
)
from brains_bot.config import Settings
from brains_bot.database.session import make_engine, make_session_factory, session_scope
from brains_bot.services import followups
from brains_bot.services.analysis import ClaudeAnalyzer, make_client
from brains_bot.services.categories import sync_categories
from brains_bot.services.chat import ChatAssistant
from brains_bot.services.chat_api import PROVIDERS, ChatAnalyzer
from brains_bot.services.outreach import Drafter

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


def _make_analyzer(settings: Settings):
    """The AI analyzer for /analyze: Mistral or DeepSeek (AI_PROVIDER, or whichever key is set),
    else Claude if ANTHROPIC_API_KEY and CLAUDE_MODEL are set, else None (analysis disabled)."""
    if settings.ai_provider in PROVIDERS:
        return ChatAnalyzer.from_env(PROVIDERS[settings.ai_provider])
    if settings.ai_provider == "anthropic":
        return ClaudeAnalyzer(make_client(), settings.claude_model)
    return None


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
    analyzer = _make_analyzer(settings)
    application.bot_data["analyzer"] = analyzer
    # Chat mode needs tool calling, which the chat-completions providers (Mistral, DeepSeek) offer.
    application.bot_data["assistant"] = (
        ChatAssistant(analyzer) if isinstance(analyzer, ChatAnalyzer) else None
    )
    application.bot_data["drafter"] = (
        Drafter(analyzer) if isinstance(analyzer, ChatAnalyzer) else None
    )

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
        "analyse": analysis_handlers.analyze,  # British spelling
        "import": import_handlers.import_help,
        "newchat": chat_handlers.new_chat,
        "draft": outreach_handlers.draft,
        "newsite": site_handlers.newsite,
        "contacts": contact_handlers.contacts_command,
        "top": followup_handlers.top,
        "followups": followup_handlers.followups_command,
        "reminders": followup_handlers.reminders,
    }
    for name, callback in commands.items():
        application.add_handler(CommandHandler(name, callback, filters=PRIVATE_CHAT))
    application.add_handler(
        CallbackQueryHandler(
            outreach_handlers.draft_callback, pattern=outreach_handlers.DRAFT_CALLBACK
        )
    )
    application.add_handler(
        CallbackQueryHandler(site_handlers.site_callback, pattern=site_handlers.SITE_CALLBACK)
    )
    application.add_handler(
        CallbackQueryHandler(
            project_handlers.projects_page_callback, pattern=project_handlers.PAGE_CALLBACK
        )
    )
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.COMMAND, handlers.unknown_command)
    )
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.TEXT & ~filters.COMMAND, chat_handlers.chat_message)
    )
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.Document.ALL, import_handlers.workbook_upload)
    )
    application.add_error_handler(handlers.error_handler)
    if application.job_queue is not None and settings.database_url:
        application.job_queue.run_daily(
            followup_handlers.send_weekly_digest,
            time=followups.DIGEST_TIME,
            days=followups.DIGEST_DAYS,
            name="weekly_digest",
        )
    return application
