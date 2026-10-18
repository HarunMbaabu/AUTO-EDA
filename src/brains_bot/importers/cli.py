"""Command line: uv run brains-bot-import "data/raw/<workbook>.xlsx" [--force]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from brains_bot.config import PROJECT_ROOT, load_env_file
from brains_bot.database.session import make_engine, make_session_factory, session_scope
from brains_bot.importers.loader import AUDIO_STATUS_MODES, load_aliases, run_import
from brains_bot.importers.report import build_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the projects workbook into PostgreSQL.")
    parser.add_argument("workbook", type=Path, help="path to the .xlsx file")
    parser.add_argument("--force", action="store_true", help="re-import an already imported file")
    parser.add_argument(
        "--report-dir", type=Path, default=PROJECT_ROOT / "data" / "processed",
        help="where to write the import report (default: data/processed, ignored by Git)",
    )  # fmt: skip
    parser.add_argument(
        "--aliases", type=Path, default=PROJECT_ROOT / "data" / "name_aliases.json",
        help="private JSON file of salesperson name corrections (default: data/name_aliases.json)",
    )  # fmt: skip
    parser.add_argument(
        "--audio-status", choices=AUDIO_STATUS_MODES, default="notes",
        help="how to read the 'Audio Status' column: 'notes' (default; it currently holds site "
        "notes) or 'status' (it holds real audio information). Add --force to re-import a file.",
    )  # fmt: skip
    args = parser.parse_args(argv)

    load_env_file()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is not set. Add it to .env.")
        return 2
    if not args.workbook.is_file():
        print(f"File not found: {args.workbook}")
        return 2

    factory = make_session_factory(make_engine(database_url))
    with session_scope(factory) as session:
        stats, result, workbook = run_import(
            session,
            args.workbook,
            force=args.force,
            aliases=load_aliases(args.aliases),
            audio_status_mode=args.audio_status,
        )

    if stats.skipped_reason:
        print(f"Nothing to do: {stats.skipped_reason}. Use --force to import it again.")
        return 0

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"import_report_batch_{stats.batch_id}.md"
    report_path.write_text(build_report(stats, result, workbook, args.workbook.name))

    print(f"Import batch {stats.batch_id} completed.")
    for sheet, count in stats.source_rows.items():
        print(f"  {sheet}: {count} records")
    print(f"  Unique projects: {stats.projects} ({len(stats.flagged)} flagged for review)")
    print(f"  Contacts created: {stats.contacts}")
    print(f"  'Audio Status' read as: {stats.audio_status_mode}")
    print(f"  Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
