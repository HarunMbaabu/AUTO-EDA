"""The contacts directory: people grouped by trade (contractors, interior designers, ...).

Everyone the bot knows is a contact: people named on sites, and people met for future work who
have no site yet. A trade, company and area make them findable (/contacts, chat, dashboard).
The same person is recognised by phone number, so new notes join their existing entry.
"""

from __future__ import annotations

import re
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
    ProjectNote,
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
    # From the workbook, where only the column a name was in is known:
    "site_contact": "Site contact (role not stated)",
    "company": "Company (role not stated)",
}
# Placeholders that a more specific trade from a later note replaces.
GENERIC = {"site_contact", "company", "other"}
# The workbook column / project role a person was named in -> their trade.
ROLE_TRADES = {"contractor": "contractor", "architect": "architect", "owner": "owner",
               "company": "company", "contact_person": "site_contact"}  # fmt: skip
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
    """Add what is newly known; never overwrite what was recorded before (except a placeholder
    trade such as 'site contact', which a specific one replaces)."""
    if trade and (contact.trade is None or (contact.trade in GENERIC and trade not in GENERIC)):
        contact.trade = trade
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
    last_how: str | None = None  # "Met", "Called", "Messaged", "Site visit", "Noted"

    @property
    def last_contact(self) -> str | None:
        """'Met on 15 Feb 2026', 'Called on 4 Feb 2026', ... for the latest note."""
        if not self.last_note_on:
            return self.last_how
        return f"{self.last_how or 'Noted'} on {self.last_note_on:%d %b %Y}"


_CALLED = re.compile(r"\b(call(ed|ing)?|phoned?|spoke|talked)\b", re.IGNORECASE)
_MESSAGED = re.compile(r"\b(whats ?app(ed)?|texted|sms|messaged)\b", re.IGNORECASE)
_MET = re.compile(r"\b(met|meet(ing)?|visit(ed)?|saw|site)\b", re.IGNORECASE)


def how_contacted(note: str) -> str:
    """How the person was reached, read from the note's own words."""
    for pattern, how in ((_CALLED, "Called"), (_MESSAGED, "Messaged"), (_MET, "Met")):
        if pattern.search(note):
            return how
    return "Noted"


def directory(
    session: Session,
    trade: str | None = None,
    text: str | None = None,
    limit: int = LIST_LIMIT,
    offset: int = 0,
) -> tuple[list[Entry], int]:
    """(entries, total) for people with a recorded trade, filtered by trade and by words in
    their name, company, area or projects' areas. Most recent note or site visit first."""
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
    visited = (
        select(ProjectContact.contact_id, func.max(Project.site_visit_date).label("on"))
        .join(Project, Project.id == ProjectContact.project_id)
        .group_by(ProjectContact.contact_id)
        .subquery()
    )
    recent = func.coalesce(ContactNote.observed_date, visited.c.on)
    rows = session.execute(
        query.add_columns(ContactNote.text, ContactNote.observed_date)
        .outerjoin(latest, latest.c.contact_id == Contact.id)
        .outerjoin(ContactNote, ContactNote.id == latest.c.note_id)
        .outerjoin(visited, visited.c.contact_id == Contact.id)
        .order_by(recent.desc().nulls_last(), Contact.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    entries = [
        Entry(c, last_note=t, last_note_on=d, last_how=how_contacted(t) if t else None)
        for c, t, d in rows
    ]
    ids = [e.contact.id for e in entries]
    _add_site_notes(session, [e for e in entries if e.last_note is None])
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


def _add_site_notes(session: Session, entries: list[Entry]) -> None:
    """People from the workbook have no notes of their own: show their site's latest visit."""
    if not entries:
        return
    ranked = (
        select(
            ProjectContact.contact_id,
            ProjectNote.text,
            func.coalesce(ProjectNote.observed_date, Project.site_visit_date).label("on"),
            func.row_number()
            .over(
                partition_by=ProjectContact.contact_id,
                order_by=(
                    func.coalesce(ProjectNote.observed_date, Project.site_visit_date)
                    .desc()
                    .nulls_last(),
                    ProjectNote.id.desc(),
                ),
            )
            .label("n"),
        )
        .join(Project, Project.id == ProjectContact.project_id)
        .join(ProjectNote, ProjectNote.project_id == Project.id)
        .where(ProjectContact.contact_id.in_([e.contact.id for e in entries]))
        .subquery()
    )
    by_contact = {e.contact.id: e for e in entries}
    for contact_id, text, on in session.execute(
        select(ranked.c.contact_id, ranked.c.text, ranked.c.on).where(ranked.c.n == 1)
    ):
        entry = by_contact[contact_id]
        entry.last_note, entry.last_note_on, entry.last_how = text, on, "Site visit"


def note_for_display(text: str | None) -> str | None:
    """A note with any phone number in it masked like everywhere else (+254 7XX XXX 545)."""
    if not text:
        return text

    def mask(match: re.Match[str]) -> str:
        e164 = n.normalize_phone(match[0]).e164
        return n.mask_phone(e164) if e164 else "[phone]"

    return n.PHONE_IN_TEXT.sub(mask, text)


def trade_counts(session: Session) -> list[tuple[str, int]]:
    rows = session.execute(
        select(Contact.trade, func.count())
        .where(Contact.trade.is_not(None))
        .group_by(Contact.trade)
    ).all()
    return sorted(((t, c) for t, c in rows), key=lambda tc: -tc[1])
