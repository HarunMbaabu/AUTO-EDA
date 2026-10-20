"""Database schema. Change it through Alembic migrations (see migrations/)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20))  # running | completed | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totals: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SourceRecord(Base):
    """One logical record from a worksheet, with its original cell values."""

    __tablename__ = "source_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    sheet: Mapped[str] = mapped_column(String(100))
    first_row: Mapped[int]
    last_row: Mapped[int]
    source_id: Mapped[int | None]
    raw: Mapped[dict[str, Any]]
    issues: Mapped[list[Any]] = mapped_column(default=list)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    match_method: Mapped[str | None] = mapped_column(String(30))


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable identity across re-imports, e.g. "db:205" for Data Base ID 205.
    source_key: Mapped[str] = mapped_column(String(120), unique=True)
    source_id: Mapped[int | None]
    name: Mapped[str] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(Text)
    google_location_url: Mapped[str | None] = mapped_column(Text)
    site_visit_date: Mapped[date | None] = mapped_column(Date)
    # Historical estimate recorded at the visit. Not evidence of current site status.
    est_paint_start: Mapped[date | None] = mapped_column(Date)
    # From the "Audio Status" column, only when the import is told it holds real audio data
    # (--audio-status status). Free text, stored verbatim.
    audio_status: Mapped[str | None] = mapped_column(Text)
    needs_review: Mapped[bool] = mapped_column(default=False)
    review_reasons: Mapped[list[Any]] = mapped_column(default=list)
    last_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))

    contacts: Mapped[list[ProjectContact]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    notes: Mapped[list[ProjectNote]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectNote.id"
    )
    sales: Mapped[SalesAssignment | None] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Contact(TimestampMixin, Base):
    """A person or organisation. Phone is set only when exactly one valid number exists."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_key: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(String(20), index=True)
    phone_raw: Mapped[str | None] = mapped_column(Text)
    # valid | multiple | invalid | placeholder | missing
    phone_status: Mapped[str] = mapped_column(String(20), default="missing")
    phone_candidates: Mapped[list[Any]] = mapped_column(default=list)


class ProjectContact(Base):
    __tablename__ = "project_contacts"
    __table_args__ = (UniqueConstraint("project_id", "contact_id", "role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    # owner | contractor | architect | contact_person | company
    role: Mapped[str] = mapped_column(String(30))

    project: Mapped[Project] = relationship(back_populates="contacts")
    contact: Mapped[Contact] = relationship()


class SalesAssignment(Base):
    __tablename__ = "sales_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    salesperson: Mapped[str | None] = mapped_column(String(120))
    salesperson_raw: Mapped[str | None] = mapped_column(Text)
    sar: Mapped[str | None] = mapped_column(String(120))
    dealer_to_route: Mapped[str | None] = mapped_column(String(120))
    site_visited_by_dealer: Mapped[str | None] = mapped_column(String(30))
    quotation_shared: Mapped[str | None] = mapped_column(String(30))
    won_lost: Mapped[str | None] = mapped_column(String(30))
    first_order_received: Mapped[str | None] = mapped_column(String(30))
    supplying_dealer: Mapped[str | None] = mapped_column(String(120))
    quote_value_kes: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tracking_status: Mapped[str | None] = mapped_column(String(60))

    project: Mapped[Project] = relationship(back_populates="sales")


class ProjectNote(Base):
    """Free-text notes, stored verbatim. Treated as untrusted data, never as instructions."""

    __tablename__ = "project_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_records.id", ondelete="SET NULL")
    )
    field: Mapped[str] = mapped_column(String(60))
    text: Mapped[str] = mapped_column(Text)
    observed_date: Mapped[date | None] = mapped_column(Date)

    project: Mapped[Project] = relationship(back_populates="notes")


class MarketingPermission(TimestampMixin, Base):
    """Consent is never inferred from a phone number or a site visit: imports set 'unknown'."""

    __tablename__ = "marketing_permissions"
    __table_args__ = (UniqueConstraint("contact_id", "channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20))  # whatsapp | sms
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    lawful_basis: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)


class DealerShop(Base):
    __tablename__ = "dealer_shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_key: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    general_location: Mapped[str | None] = mapped_column(String(200))
    exact_location: Mapped[str | None] = mapped_column(String(200))
    source_sheets: Mapped[list[Any]] = mapped_column(default=list)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str | None] = mapped_column(String(120))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Category(TimestampMixin, Base):
    """A product category the bot can analyse. Defined in data/categories.json and synced here;
    adding a category needs no code change."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    best_from: Mapped[int]  # construction stage numbers, see services/stages.py
    best_to: Mapped[int]
    retrofit_to: Mapped[int | None]
    timing_note: Mapped[str] = mapped_column(Text, default="")
    has_verified_catalog: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)


class OpportunityAssessment(Base):
    """A saved Claude analysis. Inputs are referenced, not copied: no names or phone numbers."""

    __tablename__ = "opportunity_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    category_key: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(String(100))
    data_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    stage: Mapped[int | None]
    timing_status: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict[str, Any]]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]


class CatalogProduct(TimestampMixin, Base):
    """A product or package from the verified catalog. Loading a new catalog file replaces the
    old one: products missing from it are deactivated, not deleted."""

    __tablename__ = "catalog_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True)
    category_key: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(120))
    supplier: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    price_kes: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_unit: Mapped[str | None] = mapped_column(String(60))
    install_stage: Mapped[str] = mapped_column(String(20))  # see services/catalog.py
    retrofit_ok: Mapped[bool] = mapped_column(default=False)
    available: Mapped[str | None] = mapped_column(String(60))
    price_checked: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str] = mapped_column(Text, default="")
    source_file: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)


class OutreachDraft(Base):
    """A WhatsApp message drafted by the AI for an operator to review. The bot never sends it:
    'sent' means the operator opened it in WhatsApp from the bot."""

    __tablename__ = "outreach_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    category_key: Mapped[str] = mapped_column(String(50), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text)
    product_skus: Mapped[list[Any]] = mapped_column(default=list)
    model: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by: Mapped[int | None] = mapped_column(BigInteger)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Quote(TimestampMixin, Base):
    """A quote recorded by the sales team (in the dashboard), with its outcome."""

    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    category_key: Mapped[str] = mapped_column(String(50))
    product_sku: Mapped[str | None] = mapped_column(String(80))
    amount_kes: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | won | lost
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
