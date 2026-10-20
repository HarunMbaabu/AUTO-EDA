"""Entry point: `uv run brains-bot-dashboard` (port from $PORT, default 8000)."""

from __future__ import annotations

import os

import uvicorn

from brains_bot.config import load_env_file
from brains_bot.logging_setup import configure_logging


def main() -> None:
    load_env_file()
    configure_logging(
        os.environ.get("LOG_LEVEL", "INFO"), secrets=[os.environ.get("TELEGRAM_BOT_TOKEN", "")]
    )
    from brains_bot.dashboard.app import create_app

    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - the container's public port
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
