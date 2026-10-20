import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from brains_bot.bot import chat_handlers, handlers, site_handlers
from brains_bot.database.models import AuditEvent, Contact, Project, ProjectContact, ProjectNote
from brains_bot.database.session import session_scope
from brains_bot.importers.loader import STALE_REASON
from brains_bot.services import sites
from brains_bot.services.outreach import Drafter, pick_contact
from tests.conftest import ALLOWED_ID, STRANGER_ID, make_update, reply_of
from tests.test_chat import FakeTransport, text
from tests.test_import_db import _import
from tests.workbook_factory import build_slim_workbook

REPORT = (
    "This a project at Runda mumwe Riaka they have finished on the building phase and we "
    "mostly can get in on the house finishing areas the contractor is Maina 0721462244."
    "https://maps.app.goo.gl/TkJW4TT1D9PAmb856?g_st=ac"
)
ANSWER = {"site_name": "Runda Mumwe Riaka project", "area": "Runda Mumwe Riaka",
          "city": "Nairobi", "contact_name": "Maina", "contact_role": "contractor"}  # fmt: skip


def test_extract_keeps_contact_details_local():
    transport = FakeTransport(text(json.dumps(ANSWER)))
    draft = sites.extract(transport, REPORT)
    sent = transport.requests[0]["messages"][1]["content"]
    assert "0721462244" not in sent and "maps.app" not in sent
    assert "Maina [phone].[map link]" in sent
    assert transport.requests[0]["response_format"] == {"type": "json_object"}
    assert draft.phone.e164 == "+254721462244"
    assert draft.map_url == "https://maps.app.goo.gl/TkJW4TT1D9PAmb856?g_st=ac"
    assert (draft.site_name, draft.area, draft.city) == (
        "Runda Mumwe Riaka project", "Runda Mumwe Riaka", "Nairobi")  # fmt: skip
    assert (draft.contact_name, draft.contact_role) == ("Maina", "contractor")


def test_extract_falls_back_when_the_ai_says_little():
    transport = FakeTransport(text(json.dumps({"area": "Kitengela", "contact_role": "boss",
                                               "contact_name": "null"})))  # fmt: skip
    draft = sites.extract(transport, "Site in Kitengela, owner 0712 345 678 or 0722111222")
    assert draft.site_name == "Kitengela site" and draft.contact_name is None
    assert draft.contact_role == "contact_person"
    assert draft.phone.numbers == ["+254712345678", "+254722111222"]


def test_save_creates_a_project_the_rest_of_the_bot_can_use(factory, tmp_path):
    draft = sites.extract(FakeTransport(text(json.dumps(ANSWER))), REPORT)
    with session_scope(factory) as s:
        project = sites.save(s, draft, date(2026, 10, 20))
        assert project.source_key.startswith("tg:") and project.site_visit_date == date(
            2026, 10, 20
        )
        assert project.google_location_url.startswith("https://maps.app.goo.gl/")
        note = s.scalar(select(ProjectNote).where(ProjectNote.project_id == project.id))
        assert note.text == REPORT and note.field == "WhatsApp report"
        roles = list(
            s.scalars(select(ProjectContact.role).where(ProjectContact.project_id == project.id))
        )
        assert roles == ["contact_person"]
        contact = pick_contact(s, project.id)
        assert contact.name == "Maina" and contact.phone_e164 == "+254721462244"
        project_id = project.id

    # A later workbook upload does not flag it as "missing from the latest import".
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        assert STALE_REASON not in (s.get(Project, project_id).review_reasons or [])


def _update(user_id, message):
    update = make_update(user_id)
    update.effective_message.text = message
    update.effective_chat = SimpleNamespace(send_action=AsyncMock())
    status = SimpleNamespace(edit_text=AsyncMock())
    update.effective_message.reply_text.return_value = status
    return update, status


async def test_newsite_preview_then_save(factory, context):
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(ANSWER))))
    context.user_data = {}
    update, status = _update(ALLOWED_ID, "/newsite " + REPORT)
    await site_handlers.newsite(update, context)
    preview = status.edit_text.await_args.args[0]
    assert "Site: Runda Mumwe Riaka project" in preview and "+254 7XX XXX 244" in preview
    assert "Contact: Maina (contractor)" in preview and "0721462244" not in preview
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]

    query = SimpleNamespace(
        data=save.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await site_handlers.site_callback(tap, context)
    saved = query.edit_message_text.await_args.args[0]
    assert saved.startswith("Saved as project #") and "/draft " in saved
    await site_handlers.site_callback(tap, context)  # a second tap does not save twice
    with session_scope(factory) as s:
        assert len(s.scalars(select(Project)).all()) == 1
        assert "site_created" in list(s.scalars(select(AuditEvent.action)))


async def test_bare_newsite_takes_the_next_message_and_cancel_saves_nothing(factory, context):
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(ANSWER))))
    context.bot_data["assistant"] = object()  # chat is configured, but must not be used
    context.user_data = {}
    update, _ = _update(ALLOWED_ID, "/newsite")
    await site_handlers.newsite(update, context)
    assert reply_of(update) == site_handlers.ASK

    paste, status = _update(ALLOWED_ID, REPORT)
    await chat_handlers.chat_message(paste, context)
    cancel = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][1]
    query = SimpleNamespace(
        data=cancel.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await site_handlers.site_callback(tap, context)
    assert query.edit_message_text.await_args.args[0].startswith("Cancelled")
    with session_scope(factory) as s:
        assert s.scalars(select(Project)).all() == [] and s.scalars(select(Contact)).all() == []


async def test_newsite_denied_for_strangers(context):
    update, _ = _update(STRANGER_ID, "/newsite " + REPORT)
    await site_handlers.newsite(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED
