from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

from sqlalchemy import select

from brains_bot.bot import handlers, outreach_handlers
from brains_bot.database.models import AuditEvent, OutreachDraft, Project
from brains_bot.database.session import session_scope
from brains_bot.services import catalog, outreach
from brains_bot.services.categories import resolve_category, sync_categories
from tests.conftest import ALLOWED_ID, STRANGER_ID, make_update, reply_of
from tests.test_catalog import build_catalog
from tests.test_chat import FakeTransport, text
from tests.test_import_db import _import
from tests.workbook_factory import build_slim_workbook


def test_first_name():
    assert outreach.first_name("Mr. Richard & Madam Dorcas Anita") == "Mr. Richard"
    assert outreach.first_name("Madam Rehema") == "Madam Rehema"
    assert outreach.first_name("mr.kamau") == "Mr. kamau"
    assert outreach.first_name("Jane Wanjiru") == "Jane"
    assert outreach.first_name("TBC") is None and outreach.first_name(None) is None


def test_whatsapp_url():
    url = outreach.whatsapp_url("+254712345678", "Hello Mr. Kamau & co?")
    assert url.startswith("https://wa.me/254712345678?text=")
    assert unquote(url.split("text=")[1]) == "Hello Mr. Kamau & co?"


def _seed(factory, tmp_path):
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        catalog.load_catalog(s, build_catalog(tmp_path / "c.xlsx"))
        return {p.source_key: p.id for p in s.scalars(select(Project))}


def test_draft_request_has_no_phone_and_records_named_products(factory, tmp_path):
    ids = _seed(factory, tmp_path)
    reply = "Hello Mr. Kamau, the Wall-mounted pair (from KES 300,000) could suit. Call 0712345678?"
    drafter = outreach.Drafter(FakeTransport(text(reply)))
    with session_scope(factory) as s:
        contact = outreach.pick_contact(s, ids["db:1"])
        assert contact.phone_status == "valid"
        target = outreach.DraftTarget(
            s.get(Project, ids["db:1"]), contact, resolve_category(s, "audio")
        )
        request = outreach.draft_request(s, target, date(2025, 10, 1))
        assert request["contact_name"] == "Mr. Contact"
        assert "712345678" not in str(request)
        assert [o["sku"] for o in request["catalog_options"]] == ["A-WALL", "A-LUX"]
        assert request["latest_site_note"]["text"]  # the hook for "now that ..."
        assert request["pitch_angle"].startswith("high-value upgrade")
        assert "Home Cinema" in request["goes_well_with"]
        assert request["previous_message"] is None
        draft = outreach.create_draft(s, drafter, target, ALLOWED_ID, date(2025, 10, 1))
        assert "0712345678" not in draft.text  # scrubbed even if the AI writes a number
        assert draft.product_skus == ["A-WALL"]
        assert outreach.mark_sent(s, draft.id, ALLOWED_ID).sent_at is not None
        assert outreach.mark_replied(s, draft.id).replied_at is not None
    system = drafter.transport.requests[0]["messages"][0]["content"]
    assert "Intelligent Home" in system and "Style examples" in system
    with session_scope(factory) as s:
        target = outreach.DraftTarget(
            s.get(Project, ids["db:1"]), contact, resolve_category(s, "painting")
        )
        later = outreach.draft_request(s, target, date(2025, 10, 9))
        assert later["previous_message"]["about"] == "Home Audio & Multi-room"
        assert later["previous_message"]["they_replied"] is True
        assert later["pitch_angle"].startswith("tie it to the build timeline")


async def test_draft_command_and_buttons(factory, tmp_path, context):
    ids = _seed(factory, tmp_path)
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = outreach.Drafter(FakeTransport(text("Hello Mr. Contact")))
    update = make_update(ALLOWED_ID)
    status = SimpleNamespace(edit_text=AsyncMock())
    update.effective_message.reply_text.return_value = status
    context.args = [str(ids["db:1"]), "audio"]
    await outreach_handlers.draft(update, context)
    shown, kwargs = status.edit_text.await_args.args[0], status.edit_text.await_args.kwargs
    assert "Draft for #" in shown and "Hello Mr. Contact" in shown
    assert "+254 7XX XXX 678" in shown and "consent is unknown" in shown
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "📤 Send via WhatsApp"

    query = SimpleNamespace(data=button.callback_data, answer=AsyncMock(),
                            edit_message_reply_markup=AsyncMock())  # fmt: skip
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await outreach_handlers.draft_callback(tap, context)
    markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    opened, replied = markup.inline_keyboard[0]
    assert opened.url.startswith("https://wa.me/254712345678?text=Hello")
    query.data = replied.callback_data
    await outreach_handlers.draft_callback(tap, context)
    with session_scope(factory) as s:
        saved = s.scalar(select(OutreachDraft))
        assert saved.sent_at and saved.replied_at and saved.sent_by == ALLOWED_ID
        actions = list(s.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))
        assert actions == ["draft_created", "draft_send", "draft_reply"]


async def test_draft_offers_each_of_several_numbers(factory, tmp_path, context):
    ids = _seed(factory, tmp_path)  # db:2's contact has two numbers
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = outreach.Drafter(FakeTransport(text("Hello")))
    update = make_update(ALLOWED_ID)
    status = SimpleNamespace(edit_text=AsyncMock())
    update.effective_message.reply_text.return_value = status
    context.args = [str(ids["db:2"]), "audio"]
    await outreach_handlers.draft(update, context)
    rows = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == [
        "📤 Send via WhatsApp (+254 7XX XXX 222)",
        "📤 Send via WhatsApp (+254 7XX XXX 555)",
    ]


async def test_draft_denied_and_usage(context):
    update = make_update(STRANGER_ID)
    await outreach_handlers.draft(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED
    context.bot_data["db"] = object()
    context.args = ["abc"]
    update = make_update(ALLOWED_ID)
    await outreach_handlers.draft(update, context)
    assert reply_of(update).startswith("Usage: /draft")


def test_draft_checks_and_cleanup():
    from brains_bot.services.outreach import Drafter, draft_problem

    request = {"goes_well_with": ["Smart Home", "Home Cinema"],
               "construction_stage": {"stage_number": 5},
               "latest_site_note": {"text": "Skimming done"}}  # fmt: skip
    assert "your Smart Home" in draft_problem("It keeps your smart home on.", request)
    assert "roof" in draft_problem("Panels go up once the roof is on.", request)
    assert draft_problem("Weatherproof speakers for the patio.", request) is None
    # Still wrong after the one rewrite: fixed in code, and the sign-off gets its own line.
    wrong = "Runs your future home cinema. The Intelligent Home team"
    drafter = Drafter(FakeTransport(text(wrong), text(wrong)))
    out = drafter.write(request)
    assert out == "Runs a home cinema.\n\nThe Intelligent Home team"
    assert "Rewrite the message" in drafter.transport.requests[1]["messages"][-1]["content"]
