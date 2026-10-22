"""Repeat sites: the same project entered twice, e.g. because the contact gave a Safaricom number
on one visit and an Airtel number on the next.

Two ways to spot them: the same contact number on two projects, or the same contact first name
in the same area. Nothing is merged automatically; /merge moves one project into the other.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from brains_bot.database.models import (
    Contact,
    OpportunityAssessment,
    OutreachDraft,
    Project,
    ProjectAlias,
    ProjectContact,
    ProjectNote,
    Quote,
    SalesAssignment,
)
from brains_bot.importers import normalize as n

# Only the people a site is followed up with; an architect or contractor works on many sites.
PERSON_ROLES = ("contact_person", "owner")
# Words that say nothing about where a site is.
_PLAIN_WORDS = {"road", "estate", "area", "phase", "site", "court", "near", "behind", "along",
                "next", "opposite", "nairobi", "project", "villa", "villas", "house", "home",
                "apartments", "apartment", "plot", "the", "and", "off"}  # fmt: skip
MERGED_FIELD = "Merged"
MAX_GROUPS = 15


def first_name(name: str | None) -> str | None:
    words = n.name_key(name).split()
    return words[0] if words and len(words[0]) >= 3 else None


def place_words(*values: str | None) -> set[str]:
    words = set(n.name_key(" ".join(v for v in values if v)).split())
    return {w for w in words if len(w) >= 4 and w not in _PLAIN_WORDS and not w.isdigit()}


def same_name_and_area(
    session: Session, name: str | None, *places: str | None
) -> tuple[int, str] | None:
    """The latest project whose contact or owner has this first name, in the same area. The
    places are the new entry's area, site name and message: the AI does not always fill in
    the area ("Wanza from Migaa")."""
    first = first_name(name)
    wanted = place_words(*places) - {first}
    if first is None or not wanted:
        return None
    rows = session.execute(
        select(Project.id, Project.name, Project.area, Project.address, Contact.name)
        .join(ProjectContact, ProjectContact.project_id == Project.id)
        .join(Contact, Contact.id == ProjectContact.contact_id)
        .where(ProjectContact.role.in_(PERSON_ROLES), Contact.name.ilike(f"{first}%"))
        .order_by(Project.site_visit_date.desc().nulls_last(), Project.id.desc())
    ).all()
    for project_id, project_name, project_area, address, contact_name in rows:
        if first_name(contact_name) == first and wanted & (
            place_words(project_area, address, project_name) - {first}
        ):
            return project_id, project_name
    return None


@dataclass
class Group:
    projects: list[tuple[int, str]]  # oldest first: the suggested one to keep
    reason: str


def find_repeats(session: Session) -> list[Group]:
    """Projects that look like the same site entered more than once."""
    rows = session.execute(
        select(
            Project.id,
            Project.name,
            Project.area,
            Contact.name,
            Contact.phone_e164,
            Contact.phone_candidates,
        )  # fmt: skip
        .join(ProjectContact, ProjectContact.project_id == Project.id)
        .join(Contact, Contact.id == ProjectContact.contact_id)
        .where(ProjectContact.role.in_(PERSON_ROLES))
    ).all()
    names: dict[int, str] = {}
    by_phone: dict[str, set[int]] = defaultdict(set)
    by_person: dict[tuple[str, str], set[int]] = defaultdict(set)
    for project_id, project_name, area, contact_name, e164, candidates in rows:
        names[project_id] = project_name
        for number in {e164, *(candidates or [])} - {None}:
            by_phone[number].add(project_id)
        first, place = first_name(contact_name), n.name_key(area)
        if first and place:
            by_person[(first, place)].add(project_id)

    groups: list[Group] = []
    seen: set[frozenset[int]] = set()

    def add(ids: set[int], reason: str) -> None:
        key = frozenset(ids)
        if len(ids) > 1 and key not in seen:
            seen.add(key)
            groups.append(Group([(i, names[i]) for i in sorted(ids)], reason))

    for number, ids in by_phone.items():
        add(ids, f"same number {n.format_phone(number)}")
    for (first, place), ids in by_person.items():
        add(ids, f"same name '{first.title()}' in {place.title()}")
    return groups


def merge(
    session: Session, repeat_id: int, keep_id: int, today: date, actor: int | None = None
) -> Project:
    """Move everything recorded on the repeat into the project kept, then delete the repeat.
    A workbook project merged away is remembered, so later imports do not bring it back."""
    if repeat_id == keep_id:
        raise ValueError("a project cannot be merged into itself")
    repeat, keep = session.get(Project, repeat_id), session.get(Project, keep_id)
    if repeat is None or keep is None:
        raise LookupError(f"project #{repeat_id if repeat is None else keep_id} not found")

    # Notes move without their workbook row, so a later import of the kept project keeps them.
    session.execute(
        update(ProjectNote)
        .where(ProjectNote.project_id == repeat_id)
        .values(project_id=keep_id, source_record_id=None)
    )
    have = set(
        session.execute(
            select(ProjectContact.contact_id, ProjectContact.role).where(
                ProjectContact.project_id == keep_id
            )
        ).all()
    )
    for link in session.scalars(
        select(ProjectContact).where(ProjectContact.project_id == repeat_id)
    ):
        if (link.contact_id, link.role) in have:
            session.delete(link)
        else:
            link.project_id, link.manual = keep_id, True
    for model in (OpportunityAssessment, OutreachDraft, Quote):
        session.execute(
            update(model).where(model.project_id == repeat_id).values(project_id=keep_id)
        )
    if session.scalar(select(SalesAssignment.id).where(SalesAssignment.project_id == keep_id)):
        session.execute(delete(SalesAssignment).where(SalesAssignment.project_id == repeat_id))
    else:
        session.execute(
            update(SalesAssignment)
            .where(SalesAssignment.project_id == repeat_id)
            .values(project_id=keep_id)
        )
    for column in ("area", "city", "address", "google_location_url", "est_paint_start",
                   "site_visit_date"):  # fmt: skip
        if getattr(keep, column) is None:
            setattr(keep, column, getattr(repeat, column))
    session.add(
        ProjectNote(
            project_id=keep_id,
            field=MERGED_FIELD,
            text=f"Merged repeat entry #{repeat_id} {repeat.name} into this project.",
            observed_date=today,
        )
    )
    session.execute(
        update(ProjectAlias).where(ProjectAlias.project_id == repeat_id).values(project_id=keep_id)
    )
    if not repeat.source_key.startswith("tg:"):
        session.add(ProjectAlias(source_key=repeat.source_key, project_id=keep_id, merged_by=actor))
    session.flush()
    # Plain SQL, so the repeat's (now moved) notes cached in the session are not deleted with it.
    session.execute(delete(Project).where(Project.id == repeat_id))
    session.expire_all()
    return keep
