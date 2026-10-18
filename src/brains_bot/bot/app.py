"""Builds the Telegram application and registers handlers."""

from __future__ import annotations

import time

from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from brains_bot.bot import handlers
from brains_bot.config import Settings

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

    application.add_handler(CommandHandler("start", handlers.start, filters=PRIVATE_CHAT))
    application.add_handler(CommandHandler("help", handlers.help_command, filters=PRIVATE_CHAT))
    application.add_handler(CommandHandler("myid", handlers.myid, filters=PRIVATE_CHAT))
    application.add_handler(CommandHandler("health", handlers.health, filters=PRIVATE_CHAT))
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.COMMAND, handlers.unknown_command)
    )
    application.add_handler(
        MessageHandler(PRIVATE_CHAT & filters.TEXT & ~filters.COMMAND, handlers.text_message)
    )
    application.add_error_handler(handlers.error_handler)
    return application
