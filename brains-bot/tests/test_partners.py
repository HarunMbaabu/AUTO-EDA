import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from brains_bot.bot import partner_handlers
from brains_bot.database.models import Contact, PartnerRate
from brains_bot.database.session import session_scope
from brains_bot.services import partners
from brains_bot.services.categories import active_categories, grouped, sync_categories
from brains_bot.services.chat import ProjectTools
from brains_bot.services.outreach import Drafter
from tests.conftest import ALLOWED_ID, make_update, reply_of
from tests.test_chat import FakeTransport, text
from tests.test_sites import _update

NOTE = """12th Oct 2026 partners we can give jobs to
1. Jane Decor Interiors 0790001776, interior design and curtains in Kiambu, about 1,500 per sqm
2. Otieno painter 0722111333, painting labour 250-300 per sqm"""
ANSWER = {"partners": [
    {"name": "Jane", "company": "Jane Decor Interiors", "area": "Kiambu",
     "trade": "interior_designer",
     "categories": ["interior_design", "curtains"],
     "rates": [{"category": "interior_design", "job": "interior design", "amount": "1,500",
                "amount_max": None, "unit": "per sqm"}]},
    {"name": "Otieno", "company": None, "area": None, "trade": "painter",
     "categories": ["painting", "made_up"],
     "rates": [{"category": "painting", "job": "painting labour", "amount": "250",
                "amount_max": "300", "unit": "per sqm"}]},
]}  # fmt: skip


def test_categories_come_in_groups(factory):
    with session_scope(factory) as s:
        sync_categories(s)
        groups = dict(grouped(active_categories(s)))
        assert list(groups) == ["Tech & Entertainment", "Finishes & Interiors",
                                "Exterior & Landscaping", "Utilities"]  # fmt: skip
        assert {c.key for c in groups["Utilities"]} == {"water", "ac", "solar"}
        assert "solar" in {c.key for c in groups["Tech & Entertainment"]}
        finishes = partners.category_keys(s, "finishes")
        assert set(finishes) == {"painting", "flooring", "joinery", "interior_design",
                                 "lighting", "curtains", "waterproofing"}  # fmt: skip
        assert partners.category_keys(s, "interior design") == ["interior_design"]
        assert partners.category_keys(s, "Karen") is None


def test_extract_reads_work_and_rates_without_sending_numbers(factory):
    with session_scope(factory) as s:
        sync_categories(s)
        names = partners.category_names(s)
    transport = FakeTransport(text(json.dumps(ANSWER)))
    pesh, otieno = partners.extract(transport, NOTE, names, date(2026, 10, 20))
    system, user = (m["content"] for m in transport.requests[0]["messages"])
    assert "0790001776" not in user and "[phone]" in user
    assert '"interior_design" (Interior Design)' in system
    assert pesh.services == ["interior_design", "curtains"] and pesh.phone.e164 == "+254790001776"
    assert pesh.rates[0].amount == Decimal(1500) and pesh.noted_on == date(2026, 10, 12)
    assert otieno.services == ["painting"]  # an unknown category is dropped
    assert partners.format_rate(otieno.rates[0]) == "painting labour: KES 250–300 per sqm"


async def test_partner_command_saves_and_lists(factory, context, client_factory=None):
    with session_scope(factory) as s:
        sync_categories(s)
    context.bot_data["db"] = factory
    context.user_data = {}
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(ANSWER))))
    update, status = _update(ALLOWED_ID, "/partner " + NOTE)
    await partner_handlers.partner_command(update, context)
    preview = status.edit_text.await_args.args[0]
    assert "1. PARTNER: Jane · Jane Decor Interiors · Kiambu" in preview
    assert "Work: Interior Design, Curtains & Blinds" in preview
    assert "Rate: interior design: KES 1,500 per sqm" in preview
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    query = SimpleNamespace(
        data=save.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await partner_handlers.partner_callback(tap, context)
    assert query.edit_message_text.await_args.args[0].startswith(
        "Saved: 2 new partner(s), 0 updated, 2 rate(s)."
    )
    with session_scope(factory) as s:
        pesh = s.scalar(select(Contact).where(Contact.name == "Jane"))
        assert pesh.partner and pesh.trade == "interior_designer"
        assert len(s.scalars(select(PartnerRate)).all()) == 2

    update = make_update(ALLOWED_ID)
    context.args = ["finishes"]
    await partner_handlers.partners_command(update, context)
    listing = reply_of(update)
    assert "Partners (2)" in listing and "+254 790 001 776" in listing
    assert "interior design: KES 1,500 per sqm (Oct 2026)" in listing

    update = make_update(ALLOWED_ID)
    context.args = ["interior", "design", "Kiambu"]
    await partner_handlers.partners_command(update, context)
    assert "Partners (1)" in reply_of(update)

    with session_scope(factory) as s:
        found = ProjectTools(s, ALLOWED_ID, date(2026, 10, 20)).run(
            "find_partners", {"category": "painting"}
        )
        assert found["total"] == 1 and found["partners"][0]["rates"][0]["approx_max_kes"] == 300
        assert "0722" not in json.dumps(found)


def test_rate_wording():
    rate = partners.RateDraft("interior_design", "design per sqm", Decimal(2500), None, "per sqm")
    assert partners.format_rate(rate) == "design: KES 2,500 per sqm"


def test_links_are_read_by_the_bot_and_kept_from_the_ai(factory):
    found = partners.find_links(
        "Catalogue https://wa.me/c/254790001776, site www.janedecor.co.ke. "
        "Chat wa.me/254790001776 map https://maps.app.goo.gl/abc WWW.JANEDECOR.CO.KE"
    )
    assert found == [
        {"kind": "whatsapp_catalog", "url": "https://wa.me/c/254790001776"},
        {"kind": "website", "url": "https://www.janedecor.co.ke"},
        {"kind": "whatsapp", "url": "https://wa.me/254790001776"},
    ]
    with session_scope(factory) as s:
        sync_categories(s)
        names = partners.category_names(s)
    note = "Jane Decor Interiors 0790001776, interior design, https://wa.me/c/254790001776"
    answer = {"partners": [{"name": "Jane", "company": "Jane Decor Interiors",
                            "categories": ["interior_design"], "rates": []}]}  # fmt: skip
    transport = FakeTransport(text(json.dumps(answer)))
    (draft,) = partners.extract(transport, note, names, date(2026, 10, 20))
    user = transport.requests[0]["messages"][1]["content"]
    assert "wa.me" not in user and "254790001776" not in user and "[link]" in user
    assert draft.links == [{"kind": "whatsapp_catalog", "url": "https://wa.me/c/254790001776"}]
    assert "   WhatsApp catalogue: https://wa.me/c/254790001776" in partner_handlers.preview(
        [draft], names
    )

    with session_scope(factory) as s:
        contact, _ = partners.save(s, draft, date(2026, 10, 20))
        draft.links = [*draft.links, {"kind": "website", "url": "https://janedecor.co.ke"}]
        partners.save(s, draft, date(2026, 10, 21))  # same number: links added once
        assert [x["kind"] for x in contact.links] == ["whatsapp_catalog", "website"]
        (p,), _ = partners.directory(s)
        lines = partner_handlers.partner_lines(p, names)
        assert "   Website: https://janedecor.co.ke" in lines
        found = ProjectTools(s, ALLOWED_ID, date(2026, 10, 20)).run("find_partners", {})
        assert found["partners"][0]["links"] == ["WhatsApp catalogue", "Website"]
        assert "wa.me" not in json.dumps(found)
