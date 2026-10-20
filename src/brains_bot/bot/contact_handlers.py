"""/contacts: the directory of contractors, interior designers, developers, ... by trade."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from brains_bot.bot.formatting import MAX_MESSAGE, truncate
from brains_bot.bot.handlers import _user_id, restricted
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.importers.normalize import mask_phone
from brains_bot.services import contacts
from brains_bot.services import project_service as svc

log = logging.getLogger(__name__)


# Words that only finish a trade's name ("interior designer", "quantity surveyor").
_TRADE_TAIL = {"designer", "designers", "manager", "managers", "surveyor", "surveyors", "/"}


def parse_args(args: list[str]) -> tuple[str | None, str]:
    """(trade, other words): '/contacts interior designer Karen' -> interior_designer, 'Karen'."""
    if not args:
        return None, ""
    trade, rest = contacts.trade_key(args[0]), args[1:]
    if trade is None and len(args) > 1:  # "site manager", "care taker"
        trade, rest = contacts.trade_key(" ".join(args[:2])), args[2:]
    if trade is None:
        return None, " ".join(args)
    while rest and rest[0].lower() in _TRADE_TAIL:
        rest = rest[1:]
    return trade, " ".join(rest)


def entry_lines(entry: contacts.Entry) -> list[str]:
    c = entry.contact
    details = " · ".join(x for x in (contacts.label(c.trade), c.company, c.area) if x)
    phone = mask_phone(c.phone_e164) if c.phone_e164 else "no number"
    lines = [f"• {c.name} · {details}", f"   {phone}"]
    if entry.projects:
        sites = ", ".join(f"#{pid} {name}" for pid, name in entry.projects[:3])
        more = f" +{len(entry.projects) - 3} more" if len(entry.projects) > 3 else ""
        lines.append(f"   Sites: {sites}{more}")
    if entry.last_note:
        when = f"{entry.last_contact}: " if entry.last_contact else ""
        lines.append(f"   {when}{truncate(contacts.note_for_display(entry.last_note), 160)}")
    return lines


def _usage(counts: list[tuple[str, int]]) -> str:
    known = ", ".join(f"{contacts.label(t)} {c}" for t, c in counts) or "none yet"
    return (
        "Usage: /contacts <trade> [area or name]\n"
        "Examples: /contacts contractor · /contacts interior designer Karen · "
        "/contacts Migaa\n\n"
        f"Known so far: {known}.\n"
        "Add people by pasting a note with /newsite, e.g. 'Met contractor Musa, "
        "Example Construction, Migaa 07..., good contact for future projects'."
    )


@restricted("contacts")
async def contacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = [a for a in (context.args or []) if a.strip()]
    trade, text = parse_args(args)
    actor = _user_id(update)

    def work(session: Session) -> str:
        if not args:
            return _usage(contacts.trade_counts(session))
        entries, total = contacts.directory(session, trade, text)
        svc.record_audit(session, actor, "contacts_list", results=total)
        what = contacts.label(trade) + "s" if trade else "Contacts"
        where = f" matching '{text}'" if text else ""
        if not entries:
            return f"No {what.lower()}{where} yet. Send /contacts to see what is known."
        lines = [f"{what}{where} ({total})", ""]
        for entry in entries:
            lines += entry_lines(entry)
        if total > len(entries):
            lines += ["", f"Showing the latest {len(entries)}. Add an area to narrow it down."]
        return "\n".join(lines)

    try:
        reply = await _with_session(context, work)
    except Exception:
        log.exception("contacts list failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(truncate(reply, MAX_MESSAGE))
