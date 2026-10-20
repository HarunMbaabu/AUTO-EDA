import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from brains_bot.bot import contact_handlers, site_handlers
from brains_bot.database.models import Contact, ContactNote, Project
from brains_bot.database.session import session_scope
from brains_bot.services import contacts, sites
from brains_bot.services.chat import ProjectTools
from brains_bot.services.outreach import Drafter
from tests.conftest import ALLOWED_ID, make_update, reply_of
from tests.test_chat import FakeTransport, text
from tests.test_sites import _update

MIKE = (
    "Feb 15th 2026\n"
    "Met below contractor at Migaa\n"
    "Musa Example Construction Migaa\n"
    "0790 004 545\n"
    "He is a good contact for future projects"
)
MIKE_ANSWER = {"kind": "contact", "site_name": None, "area": "Migaa", "city": None,
               "contact_name": "Musa", "contact_role": "contractor", "trade": "contractor",
               "company": "Example Construction"}  # fmt: skip


def _mike():
    transport = FakeTransport(text(json.dumps(MIKE_ANSWER)))
    (draft,) = sites.extract(transport, MIKE, date(2026, 10, 20))
    assert "0701" not in transport.requests[0]["messages"][1]["content"]
    return draft


def test_a_contact_without_a_site_goes_to_the_directory(factory):
    draft = _mike()
    assert (draft.kind, draft.trade, draft.company, draft.area) == (
        "contact", "contractor", "Example Construction", "Migaa")  # fmt: skip
    assert draft.phone.e164 == "+254790004545" and draft.visit_date == date(2026, 2, 15)
    with session_scope(factory) as s:
        sites.match_existing(s, [draft])
        assert draft.existing_contact_id is None
        saved = sites.save(s, draft, date(2026, 10, 20))
        assert saved.kind == "new_contact" and saved.name == "Musa"
        assert s.scalars(select(Project)).all() == []  # no site was made up
        mike = s.get(Contact, saved.id)
        assert (mike.trade, mike.company, mike.area) == (
            "contractor", "Example Construction", "Migaa")  # fmt: skip
        note = s.scalar(select(ContactNote))
        assert note.observed_date == date(2026, 2, 15) and "future projects" in note.text

    # Meeting him again (same number) adds a note to the same entry.
    again = _mike()
    with session_scope(factory) as s:
        sites.match_existing(s, [again])
        assert again.existing_contact_name == "Musa"
        assert sites.save(s, again, date(2026, 10, 20)).kind == "contact_note"
        assert len(s.scalars(select(Contact)).all()) == 1
        assert len(s.scalars(select(ContactNote)).all()) == 2

        entries, total = contacts.directory(s, "contractor", "migaa")
        assert total == 1 and entries[0].contact.name == "Musa"
        assert contacts.directory(s, "interior_designer")[1] == 0
        assert contacts.directory(s, None, "Karen")[1] == 0
        assert contacts.trade_counts(s) == [("contractor", 1)]

        found = ProjectTools(s, ALLOWED_ID, date(2026, 10, 20)).run(
            "find_contacts", {"trade": "contractor", "text": "Migaa"}
        )
        assert found["total"] == 1 and found["contacts"][0]["company"] == "Example Construction"
        assert "0701" not in json.dumps(found) and "+254" not in json.dumps(found)


def test_site_contacts_get_a_trade(factory):
    from tests.test_sites import ANSWER, REPORT

    (draft,) = sites.extract(FakeTransport(text(json.dumps(ANSWER))), REPORT)
    assert draft.kind == "site" and draft.trade == "contractor"
    with session_scope(factory) as s:
        sites.save(s, draft, date(2026, 10, 20))
        (entry,), _ = contacts.directory(s, "contractor", "Runda")
        assert entry.contact.name == "Mutua" and entry.projects


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], (None, "")),
        (["contractor"], ("contractor", "")),
        (["contractors", "Migaa"], ("contractor", "Migaa")),
        (["interior", "designer", "Karen"], ("interior_designer", "Karen")),
        (["site", "manager"], ("site_manager", "")),
        (["Migaa"], (None, "Migaa")),
    ],
)
def test_contacts_arguments(args, expected):
    assert contact_handlers.parse_args(args) == expected


async def test_newsite_contact_then_contacts_command(factory, context):
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(MIKE_ANSWER))))
    context.user_data = {}
    update, status = _update(ALLOWED_ID, "/newsite " + MIKE)
    await site_handlers.newsite(update, context)
    preview = status.edit_text.await_args.args[0]
    assert preview.startswith("New contact: please check")
    assert "CONTACT: Musa · Contractor · Example Construction · Migaa" in preview
    assert "Date: 15 Feb 2026 (from the message)" in preview
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    query = SimpleNamespace(
        data=save.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await site_handlers.site_callback(tap, context)
    saved = query.edit_message_text.await_args.args[0]
    assert saved.startswith("Saved: 1 new contact(s).") and "/contacts" in saved

    update = make_update(ALLOWED_ID)
    context.args = ["contractor", "Migaa"]
    await contact_handlers.contacts_command(update, context)
    reply = reply_of(update)
    assert "Contractors matching 'Migaa' (1)" in reply
    assert "Musa · Contractor · Example Construction · Migaa" in reply
    assert "+254 7XX XXX 545" in reply and "Met on 15 Feb 2026: " in reply

    update = make_update(ALLOWED_ID)
    context.args = []
    await contact_handlers.contacts_command(update, context)
    assert "Known so far: Contractor 1." in reply_of(update)


def test_workbook_people_get_a_trade_from_their_column(factory, tmp_path):
    from tests.test_import_db import _import
    from tests.workbook_factory import build_slim_workbook

    _import(factory, build_slim_workbook(tmp_path / "w.xlsx"))
    with session_scope(factory) as s:
        counts = dict(contacts.trade_counts(s))
        assert counts and set(counts) <= set(contacts.ROLE_TRADES.values())
        entries, total = contacts.directory(s)
        assert total == len(s.scalars(select(Contact)).all())  # everyone is listed
        # A later note naming the trade replaces the placeholder, never a specific one.
        site_contact = s.scalar(select(Contact).where(Contact.trade == "site_contact"))
        contacts.fill(site_contact, "interior_designer", None, None)
        assert site_contact.trade == "interior_designer"
        contacts.fill(site_contact, "contractor", None, None)
        assert site_contact.trade == "interior_designer"


def test_notes_shown_with_numbers_masked():
    shown = contacts.note_for_display("Call Musa 0790 004 545 tomorrow")
    assert shown == "Call Musa +254 7XX XXX 545 tomorrow"


@pytest.mark.parametrize(
    ("note", "how"),
    [
        ("Met below contractor at Migaa", "Met"),
        ("Called below contacts for projects on 4th feb 2026", "Called"),
        ("Sent him a WhatsApp with the catalogue", "Messaged"),
        ("Good contact for future projects", "Noted"),
    ],
)
def test_how_contacted(note, how):
    assert contacts.how_contacted(note) == how


def test_workbook_people_show_their_latest_site_visit(factory, tmp_path):
    from tests.test_import_db import _import
    from tests.workbook_factory import build_slim_workbook

    _import(factory, build_slim_workbook(tmp_path / "w.xlsx"))
    with session_scope(factory) as s:
        entries, _ = contacts.directory(s)
        noted = [e for e in entries if e.last_note]
        assert noted and all(e.last_how == "Site visit" for e in noted)
        assert all(e.last_contact.startswith("Site visit on ") for e in noted if e.last_note_on)
