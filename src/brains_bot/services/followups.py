"""Follow-ups: what needs chasing, and the weekly summary the bot sends on Monday mornings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from brains_bot.database.models import Category, OperatorSetting, OutreachDraft, Project, Quote
from brains_bot.services import opportunities

REPLY_WAIT_DAYS = 5  # a sent draft with no reply after this long needs a follow-up
QUOTE_WAIT_DAYS = 14  # an open quote this old needs chasing
DIGEST_SIZE = 5
NAIROBI = timezone(timedelta(hours=3), "EAT")  # Kenya has no daylight saving time
DIGEST_TIME = time(8, 0, tzinfo=NAIROBI)
DIGEST_DAYS = (1,)  # python-telegram-bot counts 0 = Sunday, so 1 = Monday


@dataclass
class FollowUp:
    project_id: int
    project: str
    category: str
    since: date
    days: int
    detail: str = ""


def _names(session: Session) -> dict[str, str]:
    return dict(session.execute(select(Category.key, Category.name)).all())


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=NAIROBI)


def awaiting_reply(session: Session, today: date) -> list[FollowUp]:
    names = _names(session)
    cutoff = _start_of(today - timedelta(days=REPLY_WAIT_DAYS - 1))
    rows = session.execute(
        select(OutreachDraft, Project.name)
        .join(Project, Project.id == OutreachDraft.project_id)
        .where(
            OutreachDraft.sent_at.is_not(None),
            OutreachDraft.sent_at < cutoff,
            OutreachDraft.replied_at.is_(None),
        )
        .order_by(OutreachDraft.sent_at)
    )
    return [
        FollowUp(
            d.project_id,
            name,
            names.get(d.category_key, d.category_key),
            d.sent_at.date(),
            (today - d.sent_at.date()).days,
        )  # fmt: skip
        for d, name in rows
    ]


def quotes_to_chase(session: Session, today: date) -> list[FollowUp]:
    names = _names(session)
    cutoff = _start_of(today - timedelta(days=QUOTE_WAIT_DAYS - 1))
    rows = session.execute(
        select(Quote, Project.name)
        .join(Project, Project.id == Quote.project_id)
        .where(Quote.status == "open", Quote.created_at < cutoff)
        .order_by(Quote.created_at)
    )
    return [
        FollowUp(
            q.project_id,
            name,
            names.get(q.category_key, q.category_key),
            q.created_at.date(),
            (today - q.created_at.date()).days,
            f"KES {q.amount_kes:,.0f}",
        )  # fmt: skip
        for q, name in rows
    ]


def digest(session: Session, today: date) -> str:
    """The weekly summary: who to pitch, who to chase. Plain text for Telegram."""
    top = opportunities.rank(session, today, limit=DIGEST_SIZE)
    waiting = awaiting_reply(session, today)
    quotes = quotes_to_chase(session, today)
    lines = [f"Week of {today:%d %b %Y}: your follow-ups", ""]
    lines.append("Top opportunities to pitch")
    if top:
        for o in top:
            confirm = f" (stage note is old: /stage {o.project_id})" if o.stale else ""
            lines.append(f"- #{o.project_id} {o.project}: {o.category}, score {o.score}{confirm}")
            lines.append(f"  /draft {o.project_id} {o.category_key}")
    else:
        lines.append("- None right now.")
    lines += ["", f"Sent, no reply after {REPLY_WAIT_DAYS}+ days"]
    if waiting:
        lines += [f"- #{w.project_id} {w.project}: {w.category}, sent {w.days} days ago"
                  for w in waiting[:10]]  # fmt: skip
        if len(waiting) > 10:
            lines.append(f"- …and {len(waiting) - 10} more (see the dashboard)")
    else:
        lines.append("- Nothing waiting.")
    lines += ["", f"Open quotes older than {QUOTE_WAIT_DAYS} days"]
    if quotes:
        lines += [f"- #{q.project_id} {q.project}: {q.category}, {q.detail}, {q.days} days old"
                  for q in quotes[:10]]  # fmt: skip
    else:
        lines.append("- Nothing to chase.")
    lines += [
        "",
        "Ready-to-send messages: /followups drafts (sent with this summary on Mondays)",
        "This week and month in full: /plan · confirm a stage: /stage <number>",
        "Full ranking: /top · turn this weekly message off: /reminders off",
    ]
    return "\n".join(lines)


def wants_digest(session: Session, telegram_id: int) -> bool:
    setting = session.get(OperatorSetting, telegram_id)
    return setting is None or setting.weekly_digest


def set_digest(session: Session, telegram_id: int, enabled: bool) -> None:
    setting = session.get(OperatorSetting, telegram_id)
    if setting is None:
        session.add(OperatorSetting(telegram_id=telegram_id, weekly_digest=enabled))
    else:
        setting.weekly_digest = enabled
    session.flush()
