"""Entry point: `uv run brains-bot` or `uv run python -m brains_bot`."""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv
from telegram import Update

from brains_bot.bot.app import build_application
from brains_bot.config import ConfigError, load_settings
from brains_bot.logging_setup import configure_logging

log = logging.getLogger("brains_bot")


def main() -> int:
    load_dotenv()  # values already in the environment take precedence over .env
    try:
        settings = load_settings()
    except ConfigError as exc:
        configure_logging()
        log.error("configuration error: %s", exc, extra={"event": "config_error"})
        return 2

    configure_logging(settings.log_level, secrets=[settings.telegram_bot_token])
    if not settings.allowed_user_ids:
        log.warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty: every user will be denied private features",
            extra={"event": "empty_allowlist"},
        )

    application = build_application(settings)
    log.info(
        "bot starting (long polling)",
        extra={"event": "startup", "allowlist_size": len(settings.allowed_user_ids)},
    )
    # run_polling handles SIGINT/SIGTERM and shuts the application down cleanly.
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception:
        # Route fatal errors through the redacting logger instead of a raw traceback.
        log.exception("bot stopped after a fatal error", extra={"event": "fatal"})
        return 1
    log.info("bot stopped", extra={"event": "shutdown"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
