"""Alembic environment. The database URL comes from DATABASE_URL (environment or .env)."""

import os
from logging.config import fileConfig

from alembic import context

from brains_bot.config import load_env_file
from brains_bot.database.models import Base
from brains_bot.database.session import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    load_env_file()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Add it to .env.")
    return url


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection") or make_engine(database_url())
    if hasattr(connectable, "connect"):
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
