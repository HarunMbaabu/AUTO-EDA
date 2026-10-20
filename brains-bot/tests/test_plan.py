from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from brains_bot.bot import stage_handlers
from brains_bot.database.models import Project, ProjectNote
from brains_bot.database.session import session_scope
from brains_bot.services import plan
from brains_bot.services.stages import confirmed_note, detect_stage
from tests.conftest import ALLOWED_ID, make_update, reply_of
from tests.test_dashboard import _log_in, client  # noqa: F401 - pytest fixture
from tests.test_outreach import _seed


def test_a_confirmed_stage_is_the_latest_word():
    notes = [(date(2026, 3, 24), "Skimming done in the front rooms")]
    assert detect_stage(notes).stage == 5
    notes.append((date(2026, 10, 1), confirmed_note(4, "Only plastering in most rooms")))
    found = detect_stage(notes)
    assert (found.stage, found.recorded_on) == (4, date(2026, 10, 1))  # corrected downwards
    notes.append((date(2026, 10, 10), "Painting ongoing now"))
    assert detect_stage(notes).stage == 6  # a later note moves it on
    assert [plan.stage_number(w) for w in ("skim", "Roof", "4", "handed", "x")] == [
        5, 3, 4, 7, None]  # fmt: skip


def test_plan_periods(factory, tmp_path):
    _seed(factory, tmp_path)
    with session_scope(factory) as s:
        project = s.scalars(select(Project)).first()
        project.est_paint_start = date.today() + timedelta(days=10)
        periods = plan.build(s, date.today())
        assert [p.title.split(" (")[0] for p in periods] == [
            "This week", "This month", "Next 3 months"]  # fmt: skip
        week = {sec.title: sec.items for sec in periods[0].sections}
        assert week["Pitch now"] and week["Pitch now"][0].detail.startswith("/draft ")
        month = {sec.title: sec.items for sec in periods[1].sections}
        assert month["Estimated paint start this month"][0].project_id == project.id


def test_dashboard_confirms_a_stage_and_shows_the_plan(client, factory, tmp_path):  # noqa: F811
    ids = _seed(factory, tmp_path)
    pid = ids["db:1"]  # its skimming note is from 2025: old enough to need confirming
    _log_in(client)
    page = client.get(f"/projects/{pid}").text
    assert "⚠ confirm" in page and f'action="/projects/{pid}/stage"' in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    done = client.post(
        f"/projects/{pid}/stage",
        data={"stage": "6", "on": date.today().isoformat(), "note": "Seen on site",
              "back": f"/projects/{pid}", "csrf": csrf},
        follow_redirects=False,
    )  # fmt: skip
    assert done.status_code == 303 and done.headers["location"] == f"/projects/{pid}"
    page = client.get(f"/projects/{pid}").text
    assert "painting under way" in page and "⚠ confirm" not in page.split("Site notes")[0]
    with session_scope(factory) as s:
        note = s.scalars(select(ProjectNote).where(ProjectNote.project_id == pid)
                         .order_by(ProjectNote.id.desc())).first()  # fmt: skip
        assert note.field == plan.STAGE_FIELD and note.text.endswith("Seen on site")
    bad = client.post(f"/projects/{pid}/stage", data={"stage": "4", "csrf": "x"})
    assert bad.status_code == 400
    future = (date.today() + timedelta(days=3)).isoformat()
    url = f"/projects/{pid}/stage"
    assert client.post(url, data={"stage": "4", "on": future, "csrf": csrf}).status_code == 400
    evil = client.post(
        url, data={"stage": "4", "back": "//evil.com", "csrf": csrf}, follow_redirects=False
    )
    assert evil.headers["location"] == "/"

    plan_page = client.get("/plan").text
    assert "This week" in plan_page and "Pitch now" in plan_page and "/draft " in plan_page


async def test_stage_and_plan_commands(factory, tmp_path, context):
    ids = _seed(factory, tmp_path)
    pid = ids["db:1"]
    context.bot_data["db"] = factory

    update = make_update(ALLOWED_ID)
    context.args = [str(pid)]
    await stage_handlers.stage_command(update, context)
    args = update.effective_message.reply_text.await_args
    assert "Recorded stage: skimming / finishing prep" in args.args[0]
    button = args.kwargs["reply_markup"].inline_keyboard[3][0]
    assert button.text == "plastering"
    query = SimpleNamespace(data=button.callback_data, answer=AsyncMock(),
                            edit_message_text=AsyncMock())  # fmt: skip
    tap = make_update(ALLOWED_ID)
    tap.callback_query = query
    await stage_handlers.stage_callback(tap, context)
    assert "is at plastering" in query.edit_message_text.await_args.args[0]

    update = make_update(ALLOWED_ID)
    context.args = [str(pid), "painting", "confirmed", "on", "a", "call"]
    await stage_handlers.stage_command(update, context)
    assert "is at painting under way" in reply_of(update)
    with session_scope(factory) as s:
        project = s.get(Project, pid)
        assert detect_stage([(n.observed_date, n.text) for n in project.notes]).stage == 6

    update = make_update(ALLOWED_ID)
    context.args = [str(pid), "upstairs"]
    await stage_handlers.stage_command(update, context)
    assert "I don't know the stage 'upstairs'" in reply_of(update)

    update = make_update(ALLOWED_ID)
    context.args = []
    await stage_handlers.plan_command(update, context)
    text = reply_of(update)
    assert text.startswith("This week") and "PITCH NOW" in text and "/plan month" in text
