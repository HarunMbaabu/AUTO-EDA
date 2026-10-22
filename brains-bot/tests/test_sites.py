import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
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
    "This a project at Runda Example Court they have finished on the building phase and we "
    "mostly can get in on the house finishing areas the contractor is Mutua 0790003244."
    "https://maps.app.goo.gl/ExampleMapLink01?g_st=ac"
)
ANSWER = {"site_name": "Runda Example Court project", "area": "Runda Example Court",
          "city": "Nairobi", "contact_name": "Mutua", "contact_role": "contractor"}  # fmt: skip


def test_extract_keeps_contact_details_local():
    transport = FakeTransport(text(json.dumps(ANSWER)))
    (draft,) = sites.extract(transport, REPORT)
    sent = transport.requests[0]["messages"][1]["content"]
    assert "0790003244" not in sent and "maps.app" not in sent
    assert "Mutua [phone].[map link]" in sent
    assert transport.requests[0]["response_format"] == {"type": "json_object"}
    assert draft.phone.e164 == "+254790003244"
    assert draft.map_url == "https://maps.app.goo.gl/ExampleMapLink01?g_st=ac"
    assert (draft.site_name, draft.area, draft.city) == (
        "Runda Example Court project", "Runda Example Court", "Nairobi")  # fmt: skip
    assert (draft.contact_name, draft.contact_role) == ("Mutua", "contractor")


def test_extract_falls_back_when_the_ai_says_little():
    transport = FakeTransport(text(json.dumps({"area": "Kitengela", "contact_role": "boss",
                                               "contact_name": "null"})))  # fmt: skip
    (draft,) = sites.extract(transport, "Site in Kitengela, owner 0712 345 678 or 0722111222")
    assert draft.site_name == "Kitengela site" and draft.contact_name is None
    assert draft.contact_role == "contact_person"
    assert draft.phone.numbers == ["+254712345678", "+254722111222"]


def test_save_creates_a_project_the_rest_of_the_bot_can_use(factory, tmp_path):
    (draft,) = sites.extract(FakeTransport(text(json.dumps(ANSWER))), REPORT)
    with session_scope(factory) as s:
        project = s.get(Project, sites.save(s, draft, date(2026, 10, 20)).id)
        assert project.source_key.startswith("tg:") and project.site_visit_date == date(
            2026, 10, 20
        )
        assert project.google_location_url.startswith("https://maps.app.goo.gl/")
        note = s.scalar(select(ProjectNote).where(ProjectNote.project_id == project.id))
        assert note.text.startswith(REPORT) and note.field == "WhatsApp report"
        roles = list(
            s.scalars(select(ProjectContact.role).where(ProjectContact.project_id == project.id))
        )
        assert roles == ["contact_person"]
        contact = pick_contact(s, project.id)
        assert contact.name == "Mutua" and contact.phone_e164 == "+254790003244"
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
    assert "NEW: Runda Example Court project · Runda Example Court, Nairobi" in preview
    assert "Mutua (contractor) · +254 790 003 244 · map" in preview
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]

    query = SimpleNamespace(
        data=save.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await site_handlers.site_callback(tap, context)
    saved = query.edit_message_text.await_args.args[0]
    assert saved.startswith("Saved: 1 new project(s).") and "/draft " in saved
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


MULTI = """Good morning team today i will do my follow-up in Riverview, Golden palm.

1. Halima Njeri 0790005583 - Its a site at Riverview preparing to finish on skimming.Halima is the paint contractor.
2. Daudi project 0790006086- Its a new project at Riverview 79.He is the care taker the house is skimmed in the interior.I will be meeting the Engineer.
3. Kip Villa 0790009362...Its a follow up project at Golden palm. We were to supply on the exterior.
"""  # noqa: E501
MULTI_ANSWER = {"sites": [
    {"site_name": "Halima Njeri project", "area": "Riverview", "contact_name": "Halima Njeri",
     "contact_role": "contractor", "role_description": "paint contractor"},
    {"site_name": "Daudi project", "area": "Riverview 79", "contact_name": "Daudi",
     "contact_role": "contact_person", "role_description": "caretaker"},
    {"site_name": "Kip Villa", "area": "Golden palm", "contact_role": "owner"},
]}  # fmt: skip


def test_split_report():
    intro, items = sites.split_report(MULTI)
    assert intro.startswith("Good morning team") and len(items) == 3
    assert items[0].startswith("Halima Njeri 0790005583") and items[2].startswith("Kip Villa")
    assert sites.split_report("One site only, no list") == ("", ["One site only, no list"])


def test_a_numbered_list_becomes_several_sites_and_follow_ups_match_by_phone(factory):
    transport = FakeTransport(text(json.dumps(MULTI_ANSWER)))
    drafts = sites.extract(transport, MULTI)
    payload = json.loads(transport.requests[0]["messages"][1]["content"])
    assert len(payload["items"]) == 3 and "Riverview" in payload["context"]
    assert not any(ch.isdigit() for item in payload["items"] for ch in item.replace("79", ""))
    assert [d.phone.e164 for d in drafts] == ["+254790005583", "+254790006086", "+254790009362"]
    assert drafts[1].role_description == "caretaker"
    assert drafts[0].message.endswith("paint contractor.")  # each site keeps only its own text

    with session_scope(factory) as s:
        sites.match_existing(s, drafts)
        assert all(d.existing_project_id is None for d in drafts)
        ken = sites.save(s, drafts[2], date(2026, 10, 1))
        assert ken.kind == "new_site"  # Kip Villa saved earlier

    later = sites.extract(FakeTransport(text(json.dumps(MULTI_ANSWER))), MULTI)
    with session_scope(factory) as s:
        sites.match_existing(s, later)
        assert later[2].existing_project_id == ken.id and later[0].existing_project_id is None
        for draft in later:
            sites.save(s, draft, date(2026, 10, 20))
        assert len(s.scalars(select(Project)).all()) == 3  # no duplicate Kip Villa
        notes = s.scalars(select(ProjectNote).where(ProjectNote.project_id == ken.id)).all()
        assert len(notes) == 2 and notes[-1].observed_date == date(2026, 10, 20)
        dennis = s.scalar(select(Project).where(Project.name == "Daudi project"))
        note = s.scalar(select(ProjectNote.text).where(ProjectNote.project_id == dennis.id))
        assert note.endswith("(Contact: Daudi, caretaker)")


async def test_multi_site_preview_and_save_all(factory, context):
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(MULTI_ANSWER))))
    context.user_data = {}
    update, status = _update(ALLOWED_ID, "/newsite " + MULTI)
    await site_handlers.newsite(update, context)
    preview = status.edit_text.await_args.args[0]
    assert preview.startswith("3 sites: please check")
    assert "2. NEW: Daudi project · Riverview 79" in preview and "Daudi (caretaker)" in preview
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert save.text == "✅ Save all 3"
    query = SimpleNamespace(
        data=save.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await site_handlers.site_callback(tap, context)
    assert query.edit_message_text.await_args.args[0].startswith("Saved: 3 new project(s)")


def test_list_split_survives_lost_line_breaks_and_ignores_other_numbers():
    joined = MULTI.replace("\n", " ")
    intro, items = sites.split_report(joined)
    assert len(items) == 3 and items[1].startswith("Daudi project 0790006086")
    assert "Riverview 79" in items[1]  # "79." is not a list number
    one = "Site at Riverview 79. 3 floors done, 2 more to go."
    assert sites.split_report(one) == ("", [one])  # numbers out of sequence: not a list


def test_visit_date_comes_from_the_message(factory):
    today = date(2026, 10, 20)
    answer = {**MULTI_ANSWER, "visit_date": "2026-10-17"}
    answer["sites"] = [dict(x) for x in MULTI_ANSWER["sites"]]
    answer["sites"][2]["visit_date"] = "2026-10-15"  # this site states its own date
    transport = FakeTransport(text(json.dumps(answer)))
    # No date the bot can read itself in this intro, so the AI's reading is used.
    report = MULTI.replace("today i will do my follow-up", "my follow-up")
    drafts = sites.extract(transport, report, today)
    assert "Today is 2026-10-20" in transport.requests[0]["messages"][0]["content"]
    assert [d.visit_date for d in drafts] == [
        date(2026, 10, 17),
        date(2026, 10, 17),
        date(2026, 10, 15),
    ]
    with session_scope(factory) as s:
        project = s.get(Project, sites.save(s, drafts[0], today).id)
        assert project.site_visit_date == date(2026, 10, 17)
        note = s.scalar(select(ProjectNote).where(ProjectNote.project_id == project.id))
        assert note.observed_date == date(2026, 10, 17)


def test_impossible_or_missing_dates_fall_back_to_today():
    today = date(2026, 10, 20)
    for value in ("2026-12-01", "2020-01-01", "17th Oct", None):
        answer = {**ANSWER, "visit_date": value}
        (draft,) = sites.extract(FakeTransport(text(json.dumps(answer))), REPORT, today)
        assert draft.visit_date is None, value


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Called below contacts for projects on 4th feb 2026", date(2026, 2, 4)),
     ("on 4 February 2026", date(2026, 2, 4)), ("Feb 4, 2026", date(2026, 2, 4)),
     ("04/02/2026", date(2026, 2, 4)), ("4.2.26", date(2026, 2, 4)),
     ("2026-02-04", date(2026, 2, 4)), ("visited on 13th Oct", date(2026, 10, 13)),
     ("visited 25 dec", date(2025, 12, 25)), ("Yesterday I visited", date(2026, 10, 19)),
     ("On Monday i visited", date(2026, 10, 19)), ("call 0790001776 now", None),
     ("Riverview 79. 3 floors", None), ("mid this month", None), ("4th feb 2027", None)],
)  # fmt: skip
def test_date_in_text(text, expected):
    assert sites.date_in_text(text, date(2026, 10, 20), relative=True) == expected


def test_report_date_is_read_by_the_bot_even_when_the_ai_misses_it():
    report = (
        "Called below contacts for projects on 4th feb 2026\n\n"
        "1.Jane Decor Interiors 0790001776...... She is the interior designer at Golden palm.\n"
        "2.Bakari project 0790012549......After the call today he confirmed they will call."
    )
    answer = {"visit_date": None, "sites": [{"site_name": "Jane Decor Interiors"},
                                            {"site_name": "Bakari project"}]}  # fmt: skip
    drafts = sites.extract(FakeTransport(text(json.dumps(answer))), report, date(2026, 10, 20))
    # "today" inside site 2's own text does not override the report's date.
    assert [d.visit_date for d in drafts] == [date(2026, 2, 4), date(2026, 2, 4)]
