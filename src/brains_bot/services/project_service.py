"""Read-only project queries used by the bot. Synchronous; the bot runs them in a thread."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from brains_bot.database.models import (
    AuditEvent,
    Contact,
    MarketingPermission,
    Project,
    ProjectContact,
    SourceRecord,
)

PAGE_SIZE = 10
SEARCH_LIMIT = 15
ROLE_ORDER = ("owner", "contractor", "architect", "company", "contact_person")


@dataclass
class ProjectSummary:
    id: int
    name: str
    area: str | None
    city: str | None
    site_visit_date: date | None
    needs_review: bool


@dataclass
class ContactView:
    role: str
    name: str
    phone_e164: str | None
    phone_status: str
    phone_count: int
    consent: str  # summary of marketing permission statuses


@dataclass
class ProjectDetail:
    summary: ProjectSummary
    address: str | None
    google_location_url: str | None
    est_paint_start: date | None
    salesperson: str | None
    sar: str | None
    dealer_to_route: str | None
    site_visited_by_dealer: str | None
    quotation_shared: str | None
    won_lost: str | None
    quote_value_kes: Decimal | None
    supplying_dealer: str | None
    tracking_status: str | None
    contacts: list[ContactView] = field(default_factory=list)
    notes: list[tuple[date | None, str]] = field(default_factory=list)
    note_count: int = 0
    review_reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _summary(p: Project) -> ProjectSummary:
    return ProjectSummary(p.id, p.name, p.area, p.city, p.site_visit_date, p.needs_review)


def _ordered():
    return select(Project).order_by(Project.site_visit_date.desc().nulls_last(), Project.id.desc())


def list_projects(session: Session, page: int) -> tuple[list[ProjectSummary], int]:
    """Return one page (1-based) of projects, most recent visit first, and the page count."""
    total = session.scalar(select(func.count(Project.id))) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = session.scalars(_ordered().offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
    return [_summary(p) for p in rows], pages


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_projects(session: Session, term: str) -> list[ProjectSummary]:
    """Match site name, area, city, address, or the names of linked people and companies.
    Phone numbers are deliberately not searchable."""
    pattern = _like(term.strip())
    contact_match = (
        select(ProjectContact.project_id)
        .join(Contact, Contact.id == ProjectContact.contact_id)
        .where(Contact.name.ilike(pattern, escape="\\"))
    )
    query = _ordered().where(
        or_(
            Project.name.ilike(pattern, escape="\\"),
            Project.area.ilike(pattern, escape="\\"),
            Project.city.ilike(pattern, escape="\\"),
            Project.address.ilike(pattern, escape="\\"),
            Project.id.in_(contact_match),
        )
    )
    return [_summary(p) for p in session.scalars(query.limit(SEARCH_LIMIT))]


def _consent(session: Session, contact_id: int) -> str:
    statuses = session.scalars(
        select(MarketingPermission.status).where(MarketingPermission.contact_id == contact_id)
    ).all()
    if not statuses:
        return "unknown"
    return "/".join(sorted(set(statuses)))


def get_project(session: Session, project_id: int, note_limit: int = 3) -> ProjectDetail | None:
    project = session.scalar(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.contacts).selectinload(ProjectContact.contact),
            selectinload(Project.notes),
            selectinload(Project.sales),
        )
    )
    if project is None:
        return None
    sales = project.sales
    detail = ProjectDetail(
        summary=_summary(project),
        address=project.address,
        google_location_url=project.google_location_url,
        est_paint_start=project.est_paint_start,
        salesperson=sales.salesperson if sales else None,
        sar=sales.sar if sales else None,
        dealer_to_route=sales.dealer_to_route if sales else None,
        site_visited_by_dealer=sales.site_visited_by_dealer if sales else None,
        quotation_shared=sales.quotation_shared if sales else None,
        won_lost=sales.won_lost if sales else None,
        quote_value_kes=sales.quote_value_kes if sales else None,
        supplying_dealer=sales.supplying_dealer if sales else None,
        tracking_status=sales.tracking_status if sales else None,
        review_reasons=list(project.review_reasons),
        note_count=len(project.notes),
    )
    links = sorted(project.contacts, key=lambda pc: ROLE_ORDER.index(pc.role))
    for link in links:
        c = link.contact
        detail.contacts.append(
            ContactView(
                role=link.role,
                name=c.name,
                phone_e164=c.phone_e164,
                phone_status=c.phone_status,
                phone_count=len(c.phone_candidates),
                consent=_consent(session, c.id) if link.role == "contact_person" else "",
            )
        )
    notes = sorted(project.notes, key=lambda nt: (nt.observed_date or date.min, nt.id))
    detail.notes = [(nt.observed_date, nt.text) for nt in notes[-note_limit:]][::-1]

    latest_batch = session.scalar(
        select(func.max(SourceRecord.batch_id)).where(SourceRecord.project_id == project.id)
    )
    records = session.scalars(
        select(SourceRecord).where(
            SourceRecord.project_id == project.id, SourceRecord.batch_id == latest_batch
        )
    ).all()
    sheet_order = ("Data Base", "Active Sheet", "To share with Dealer")
    records.sort(key=lambda r: (sheet_order.index(r.sheet) if r.sheet in sheet_order else 9, r.id))
    for r in records:
        rows = (
            f"row {r.first_row}"
            if r.first_row == r.last_row
            else f"rows {r.first_row}-{r.last_row}"
        )
        sid = f", ID {r.source_id}" if r.source_id is not None else ", no ID"
        method = (
            " - matched by content, please confirm" if r.match_method == "content_match" else ""
        )
        detail.sources.append(f"{r.sheet} {rows}{sid}{method}")
    return detail


def record_audit(
    session: Session, actor: int | None, action: str, target: str | None = None, **details
) -> None:
    """Who looked at what. Never store search terms, phone numbers or note text here."""
    session.add(AuditEvent(actor_telegram_id=actor, action=action, target=target, details=details))
