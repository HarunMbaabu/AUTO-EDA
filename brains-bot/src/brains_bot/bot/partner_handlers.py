"""/partner: add the people and companies we give jobs to, with their rates. /partners: list."""

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
from brains_bot.services import contacts, partners
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

PARTNER_CALLBACK = re.compile(r"^partner:(save|cancel):([A-Za-z0-9_-]{6,20})$")
AWAITING = "awaiting_partner"
PENDING = "pending_partners"
NOT_AVAILABLE = "Adding partners needs MISTRAL_API_KEY or DEEPSEEK_API_KEY to be set."
ASK = (
    "Paste the details now: one partner, or a numbered list (1. ... 2. ...). Include the work "
    "they do and any rates, e.g. 'Pesh Interiors 0714222776, interior design, about 1,500 per "
    "sqm'. I'll show you what I understood before saving anything."
)


def _draft_lines(i: int | None, draft: partners.PartnerDraft, names: dict[str, str]) -> list[str]:
    prefix = f"{i}. " if i is not None else ""
    head = (
        f"{prefix}UPDATE → {draft.existing_name}"
        if draft.existing_contact_id is not None
        else f"{prefix}PARTNER: {draft.label}"
    )
    numbers = ", ".join(mask_phone(x) for x in draft.phone.numbers) or "no number"
    lines = [head + (f" · {draft.area}" if draft.area else ""), f"   {numbers}"]
    work = ", ".join(names.get(k, k) for k in draft.services)
    lines.append(f"   Work: {work or 'not stated'}")
    lines += [f"   Rate: {partners.format_rate(r)}" for r in draft.rates]
    return lines


def _preview(drafts: list[partners.PartnerDraft], names: dict[str, str]) -> str:
    many = len(drafts) > 1
    lines = [f"{len(drafts)} partners: please check" if many else "Partner: please check", ""]
    for i, draft in enumerate(drafts, start=1):
        lines += _draft_lines(i if many else None, draft, names)
    lines += [
        "",
        "Rates are saved as approximate, with today's date or the date in the message. "
        "If something is wrong, tap Cancel and send /partner again with the message corrected.",
    ]
    return truncate("\n".join(lines), MAX_MESSAGE)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    drafter = context.bot_data.get("drafter")
    if drafter is None:
        await update.effective_message.reply_text(NOT_AVAILABLE)
        return
    status = await update.effective_message.reply_text("Reading the partner details…")
    try:
        names = await _with_session(context, partners.category_names)
        drafts = await asyncio.to_thread(partners.extract, drafter.transport, text, names)

        def match(session: Session) -> None:
            partners.match_existing(session, drafts)

        await _with_session(context, match)
    except AnalysisError as exc:
        await status.edit_text(f"I couldn't read that: {exc}")
        return
    except Exception:
        log.exception("partner extraction failed", extra={"event": "partner_error"})
        await status.edit_text("Something went wrong reading that. Please try again.")
        return
    token = secrets.token_urlsafe(9)
    context.user_data.setdefault(PENDING, {})[token] = drafts
    label = f"✅ Save all {len(drafts)}" if len(drafts) > 1 else "✅ Save"
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"partner:save:{token}"),
          InlineKeyboardButton("✖ Cancel", callback_data=f"partner:cancel:{token}")]]
    )  # fmt: skip
    await status.edit_text(_preview(drafts, names), reply_markup=buttons)


@restricted("partner")
async def partner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        context.user_data[AWAITING] = True
        await update.effective_message.reply_text(ASK)
        return
    await handle_message(update, context, text)


async def partner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "partner_button", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = PARTNER_CALLBACK.match(query.data or "")
    pending = context.user_data.get(PENDING, {})
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    action, token = match.groups()
    drafts = pending.pop(token, None)
    if drafts is None:
        await query.answer("This preview has expired. Send /partner again.", show_alert=True)
        return
    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled: nothing was saved.")
        return
    actor = _user_id(update)

    def work(session: Session) -> list[tuple[str, bool, int]]:
        saved = []
        for draft in drafts:
            contact, is_new = partners.save(session, draft, date.today(), actor)
            action_name = "partner_created" if is_new else "partner_updated"
            svc.record_audit(session, actor, action_name, target=f"contact:{contact.id}")
            saved.append((contact.name, is_new, len(draft.rates)))
        return saved

    try:
        saved = await _with_session(context, work)
    except Exception:
        log.exception("saving partners failed", extra={"event": "db_error"})
        pending[token] = drafts
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    await query.answer("Saved.")
    new = sum(is_new for _, is_new, _ in saved)
    rates = sum(r for _, _, r in saved)
    lines = [f"Saved: {new} new partner(s), {len(saved) - new} updated, {rates} rate(s)."]
    lines += [f"- {'new' if is_new else 'updated'}: {name}" for name, is_new, _ in saved]
    lines.append("See them with /partners (e.g. /partners interior_design, /partners finishes).")
    await query.edit_message_text(truncate("\n".join(lines), MAX_MESSAGE))


def partner_lines(p: partners.Partner, names: dict[str, str]) -> list[str]:
    c = p.contact
    who = " · ".join(x for x in (c.name, c.company if c.company != c.name else None, c.area) if x)
    phone = mask_phone(c.phone_e164) if c.phone_e164 else "no number"
    work = ", ".join(names.get(k, k) for k in c.services or []) or contacts.label(c.trade)
    lines = [f"• {who}", f"   {phone} · {work}"]
    for rate in p.rates[:4]:
        when = f" ({rate.noted_on:%b %Y})" if rate.noted_on else ""
        lines.append(f"   {partners.format_rate(rate)}{when}")
    return lines


@restricted("partners")
async def partners_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = [a for a in (context.args or []) if a.strip()]
    actor = _user_id(update)

    def work(session: Session) -> str:
        names = partners.category_names(session)
        keys, words = None, args
        for take in (3, 2, 1):  # "finishes & interiors", "interior design", "audio"
            if len(args) >= take and (
                found := partners.category_keys(session, " ".join(args[:take]))
            ):
                keys, words = found, args[take:]
                break
        found, total = partners.directory(session, keys, " ".join(words), partners.LIST_LIMIT)
        svc.record_audit(session, actor, "partners_list", results=total)
        if not found:
            return (
                "No partners found yet." if not args else "No partners match that."
            ) + " Add one with /partner, e.g. /partner Pesh Interiors 0714222776, interior "
            "design, about 1,500 per sqm."
        lines = [f"Partners ({total})", ""]
        for p in found:
            lines += partner_lines(p, names)
        if total > len(found):
            lines += ["", f"Showing {len(found)}. Add a category or area to narrow it down."]
        return "\n".join(lines)

    try:
        reply = await _with_session(context, work)
    except Exception:
        log.exception("partners list failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(truncate(reply, MAX_MESSAGE))
