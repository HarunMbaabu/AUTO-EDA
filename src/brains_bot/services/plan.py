"""What to do this week, this month and in the next three months, and confirming a project's
construction stage (which clears the "confirm" flag on an old stage note)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from brains_bot.database.models import Category, OutreachDraft, Project, ProjectNote, Quote
from brains_bot.services import followups, metrics, opportunities
from brains_bot.services.stages import STAGES, assess_timing, confirmed_note

STAGE_FIELD = "Stage update"
PITCH_NOW = 10
LIST_LIMIT = 25
# Words people use for each stage, for /stage 349 skimming.
_ALIASES = {1: ("foundation", "excavation", "early"), 2: ("walling", "walls", "structure", "slab"),
            3: ("roofing", "roof"), 4: ("plastering", "plaster"),
            5: ("skimming", "skim", "finishing"), 6: ("painting", "paint", "undercoat"),
            7: ("complete", "handed", "handover", "done", "finished", "occupied")}  # fmt: skip


def stage_number(text: str) -> int | None:
    """'skimming' -> 5, '4' -> 4, 'Roof' -> 3."""
    word = text.strip().lower()
    if word.isdigit() and int(word) in STAGES:
        return int(word)
    return next((n for n, words in _ALIASES.items() if any(word.startswith(w) for w in words)),
                None)  # fmt: skip


def record_stage(
    session: Session,
    project_id: int,
    stage: int,
    on: date,
    note: str | None = None,
    actor: int | None = None,
) -> Project | None:
    """Save a stage a person confirmed (seen on site or told on a call) as a dated note."""
    project = session.get(Project, project_id)
    if project is None or stage not in STAGES:
        return None
    session.add(
        ProjectNote(
            project_id=project_id,
            field=STAGE_FIELD,
            text=confirmed_note(stage, (note or "")[:500]),
            observed_date=on,
        )
    )
    session.flush()
    return project


@dataclass
class Item:
    project_id: int
    project: str
    area: str | None
    what: str  # e.g. "Home Audio & Multi-room" or "Sent 9 days ago, no reply"
    detail: str = ""  # e.g. "/draft 205 audio"
    due: date | None = None
    stage: str | None = None
    recorded_on: date | None = None
    stale: bool = False


@dataclass
class Section:
    title: str
    hint: str
    items: list[Item] = field(default_factory=list)


@dataclass
class Period:
    title: str
    sections: list[Section]

    @property
    def count(self) -> int:
        return sum(len(s.items) for s in self.sections)


def build(session: Session, today: date) -> list[Period]:
    week_end, month_end, later_end = (today + timedelta(days=d) for d in (7, 30, 90))
    rows = metrics.project_stages(session)
    categories = list(session.scalars(select(Category).where(Category.active)))

    # This week
    pitch = [
        Item(o.project_id, o.project, o.area, o.category, f"/draft {o.project_id} {o.category_key}",
             stage=o.stage, recorded_on=o.recorded_on, stale=o.stale)
        for o in opportunities.rank(session, today, limit=PITCH_NOW)
    ]  # fmt: skip
    replies = [
        Item(w.project_id, w.project, None, w.category, f"sent {w.days} days ago, no reply",
             due=w.since + timedelta(days=followups.REPLY_WAIT_DAYS))
        for w in followups.awaiting_reply(session, today)
    ]  # fmt: skip
    quotes = [
        Item(q.project_id, q.project, None, q.category, f"{q.detail}, {q.days} days old",
             due=q.since + timedelta(days=followups.QUOTE_WAIT_DAYS))
        for q in followups.quotes_to_chase(session, today)
    ]  # fmt: skip
    # Stale stages on projects that matter now or soon: confirm them before pitching.
    confirm: dict[int, Item] = {}
    soon: dict[int, Item] = {}
    two_ahead: dict[int, Item] = {}
    for project, stage in rows:
        for category in categories:
            timing = assess_timing(category, stage, today)
            near = stage.stage is not None and stage.stage == category.best_from - 1
            item = Item(project.id, project.name, project.area, category.name,
                        stage=stage.label, recorded_on=stage.recorded_on,
                        stale=timing.stale)  # fmt: skip
            if timing.stale and (timing.status == "in_window" or near):
                confirm.setdefault(project.id, item)
            elif near:
                _add(soon, item)
            elif stage.stage is not None and stage.stage == category.best_from - 2:
                _add(two_ahead, item)

    # This month and later
    replies_soon, quotes_soon = _coming_due(session, today, month_end)
    paint = [
        Item(p.id, p.name, p.area, "Estimated paint start (from the workbook)",
             due=p.est_paint_start)
        for p, _ in rows
        if p.est_paint_start and today <= p.est_paint_start <= later_end
    ]  # fmt: skip
    paint.sort(key=lambda i: i.due)

    def newest(items: dict[int, Item]) -> list[Item]:
        found = sorted(items.values(), key=lambda i: i.recorded_on or date.min, reverse=True)
        return found[:LIST_LIMIT]

    return [
        Period(f"This week (to {week_end:%d %b})", [
            Section("Chase replies", "Sent drafts with no reply yet: call or send a follow-up.",
                    replies[:LIST_LIMIT]),
            Section("Chase quotes", "Open quotes waiting two weeks or more.", quotes[:LIST_LIMIT]),
            Section("Confirm the stage first", "In or near a best window, but the stage note "
                    "is over 4 months old: call or visit, then update it.",
                    newest(confirm)),
            Section("Pitch now", "Best opportunities right now; draft in Telegram.", pitch),
        ]),
        Period(f"This month (to {month_end:%d %b})", [
            Section("Visit: coming into the window next", "One stage before a category's best "
                    "window: plan visits so you are there in time.", newest(soon)),
            Section("Follow-ups falling due", "Drafts and quotes that will need chasing.",
                    (replies_soon + quotes_soon)[:LIST_LIMIT]),
            Section("Estimated paint start this month", "From the workbook's estimates: check "
                    "they still hold.", [i for i in paint if i.due <= month_end]),
        ]),
        Period(f"Next 3 months (to {later_end:%d %b})", [
            Section("Two stages before a window", "Keep in touch; they will be ready later.",
                    newest(two_ahead)),
            Section("Estimated paint start", "From the workbook's estimates.",
                    [i for i in paint if i.due > month_end]),
        ]),
    ]  # fmt: skip


def _add(bucket: dict[int, Item], item: Item) -> None:
    """One row per project, its categories joined."""
    if item.project_id in bucket:
        bucket[item.project_id].what += f", {item.what}"
    else:
        bucket[item.project_id] = item


def _coming_due(session: Session, today: date, until: date) -> tuple[list[Item], list[Item]]:
    """Sent drafts and open quotes not yet due for chasing, but due before `until`."""
    names = dict(session.execute(select(Category.key, Category.name)).all())
    drafts = session.execute(
        select(OutreachDraft, Project.name, Project.area)
        .join(Project, Project.id == OutreachDraft.project_id)
        .where(OutreachDraft.sent_at.is_not(None), OutreachDraft.replied_at.is_(None))
    ).all()
    replies = []
    for draft, name, area in drafts:
        due = draft.sent_at.date() + timedelta(days=followups.REPLY_WAIT_DAYS)
        if today < due <= until:
            replies.append(Item(draft.project_id, name, area,
                                names.get(draft.category_key, draft.category_key),
                                "chase if no reply", due=due))  # fmt: skip
    quotes = []
    for quote, name, area in session.execute(
        select(Quote, Project.name, Project.area)
        .join(Project, Project.id == Quote.project_id)
        .where(Quote.status == "open")
    ).all():
        due = quote.created_at.date() + timedelta(days=followups.QUOTE_WAIT_DAYS)
        if today < due <= until:
            quotes.append(Item(quote.project_id, name, area,
                               names.get(quote.category_key, quote.category_key),
                               f"quote KES {quote.amount_kes:,.0f}", due=due))  # fmt: skip
    return sorted(replies, key=lambda i: i.due), sorted(quotes, key=lambda i: i.due)


def stage_choices() -> list[tuple[int, str]]:
    return [(n, STAGES[n]) for n in sorted(STAGES)]
