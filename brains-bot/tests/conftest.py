from __future__ import annotations

import os
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


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def engine():
    """A wiped PostgreSQL test database. Tests using it are skipped without TEST_DATABASE_URL."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from sqlalchemy import text

    from brains_bot.database.models import Base
    from brains_bot.database.session import make_engine

    engine = make_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    from brains_bot.database.session import make_session_factory

    return make_session_factory(engine)
