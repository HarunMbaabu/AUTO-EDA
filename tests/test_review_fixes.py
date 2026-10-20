"""Regression tests for the issues found in code review of PR #1."""

from datetime import date, datetime

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from brains_bot.database.models import (
    Category,
    Contact,
    ImportBatch,
    OpportunityAssessment,
    Project,
    ProjectContact,
    ProjectNote,
)
from brains_bot.database.session import session_scope
from brains_bot.importers.loader import ImportStats, Loader, run_import
from brains_bot.importers.normalize import redact_contact_details
from brains_bot.importers.reconcile import Link, ProjectDraft
from brains_bot.importers.workbook import SourceRow
from brains_bot.services import analysis as an
from brains_bot.services.categories import resolve_category, sync_categories
from brains_bot.services.stages import detect_stage
from tests.workbook_factory import SLIM_HEADERS


@pytest.mark.parametrize(
    "text",
    ["call 0712 345 678", "call 712345678", "call 712 345 678", "call (+254) 712 345 678",
     "call +254-712-345-678", "call 254712345678", "call 0112345678", "mail a.b@c.co.ke"],
)  # fmt: skip
def test_every_accepted_phone_format_is_redacted(text):
    cleaned = redact_contact_details(text)
    assert not any(ch.isdigit() for ch in cleaned) and "@" not in cleaned


def test_redaction_keeps_dates_and_short_numbers():
    text = "Visit on 25/09/2025, 3 floors, KES 300,000"
    assert redact_contact_details(text) == text


def test_scrub_lowercase_titles_site_and_contractor_names():
    assert an.scrub("mr. kamau asked for a visit") == "[person] asked for a visit"
    names = an.name_tokens(["Kamau proposed villa", "John Otieno"])
    cleaned = an.scrub("Otieno started roofing at Kamau's site", names)
    assert "Otieno" not in cleaned and "Kamau" not in cleaned


@pytest.mark.parametrize(
    ("note", "stage"),
    [("Plastering has not started.", None), ("Waiting for roofing.", None),
     ("Roofing done, plastering will start next week.", 3),
     ("Plaster yet to start.", None), ("No plastering yet, walling ongoing.", 2),
     ("They are expected to be handed over by June.", None),
     ("Waiting to resume painting since they are finishing plastering.", 4),
     ("Interior skimming and two coats undercoat done.", 6)],
)  # fmt: skip
def test_negated_stage_mentions_are_ignored(note, stage):
    assert detect_stage([(date(2025, 1, 1), note)]).stage == stage


def _slim(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Data Base"
    ws.append(SLIM_HEADERS)
    for row in rows:
        ws.append(row + [None] * (len(SLIM_HEADERS) - len(row)))
    wb.save(path)
    return path


def test_duplicate_ids_with_the_same_site_stay_separate(factory, tmp_path):
    rows = [[7, "3/1/2025", "Karen", "Nairobi", "Hill View", None, None, None, None, None, None,
             None, f"Visit {i}: foundation stage."] for i in (1, 2, 3)]  # fmt: skip
    path = _slim(tmp_path / "dup.xlsx", rows)
    with session_scope(factory) as s:
        stats, _, _ = run_import(s, path)
        assert stats.projects == 3
        notes = sorted(s.scalars(select(ProjectNote.text)))
        assert notes == ["Visit 1: foundation stage.", "Visit 2: foundation stage.",
                         "Visit 3: foundation stage."]  # fmt: skip
        keys = sorted(s.scalars(select(Project.source_key)))
        assert len(set(keys)) == 3


def test_a_shorter_note_is_only_dropped_when_it_is_a_whole_word_copy(factory, tmp_path):
    rows = [
        [1, "3/1/2025", "Karen", "Nairobi", "Site A", None, None, None, None, None, None, None,
         "Waterproofing done."],
        [None] * 12 + ["Roofing done."],
        [None] * 12 + ["Waterproofing"],
    ]  # fmt: skip
    path = _slim(tmp_path / "notes.xlsx", rows)
    with session_scope(factory) as s:
        run_import(s, path)
        notes = sorted(s.scalars(select(ProjectNote.text)))
        assert notes == ["Roofing done.", "Waterproofing done."]


def test_contact_phone_is_found_on_another_row_of_the_project(factory):
    def row(sheet, phone):
        values = {"Contact Person Name": "Mr. Otieno", "Contact Details": phone}
        return SourceRow(sheet, 2, 2, values)

    with session_scope(factory) as s:
        batch = ImportBatch(file_name="t.xlsx", file_sha256="0" * 64, status="running")
        s.add(batch)
        project = Project(source_key="db:1", name="X")
        s.add_all([project])
        s.flush()
        draft = ProjectDraft("db:1", row("Data Base", None))
        draft.links = [
            Link(draft.primary, "primary"),
            Link(row("Active Sheet", "0712345678"), "id"),
        ]
        Loader(s, ImportStats(), batch)._add_contacts(project, draft)
        contact = s.scalar(
            select(Contact).join(ProjectContact).where(ProjectContact.role == "contact_person")
        )
        assert contact.phone_e164 == "+254712345678"


def test_saved_analysis_expires_when_the_category_changes(factory, tmp_path):
    from tests.test_import_db import _import
    from tests.workbook_factory import build_slim_workbook

    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        project = s.scalar(select(Project).where(Project.source_key == "db:1"))
        category = resolve_category(s, "audio")
        ctx = an.build_context(s, project, category, date(2025, 10, 1))
        s.add(OpportunityAssessment(project_id=project.id, category_key="audio", model="m",
                                    data_batch_id=ctx.data_batch_id, timing_status="x",
                                    result={"fit": "weak"}))  # fmt: skip
        s.flush()
        assert an.recent_assessment(s, ctx) is not None
        category.updated_at = datetime(2999, 1, 1)
        s.flush()
        assert an.recent_assessment(s, ctx) is None
        assert s.get(Category, category.id) is not None
