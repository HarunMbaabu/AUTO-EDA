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

from brains_bot.bot.formatting import MAX_MESSAGE, truncate
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
    "Paste the WhatsApp message now: one site, or a numbered list of sites (1. ... 2. ...). "
    "I'll show you what I understood before saving anything."
)
ROLE_LABELS = {"contact_person": "contact", "owner": "owner", "contractor": "contractor",
               "architect": "architect"}  # fmt: skip


def _site_lines(n: int | None, draft: sites.SiteDraft) -> list[str]:
    numbers = ", ".join(mask_phone(x) for x in draft.phone.numbers) or "no number"
    role = draft.role_description or ROLE_LABELS[draft.contact_role]
    prefix = f"{n}. " if n is not None else ""
    if draft.existing_project_id is not None:
        head = f"{prefix}FOLLOW-UP → #{draft.existing_project_id} {draft.existing_name}"
        return [head, f"   adds this as a new note · {numbers}"]
    place = ", ".join(x for x in (draft.area, draft.city) if x) or "area not stated"
    return [
        f"{prefix}NEW: {draft.site_name} · {place}",
        f"   {draft.contact_name or 'no name'} ({role}) · {numbers}"
        + (" · map" if draft.map_url else ""),
    ]


def _preview(drafts: list[sites.SiteDraft]) -> str:
    many = len(drafts) > 1
    lines = [f"{len(drafts)} sites: please check" if many else "New site: please check", ""]
    for i, draft in enumerate(drafts, start=1):
        lines += _site_lines(i if many else None, draft)
    lines += [
        "",
        f"Visit date: {date.today():%d %b %Y}. Each site's own text is saved as its note. "
        "Follow-ups are matched by phone number. If something is wrong, tap Cancel and send "
        "/newsite again with the message corrected.",
    ]
    return truncate("\n".join(lines), MAX_MESSAGE)


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Extract the site(s) from the pasted text and show them with Save / Cancel."""
    drafter = context.bot_data.get("drafter")
    if drafter is None:
        await update.effective_message.reply_text(NOT_AVAILABLE)
        return
    status = await update.effective_message.reply_text("Reading the site details…")
    try:
        drafts = await asyncio.to_thread(sites.extract, drafter.transport, text)

        def match(session: Session) -> None:
            sites.match_existing(session, drafts)

        await _with_session(context, match)
    except AnalysisError as exc:
        await status.edit_text(f"I couldn't read that: {exc}")
        return
    except Exception:
        log.exception("site extraction failed", extra={"event": "site_error"})
        await status.edit_text("Something went wrong reading that. Please try again.")
        return
    token = secrets.token_urlsafe(9)
    context.user_data.setdefault(PENDING, {})[token] = drafts
    save_label = f"✅ Save all {len(drafts)}" if len(drafts) > 1 else "✅ Save"
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton(save_label, callback_data=f"site:save:{token}"),
          InlineKeyboardButton("✖ Cancel", callback_data=f"site:cancel:{token}")]]
    )  # fmt: skip
    await status.edit_text(_preview(drafts), reply_markup=buttons)


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
    drafts = pending.pop(token, None)
    if drafts is None:
        await query.answer("This preview has expired. Send /newsite again.", show_alert=True)
        return
    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled: nothing was saved.")
        return
    actor = _user_id(update)

    def work(session: Session) -> list[tuple[int, str, bool]]:
        saved = []
        for draft in drafts:
            follow_up = draft.existing_project_id is not None
            project = sites.save(session, draft, date.today())
            action_name = "site_followup" if follow_up else "site_created"
            svc.record_audit(session, actor, action_name, target=f"project:{project.id}")
            saved.append((project.id, project.name, follow_up))
        return saved

    try:
        saved = await _with_session(context, work)
    except Exception:
        log.exception("saving sites failed", extra={"event": "db_error"})
        pending[token] = drafts  # nothing was saved (one transaction); let them try again
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    await query.answer("Saved.")
    new = [s for s in saved if not s[2]]
    lines = [f"Saved: {len(new)} new project(s), {len(saved) - len(new)} follow-up note(s)."]
    for project_id, name, follow_up in saved:
        kind = "note added to" if follow_up else "new"
        lines.append(f"- {kind} #{project_id} {name}")
    first = saved[0][0]
    lines.append(f"Next: /project {first} · /analyze {first} audio · /draft {first} audio · /top")
    await query.edit_message_text(truncate("\n".join(lines), MAX_MESSAGE))
