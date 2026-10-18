from collections import Counter

import pytest

from brains_bot.importers.reconcile import reconcile
from brains_bot.importers.workbook import read_workbook
from tests.workbook_factory import build_workbook


@pytest.fixture
def workbook(tmp_path):
    return read_workbook(build_workbook(tmp_path / "fictional.xlsx"))


def test_reads_sheets_with_offset_headers_and_skips_pivots(workbook):
    assert {k: len(v) for k, v in workbook.sheets.items()} == {
        "Data Base": 5,
        "Active Sheet": 6,
        "To share with Dealer": 2,
        "Philmax data": 3,
        "Sheet3": 2,
    }
    assert workbook.skipped_sheets == ["Hit Rate"]


def test_continuation_rows_fold_into_the_visit(workbook):
    baraka = workbook.sheets["Data Base"][2]
    assert baraka.source_id == 3
    assert (baraka.first_row, baraka.last_row) == (4, 6)
    assert baraka.extra == [("Comments (If Any)", "Follow-up: samples delivered.")] * 2


def test_record_without_id_is_flagged(workbook):
    pine = workbook.sheets["Active Sheet"][4]
    assert pine.source_id is None
    assert "missing source ID" in pine.issues


def test_reconciliation_links_by_id_only_when_content_agrees(workbook):
    result = reconcile(workbook.sheets)
    by_key = {p.source_key: p for p in result.projects}

    # Every project record ends up in exactly one project.
    linked = sum(len(p.links) for p in result.projects)
    assert linked == 5 + 6 + 2

    # ID 1 matches across all three sheets.
    assert Counter(r.sheet for r in by_key["db:1"].rows()) == {
        "Data Base": 1, "Active Sheet": 1, "To share with Dealer": 1,
    }  # fmt: skip

    # The inserted Active row reuses ID 3 but is a different site: kept separate, flagged.
    ocean = [
        p
        for p in result.projects
        if p.primary.get("Name of Potential Site") == "Ocean Breeze Hotel"
    ]
    assert len(ocean) == 1
    assert ocean[0].source_key.startswith("active:3:")
    assert any("only found in Active Sheet" in r for r in ocean[0].review_reasons)

    # The genuine ID 3 row links to Baraka Heights by ID and phone.
    assert [lk.method for lk in by_key["db:3"].links] == ["primary", "source_id"]

    # A record with no ID and no match elsewhere becomes its own flagged project.
    pine = [p for p in result.projects if p.source_key.startswith("active:noid:")]
    assert len(pine) == 1 and pine[0].review_reasons


def test_shared_first_name_with_different_phones_is_not_merged(workbook):
    sheets = workbook.sheets
    impostor = sheets["Active Sheet"][1]
    impostor.values = {**impostor.values, "ID": 99, "Name of Potential Site": "Mr. Sunrise",
                       "Contact Details": "0799555444"}  # fmt: skip
    # Data Base has "Sunrise Apartments"; similar name but conflicting phone: no merge.
    result = reconcile(sheets)
    assert any(p.source_key.startswith("active:99:") for p in result.projects)
