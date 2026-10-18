"""Core Telegram command handlers and the access check."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import ContextTypes

from brains_bot.config import Settings

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

PUBLIC_COMMANDS = {
    "start": "Welcome and access check",
    "help": "List supported commands",
    "myid": "Show your Telegram numeric ID",
}
PRIVATE_COMMANDS = {
    "health": "Confirm the bot is running",
    "projects": "Browse projects, most recent visit first",
    "search": "Search by site, area, city or a person/company name",
    "project": "Show one project, e.g. /project 12",
    "categories": "Product categories and their best timing",
    "analyze": "Claude analysis for a project and category, e.g. /analyze 12 audio",
    "import": "How to load or update the data: send the workbook (.xlsx) as a file",
}

NOT_AUTHORIZED = (
    "This is a private bot and you are not authorized to use it.\n"
    "If you should have access, send /myid and share the number with the administrator."
)


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return _settings(context).is_allowed(_user_id(update))


def _log_command(update: Update, command: str, authorized: bool) -> None:
    log.info(
        "command received",
        extra={
            "event": "command",
            "command": command,
            "user_id": _user_id(update),
            "authorized": authorized,
        },
    )


def restricted(command: str) -> Callable[[Handler], Handler]:
    """Only allowlisted users reach the wrapped handler; everyone else is denied."""

    def decorator(func: Handler) -> Handler:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            authorized = _is_authorized(update, context)
            _log_command(update, command, authorized)
            if not authorized:
                if update.effective_message:
                    await update.effective_message.reply_text(NOT_AUTHORIZED)
                return
            await func(update, context)

        return wrapper

    return decorator


def _format_commands(commands: dict[str, str]) -> str:
    return "\n".join(f"/{name} - {desc}" for name, desc in commands.items())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = _is_authorized(update, context)
    _log_command(update, "start", authorized)
    if authorized:
        text = "Welcome to Brains Bot. You are authorized.\nSend /help to see what I can do."
    else:
        text = NOT_AUTHORIZED
    await update.effective_message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = _is_authorized(update, context)
    _log_command(update, "help", authorized)
    commands = {**PUBLIC_COMMANDS, **PRIVATE_COMMANDS} if authorized else PUBLIC_COMMANDS
    await update.effective_message.reply_text("Commands:\n" + _format_commands(commands))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Public on purpose: needed for enrollment. Reveals only the caller's own ID.
    _log_command(update, "myid", _is_authorized(update, context))
    user_id = _user_id(update)
    if user_id is None:
        await update.effective_message.reply_text("I couldn't determine your Telegram ID.")
        return
    await update.effective_message.reply_text(f"Your Telegram user ID is: {user_id}")


@restricted("health")
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime = int(time.monotonic() - context.bot_data["started_at"])
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    await update.effective_message.reply_text(
        f"OK - Brains Bot is running. Uptime {hours}h {minutes}m {seconds}s."
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = _is_authorized(update, context)
    _log_command(update, "unknown", authorized)
    if not authorized:
        await update.effective_message.reply_text(NOT_AUTHORIZED)
        return
    await update.effective_message.reply_text("Unknown command. Send /help for the list.")


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Message text is never logged: it may contain customer data.
    authorized = _is_authorized(update, context)
    _log_command(update, "text", authorized)
    if not authorized:
        await update.effective_message.reply_text(NOT_AUTHORIZED)
        return
    await update.effective_message.reply_text(
        "I only understand commands for now. Send /help for the list."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError) and not isinstance(update, Update):
        # A dropped connection while polling Telegram; the library retries on its own.
        log.warning(
            "temporary network problem talking to Telegram; retrying (%s)",
            type(context.error).__name__,
            extra={"event": "network_retry"},
        )
        return
    # Log the exception type and traceback only, never the update payload.
    log.error(
        "unhandled error while processing update",
        exc_info=context.error,
        extra={"event": "handler_error"},
    )
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Sorry, something went wrong. Please try again.")
