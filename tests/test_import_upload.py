import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from brains_bot.bot import handlers, import_handlers
from brains_bot.database.models import AuditEvent, Project
from brains_bot.database.session import session_scope
from tests.conftest import ALLOWED_ID, STRANGER_ID, make_update, reply_of
from tests.workbook_factory import build_slim_workbook


def _upload(user_id, name="projects.xlsx", size=1000, caption=None, source=None):
    update = make_update(user_id)
    message = update.effective_message
    message.caption = caption

    async def download_to_drive(path):
        shutil.copy(source, path)

    message.document = SimpleNamespace(
        file_name=name,
        file_size=size,
        get_file=AsyncMock(return_value=SimpleNamespace(download_to_drive=download_to_drive)),
    )
    status = SimpleNamespace(texts=[])

    async def edit_text(text):
        status.texts.append(text)

    message.reply_text.return_value = SimpleNamespace(edit_text=edit_text)
    return update, status


async def test_upload_denied_for_strangers(context):
    context.bot_data["db"] = object()
    update, _ = _upload(STRANGER_ID)
    await import_handlers.workbook_upload(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_upload_rejects_other_file_types(context):
    context.bot_data["db"] = object()
    update, _ = _upload(ALLOWED_ID, name="notes.pdf")
    await import_handlers.workbook_upload(update, context)
    assert ".xlsx" in reply_of(update)


async def test_upload_rejects_files_over_telegram_limit(context):
    context.bot_data["db"] = object()
    update, _ = _upload(ALLOWED_ID, size=import_handlers.MAX_BYTES + 1)
    await import_handlers.workbook_upload(update, context)
    assert "20 MB" in reply_of(update)


async def test_upload_without_database(context):
    context.bot_data["db"] = None
    update, _ = _upload(ALLOWED_ID)
    await import_handlers.workbook_upload(update, context)
    assert "not configured" in reply_of(update)


async def test_import_help(context):
    update = make_update(ALLOWED_ID)
    await import_handlers.import_help(update, context)
    assert "send the workbook" in reply_of(update)


async def test_upload_imports_then_skips_then_forces(factory, tmp_path, context):
    source = build_slim_workbook(tmp_path / "slim.xlsx")
    context.bot_data["db"] = factory

    update, status = _upload(ALLOWED_ID, source=source)
    await import_handlers.workbook_upload(update, context)
    assert "Import batch 1 completed." in status.texts[-1]
    assert "Unique projects:" in status.texts[-1]
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Project)) > 0

    update, status = _upload(ALLOWED_ID, source=source)
    await import_handlers.workbook_upload(update, context)
    assert "Nothing to do" in status.texts[-1] and "force" in status.texts[-1]

    update, status = _upload(ALLOWED_ID, source=source, caption="force")
    await import_handlers.workbook_upload(update, context)
    assert "Import batch 2 completed." in status.texts[-1]

    with session_scope(factory) as s:
        actions = list(s.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))
        assert actions == ["workbook_import", "workbook_import"]


async def test_upload_reports_unreadable_workbook(factory, tmp_path, context):
    bad = tmp_path / "broken.xlsx"
    bad.write_bytes(b"not a real workbook")
    context.bot_data["db"] = factory
    update, status = _upload(ALLOWED_ID, source=bad)
    await import_handlers.workbook_upload(update, context)
    assert status.texts[-1].startswith("I couldn't import that workbook")
