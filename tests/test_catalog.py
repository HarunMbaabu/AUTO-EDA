from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from brains_bot.bot import formatting, import_handlers
from brains_bot.database.models import CatalogProduct, Category, Project
from brains_bot.database.session import session_scope
from brains_bot.services import analysis as an
from brains_bot.services import catalog
from brains_bot.services.categories import resolve_category, sync_categories
from brains_bot.services.chat import ProjectTools
from tests.conftest import ALLOWED_ID
from tests.test_import_db import _import
from tests.test_import_upload import _upload
from tests.workbook_factory import build_slim_workbook

HEADER = list(catalog.COLUMNS)
HELP = ["Category key (see Instructions)", "Your product code", "Product name"]
PRODUCTS = [
    ["audio", "A-WIRED", "In-ceiling pair", "Canton", "Intelligent Home", "Ceiling speakers",
     "100,000", "package (from)", "first fix", "no", "in stock", "2026-10-01", ""],
    ["audio", "A-WALL", "Wall-mounted pair", "Canton", "Prime Audio Solutions", "Wall speakers",
     300000, "package (from)", "any time", "yes", "", None, ""],
    ["audio", "A-LUX", "Luxury package", "Canton", "Intelligent Home", "Atelier series",
     "950K", "package (from)", "finishing", "yes", "", None, ""],
    ["smart_home", "S-LIGHT", "Smart Lighting", "", "Intelligent Home", "Lights, breakers",
     None, "to be scoped", "first fix", "no", "", None, ""],
]  # fmt: skip
KEYS = {"audio", "smart_home", "painting"}


def build_catalog(path, rows=PRODUCTS):
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalog"
    ws.append(HEADER)
    ws.append(HELP)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [(300000, 300000), ("300,000", 300000), ("KES 300K", 300000), ("1.5M", 1_500_000),
     ("From 950k", 950000), (None, None), ("", None)],
)  # fmt: skip
def test_parse_price(value, expected):
    assert catalog.parse_price(value) == (None if expected is None else Decimal(expected))


def test_read_catalog_validates_every_row(tmp_path):
    rows = catalog.read_catalog(build_catalog(tmp_path / "c.xlsx"), KEYS)
    assert [r["sku"] for r in rows] == ["A-WIRED", "A-WALL", "A-LUX", "S-LIGHT"]
    assert rows[0]["price_kes"] == 100000 and rows[0]["price_checked"] == date(2026, 10, 1)
    assert rows[1]["retrofit_ok"] is True and rows[3]["price_kes"] is None

    bad = [
        ["kitchen", "K1", "Sink", "", "", "", 1, "", "any time", "no", "", None, ""],
        ["audio", "A-WIRED", "Again", "", "", "", 1, "", "any time", "no", "", None, ""],
        ["audio", "A2", "Speaker", "", "", "", "cheap", "", "any time", "no", "", None, ""],
        ["audio", "A3", "Speaker", "", "", "", 1, "", "someday", "maybe", "", None, ""],
    ]
    with pytest.raises(catalog.CatalogError) as err:
        catalog.read_catalog(build_catalog(tmp_path / "bad.xlsx", PRODUCTS + bad), KEYS)
    message = str(err.value)
    assert message.startswith("4 row(s) need fixing")
    assert "row 7: unknown category 'kitchen'" in message
    assert "row 8: sku 'A-WIRED' appears twice" in message
    assert "row 9: price 'cheap' is not a number" in message
    assert "row 10: install_stage 'someday'" in message


def test_load_suggest_and_replace(factory, tmp_path):
    with session_scope(factory) as s:
        sync_categories(s)
        stats = catalog.load_catalog(s, build_catalog(tmp_path / "c.xlsx"))
        assert stats.products == 4 and stats.by_category == {"audio": 3, "smart_home": 1}
        assert stats.unpriced == 1
        flags = dict(s.execute(select(Category.key, Category.has_verified_catalog)).all())
        assert flags["audio"] and flags["smart_home"] and not flags["painting"]

        # Plastering (4): everything fits, wired first. Painting (6): wired is out.
        assert [p.sku for p in catalog.suggestions(s, "audio", 4)] == ["A-WIRED", "A-WALL", "A-LUX"]
        assert [p.sku for p in catalog.suggestions(s, "audio", 6)] == ["A-WALL", "A-LUX"]
        assert [p.sku for p in catalog.suggestions(s, "audio", 7)] == ["A-WALL", "A-LUX"]
        assert len(catalog.suggestions(s, "audio", None)) == 3
        wall = s.scalar(select(CatalogProduct).where(CatalogProduct.sku == "A-WALL"))
        assert catalog.price_text(wall) == "from KES 300,000 package"
        assert catalog.product_summary(wall, 7)["fits_current_stage_as"] == "normal install"
        lux = s.scalar(select(CatalogProduct).where(CatalogProduct.sku == "A-LUX"))
        assert catalog.product_summary(lux, 7)["fits_current_stage_as"] == "retrofit"

    with session_scope(factory) as s:
        stats = catalog.load_catalog(s, build_catalog(tmp_path / "c2.xlsx", PRODUCTS[:2]))
        assert stats.deactivated == 2
        assert not s.scalar(
            select(Category.has_verified_catalog).where(Category.key == "smart_home")
        )
        assert [p.sku for p in catalog.products_for(s, "audio")] == ["A-WIRED", "A-WALL"]


def test_analysis_shows_catalog_options(factory, tmp_path):
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        catalog.load_catalog(s, build_catalog(tmp_path / "c.xlsx"))
        project = s.scalar(select(Project).where(Project.source_key == "db:1"))  # skimming (5)
        ctx = an.build_context(s, project, resolve_category(s, "audio"), date(2025, 10, 1))
        assert [o["sku"] for o in ctx.payload["catalog_options"]] == ["A-WALL", "A-LUX"]
        assert "catalog_options" in an._user_message(ctx)
        result = an.AnalysisResult(
            fit="possible", summary="s", recorded_facts=[], possible_needs=[], unknowns=[],
            timing_comment="", suggested_next_step="Ask about audio.",
        )  # fmt: skip
        text = formatting.analysis(ctx, result, "note")
        assert "Catalog options that fit this stage" in text
        assert "- Wall-mounted pair: from KES 300,000 package · Prime Audio Solutions" in text
        assert "In-ceiling pair" not in text
        assert "confirm before quoting" in text


def test_chat_find_products(factory, tmp_path):
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        tools = ProjectTools(s, ALLOWED_ID, date(2025, 10, 1))
        assert tools.find_products() == {"error": "no product catalog has been loaded yet"}
        catalog.load_catalog(s, build_catalog(tmp_path / "c.xlsx"))
        everything = tools.find_products()
        assert everything["total"] == 4
        cheap = tools.find_products(category="audio", max_price_kes=300000)
        assert [p["sku"] for p in cheap["products"]] == ["A-WIRED", "A-WALL"]
        assert tools.find_products(text="atelier")["products"][0]["sku"] == "A-LUX"
        project = s.scalar(select(Project).where(Project.source_key == "db:1"))
        for_project = tools.find_products(category="audio", project_id=project.id)
        assert for_project["project_stage"] == "skimming / finishing prep"
        assert [p["sku"] for p in for_project["products"]] == ["A-WALL", "A-LUX"]
        assert tools.find_products(category="kitchen")["error"].startswith("unknown category")


async def test_upload_loads_catalog(factory, tmp_path, context):
    with session_scope(factory) as s:
        sync_categories(s)
    context.bot_data["db"] = factory
    update, status = _upload(
        ALLOWED_ID, name="catalog.xlsx", source=build_catalog(tmp_path / "c.xlsx")
    )
    await import_handlers.workbook_upload(update, context)
    assert status.texts[-1].startswith("Catalog loaded: 4 products.")
    assert "- audio: 3" in status.texts[-1]

    bad = build_catalog(
        tmp_path / "bad.xlsx",
        [["kitchen", "K", "Sink", "", "", "", 1, "", "any time", "no", "", None, ""]],
    )
    update, status = _upload(ALLOWED_ID, name="bad.xlsx", source=bad)
    await import_handlers.workbook_upload(update, context)
    assert "row 3: unknown category 'kitchen'" in status.texts[-1]
    with session_scope(factory) as s:  # the bad file changed nothing
        assert s.scalar(select(CatalogProduct).where(CatalogProduct.sku == "A-WALL")).active
