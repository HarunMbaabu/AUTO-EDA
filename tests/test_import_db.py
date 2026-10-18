"""Database tests. They need PostgreSQL: set TEST_DATABASE_URL (they are skipped otherwise).
The test database is wiped on every test, so never point it at real data."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from brains_bot.database.models import (
    AuditEvent,
    Base,
    Contact,
    DealerShop,
    MarketingPermission,
    Project,
    ProjectContact,
    ProjectNote,
    SalesAssignment,
    SourceRecord,
)
from brains_bot.database.session import make_engine, make_session_factory, session_scope
from brains_bot.importers.loader import STALE_REASON, run_import
from brains_bot.importers.report import build_report
from brains_bot.services import project_service as svc
from tests.workbook_factory import build_workbook

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine():
    engine = make_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def workbook_path(tmp_path):
    return build_workbook(tmp_path / "fictional.xlsx")


def _import(factory, path, force=False):
    with session_scope(factory) as session:
        return run_import(session, path, force=force)


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def _project(session, key) -> Project:
    return session.scalar(select(Project).where(Project.source_key == key))


def test_import_counts_and_reconciliation(factory, workbook_path):
    stats, result, _ = _import(factory, workbook_path)
    assert stats.source_rows["Data Base"] == 5
    with session_scope(factory) as s:
        # 5 Data Base projects + the inserted "Ocean Breeze" row + the ID-less "Pine Ridge".
        assert _count(s, Project) == 7
        assert _count(s, SourceRecord) == 5 + 6 + 2 + 3 + 2
        unlinked = s.scalar(
            select(func.count()).where(
                SourceRecord.project_id.is_(None),
                SourceRecord.sheet.not_in(["Philmax data", "Sheet3"]),
            )
        )
        assert unlinked == 0
        assert _count(s, DealerShop) == 2  # duplicate shop row merged


def test_import_is_idempotent(factory, workbook_path):
    _import(factory, workbook_path)
    with session_scope(factory) as s:
        before = {m: _count(s, m) for m in (Project, Contact, ProjectContact, ProjectNote,
                                            SalesAssignment, MarketingPermission)}  # fmt: skip

    stats, _, _ = _import(factory, workbook_path)
    assert stats.skipped_reason and "already imported" in stats.skipped_reason

    _import(factory, workbook_path, force=True)
    with session_scope(factory) as s:
        after = {m: _count(s, m) for m in before}
    assert after == before


def test_contacts_phones_and_consent(factory, workbook_path):
    _import(factory, workbook_path)
    with session_scope(factory) as s:
        person = s.scalar(select(Contact).where(Contact.phone_e164 == "+254712345678"))
        assert person.phone_status == "valid"
        multi = s.scalar(select(Contact).where(Contact.phone_status == "multiple"))
        assert multi.phone_e164 is None and len(multi.phone_candidates) == 2
        # Consent is never inferred: every permission starts as unknown.
        assert set(s.scalars(select(MarketingPermission.status))) == {"unknown"}
        # Companies are shared across projects; "acme builders ltd" == "Acme Builders Ltd".
        acme = s.scalars(
            select(Contact).where(Contact.contact_key == "org:acme builders ltd")
        ).all()
        assert len(acme) == 1
        links = s.scalar(select(func.count()).where(ProjectContact.contact_id == acme[0].id))
        assert links >= 5
        # Placeholders (Architect "TBC", Company "N/A") are not stored as contacts.
        assert not s.scalar(select(func.count()).where(Contact.name.in_(["TBC", "N/A"])))


def test_project_fields_notes_sales_and_flags(factory, workbook_path):
    _import(factory, workbook_path)
    with session_scope(factory) as s:
        kamau = _project(s, "db:1")
        assert kamau.city == "Nairobi" and not kamau.needs_review
        assert kamau.sales.won_lost == "Won"
        assert kamau.sales.tracking_status == "Following"  # Active Sheet wins over dealer sheet
        assert kamau.sales.dealer_to_route == "Cidone" and kamau.sales.sar == "Antony"

        sunrise = _project(s, "db:2")
        assert sunrise.sales.salesperson == "Jane Wanjiru"
        assert sunrise.sales.salesperson_raw == "Jane Wanjiru - project."

        baraka = _project(s, "db:3")
        # Data Base comment + one copy of the repeated follow-up row + the Active Sheet comment.
        assert sorted(nt.text for nt in baraka.notes) == [
            "Follow-up: samples delivered.", "Roofing done.", "Walls being plastered.",
        ]  # fmt: skip
        assert baraka.sales.quote_value_kes == 450000

        green = _project(s, "db:4")
        assert green.city == "Eldoret"
        assert green.needs_review and "please check this date" in green.review_reasons[0]


def test_stale_projects_are_flagged_not_deleted(factory, workbook_path):
    _import(factory, workbook_path)
    with session_scope(factory) as s:
        s.add(Project(source_key="db:999", name="Removed site"))
    _import(factory, workbook_path, force=True)
    with session_scope(factory) as s:
        removed = _project(s, "db:999")
        assert removed.needs_review and STALE_REASON in removed.review_reasons


def test_report_has_no_phone_numbers(factory, workbook_path):
    stats, result, workbook = _import(factory, workbook_path)
    report = build_report(stats, result, workbook, workbook_path.name)
    assert "Every project record is accounted for: 13 linked = 13 read." in report
    assert "Ocean Breeze Hotel" in report
    assert not re.search(r"(\+?254|\b0)[17]\d{8}", report)
    assert "7XX" not in report  # not even masked numbers


def test_project_service_queries_and_audit(factory, workbook_path):
    _import(factory, workbook_path)
    with session_scope(factory) as s:
        items, pages = svc.list_projects(s, 1)
        assert pages == 1 and len(items) == 7
        assert [p.name for p in svc.search_projects(s, "lakeview")] == ["Lakeview Towers"]
        assert len(svc.search_projects(s, "acme builders")) >= 5  # by contractor name
        assert svc.search_projects(s, "0712345678") == []  # phones are not searchable
        assert svc.search_projects(s, "100%") == []  # LIKE wildcards are escaped

        kamau_id = _project(s, "db:1").id
        detail = svc.get_project(s, kamau_id)
        assert detail.sources[0].startswith("Data Base row 2, ID 1")
        assert len(detail.sources) == 3
        person = next(c for c in detail.contacts if c.role == "contact_person")
        assert person.phone_e164 == "+254712345678" and person.consent == "unknown"
        assert svc.get_project(s, 999999) is None

        svc.record_audit(s, 42, "project_search", results=1)
    with session_scope(factory) as s:
        event = s.scalar(select(AuditEvent))
        assert event.actor_telegram_id == 42 and event.details == {"results": 1}


def test_migrations_match_models(engine, monkeypatch):
    Base.metadata.drop_all(engine)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)  # fails if the models have changes with no migration
    command.downgrade(config, "base")


async def test_project_command_masks_phone(factory, workbook_path, context):
    from brains_bot.bot import project_handlers
    from tests.conftest import ALLOWED_ID, make_update, reply_of

    _import(factory, workbook_path)
    with session_scope(factory) as s:
        kamau_id = _project(s, "db:1").id
    context.bot_data["db"] = factory
    context.args = [str(kamau_id)]
    update = make_update(ALLOWED_ID)
    await project_handlers.project(update, context)
    text = reply_of(update)
    assert "Kamau proposed villa" in text
    assert "+254 7XX XXX 678" in text and "712345678" not in text
    assert "not a verified current status" in text
    with session_scope(factory) as s:
        event = s.scalar(select(AuditEvent).where(AuditEvent.action == "project_view"))
        assert event.target == f"project:{kamau_id}" and event.actor_telegram_id == ALLOWED_ID
