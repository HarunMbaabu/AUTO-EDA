"""Database-backed Phase 3 tests (skipped without TEST_DATABASE_URL)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from sqlalchemy import func, select

from brains_bot.bot import analysis_handlers
from brains_bot.database.models import AuditEvent, Category, OpportunityAssessment
from brains_bot.database.session import session_scope
from brains_bot.services import analysis as an
from brains_bot.services.categories import active_categories, resolve_category, sync_categories
from tests.conftest import ALLOWED_ID, make_update
from tests.test_analysis import VALID, fake_client, response
from tests.test_import_db import _import, _project, pytestmark  # noqa: F401
from tests.workbook_factory import build_slim_workbook


def test_category_sync_is_idempotent_and_deactivates_removed(factory):
    with session_scope(factory) as s:
        assert sync_categories(s) == 20
        sync_categories(s)
        assert s.scalar(select(func.count()).select_from(Category)) == 20
        keys = [c.key for c in active_categories(s)]
        assert keys[0] == "audio" and "interior_design" in keys
        assert not any(c.has_verified_catalog for c in active_categories(s))

        sync_categories(s, [d for d in an_definitions() if d["key"] != "flooring"])
        assert "flooring" not in [c.key for c in active_categories(s)]
        assert s.scalar(select(Category).where(Category.key == "flooring")).active is False


def an_definitions():
    from brains_bot.services.categories import load_definitions

    return load_definitions()


def test_resolve_category(factory):
    with session_scope(factory) as s:
        sync_categories(s)
        assert resolve_category(s, "smart_home").key == "smart_home"
        assert resolve_category(s, "Interior").key == "interior_design"
        assert resolve_category(s, "home cinema").key == "cinema_addons"
        assert resolve_category(s, "home") is None  # ambiguous (Home Audio, Home Cinema)
        assert resolve_category(s, "boats") is None


def test_context_sent_to_claude_has_no_names_or_phones(factory, tmp_path):
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        project = _project(s, "db:1")
        ctx = an.build_context(s, project, resolve_category(s, "audio"), date(2025, 10, 1))
        sent = json.dumps(ctx.payload) + an._user_message(ctx)
    for private in ("Kamau", "Mr. Contact", "712345678", "Kamau proposed villa"):
        assert private not in sent
    assert ctx.payload["project"]["reference"] == f"Project #{project.id}"
    assert ctx.payload["construction_stage"]["last_recorded_stage"] == "skimming / finishing prep"
    assert ctx.timing.status == "retrofit_only"
    assert "contact_person" in ctx.payload["project"]["parties_on_record"]


async def test_analyze_command_end_to_end(factory, tmp_path, context):
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        project_id = _project(s, "db:1").id
    client, messages = fake_client(response(), response())
    context.bot_data["db"] = factory
    context.bot_data["analyzer"] = an.ClaudeAnalyzer(client, "claude-opus-5-5")

    status = SimpleNamespace(texts=[])

    async def edit_text(text):
        status.texts.append(text)

    update = make_update(ALLOWED_ID)
    update.effective_message.reply_text.return_value = SimpleNamespace(edit_text=edit_text)
    context.args = [str(project_id), "audio"]
    await analysis_handlers.analyze(update, context)
    assert "Analysing project" in update.effective_message.reply_text.await_args.args[0]
    text = status.texts[-1]
    assert "Fit: Possible" in text and "Late: retrofit only" in text
    assert "Possible needs (inferred, not confirmed)" in text
    assert "No products or prices are suggested" in text
    assert len(messages.calls) == 1

    # Second request re-uses the saved analysis instead of calling Claude again.
    update2 = make_update(ALLOWED_ID)
    await analysis_handlers.analyze(update2, context)
    assert "Saved analysis from" in update2.effective_message.reply_text.await_args.args[0]
    assert len(messages.calls) == 1

    # 'refresh' forces a new call.
    update3 = make_update(ALLOWED_ID)
    update3.effective_message.reply_text.return_value = SimpleNamespace(edit_text=edit_text)
    context.args = [str(project_id), "audio", "refresh"]
    await analysis_handlers.analyze(update3, context)
    assert len(messages.calls) == 2

    with session_scope(factory) as s:
        saved = s.scalars(select(OpportunityAssessment)).all()
        assert len(saved) == 2 and saved[0].result == VALID and saved[0].input_tokens == 900
        actions = list(s.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))
        assert actions == ["analysis_created", "analysis_reused", "analysis_created"]


async def test_analyze_without_claude_configured(factory, context):
    context.bot_data["db"] = factory
    context.bot_data["analyzer"] = None
    context.args = ["1", "audio"]
    update = make_update(ALLOWED_ID)
    await analysis_handlers.analyze(update, context)
    assert (
        update.effective_message.reply_text.await_args.args[0] == analysis_handlers.NOT_CONFIGURED
    )
