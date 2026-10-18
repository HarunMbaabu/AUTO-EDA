from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brains_bot.config import Settings

ALLOWED_ID = 111
STRANGER_ID = 999
FAKE_TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


@pytest.fixture
def settings() -> Settings:
    return Settings(telegram_bot_token=FAKE_TOKEN, allowed_user_ids=frozenset({ALLOWED_ID}))


@pytest.fixture
def context(settings):
    return SimpleNamespace(
        bot_data={"settings": settings, "started_at": time.monotonic() - 65},
        error=None,
    )


def make_update(user_id: int | None):
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=user_id) if user_id is not None else None
    update.effective_message.reply_text = AsyncMock()
    return update


def reply_of(update) -> str:
    update.effective_message.reply_text.assert_awaited_once()
    return update.effective_message.reply_text.await_args.args[0]
