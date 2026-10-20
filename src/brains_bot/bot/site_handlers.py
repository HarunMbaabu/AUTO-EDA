"""/newsite: paste a WhatsApp site report, check what was understood, tap Save."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import date

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from brains_bot.bot.handlers import (
    NOT_AUTHORIZED,
    _is_authorized,
    _log_command,
    _user_id,
    restricted,
)
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.importers.normalize import mask_phone
from brains_bot.services import project_service as svc
from brains_bot.services import sites
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

SITE_CALLBACK = re.compile(r"^site:(save|cancel):([A-Za-z0-9_-]{6,20})$")
AWAITING = "awaiting_site"
PENDING = "pending_sites"
NOT_AVAILABLE = "Adding sites needs MISTRAL_API_KEY or DEEPSEEK_API_KEY to be set."
ASK = (
    "Paste the WhatsApp message about the site now (one message). "
    "I'll show you what I understood before saving anything."
)
ROLE_LABELS = {"contact_person": "contact", "owner": "owner", "contractor": "contractor",
               "architect": "architect"}  # fmt: skip


def _preview(draft: sites.SiteDraft) -> str:
    numbers = ", ".join(mask_phone(x) for x in draft.phone.numbers) or "none found"
    lines = [
        "New site: please check",
        f"Site: {draft.site_name}",
        f"Area: {draft.area or '—'}" + (f", {draft.city}" if draft.city else ""),
        f"Contact: {draft.contact_name or '—'} ({ROLE_LABELS[draft.contact_role]})",
        f"WhatsApp number: {numbers}",
        f"Map: {'yes' if draft.map_url else 'no link'}",
        f"Visit date: {date.today():%d %b %Y}",
        "",
        "The whole message is saved as the first note. If something is wrong, tap Cancel and "
        "send /newsite again with the message corrected.",
    ]
    return "\n".join(lines)


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Extract a site from the pasted text and show it with Save / Cancel."""
    transport = context.bot_data.get("drafter")
    if transport is None:
        await update.effective_message.reply_text(NOT_AVAILABLE)
        return
    status = await update.effective_message.reply_text("Reading the site details…")
    try:
        draft = await asyncio.to_thread(sites.extract, transport.transport, text)
    except AnalysisError as exc:
        await status.edit_text(f"I couldn't read that: {exc}")
        return
    except Exception:
        log.exception("site extraction failed", extra={"event": "site_error"})
        await status.edit_text("Something went wrong reading that. Please try again.")
        return
    token = secrets.token_urlsafe(9)
    context.user_data.setdefault(PENDING, {})[token] = draft
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Save", callback_data=f"site:save:{token}"),
          InlineKeyboardButton("✖ Cancel", callback_data=f"site:cancel:{token}")]]
    )  # fmt: skip
    await status.edit_text(_preview(draft), reply_markup=buttons)


@restricted("newsite")
async def newsite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    # Keep the message's own line breaks: take everything after the command itself.
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        context.user_data[AWAITING] = True
        await update.effective_message.reply_text(ASK)
        return
    await handle_report(update, context, text)


async def site_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "site_button", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = SITE_CALLBACK.match(query.data or "")
    pending = context.user_data.get(PENDING, {})
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    action, token = match.groups()
    draft = pending.pop(token, None)
    if draft is None:
        await query.answer("This preview has expired. Send /newsite again.", show_alert=True)
        return
    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled: nothing was saved.")
        return
    actor = _user_id(update)

    def work(session: Session) -> tuple[int, str]:
        project = sites.save(session, draft, date.today())
        svc.record_audit(session, actor, "site_created", target=f"project:{project.id}")
        return project.id, project.name

    try:
        project_id, name = await _with_session(context, work)
    except Exception:
        log.exception("saving site failed", extra={"event": "db_error"})
        pending[token] = draft  # let them try Save again
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    await query.answer("Saved.")
    await query.edit_message_text(
        f"Saved as project #{project_id} {name}.\n"
        f"/project {project_id} · /analyze {project_id} audio · /draft {project_id} audio"
    )
