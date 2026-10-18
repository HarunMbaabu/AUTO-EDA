"""Environment-based settings. Secrets are read from the environment only."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def parse_user_ids(raw: str | None) -> frozenset[int]:
    """Parse a comma-separated list of numeric Telegram user IDs."""
    if not raw or not raw.strip():
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS must contain only numeric Telegram user IDs, "
                "separated by commas"
            )
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    log_level: str = "INFO"

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from the environment (or a supplied mapping, for tests)."""
    env = os.environ if env is None else env

    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set")

    # TELEGRAM_ALLOWED_USER_ID (singular) is accepted for compatibility with the spec.
    raw_ids = env.get("TELEGRAM_ALLOWED_USER_IDS") or env.get("TELEGRAM_ALLOWED_USER_ID")
    allowed = parse_user_ids(raw_ids)

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")

    return Settings(telegram_bot_token=token, allowed_user_ids=allowed, log_level=log_level)
