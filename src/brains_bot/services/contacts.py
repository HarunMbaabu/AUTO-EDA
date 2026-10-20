"""The contacts directory: people grouped by trade (contractors, interior designers, ...).

Everyone the bot knows is a contact: people named on sites, and people met for future work who
have no site yet. A trade, company and area make them findable (/contacts, chat, dashboard).
The same person is recognised by phone number, so new notes join their existing entry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brains_bot.database.models import (
    Contact,
    ContactNote,
    MarketingPermission,
    Project,
    ProjectContact,
)
from brains_bot.importers import normalize as n
from brains_bot.services import project_service as svc

TRADES = {
    "contractor": "Contractor",
    "developer": "Developer",
    "interior_designer": "Interior designer",
    "architect": "Architect",
    "painter": "Painter / fundi",
    "site_manager": "Site manager",
    "engineer": "Engineer",
    "foreman": "Foreman",
    "quantity_surveyor": "Quantity surveyor",
    "caretaker": "Caretaker",
    "owner": "Owner / client",
    "supplier": "Supplier / dealer",
    "other": "Other",
}
# Checked in order: the first keyword found in a role description decides the trade.
_KEYWORDS = [
    ("interior", "interior_designer"), ("paint", "painter"), ("fundi", "painter"),
    ("develop", "developer"), ("architect", "architect"), ("site manager", "site_manager"),
    ("project manager", "site_manager"), ("engineer", "engineer"), ("foreman", "foreman"),
    ("surveyor", "quantity_surveyor"), ("care taker", "caretaker"), ("caretaker", "caretaker"),
    ("owner", "owner"), ("client", "owner"), ("supplier", "supplier"), ("dealer", "supplier"),
    ("hardware", "supplier"), ("contractor", "contractor"), ("builder", "contractor"),
    ("construction", "contractor"),
]  # fmt: skip
LIST_LIMIT = 25


def trade_key(value: str | None) -> str | None:
    """'paint contractor' -> painter, 'Interior Designer' -> interior_designer, ..."""
    if not value:
        return None
    text = " ".join(value.lower().replace("_", " ").split())
    if text.replace(" ", "_") in TRADES:
        return text.replace(" ", "_")
    for word, key in _KEYWORDS:
        if word in text:
            return key
    return None


def label(trade: str | None) -> str:
    return TRADES.get(trade or "", "Trade not recorded")


def by_phone(session: Session, numbers: list[str]) -> Contact | None:
    if not numbers:
        return None
    return session.scalar(
        select(Contact)
        .where(
            or_(
                Contact.phone_e164.in_(numbers),
                *[Contact.phone_candidates.contains([number]) for number in numbers],
            )
        )
        .order_by(Contact.id)
        .limit(1)
    )


def fill(contact: Contact, trade: str | None, company: str | None, area: str | None) -> None:
    """Add what is newly known; never overwrite what was recorded before."""
    contact.trade = contact.trade or trade
    contact.company = contact.company or company
    contact.area = contact.area or area


def save_contact(
    session: Session,
    name: str | None,
    phone: n.PhoneResult,
    trade: str | None,
    company: str | None,
    area: str | None,
    note: str,
    on: date,
    actor: int | None = None,
) -> tuple[Contact, bool]:
    """(contact, is_new). A known phone number adds the note to that person's entry."""
    contact = by_phone(session, phone.numbers)
    is_new = contact is None
    if contact is None:
        key = (
            f"person:{phone.e164}:{n.name_key(name or '')}"
            if phone.e164
            else f"network:{uuid.uuid4().hex[:12]}"
        )
        contact = Contact(contact_key=key, name=name or company or "Contact")
        session.add(contact)
        if phone.numbers:
            contact.phone_raw = phone.raw
            contact.phone_e164 = phone.e164
            contact.phone_status = phone.status
            contact.phone_candidates = phone.numbers
        session.flush()
        if contact.phone_candidates:
            for channel in ("whatsapp", "sms"):
                session.add(MarketingPermission(contact_id=contact.id, channel=channel))
    fill(contact, trade, company, area)
    session.add(ContactNote(contact_id=contact.id, text=note, observed_date=on, created_by=actor))
    session.flush()
    return contact, is_new


@dataclass
class Entry:
    contact: Contact
    projects: list[tuple[int, str]] = field(default_factory=list)
    last_note: str | None = None
    last_note_on: date | None = None


def directory(
    session: Session, trade: str | None = None, text: str | None = None, limit: int = LIST_LIMIT
) -> tuple[list[Entry], int]:
    """(entries, total) for people with a recorded trade, filtered by trade and by words in
    their name, company, area or projects' areas. Most recently noted first."""
    query = select(Contact).where(Contact.trade.is_not(None))
    if trade:
        query = query.where(Contact.trade == trade)
    for word in (text or "").split():
        pattern = svc.like_pattern(word)
        site_match = or_(
            Project.area.ilike(pattern, escape="\\"), Project.name.ilike(pattern, escape="\\")
        )
        in_projects = (
            select(ProjectContact.contact_id)
            .join(Project, Project.id == ProjectContact.project_id)
            .where(site_match)
        )
        query = query.where(
            or_(
                Contact.name.ilike(pattern, escape="\\"),
                Contact.company.ilike(pattern, escape="\\"),
                Contact.area.ilike(pattern, escape="\\"),
                Contact.id.in_(in_projects),
            )
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    latest = (
        select(ContactNote.contact_id, func.max(ContactNote.id).label("note_id"))
        .group_by(ContactNote.contact_id)
        .subquery()
    )
    rows = session.execute(
        query.add_columns(ContactNote.text, ContactNote.observed_date)
        .outerjoin(latest, latest.c.contact_id == Contact.id)
        .outerjoin(ContactNote, ContactNote.id == latest.c.note_id)
        .order_by(ContactNote.observed_date.desc().nulls_last(), Contact.id.desc())
        .limit(limit)
    ).all()
    entries = [Entry(c, last_note=t, last_note_on=d) for c, t, d in rows]
    ids = [e.contact.id for e in entries]
    links = session.execute(
        select(ProjectContact.contact_id, Project.id, Project.name)
        .join(Project, Project.id == ProjectContact.project_id)
        .where(ProjectContact.contact_id.in_(ids))
        .distinct()
    ).all()
    by_contact = {e.contact.id: e for e in entries}
    for contact_id, project_id, name in links:
        by_contact[contact_id].projects.append((project_id, name))
    return entries, total


def trade_counts(session: Session) -> list[tuple[str, int]]:
    rows = session.execute(
        select(Contact.trade, func.count())
        .where(Contact.trade.is_not(None))
        .group_by(Contact.trade)
    ).all()
    return sorted(((t, c) for t, c in rows), key=lambda tc: -tc[1])
