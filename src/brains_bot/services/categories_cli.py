"""Command line: uv run brains-bot-categories  (sync data/categories.json to the database)."""

from __future__ import annotations

import os
import sys

from brains_bot.config import load_env_file
from brains_bot.database.session import make_engine, make_session_factory, session_scope
from brains_bot.services.categories import active_categories, sync_categories


def main() -> int:
    load_env_file()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is not set. Add it to .env.")
        return 2
    with session_scope(make_session_factory(make_engine(database_url))) as session:
        sync_categories(session)
        for c in active_categories(session):
            print(f"  {c.key}: {c.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
