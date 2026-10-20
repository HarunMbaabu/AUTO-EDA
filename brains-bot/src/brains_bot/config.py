"""Environment-based settings. Secrets are read from the environment only."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

# brains-bot/ (the folder holding pyproject.toml), resolved from src/brains_bot/config.py.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def env_file_candidates() -> list[Path]:
    return list(dict.fromkeys([Path.cwd() / ".env", PROJECT_ROOT / ".env"]))


def load_env_file(
    candidates: list[Path] | None = None, env: MutableMapping[str, str] | None = None
) -> Path | None:
    """Load the first .env found into the environment and return its path.

    A variable that is already set to a non-empty value wins over .env, but an empty one
    (e.g. a stray `export TELEGRAM_BOT_TOKEN=`) is filled from .env.
    """
    env = os.environ if env is None else env
    for path in env_file_candidates() if candidates is None else candidates:
        if path.is_file():
            for key, value in dotenv_values(path).items():
                if value is not None and not env.get(key, "").strip():
                    env[key] = value
            return path
    return None


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
    database_url: str | None = field(default=None, repr=False)
    claude_model: str | None = None
    # Whether an Anthropic key is configured. The key itself stays in the environment, where
    # the SDK reads it; it is never copied into settings.
    anthropic_key_present: bool = False
    # Chat-API provider (mistral or deepseek) chosen from AI_PROVIDER or whichever key is set.
    # Keys stay in the environment; only the choice is recorded here.
    chat_provider: str | None = None
    ai_provider_requested: str | None = None

    @property
    def ai_provider(self) -> str | None:
        if self.chat_provider:
            return self.chat_provider
        if self.ai_provider_requested not in (None, "anthropic"):
            return None  # an explicitly requested provider without a key: stay disabled
        if self.anthropic_key_present and self.claude_model:
            return "anthropic"
        return None

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids


def _chat_provider(env: Mapping[str, str]) -> str | None:
    from brains_bot.services.chat_api import select_provider

    provider = select_provider(env)
    return provider.key if provider else None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from the environment (or a supplied mapping, for tests)."""
    env = os.environ if env is None else env

    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", token):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN looks incomplete: it should be '<bot number>:<secret>', "
            "e.g. 123456789:AAH... (copy the whole line from @BotFather)"
        )

    # TELEGRAM_ALLOWED_USER_ID (singular) is accepted for compatibility with the spec.
    raw_ids = env.get("TELEGRAM_ALLOWED_USER_IDS") or env.get("TELEGRAM_ALLOWED_USER_ID")
    allowed = parse_user_ids(raw_ids)

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")

    database_url = env.get("DATABASE_URL", "").strip() or None
    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=allowed,
        log_level=log_level,
        database_url=database_url,
        claude_model=env.get("CLAUDE_MODEL", "").strip() or None,
        anthropic_key_present=bool(env.get("ANTHROPIC_API_KEY", "").strip()),
        chat_provider=_chat_provider(env),
        ai_provider_requested=env.get("AI_PROVIDER", "").strip().lower() or None,
    )
