"""/top, /followups, /reminders and the weekly Monday summary."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from brains_bot.bot import outreach_handlers
from brains_bot.bot.formatting import MAX_MESSAGE, fmt_date, truncate
from brains_bot.bot.handlers import _user_id, restricted
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.database.models import Category, Project
from brains_bot.database.session import session_scope
from brains_bot.services import brochures, followups, opportunities, outreach
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.categories import resolve_category

log = logging.getLogger(__name__)

TOP_SIZE = 10
PUSH_DRAFTS = 3  # ready-to-send messages with the Monday summary and /followups drafts
PUSH_POOL = 15  # opportunities looked at to find them


def format_top(items: list[opportunities.Opportunity], title: str) -> str:
    if not items:
        return f"{title}\n\nNo opportunities found."
    lines = [
        title,
        "Higher score = better timing, fresher notes, a WhatsApp number, a good AI fit.",
        "",
    ]
    for n, o in enumerate(items, start=1):
        when = fmt_date(o.recorded_on) if o.recorded_on else "no date"
        confirm = f", confirm: /stage {o.project_id}" if o.stale else ""
        lines.append(f"{n}. #{o.project_id} {o.project}" + (f" · {o.area}" if o.area else ""))
        lines.append(f"   {o.category} · score {o.score} · {o.stage} ({when}{confirm})")
        if o.also:
            lines.append(f"   Also: {', '.join(o.also[:3])}")
        lines.append(f"   Why: {'; '.join(o.reasons)}")
        lines.append(f"   /draft {o.project_id} {o.category_key}")
    return truncate("\n".join(lines), MAX_MESSAGE)


@restricted("top")
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    wanted = " ".join(context.args or []).strip()
    actor = _user_id(update)

    def work(session: Session):
        category = resolve_category(session, wanted) if wanted else None
        if wanted and category is None:
            return None, None
        items = opportunities.rank(
            session, date.today(), category.key if category else None, TOP_SIZE
        )
        svc.record_audit(session, actor, "top", category=category.key if category else None)
        return category, items

    try:
        category, items = await _with_session(context, work)
    except Exception:
        log.exception("ranking failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    if items is None:
        await update.effective_message.reply_text(
            f"I don't know the category '{wanted}'. See /categories for the list."
        )
        return
    title = f"Top opportunities: {category.name}" if category else "Top opportunities"
    await update.effective_message.reply_text(format_top(items, title))


def ready_drafts(session: Session, drafter, actor: int | None, today: date) -> list[tuple]:
    """Ready-to-send drafts for the best opportunities whose contact has a WhatsApp number:
    [(text, buttons)]. One per project."""
    out, seen = [], set()
    for o in opportunities.rank(session, today, limit=PUSH_POOL):
        if len(out) >= PUSH_DRAFTS or o.project_id in seen:
            continue
        project = session.get(Project, o.project_id)
        contact = outreach.pick_contact(session, o.project_id)
        if project is None or not outreach_handlers._phones(contact):
            continue
        category = session.scalar(select(Category).where(Category.key == o.category_key))
        target = outreach.DraftTarget(project, contact, category)
        try:
            created = outreach.create_draft(session, drafter, target, actor, today)
        except AnalysisError:
            log.warning("pushed draft failed", extra={"event": "draft_error"})
            continue
        seen.add(o.project_id)
        count = brochures.library(session, [category.key])[1]
        out.append(outreach_handlers.draft_message(project, category, contact, created, count))
    return out


@restricted("followups")
async def followups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    if (context.args or [""])[0].lower() == "drafts":
        await _push_drafts(update, context)
        return
    try:
        text = await _with_session(context, lambda s: followups.digest(s, date.today()))
    except Exception:
        log.exception("follow-ups failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(truncate(text, MAX_MESSAGE))


async def _push_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    drafter = context.bot_data.get("drafter")
    if drafter is None:
        await update.effective_message.reply_text(outreach_handlers.NOT_AVAILABLE)
        return
    actor = _user_id(update)
    status = await update.effective_message.reply_text(
        "Writing ready-to-send messages for your best opportunities…"
    )
    try:
        drafts = await _with_session(
            context, lambda s: ready_drafts(s, drafter, actor, date.today())
        )
    except Exception:
        log.exception("pushed drafts failed", extra={"event": "draft_error"})
        await status.edit_text(DB_ERROR)
        return
    if not drafts:
        await status.edit_text("No opportunities with a WhatsApp number to draft for right now.")
        return
    await status.edit_text(f"{len(drafts)} ready-to-send messages: check each, then tap Send.")
    for draft_text, buttons in drafts:
        await update.effective_message.reply_text(draft_text, reply_markup=buttons)


@restricted("reminders")
async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    actor = _user_id(update)
    choice = (context.args[0].lower() if context.args else "").strip()
    if choice not in ("", "on", "off"):
        await update.effective_message.reply_text("Usage: /reminders on  or  /reminders off")
        return

    def work(session: Session) -> bool:
        if choice:
            followups.set_digest(session, actor, choice == "on")
        return followups.wants_digest(session, actor)

    enabled = await _with_session(context, work)
    state = "on" if enabled else "off"
    await update.effective_message.reply_text(
        f"Weekly follow-up summary: {state} (Mondays at 08:00 Nairobi time). "
        f"Use /reminders {'off' if enabled else 'on'} to change it, or /followups to see it now."
    )


async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: the summary to every allowlisted operator who has not opted out."""
    factory = context.bot_data.get("db")
    if factory is None:
        return
    settings = context.bot_data["settings"]
    with session_scope(factory) as session:
        text = followups.digest(session, date.today())
        recipients = [uid for uid in sorted(settings.allowed_user_ids)
                      if followups.wants_digest(session, uid)]  # fmt: skip
    drafter = context.bot_data.get("drafter")
    drafts = []
    if drafter is not None and recipients:

        def make() -> list[tuple]:
            with session_scope(factory) as session:
                return ready_drafts(session, drafter, None, date.today())

        try:
            drafts = await asyncio.to_thread(make)
        except Exception:
            log.exception("weekly drafts failed", extra={"event": "draft_error"})
    for uid in recipients:
        try:
            await context.bot.send_message(uid, truncate(text, MAX_MESSAGE))
            for draft_text, buttons in drafts:
                await context.bot.send_message(uid, draft_text, reply_markup=buttons)
        except Exception:
            # e.g. the person never opened a chat with the bot or blocked it.
            log.warning(
                "weekly summary not delivered", extra={"event": "digest_failed", "user_id": uid}
            )
    log.info("weekly summary sent", extra={"event": "digest_sent", "command": str(len(recipients))})
