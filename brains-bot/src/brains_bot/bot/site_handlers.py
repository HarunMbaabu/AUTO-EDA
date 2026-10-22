"""/newsite: paste a WhatsApp site report, check what was understood, tap Save."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import replace
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
from brains_bot.importers.normalize import format_phone
from brains_bot.services import contacts, sites
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

SITE_CALLBACK = re.compile(r"^site:(save|new|cancel):([A-Za-z0-9_-]{6,20})$")
AWAITING = "awaiting_site"
PENDING = "pending_sites"
NOT_AVAILABLE = "Adding sites needs MISTRAL_API_KEY or DEEPSEEK_API_KEY to be set."
ASK = (
    "Paste the WhatsApp message now: one site, or a numbered list of sites (1. ... 2. ...). "
    "I'll show you what I understood before saving anything."
)
ROLE_LABELS = {"contact_person": "contact", "owner": "owner", "contractor": "contractor",
               "architect": "architect"}  # fmt: skip
CONTACT_KINDS = {"new_contact", "contact_note"}
AUDIT = {"new_site": "site_created", "follow_up": "site_followup",
         "new_contact": "contact_created", "contact_note": "contact_note"}  # fmt: skip
SUMMARY = {"new_site": "new project(s)", "follow_up": "follow-up note(s)",
           "new_contact": "new contact(s)", "contact_note": "contact note(s)"}  # fmt: skip
ITEM = {"new_site": "new", "follow_up": "note added to", "new_contact": "new contact",
        "contact_note": "note added to contact"}  # fmt: skip


def _numbers(draft: sites.SiteDraft) -> str:
    shown = [format_phone(x) for x in draft.phone.numbers]
    shown += [f"⚠ CHECK NUMBER: {p}" for p in sites.number_problems(draft.phone)]
    return ", ".join(shown) or "no number"


def _site_lines(n: int | None, draft: sites.SiteDraft) -> list[str]:
    numbers = _numbers(draft)
    role = draft.role_description or ROLE_LABELS[draft.contact_role]
    prefix = f"{n}. " if n is not None else ""
    if draft.kind == "contact":
        if draft.existing_contact_id is not None:
            head = f"{prefix}UPDATE CONTACT → {draft.existing_contact_name}"
            return [head, f"   adds this as a new note · {numbers}"]
        details = " · ".join(
            x for x in (contacts.label(draft.trade), draft.company, draft.area) if x
        )
        return [
            f"{prefix}CONTACT: {draft.contact_name or draft.company or 'no name'} · {details}",
            f"   {numbers} · no site; saved to the contacts directory",
        ]
    if draft.existing_project_id is not None:
        head = f"{prefix}FOLLOW-UP → #{draft.existing_project_id} {draft.existing_name}"
        return [head, f"   adds this as a new note · {numbers}"]
    if draft.possible_project_id is not None:
        head = f"{prefix}POSSIBLE REPEAT → #{draft.possible_project_id} {draft.possible_name}"
        return [
            head,
            f"   same name ({draft.contact_name}) in the same area, different number · {numbers}",
        ]
    place = ", ".join(x for x in (draft.area, draft.city) if x) or "area not stated"
    if draft.visit_date:
        place += f" · visited {draft.visit_date:%d %b}"
    return [
        f"{prefix}NEW: {draft.site_name} · {place}",
        f"   {draft.contact_name or 'no name'} ({role}) · {numbers}"
        + (" · map" if draft.map_url else ""),
    ]


def _preview(drafts: list[sites.SiteDraft]) -> str:
    many = len(drafts) > 1
    kinds = {d.kind for d in drafts}
    noun = {"site": "site", "contact": "contact"}.get(kinds.pop(), "") if len(kinds) == 1 else ""
    if many:
        head = f"{len(drafts)} {noun + 's' if noun else 'entries'}: please check"
    else:
        head = f"New {noun}: please check"
    lines = [head, ""]
    for i, draft in enumerate(drafts, start=1):
        lines += _site_lines(i if many else None, draft)
    dates = {d.visit_date for d in drafts}
    if dates == {None}:
        when = f"Date: {date.today():%d %b %Y} (today; no date found in the message)."
    elif len(dates) == 1:
        when = f"Date: {next(iter(dates)):%d %b %Y} (from the message)."
    else:
        when = "Dates: as shown per site."
    if any(d.phone.invalid_parts for d in drafts):
        lines += [
            "",
            "⚠ A number marked CHECK NUMBER is not a valid Kenyan mobile number (10 digits, "
            "e.g. 0712 345 678). Tap Cancel and send the message again with it corrected, or "
            "save now and send the right number later as a follow-up: it will be added to "
            "that site.",
        ]
    lines += [
        "",
        when,
        "Each entry's own text is saved as its note. "
        "Follow-ups and known contacts are matched by phone number"
        + (
            "; a POSSIBLE REPEAT is saved as a follow-up with its new number unless you tap "
            "'Save repeats as new'. "
            if any(d.possible_project_id for d in drafts)
            else ". "
        )
        + "If something is wrong, tap Cancel and send /newsite again with the message corrected.",
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
    cancel = InlineKeyboardButton("✖ Cancel", callback_data=f"site:cancel:{token}")
    if any(d.possible_project_id for d in drafts):
        rows = [[InlineKeyboardButton(f"{save_label} (repeats as follow-ups)",
                                      callback_data=f"site:save:{token}")],
                [InlineKeyboardButton("➕ Save repeats as new", callback_data=f"site:new:{token}"),
                 cancel]]  # fmt: skip
    else:
        rows = [[InlineKeyboardButton(save_label, callback_data=f"site:save:{token}"), cancel]]
    buttons = InlineKeyboardMarkup(rows)
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
    # "Save" makes a possible repeat a follow-up on that project; "Save repeats as new" does not.
    chosen = [
        replace(d, existing_project_id=d.possible_project_id, existing_name=d.possible_name)
        if d.possible_project_id is not None and action == "save"
        else d
        for d in drafts
    ]

    def work(session: Session) -> list[sites.Saved]:
        saved = []
        for draft in chosen:
            result = sites.save(session, draft, date.today(), actor)
            target = "contact" if result.kind in CONTACT_KINDS else "project"
            svc.record_audit(session, actor, AUDIT[result.kind], target=f"{target}:{result.id}")
            saved.append(result)
        return saved

    try:
        saved = await _with_session(context, work)
    except Exception:
        log.exception("saving sites failed", extra={"event": "db_error"})
        pending[token] = drafts  # nothing was saved (one transaction); let them try again
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    await query.answer("Saved.")
    counts = {kind: sum(s.kind == kind for s in saved) for kind in AUDIT}
    parts = [f"{counts[kind]} {word}" for kind, word in SUMMARY.items() if counts[kind]]
    lines = ["Saved: " + ", ".join(parts) + "."]
    for s in saved:
        ref = s.name if s.kind in CONTACT_KINDS else f"#{s.id} {s.name}"
        lines.append(f"- {ITEM[s.kind]} {ref}")
    projects = [s.id for s in saved if s.kind not in CONTACT_KINDS]
    if projects:
        first = projects[0]
        lines.append(
            f"Next: /project {first} · /analyze {first} audio · /draft {first} audio · /top"
        )
    else:
        lines.append("See them with /contacts (e.g. /contacts contractor Migaa).")
    await query.edit_message_text(truncate("\n".join(lines), MAX_MESSAGE))
