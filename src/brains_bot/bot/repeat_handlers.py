"""/repeats lists projects that look like the same site entered twice; /merge joins two."""

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
from brains_bot.services import project_service as svc
from brains_bot.services import repeats

log = logging.getLogger(__name__)

MERGE_CALLBACK = re.compile(r"^merge:(go|cancel):(\d{1,9}):(\d{1,9})$")
USAGE = (
    "Usage: /merge <repeat> <keep>\n"
    "Example: /merge 214 37 moves everything on #214 (notes, people and numbers, drafts, "
    "quotes) into #37 and removes #214. Send /repeats to see likely repeats."
)


@restricted("repeats")
async def repeats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return

    def work(session: Session) -> str:
        groups = repeats.find_repeats(session)
        if not groups:
            return "No likely repeats found: no two projects share a number or a name and area."
        lines = [f"Likely repeat sites ({len(groups)})", ""]
        for group in groups[: repeats.MAX_GROUPS]:
            sites = " · ".join(f"#{pid} {name}" for pid, name in group.projects)
            keep = group.projects[0][0]
            merges = ", ".join(f"/merge {pid} {keep}" for pid, _ in group.projects[1:])
            lines += [f"• {sites}", f"   {group.reason} → {merges}"]
        if len(groups) > repeats.MAX_GROUPS:
            lines += ["", f"Showing {repeats.MAX_GROUPS}. Merge these, then send /repeats again."]
        lines += [
            "",
            "Check with /project <number> first: the same caretaker or owner can run two "
            "different sites. Merging asks for confirmation.",
        ]
        return "\n".join(lines)

    try:
        reply = await _with_session(context, work)
    except Exception:
        log.exception("repeats failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(truncate(reply, MAX_MESSAGE))


@restricted("merge")
async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = context.args or []
    if len(args) != 2 or not all(a.lstrip("#").isdigit() for a in args):
        await update.effective_message.reply_text(USAGE)
        return
    repeat_id, keep_id = (int(a.lstrip("#")) for a in args)
    if repeat_id == keep_id:
        await update.effective_message.reply_text("Those are the same project. " + USAGE)
        return

    def work(session: Session) -> tuple[Project | None, Project | None]:
        return session.get(Project, repeat_id), session.get(Project, keep_id)

    try:
        repeat, keep = await _with_session(context, work)
    except Exception:
        log.exception("merge lookup failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    missing = [f"#{pid}" for pid, p in ((repeat_id, repeat), (keep_id, keep)) if p is None]
    if missing:
        await update.effective_message.reply_text(f"Project {', '.join(missing)} not found.")
        return
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Merge", callback_data=f"merge:go:{repeat_id}:{keep_id}"),
          InlineKeyboardButton("✖ Cancel", callback_data=f"merge:cancel:{repeat_id}:{keep_id}")]]
    )  # fmt: skip
    await update.effective_message.reply_text(
        f"Merge #{repeat_id} {repeat.name} into #{keep_id} {keep.name}?\n"
        f"Its notes, people and numbers, drafts and quotes move to #{keep_id}, and "
        f"#{repeat_id} is removed. This cannot be undone.",
        reply_markup=buttons,
    )


async def merge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "merge_button", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = MERGE_CALLBACK.match(query.data or "")
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    action, repeat_id, keep_id = match[1], int(match[2]), int(match[3])
    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled: nothing was merged.")
        return
    actor = _user_id(update)

    def work(session: Session) -> str:
        keep = repeats.merge(session, repeat_id, keep_id, date.today(), actor)
        svc.record_audit(
            session, actor, "project_merged", target=f"project:{keep_id}", merged=repeat_id
        )
        return keep.name

    try:
        name = await _with_session(context, work)
    except LookupError:
        await query.answer("Already merged or removed.", show_alert=True)
        return
    except Exception:
        log.exception("merge failed", extra={"event": "db_error"})
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    await query.answer("Merged.")
    await query.edit_message_text(
        f"Merged #{repeat_id} into #{keep_id} {name}. See it with /project {keep_id}."
    )
