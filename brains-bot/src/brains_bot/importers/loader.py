"""Write reconciled workbook records to PostgreSQL. Safe to re-run (idempotent)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from brains_bot.database.models import (
    Contact,
    DealerShop,
    ImportBatch,
    MarketingPermission,
    Project,
    ProjectContact,
    ProjectNote,
    SalesAssignment,
    SourceRecord,
)
from brains_bot.importers import normalize as n
from brains_bot.importers.reconcile import ProjectDraft, Reconciliation, reconcile
from brains_bot.importers.workbook import DEALER_SHEETS, SourceRow, Workbook, read_workbook

ROLE_COLUMNS = {
    "owner": "Name of Owners",
    "contractor": "Contractor",
    "architect": "Architect",
    "company": "Name of Company",
    "contact_person": "Contact Person Name",
}
ORGANISATION_ROLES = {"contractor", "company"}
# "Audio Status" in the slimmed-down export holds the same site/paint notes as "Comments (If Any)"
# in the full export; it is imported as a note, never as evidence of interest in audio.
NOTE_COLUMNS = ("Comments (If Any)", "Audio Status", "Jotun Comments")
TRACKING_COLUMNS = {
    "site_visited_by_dealer": "Site Visited by Dealer/ODS",
    "quotation_shared": "Quotation Shared",
    "won_lost": "Won/ Lost",
    "first_order_received": "First Order received",
    "supplying_dealer": "Supplying Dealer Name",
    "tracking_status": "Status",
}
# Sales tracking is kept up to date in Active Sheet; the dealer sheet is the fallback.
TRACKING_SHEET_ORDER = ("Active Sheet", "To share with Dealer", "Data Base")


@dataclass
class ImportStats:
    batch_id: int | None = None
    skipped_reason: str | None = None
    source_rows: Counter[str] = field(default_factory=Counter)
    issues: Counter[str] = field(default_factory=Counter)
    phone_status: Counter[str] = field(default_factory=Counter)
    aliases: Counter[str] = field(default_factory=Counter)
    conflicts: list[str] = field(default_factory=list)
    date_issues: list[str] = field(default_factory=list)
    projects: int = 0
    flagged: list[tuple[str, str, list[str]]] = field(default_factory=list)
    contacts: int = 0
    dealer_shops: int = 0
    dealer_duplicates: int = 0
    won_lost_by_dealer: dict[str, Counter[str]] = field(default_factory=dict)
    stale: list[tuple[str, str]] = field(default_factory=list)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first(rows: list[SourceRow], column: str) -> tuple[Any, SourceRow | None]:
    for row in rows:
        if not n.is_placeholder(row.get(column)):
            return row.get(column), row
    return None, None


def _where(row: SourceRow) -> str:
    return f"{row.sheet} row {row.first_row}"


def load_aliases(path: Path) -> dict[str, str]:
    """Salesperson name corrections from a private JSON file: {"salesperson": {"Jane": ...}}.
    Kept out of Git because it holds staff names."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    return {str(k): str(v) for k, v in data.get("salesperson", {}).items()}


class Loader:
    def __init__(
        self,
        session: Session,
        stats: ImportStats,
        batch: ImportBatch,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.session = session
        self.stats = stats
        self.batch = batch
        self.aliases = aliases or {}
        self.source_ids: dict[tuple[str, int], int] = {}

    # -- source records --------------------------------------------------------------------

    def add_source_records(self, workbook: Workbook) -> None:
        for sheet, rows in workbook.sheets.items():
            for row in rows:
                self.stats.source_rows[sheet] += 1
                record = SourceRecord(
                    batch_id=self.batch.id,
                    sheet=sheet,
                    first_row=row.first_row,
                    last_row=row.last_row,
                    source_id=row.source_id,
                    raw=row.raw(),
                    issues=list(row.issues),
                )
                self.session.add(record)
                self.session.flush()
                self.source_ids[(sheet, row.first_row)] = record.id
                for issue in row.issues:
                    self.stats.issues[issue] += 1

    def _record_id(self, row: SourceRow) -> int:
        return self.source_ids[(row.sheet, row.first_row)]

    # -- projects --------------------------------------------------------------------------

    def upsert_project(self, draft: ProjectDraft) -> Project:
        rows = draft.rows()
        primary = draft.primary
        project = self.session.scalar(select(Project).where(Project.source_key == draft.source_key))
        if project is None:
            project = Project(source_key=draft.source_key)
            self.session.add(project)

        name, _ = _first(rows, "Name of Potential Site")
        project.source_id = primary.source_id if primary.sheet == "Data Base" else None
        project.name = n.clean_text(name) or "(unnamed site)"
        project.area = n.meaningful(_first(rows, "Name of Area (Location) of Site")[0])
        project.city = n.normalize_city(_first(rows, "City")[0])
        project.address = n.meaningful(_first(rows, "Address")[0])
        url = n.clean_text(_first(rows, "Google Location")[0])
        project.google_location_url = url if url and url.startswith("http") else None
        project.site_visit_date = self._date(primary, rows, "Date of Site Visit")
        project.est_paint_start = self._date(primary, rows, "Estimated Paint Start Time Line")
        reasons = list(draft.review_reasons) + self._check_dates(primary, project)
        project.review_reasons = reasons
        project.needs_review = bool(reasons)
        project.last_batch_id = self.batch.id
        self._record_name_variants(draft)
        self.session.flush()

        for row in rows:
            record = self.session.get(SourceRecord, self._record_id(row))
            record.project_id = project.id
            record.match_method = next(lk.method for lk in draft.links if lk.row is row)

        # Child rows are derived entirely from the workbook, so rebuild them on every import.
        self.session.execute(delete(ProjectContact).where(ProjectContact.project_id == project.id))
        self.session.execute(delete(ProjectNote).where(ProjectNote.project_id == project.id))
        self.session.execute(
            delete(SalesAssignment).where(SalesAssignment.project_id == project.id)
        )
        self.session.flush()
        self._add_contacts(project, draft)
        self._add_notes(project, draft)
        self._add_sales(project, draft)
        self.stats.projects += 1
        if reasons:
            self.stats.flagged.append((draft.source_key, project.name, reasons))
        return project

    def _date(self, primary: SourceRow, rows: list[SourceRow], column: str):
        for row in [primary, *[r for r in rows if r is not primary]]:
            value, issue = n.parse_date(row.get(column))
            if issue:
                self.stats.date_issues.append(f"{_where(row)}, {column}: {issue}")
            if value:
                return value
        return None

    def _check_dates(self, row: SourceRow, project: Project) -> list[str]:
        problems: list[str] = []
        visit = project.site_visit_date
        submitted, _ = n.parse_datetime(row.get("Start time"))
        if visit and submitted:
            gap = (submitted.date() - visit).days
            if gap < -1 or gap > 120:
                problems.append(
                    f"site visit date {visit.isoformat()} is {abs(gap)} days "
                    f"{'after' if gap < 0 else 'before'} the form was submitted "
                    f"({submitted.date().isoformat()}); please check this date ({_where(row)})"
                )
        estimate = project.est_paint_start
        if visit and estimate and (visit - estimate).days > 30:
            problems.append(
                f"estimated paint start {estimate.isoformat()} is before the site visit "
                f"({visit.isoformat()}); possibly typed day-first. Please check ({_where(row)})"
            )
        self.stats.date_issues.extend(problems)
        return problems

    def _record_name_variants(self, draft: ProjectDraft) -> None:
        names = {n.clean_text(r.get("Name of Potential Site")) for r in draft.rows()} - {None}
        if len(names) > 1:
            self.stats.conflicts.append(f"{draft.source_key}: site name varies: {sorted(names)}")

    # -- contacts --------------------------------------------------------------------------

    def _add_contacts(self, project: Project, draft: ProjectDraft) -> None:
        rows = draft.rows()
        for role, column in ROLE_COLUMNS.items():
            value, row = _first(rows, column)
            name = n.meaningful(value)
            if name is None or n.name_key(name) in {"client", "client s", "working with client"}:
                continue
            phone = (
                n.normalize_phone(row.get("Contact Details")) if role == "contact_person" else None
            )
            contact = self._upsert_contact(name, role, phone, draft.source_key)
            self.session.add(
                ProjectContact(project_id=project.id, contact_id=contact.id, role=role)
            )
        self.session.flush()

    def _upsert_contact(
        self, name: str, role: str, phone: n.PhoneResult | None, project_key: str
    ) -> Contact:
        key_name = n.name_key(name)
        if role in ORGANISATION_ROLES:
            key = f"org:{key_name}"  # companies are shared across projects by name
        elif phone and phone.e164:
            key = f"person:{phone.e164}:{key_name}"  # same number and same name
        else:
            key = f"{role}:{project_key}:{key_name}"  # never merged across projects
        contact = self.session.scalar(select(Contact).where(Contact.contact_key == key))
        if contact is None:
            contact = Contact(contact_key=key, name=name)
            self.session.add(contact)
            self.stats.contacts += 1
        if phone is not None:
            contact.phone_raw = phone.raw
            contact.phone_e164 = phone.e164
            contact.phone_status = phone.status
            contact.phone_candidates = phone.numbers
            self.stats.phone_status[phone.status] += 1
        self.session.flush()
        if contact.phone_candidates:
            self._ensure_permissions(contact)
        return contact

    def _ensure_permissions(self, contact: Contact) -> None:
        for channel in ("whatsapp", "sms"):
            exists = self.session.scalar(
                select(MarketingPermission).where(
                    MarketingPermission.contact_id == contact.id,
                    MarketingPermission.channel == channel,
                )
            )
            if exists is None:  # never overwrite a status someone has recorded since
                self.session.add(
                    MarketingPermission(contact_id=contact.id, channel=channel, status="unknown")
                )

    # -- notes and sales -------------------------------------------------------------------

    def _add_notes(self, project: Project, draft: ProjectDraft) -> None:
        candidates: list[tuple[str, str, SourceRow]] = []  # (column, text, row)
        for row in draft.rows():
            entries = [(c, row.get(c)) for c in NOTE_COLUMNS]
            entries += [(c, v) for c, v in row.extra if c in NOTE_COLUMNS]
            for column, value in entries:
                text = value.strip() if isinstance(value, str) else n.clean_text(value)
                if text:
                    candidates.append((column, text, row))

        # The same comment is often copied between sheets, sometimes with more text added.
        # Keep one copy, and drop a note whose text is contained in a longer one.
        kept: list[tuple[str, str, SourceRow]] = []
        for column, text, row in sorted(candidates, key=lambda c: -len(c[1])):
            key = n.name_key(text)
            if any(key in n.name_key(other) for _, other, _ in kept):
                continue
            kept.append((column, text, row))
        kept.sort(key=lambda c: candidates.index(c))  # back to workbook order

        for column, text, row in kept:
            observed, _ = n.parse_date(row.get("Date of Site Visit"))
            self.session.add(
                ProjectNote(
                    project_id=project.id,
                    source_record_id=self._record_id(row),
                    field=column,
                    text=text,
                    observed_date=observed,
                )
            )

    def _add_sales(self, project: Project, draft: ProjectDraft) -> None:
        ordered = sorted(
            draft.rows(),
            key=lambda r: (
                TRACKING_SHEET_ORDER.index(r.sheet) if r.sheet in TRACKING_SHEET_ORDER else 99
            ),
        )
        primary_first = [draft.primary, *[r for r in draft.rows() if r is not draft.primary]]
        raw_sp, _ = _first(primary_first, "Name of Salesperson")
        salesperson = n.normalize_salesperson(raw_sp, self.aliases)
        if salesperson.alias_applied:
            self.stats.aliases[f"{salesperson.alias_applied} -> {salesperson.name}"] += 1

        sales = SalesAssignment(
            project_id=project.id,
            salesperson=salesperson.name,
            salesperson_raw=n.clean_text(raw_sp),
            sar=n.meaningful(_first(primary_first, "SAR")[0]),
            dealer_to_route=n.normalize_business_name(_first(primary_first, "Dealer To Route")[0]),
        )
        for attr, column in TRACKING_COLUMNS.items():
            clean = n.normalize_business_name if attr == "supplying_dealer" else n.normalize_choice
            values = {r.sheet: clean(r.get(column)) for r in ordered if clean(r.get(column))}
            if values:
                chosen = next(iter(values.values()))
                setattr(sales, attr, chosen)
                if len(set(values.values())) > 1:
                    self.stats.conflicts.append(
                        f"{draft.source_key}: {column} differs between sheets: {values}"
                    )
        for row in ordered:
            amount, issue = n.parse_amount(row.get("Quote Value"))
            if issue:
                self.stats.issues[issue] += 1
            if amount is not None:
                sales.quote_value_kes = amount
                break
        self.session.add(sales)
        dealer = sales.dealer_to_route or "(not routed)"
        if sales.won_lost:
            self.stats.won_lost_by_dealer.setdefault(dealer, Counter())[sales.won_lost] += 1

    # -- dealer shops ----------------------------------------------------------------------

    def load_dealer_shops(self, workbook: Workbook) -> None:
        seen: dict[str, DealerShop] = {}
        for sheet in DEALER_SHEETS:
            for row in workbook.sheets.get(sheet, []):
                name = n.meaningful(row.get("Brand Ambasador Shop"))
                if name is None:
                    continue
                exact = n.meaningful(row.get("Exact Location"))
                key = f"{n.name_key(name)}|{n.name_key(exact)}"
                shop = seen.get(key) or self.session.scalar(
                    select(DealerShop).where(DealerShop.shop_key == key)
                )
                if shop is None:
                    shop = DealerShop(shop_key=key, name=name, source_sheets=[])
                    self.session.add(shop)
                elif key in seen:
                    self.stats.dealer_duplicates += 1
                shop.general_location = n.meaningful(row.get("General Location"))
                shop.exact_location = exact
                if sheet not in shop.source_sheets:
                    shop.source_sheets = [*shop.source_sheets, sheet]
                seen[key] = shop
        self.stats.dealer_shops = len(seen)


STALE_REASON = "not found in the latest import; it may have been removed from the workbook"


def _flag_stale_projects(session: Session, stats: ImportStats, batch: ImportBatch) -> None:
    """Projects from earlier imports are kept (history may point at them), but flagged."""
    stale = session.scalars(
        select(Project).where((Project.last_batch_id != batch.id) | Project.last_batch_id.is_(None))
    )
    for project in stale:
        if STALE_REASON not in project.review_reasons:
            project.review_reasons = [*project.review_reasons, STALE_REASON]
        project.needs_review = True
        stats.stale.append((project.source_key, project.name))


def run_import(
    session: Session, path: Path, force: bool = False, aliases: dict[str, str] | None = None
) -> tuple[ImportStats, Reconciliation | None, Workbook | None]:
    stats = ImportStats()
    sha = file_sha256(path)
    previous = session.scalar(
        select(ImportBatch).where(ImportBatch.file_sha256 == sha, ImportBatch.status == "completed")
    )
    if previous is not None and not force:
        stats.batch_id = previous.id
        stats.skipped_reason = f"this file was already imported (batch {previous.id})"
        return stats, None, None

    workbook = read_workbook(path)
    result = reconcile(workbook.sheets)

    batch = ImportBatch(file_name=path.name, file_sha256=sha, status="running")
    session.add(batch)
    session.flush()
    stats.batch_id = batch.id

    loader = Loader(session, stats, batch, aliases)
    loader.add_source_records(workbook)
    for draft in result.projects:
        loader.upsert_project(draft)
    loader.load_dealer_shops(workbook)
    _flag_stale_projects(session, stats, batch)

    batch.status = "completed"
    batch.finished_at = datetime.now(UTC)
    batch.totals = {
        "source_rows": dict(stats.source_rows),
        "projects": stats.projects,
        "flagged_projects": len(stats.flagged),
        "contacts_created": stats.contacts,
        "dealer_shops": stats.dealer_shops,
        "stale_projects": len(stats.stale),
    }
    session.flush()
    return stats, result, workbook
