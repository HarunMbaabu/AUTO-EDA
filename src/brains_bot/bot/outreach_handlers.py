"""/draft and the buttons under a draft: [Send via WhatsApp] and [They replied]."""

from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from brains_bot.bot import brochure_handlers
from brains_bot.bot.formatting import truncate
from brains_bot.bot.handlers import (
    NOT_AUTHORIZED,
    _is_authorized,
    _log_command,
    _user_id,
    restricted,
)
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.database.models import Contact, OutreachDraft, Project
from brains_bot.importers.normalize import format_phone
from brains_bot.services import brochures, outreach
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.categories import resolve_category

log = logging.getLogger(__name__)

DRAFT_CALLBACK = re.compile(r"^draft:(send|reply):(\d{1,9}):(\d)$")
USAGE = (
    "Usage: /draft <project number> <category>\n"
    "Example: /draft 205 audio\nSee /categories for the list."
)
NOT_AVAILABLE = "Drafting needs MISTRAL_API_KEY or DEEPSEEK_API_KEY to be set."


def _buttons(
    draft: OutreachDraft, phones: list[str], chosen: int | None = None
) -> InlineKeyboardMarkup | None:
    """Before sending: one Send button per number. After: open WhatsApp on the chosen one."""
    if not phones:
        return None
    if draft.sent_at is None or chosen is None or chosen >= len(phones):
        label = "📤 Send via WhatsApp"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{label} ({format_phone(phone)})" if len(phones) > 1 else label,
                        callback_data=f"draft:send:{draft.id}:{i}",
                    )
                ]
                for i, phone in enumerate(phones)
            ]
        )
    url = outreach.whatsapp_url(phones[chosen], draft.text)
    row = [InlineKeyboardButton("Open WhatsApp", url=url)]
    if draft.replied_at is None:
        row.append(
            InlineKeyboardButton(
                "✅ They replied", callback_data=f"draft:reply:{draft.id}:{chosen}"
            )
        )
    return InlineKeyboardMarkup([row])


def _phones(contact: Contact | None) -> list[str]:
    """Usable WhatsApp numbers: the one valid number, or each of several candidates."""
    if contact is None or contact.phone_status not in ("valid", "multiple"):
        return []
    return [str(n) for n in contact.phone_candidates if str(n).startswith("+254")][:3]


def draft_message(
    project: Project,
    category,
    contact: Contact | None,
    created: OutreachDraft,
    brochure_count: int = 0,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The draft as shown in Telegram, with its Send buttons (and the category's brochures,
    to forward with the message)."""
    phones = _phones(contact)
    to = contact.name if contact else "no contact person on record"
    numbers = ", ".join(format_phone(n) for n in phones) or "no valid WhatsApp number"
    lines = [
        f"Draft for #{project.id} {project.name} · {category.name}",
        f"To: {to} ({numbers})",
        "⚠ Marketing consent is unknown. Only send if they have agreed to hear from you.",
        "",
        created.text,
        "",
        "Tap Send to open it in WhatsApp, where you can edit it before sending."
        if phones
        else "There is no valid number on record, so there is no WhatsApp link. "
        f"Check /project {project.id}.",
    ]
    buttons = _buttons(created, phones)
    extra = brochure_handlers.draft_button(category.key, brochure_count)
    if extra:
        rows = list(buttons.inline_keyboard) if buttons else []
        buttons = InlineKeyboardMarkup([*rows, extra])
    return truncate("\n".join(lines), 4000), buttons


@restricted("draft")
async def draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = list(context.args or [])
    raw_id = args[0].lstrip("#") if args else ""
    if len(args) < 2 or not raw_id.isdigit() or len(raw_id) > 9:
        await update.effective_message.reply_text(USAGE)
        return
    drafter = context.bot_data.get("drafter")
    if drafter is None:
        await update.effective_message.reply_text(NOT_AVAILABLE)
        return
    project_id, category_text, actor = int(raw_id), " ".join(args[1:]), _user_id(update)
    status = await update.effective_message.reply_text(
        f"Drafting a message for project #{project_id}…"
    )

    def work(session: Session):
        project = session.get(Project, project_id)
        category = resolve_category(session, category_text)
        if project is None or category is None:
            return project, category, None, None, 0
        contact = outreach.pick_contact(session, project.id)
        target = outreach.DraftTarget(project, contact, category)
        created = outreach.create_draft(session, drafter, target, actor, date.today())
        svc.record_audit(session, actor, "draft_created", target=f"project:{project.id}",
                         category=category.key, draft=created.id)  # fmt: skip
        count = brochures.library(session, [category.key])[1]
        return project, category, contact, created, count

    try:
        project, category, contact, created, count = await _with_session(context, work)
    except AnalysisError as exc:
        await status.edit_text(f"Drafting failed: {exc}")
        return
    except Exception:
        log.exception("draft failed", extra={"event": "draft_error"})
        await status.edit_text(DB_ERROR)
        return
    if project is None:
        await status.edit_text(f"No project #{project_id}.")
        return
    if category is None:
        await status.edit_text(
            f"I don't know the category '{category_text}'. See /categories for the list."
        )
        return

    text, buttons = draft_message(project, category, contact, created, count)
    await status.edit_text(text, reply_markup=buttons)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "draft_button", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = DRAFT_CALLBACK.match(query.data or "")
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    action, draft_id, chosen = match.group(1), int(match.group(2)), int(match.group(3))
    actor = _user_id(update)

    def work(session: Session):
        if action == "send":
            found = outreach.mark_sent(session, draft_id, actor)
        else:
            found = outreach.mark_replied(session, draft_id)
        if found is None:
            return None, None
        svc.record_audit(session, actor, f"draft_{action}", target=f"project:{found.project_id}",
                         draft=found.id)  # fmt: skip
        contact = session.get(Contact, found.contact_id) if found.contact_id else None
        return found, _phones(contact)

    try:
        found, phones = await _with_session(context, work)
    except Exception:
        log.exception("draft button failed", extra={"event": "db_error"})
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    if found is None:
        await query.answer("That draft no longer exists.", show_alert=True)
        return
    await query.answer(
        "Recorded as sent. Tap Open WhatsApp." if action == "send" else "Reply recorded."
    )
    await query.edit_message_reply_markup(reply_markup=_buttons(found, phones, chosen))
