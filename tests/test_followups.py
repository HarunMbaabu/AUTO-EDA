from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from brains_bot.bot import followup_handlers, handlers
from brains_bot.database.models import (
    AuditEvent,
    OpportunityAssessment,
    OutreachDraft,
    Quote,
    SourceRecord,
)
from brains_bot.database.session import session_scope
from brains_bot.services import followups, opportunities
from brains_bot.services.chat import ProjectTools
from tests.conftest import ALLOWED_ID, STRANGER_ID, make_update, reply_of
from tests.test_dashboard import _log_in, client  # noqa: F401 - pytest fixture
from tests.test_outreach import _seed

TODAY = date(2025, 10, 1)  # db:1 skimming noted 2025-03-01; db:2 foundation noted 2025-09-16


def _ago(days):
    return datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC) - timedelta(days=days)


def test_rank_scores_and_reasons(factory, tmp_path):
    ids = _seed(factory, tmp_path)
    with session_scope(factory) as s:
        audio = {o.project_id: o for o in opportunities.rank(s, TODAY, "audio", 10)}
        # db:2 at foundation: in the audio window, noted 15 days ago, reachable, catalog fits.
        assert audio[ids["db:2"]].score == 40 + 20 + 15 + 5
        assert audio[ids["db:2"]].reasons == [
            "best time for it now (+40)",
            "stage noted 15 days ago (+20)",
            "has a WhatsApp number (+15)",
            "a catalog product fits the stage (+5)",
        ]
        # db:1 at skimming is past audio's window: retrofit only, note 7 months old.
        assert audio[ids["db:1"]].timing == "retrofit_only"
        assert audio[ids["db:1"]].score == 15 + 10 + 15 + 5
        # db:3 has no stage and no reachable contact.
        assert audio[ids["db:3"]].score == 5
        assert [o.project_id for o in opportunities.rank(s, TODAY, "audio", 10)][0] == ids["db:2"]

        batch = s.scalar(
            select(func.max(SourceRecord.batch_id)).where(SourceRecord.project_id == ids["db:1"])
        )
        s.add(OpportunityAssessment(project_id=ids["db:1"], category_key="audio", model="m",
                                    data_batch_id=batch, timing_status="retrofit_only",
                                    result={"fit": "strong"}))  # fmt: skip
        s.add(OutreachDraft(project_id=ids["db:2"], category_key="audio", text="t", model="m",
                            created_at=_ago(3)))  # fmt: skip
        s.add(Quote(project_id=ids["db:3"], category_key="audio", amount_kes=Decimal(1)))
        s.flush()
        audio = {o.project_id: o for o in opportunities.rank(s, TODAY, "audio", 10)}
        assert (
            audio[ids["db:1"]].score == 45 + 25
            and "AI analysis: strong fit (+25)" in audio[ids["db:1"]].reasons
        )
        assert audio[ids["db:2"]].score == 80 - 25
        assert ids["db:3"] not in audio  # an open quote: no longer an opportunity

        best = opportunities.rank(s, TODAY, None, 10)
        assert len({o.project_id for o in best}) == len(best)  # one row per project


def test_followups_and_digest(factory, tmp_path):
    ids = _seed(factory, tmp_path)
    with session_scope(factory) as s:
        s.add_all([
            OutreachDraft(project_id=ids["db:1"], category_key="audio", text="a", model="m",
                          sent_at=_ago(6)),
            OutreachDraft(project_id=ids["db:2"], category_key="audio", text="b", model="m",
                          sent_at=_ago(2)),
            OutreachDraft(project_id=ids["db:3"], category_key="audio", text="c", model="m",
                          sent_at=_ago(9), replied_at=_ago(8)),
            Quote(project_id=ids["db:2"], category_key="audio", amount_kes=Decimal(300000),
                  created_at=_ago(20)),
            Quote(project_id=ids["db:1"], category_key="audio", amount_kes=Decimal(5),
                  created_at=_ago(3)),
        ])  # fmt: skip
        s.flush()
        waiting = followups.awaiting_reply(s, TODAY)
        assert [(w.project_id, w.days) for w in waiting] == [(ids["db:1"], 6)]
        chase = followups.quotes_to_chase(s, TODAY)
        assert [(q.project_id, q.detail) for q in chase] == [(ids["db:2"], "KES 300,000")]
        text = followups.digest(s, TODAY)
        assert "Top opportunities to pitch" in text and f"/draft {ids['db:3']}" in text
        assert "Kamau proposed villa: Home Audio & Multi-room, sent 6 days ago" in text
        assert "Sunrise Apartments: Home Audio & Multi-room, KES 300,000, 20 days old" in text

        assert followups.wants_digest(s, ALLOWED_ID)
        followups.set_digest(s, ALLOWED_ID, False)
        assert not followups.wants_digest(s, ALLOWED_ID)

        tools = ProjectTools(s, ALLOWED_ID, TODAY)
        ranked = tools.rank_opportunities(limit=2)  # audio is quoted for db:1 and db:2
        assert len(ranked["opportunities"]) == 2
        assert ranked["opportunities"][0]["draft_command"].startswith("/draft ")
        assert ranked["sent_without_reply"][0]["days_since_sent"] == 6
        assert tools.rank_opportunities(category="kitchen")["error"].startswith("unknown")


async def test_commands(factory, tmp_path, context):
    _seed(factory, tmp_path)
    context.bot_data["db"] = factory
    update = make_update(ALLOWED_ID)
    context.args = ["audio"]
    await followup_handlers.top(update, context)
    text = reply_of(update)
    assert text.startswith("Top opportunities: Home Audio & Multi-room")
    assert "Why: best time for it now (+40)" in text and "/draft " in text

    update = make_update(ALLOWED_ID)
    context.args = ["kitchen"]
    await followup_handlers.top(update, context)
    assert "I don't know the category 'kitchen'" in reply_of(update)

    update = make_update(ALLOWED_ID)
    await followup_handlers.followups_command(update, context)
    assert "your follow-ups" in reply_of(update)

    update = make_update(ALLOWED_ID)
    context.args = ["off"]
    await followup_handlers.reminders(update, context)
    assert reply_of(update).startswith("Weekly follow-up summary: off")

    update = make_update(STRANGER_ID)
    await followup_handlers.top(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED
    with session_scope(factory) as s:
        assert "top" in list(s.scalars(select(AuditEvent.action)))


async def test_weekly_digest_skips_opted_out(factory, tmp_path, context):
    _seed(factory, tmp_path)
    context.bot_data["db"] = factory
    context.bot = SimpleNamespace(send_message=AsyncMock())
    await followup_handlers.send_weekly_digest(context)
    recipient, text = context.bot.send_message.await_args.args
    assert recipient == ALLOWED_ID and "your follow-ups" in text

    with session_scope(factory) as s:
        followups.set_digest(s, ALLOWED_ID, False)
    context.bot.send_message.reset_mock()
    await followup_handlers.send_weekly_digest(context)
    context.bot.send_message.assert_not_awaited()


def test_dashboard_opportunities_page(client, factory, tmp_path):  # noqa: F811
    _seed(factory, tmp_path)
    _log_in(client)
    page = client.get("/opportunities", params={"category": "audio"}).text
    assert "Opportunities" in page and "best time for it now (+40)" in page
    assert "/draft " in page
    overview = client.get("/").text
    assert "Top opportunities" in overview and "Follow-ups" in overview


async def test_ready_to_send_drafts_are_pushed(factory, tmp_path, context):
    from brains_bot.services.outreach import Drafter
    from tests.test_chat import FakeTransport, text

    _seed(factory, tmp_path)
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = Drafter(
        FakeTransport(*[text(f"Hello, message {i}") for i in range(5)])
    )
    update = make_update(ALLOWED_ID)
    status = SimpleNamespace(edit_text=AsyncMock())
    update.effective_message.reply_text.return_value = status
    context.args = ["drafts"]
    await followup_handlers.followups_command(update, context)
    assert status.edit_text.await_args.args[0].endswith("check each, then tap Send.")
    pushed = update.effective_message.reply_text.await_args_list[1:]
    assert pushed and pushed[0].args[0].startswith("Draft for #")
    assert pushed[0].kwargs["reply_markup"].inline_keyboard[0][0].text.startswith("📤 Send")

    context.bot = SimpleNamespace(send_message=AsyncMock())
    await followup_handlers.send_weekly_digest(context)
    sent = [c.args[1] for c in context.bot.send_message.await_args_list]
    assert "your follow-ups" in sent[0] and sent[1].startswith("Draft for #")
