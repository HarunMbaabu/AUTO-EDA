"""/import and workbook uploads: an authorized user sends the .xlsx in a private chat."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from brains_bot.bot.handlers import _user_id, restricted
from brains_bot.bot.project_handlers import _guard_db, _with_session
from brains_bot.config import PROJECT_ROOT
from brains_bot.importers.loader import ImportStats, load_aliases, run_import
from brains_bot.services import catalog
from brains_bot.services import project_service as svc

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024  # the largest file a Telegram bot can download
ALIASES = PROJECT_ROOT / "data" / "name_aliases.json"
UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._()-]")

HOW_TO = (
    "To load or update the project data, send the workbook (.xlsx) to me here as a file.\n"
    "Sending the same file again does nothing; to re-import it anyway, add the caption: force\n\n"
    "To load or update the product catalog, send the catalog file (.xlsx with a 'Catalog' "
    "sheet). It replaces the previous catalog."
)


@restricted("import")
async def import_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HOW_TO)


def _catalog_summary(stats: catalog.CatalogStats) -> str:
    lines = [
        f"Catalog loaded: {stats.products} products from {', '.join(stats.suppliers)}.",
        "Other suppliers' products are unchanged.",
    ]
    lines += [f"- {key}: {count}" for key, count in sorted(stats.by_category.items())]
    if stats.unpriced:
        lines.append(f"- Without a price (shown as 'to be scoped'): {stats.unpriced}")
    if stats.deactivated:
        lines.append(f"- No longer in these suppliers' lists (hidden): {stats.deactivated}")
    lines.append("/analyze and chat now suggest these products where they fit a project's stage.")
    return "\n".join(lines)


def _summary(stats: ImportStats) -> str:
    lines = [f"Import batch {stats.batch_id} completed."]
    lines += [f"- {sheet}: {count} records" for sheet, count in stats.source_rows.items()]
    lines.append(f"- Unique projects: {stats.projects} ({len(stats.flagged)} flagged for review)")
    lines.append(f"- Contacts created: {stats.contacts}")
    if stats.stale:
        lines.append(f"- No longer in the workbook (flagged, not deleted): {len(stats.stale)}")
    if stats.merged:
        lines.append(f"- Skipped, merged earlier as repeats (/merge): {stats.merged}")
    return "\n".join(lines)


@restricted("upload")
async def workbook_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    message = update.effective_message
    document = message.document
    name = document.file_name or "workbook.xlsx"
    if not name.lower().endswith(".xlsx"):
        await message.reply_text("I can only import Excel workbooks (.xlsx).")
        return
    if document.file_size and document.file_size > MAX_BYTES:
        await message.reply_text(
            "That file is larger than 20 MB, which Telegram won't let me download."
        )
        return
    force = (message.caption or "").strip().lower() == "force"

    lock: asyncio.Lock = context.bot_data.setdefault("import_lock", asyncio.Lock())
    if lock.locked():
        await message.reply_text("An import is already running. Try again when it has finished.")
        return

    actor = _user_id(update)
    async with lock:
        status = await message.reply_text(f"Importing {name}. This can take a minute…")
        # The workbook is private: keep it only for the import, in a folder deleted afterwards.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / (UNSAFE_NAME_CHARS.sub("_", Path(name).name) or "workbook.xlsx")
            try:
                telegram_file = await document.get_file()
                await telegram_file.download_to_drive(path)
            except Exception:
                log.exception("workbook download failed", extra={"event": "import_error"})
                await status.edit_text("I couldn't download that file from Telegram. Try again.")
                return

            def work(session: Session) -> str:
                if catalog.is_catalog_file(path):
                    loaded = catalog.load_catalog(session, path)
                    svc.record_audit(
                        session, actor, "catalog_import", products=loaded.products, file=path.name
                    )
                    return _catalog_summary(loaded)
                stats, _, _ = run_import(session, path, force=force, aliases=load_aliases(ALIASES))
                if stats.skipped_reason:
                    return (
                        f"Nothing to do: {stats.skipped_reason}. Send it again with the caption "
                        "'force' to re-import it."
                    )
                svc.record_audit(
                    session, actor, "workbook_import", batch=stats.batch_id, force=force
                )
                log.info(
                    "workbook imported",
                    extra={"event": "workbook_import", "command": str(stats.batch_id)},
                )
                return _summary(stats)

            try:
                reply = await _with_session(context, work)
            except (ValueError, zipfile.BadZipFile) as exc:
                log.warning(
                    "file rejected (%s)", type(exc).__name__, extra={"event": "import_rejected"}
                )
                await status.edit_text(f"I couldn't import that file: {exc}")
                return
            except Exception:
                log.exception("import failed", extra={"event": "import_error"})
                await status.edit_text("The import failed because of an unexpected error.")
                return
    await status.edit_text(reply)
