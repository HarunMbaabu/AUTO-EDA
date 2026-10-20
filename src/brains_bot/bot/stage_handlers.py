"""/stage: confirm or correct a project's construction stage; /plan: what to do and when."""

from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from brains_bot.bot.formatting import MAX_MESSAGE, truncate
from brains_bot.bot.handlers import (
    NOT_AUTHORIZED,
    _is_authorized,
    _log_command,
    _user_id,
    restricted,
)
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.database.models import Project
from brains_bot.services import plan, sites
from brains_bot.services import project_service as svc
from brains_bot.services.stages import STAGES, detect_stage

log = logging.getLogger(__name__)

STAGE_CALLBACK = re.compile(r"^stage:(\d{1,9}):([1-7])$")
USAGE = (
    "Usage: /stage <project number> [stage] [note]\n"
    "Examples: /stage 349 skimming · /stage 349 painting confirmed on a call on 3rd Oct\n"
    "Send /stage 349 alone to pick the stage from buttons. Stages: foundation, walling, "
    "roofing, plastering, skimming, painting, complete."
)
PERIODS = {"week": 0, "month": 1, "later": 2}
PLAN_ITEMS = 8  # per section in Telegram; the dashboard's Plan page shows more


def _save(context, project_id: int, stage: int, on: date, note: str | None, actor):
    def work(session: Session) -> str | None:
        project = plan.record_stage(session, project_id, stage, on, note, actor)
        if project is None:
            return None
        svc.record_audit(session, actor, "stage_confirmed", target=f"project:{project_id}",
                         stage=stage)  # fmt: skip
        return project.name

    return _with_session(context, work)


@restricted("stage")
async def stage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = list(context.args or [])
    raw_id = args[0].lstrip("#") if args else ""
    if not raw_id.isdigit() or len(raw_id) > 9:
        await update.effective_message.reply_text(USAGE)
        return
    project_id, actor = int(raw_id), _user_id(update)

    if len(args) == 1:  # show the current stage and buttons to pick the new one

        def current(session: Session):
            project = session.get(Project, project_id)
            if project is None:
                return None
            return project.name, detect_stage([(n.observed_date, n.text) for n in project.notes])

        try:
            found = await _with_session(context, current)
        except Exception:
            log.exception("stage lookup failed", extra={"event": "db_error"})
            await update.effective_message.reply_text(DB_ERROR)
            return
        if found is None:
            await update.effective_message.reply_text(f"No project #{project_id}.")
            return
        name, stage = found
        when = f" ({stage.recorded_on:%d %b %Y})" if stage.recorded_on else ""
        buttons = InlineKeyboardMarkup(
            [[InlineKeyboardButton(STAGES[n], callback_data=f"stage:{project_id}:{n}")]
             for n in sorted(STAGES)]
        )  # fmt: skip
        await update.effective_message.reply_text(
            f"#{project_id} {name}\nRecorded stage: {stage.label}{when}\n"
            "What stage is it at now? (saved with today's date)",
            reply_markup=buttons,
        )
        return

    stage = plan.stage_number(args[1])
    if stage is None:
        await update.effective_message.reply_text(f"I don't know the stage '{args[1]}'.\n{USAGE}")
        return
    note = " ".join(args[2:]).strip() or None
    on = (sites.date_in_text(note, date.today(), relative=True) if note else None) or date.today()
    try:
        name = await _save(context, project_id, stage, on, note, actor)
    except Exception:
        log.exception("stage update failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    if name is None:
        await update.effective_message.reply_text(f"No project #{project_id}.")
        return
    await update.effective_message.reply_text(
        f"Saved: #{project_id} {name} is at {STAGES[stage]} ({on:%d %b %Y}). "
        f"Timing and ranking now use this. See /project {project_id}."
    )


async def stage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "stage_button", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = STAGE_CALLBACK.match(query.data or "")
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    project_id, stage = int(match[1]), int(match[2])
    try:
        name = await _save(context, project_id, stage, date.today(), None, _user_id(update))
    except Exception:
        log.exception("stage update failed", extra={"event": "db_error"})
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    if name is None:
        await query.answer("That project no longer exists.", show_alert=True)
        return
    await query.answer("Saved.")
    await query.edit_message_text(
        f"Saved: #{project_id} {name} is at {STAGES[stage]} ({date.today():%d %b %Y})."
    )


def format_period(period: plan.Period) -> str:
    lines = [period.title, ""]
    for section in period.sections:
        if not section.items:
            continue
        lines.append(f"{section.title.upper()} ({len(section.items)})")
        for i in section.items[:PLAN_ITEMS]:
            when = f" · {i.due:%a %d %b}" if i.due else ""
            stage = f" · {i.stage}" if i.stage else ""
            lines.append(f"- #{i.project_id} {i.project}: {i.what}{when}{stage}")
            if i.stale:
                lines.append(f"  stage note is old: /stage {i.project_id}")
            elif i.detail:
                lines.append(f"  {i.detail}")
        if len(section.items) > PLAN_ITEMS:
            lines.append(f"  …and {len(section.items) - PLAN_ITEMS} more on the dashboard's Plan")
        lines.append("")
    if period.count == 0:
        lines.append("Nothing to do here right now.")
    lines.append("Other periods: /plan week · /plan month · /plan later")
    return truncate("\n".join(lines), MAX_MESSAGE)


@restricted("plan")
async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    wanted = (context.args or ["week"])[0].lower()
    if wanted not in PERIODS:
        await update.effective_message.reply_text("Usage: /plan [week|month|later]")
        return
    try:
        periods = await _with_session(context, lambda s: plan.build(s, date.today()))
    except Exception:
        log.exception("plan failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(format_period(periods[PERIODS[wanted]]))
