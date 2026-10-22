import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from brains_bot.bot import repeat_handlers, site_handlers
from brains_bot.database.models import (
    Contact,
    Project,
    ProjectAlias,
    ProjectContact,
    ProjectNote,
)
from brains_bot.database.session import session_scope
from brains_bot.services import repeats, sites
from brains_bot.services.outreach import Drafter
from tests.conftest import ALLOWED_ID, make_update, reply_of
from tests.test_chat import FakeTransport, text
from tests.test_import_db import _import
from tests.test_sites import _update
from tests.workbook_factory import build_slim_workbook

FIRST = "Wanza's house in Migaa, walling stage. Wanza 0790008912"
AGAIN = "27th Feb 2026 follow up: Wanza from Migaa, roofing now. 0790007098"
BETTY = {"site_name": "Wanza project", "area": "Migaa", "city": "Kiambu",
         "contact_name": "Wanza", "contact_role": "owner"}  # fmt: skip


def _drafts(message):
    return sites.extract(FakeTransport(text(json.dumps(BETTY))), message, date(2026, 10, 20))


def _tap(callback_data):
    query = SimpleNamespace(data=callback_data, answer=AsyncMock(), edit_message_text=AsyncMock())
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    return tap, query


def _count(factory, model):
    with session_scope(factory) as s:
        return s.scalar(select(func.count()).select_from(model))


async def _send(context, message, button):
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(BETTY))))
    update, status = _update(ALLOWED_ID, "/newsite " + message)
    await site_handlers.newsite(update, context)
    preview = status.edit_text.await_args.args[0]
    keyboard = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    buttons = {b.callback_data.split(":")[1]: b for row in keyboard for b in row}
    tap, query = _tap(buttons[button].callback_data)
    await site_handlers.site_callback(tap, context)
    return preview, query.edit_message_text.await_args.args[0]


async def test_a_new_number_with_the_same_name_and_area_is_offered_as_a_follow_up(factory, context):
    context.bot_data["db"] = factory
    context.user_data = {}
    with session_scope(factory) as s:
        betty_id = sites.save(s, _drafts(FIRST)[0], date(2026, 1, 10)).id

    preview, saved = await _send(context, AGAIN, "save")
    assert f"POSSIBLE REPEAT → #{betty_id} Wanza project" in preview
    assert "different number · +254 790 007 098" in preview
    assert saved.startswith("Saved: 1 follow-up note(s).")
    assert _count(factory, Project) == 1
    with session_scope(factory) as s:
        notes = s.scalars(select(ProjectNote).where(ProjectNote.project_id == betty_id)).all()
        assert notes[-1].observed_date == date(2026, 2, 27)
        numbers = s.scalars(
            select(Contact.phone_e164)
            .join(ProjectContact, ProjectContact.contact_id == Contact.id)
            .where(ProjectContact.project_id == betty_id)
        ).all()
        assert set(numbers) == {"+254790008912", "+254790007098"}  # both numbers now known

    # Next time either number is recognised straight away.
    (draft,) = _drafts("Wanza again, plastering. 0790007098")
    with session_scope(factory) as s:
        sites.match_existing(s, [draft])
    assert draft.existing_project_id == betty_id and draft.possible_project_id is None


async def test_save_as_new_then_find_and_merge_the_repeat(factory, context):
    context.bot_data["db"] = factory
    context.user_data = {}
    with session_scope(factory) as s:
        betty_id = sites.save(s, _drafts(FIRST)[0], date(2026, 1, 10)).id
    _, saved = await _send(context, AGAIN, "new")
    assert saved.startswith("Saved: 1 new project(s).")
    with session_scope(factory) as s:
        repeat_id = s.scalar(select(func.max(Project.id)))
    assert repeat_id != betty_id

    update = make_update(ALLOWED_ID)
    await repeat_handlers.repeats_command(update, context)
    listing = reply_of(update)
    assert "same name 'Wanza' in Migaa" in listing and f"/merge {repeat_id} {betty_id}" in listing

    update = make_update(ALLOWED_ID)
    context.args = [str(repeat_id), str(betty_id)]
    await repeat_handlers.merge_command(update, context)
    confirm = update.effective_message.reply_text.await_args
    assert "cannot be undone" in confirm.args[0] and _count(factory, Project) == 2
    go = confirm.kwargs["reply_markup"].inline_keyboard[0][0]
    tap, query = _tap(go.callback_data)
    await repeat_handlers.merge_callback(tap, context)
    assert query.edit_message_text.await_args.args[0].startswith(f"Merged #{repeat_id} into")

    assert _count(factory, Project) == 1
    with session_scope(factory) as s:
        notes = s.scalars(select(ProjectNote.field).where(ProjectNote.project_id == betty_id))
        assert sorted(notes) == ["Merged", "WhatsApp report", "WhatsApp report"]
        assert repeats.find_repeats(s) == []
    tap, query = _tap(go.callback_data)  # a second tap
    await repeat_handlers.merge_callback(tap, context)
    query.answer.assert_awaited_with("Already merged or removed.", show_alert=True)


def test_reimport_keeps_telegram_notes_and_skips_merged_workbook_projects(factory, tmp_path):
    path = build_slim_workbook(tmp_path / "slim.xlsx")
    _import(factory, path)
    with session_scope(factory) as s:
        keep, repeat = s.scalars(select(Project).order_by(Project.id).limit(2)).all()
        keep_id, repeat_id = keep.id, repeat.id
        s.add(ProjectNote(project_id=keep_id, field=sites.NOTE_FIELD, text="Called: roofing",
                          observed_date=date(2026, 10, 1)))  # fmt: skip
        repeat_notes = s.scalar(select(func.count()).where(ProjectNote.project_id == repeat_id))
        repeats.merge(s, repeat_id, keep_id, date(2026, 10, 20))
        projects = s.scalar(select(func.count()).select_from(Project))
        assert s.scalar(select(ProjectAlias.project_id)) == keep_id

    stats, _, _ = _import(factory, path, force=True)
    assert stats.merged == 1
    with session_scope(factory) as s:
        assert s.scalar(select(func.count()).select_from(Project)) == projects  # not back
        texts = list(s.scalars(select(ProjectNote.text).where(ProjectNote.project_id == keep_id)))
        assert "Called: roofing" in texts  # the Telegram note survived the re-import
        assert any(t.startswith(f"Merged repeat entry #{repeat_id}") for t in texts)
        moved = s.scalar(
            select(func.count()).where(
                ProjectNote.project_id == keep_id, ProjectNote.source_record_id.is_(None)
            )
        )
        assert moved == repeat_notes + 2  # the repeat's notes, the Telegram note, the merge note


def test_same_name_and_area_needs_both():
    assert repeats.place_words("Migaa estate, Kiambu road") == {"migaa", "kiambu"}
    assert repeats.first_name("Mr. Jo") is None and repeats.first_name("Wanza W") == "wanza"


def test_the_area_is_also_read_from_the_message_when_the_ai_leaves_it_out(factory):
    with session_scope(factory) as s:
        betty_id = sites.save(s, _drafts(FIRST)[0], date(2026, 1, 10)).id
        found = repeats.same_name_and_area(
            s, "Wanza", None, "Wanza project", "Wanza from Migaa was visited on 27th Feb 2026"
        )
        assert found == (betty_id, "Wanza project")
        # The name alone (here also in the site name) is not enough.
        assert repeats.same_name_and_area(s, "Wanza", None, "Wanza project", "in Ruiru") is None


KIORA = """3rd March 2026
Kiora area
I went to Kiora and i made some follow up and also identified two new.
1.Baraka project 0790013076... Its a new project in kiora which is partially skimmed.
2.Kiprono Contractor 0790010478.... Its a follow up project, they purchased skimming products.
3.Kip interior designer 07900022338 project..... He has a stalled project at kiora.
4.Gitau Contractor 0790011948....He is a contractor at Kiora and its a new project."""
KIORA_ANSWER = {"sites": [
    {"site_name": "Baraka project", "area": "Kiora"},
    {"site_name": "Kiprono project", "area": "Kiora", "contact_name": "Kiprono Contractor",
     "contact_role": "contractor"},
    {"site_name": "Kip project", "area": "Kiora", "contact_name": "Kip",
     "contact_role": "contact_person", "role_description": "interior designer"},
    {"site_name": "Gitau Contractor project", "area": "Kiora",
     "contact_name": "Gitau Contractor", "contact_role": "contractor"},
]}  # fmt: skip


async def test_a_mistyped_number_is_flagged_kept_and_corrected_by_a_follow_up(factory, context):
    context.bot_data["db"] = factory
    context.user_data = {}
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(KIORA_ANSWER))))
    update, status = _update(ALLOWED_ID, "/newsite " + KIORA)
    await site_handlers.newsite(update, context)
    preview = status.edit_text.await_args.args[0]
    assert "Kip (interior designer) · ⚠ CHECK NUMBER: 07900022338 has 11 digits" in preview
    assert "not a valid Kenyan mobile number" in preview
    assert "4. NEW: Gitau project" in preview and "Gitau (contractor)" in preview
    assert "Kiprono (contractor)" in preview
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    tap, _ = _tap(save.callback_data)
    await site_handlers.site_callback(tap, context)
    with session_scope(factory) as s:
        ken_id = s.scalar(select(Project.id).where(Project.name == "Kip project"))
        ken = s.scalar(select(Contact).where(Contact.name == "Kip"))
        assert ken.phone_raw == "07900022338" and ken.phone_status == "invalid"

    # The right number, sent later as a follow-up, goes on Kip's entry.
    fix = {"site_name": "Kip project", "area": "Kiora", "contact_name": "Kip"}
    context.bot_data["drafter"] = Drafter(FakeTransport(text(json.dumps(fix))))
    update, status = _update(ALLOWED_ID, "/newsite Kip in Kiora, correct number 0790002233")
    await site_handlers.newsite(update, context)
    assert f"POSSIBLE REPEAT → #{ken_id} Kip project" in status.edit_text.await_args.args[0]
    save = status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    tap, _ = _tap(save.callback_data)
    await site_handlers.site_callback(tap, context)
    with session_scope(factory) as s:
        kens = s.scalars(select(Contact).where(Contact.name == "Kip")).all()
        assert [(k.phone_e164, k.phone_status) for k in kens] == [("+254790002233", "valid")]
