import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from brains_bot.bot import website_handlers
from brains_bot.database.session import session_scope
from brains_bot.services import brochures, partners, sites, websites
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.categories import sync_categories
from tests.conftest import ALLOWED_ID
from tests.test_brochures import PRODUCTS
from tests.test_chat import FakeTransport, text
from tests.test_sites import _update

HOME = """<html><head><title>Jane Decor</title><script>var x = "hidden";</script></head>
<body><nav><a href="/shop/curtains">Curtains</a> <a href="/about">About</a>
<a href="/cart">Cart</a> <a href="https://other.example/shop">Elsewhere</a></nav>
<h1>Jane Decor Interiors</h1><p>Call 0790 001 776</p></body></html>"""
SHOP = """<html><body><h2>Blackout curtains</h2><p>KES 4,500 per metre</p>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
 {"@type": "Product", "name": "Roller blind", "brand": {"name": "Shade"},
  "offers": {"@type": "Offer", "price": "7800", "priceCurrency": "KES"}}]}</script>
</body></html>"""
REAL_PUBLIC = websites._public  # before the fixture below replaces it


def _client(routes):
    def handle(request):
        found = routes.get(request.url.path)
        if found is None:
            return httpx.Response(404, text="not found")
        status, kind, body = found
        headers = {"content-type": kind}
        if status in (301, 302):
            headers["location"] = body
            body = ""
        return httpx.Response(status, headers=headers, text=body)

    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    monkeypatch.setattr(websites, "_public", lambda host: host != "localhost")


def test_pages_are_read_with_their_product_data_and_shop_links_followed():
    client = _client({
        "/": (301, "", "https://janedecor.example/home"),
        "/home": (200, "text/html", HOME),
        "/shop/curtains": (200, "text/html; charset=utf-8", SHOP),
        "/about": (200, "text/html", "<p>About us</p>"),
    })  # fmt: skip
    reading = websites.read_site("janedecor.example", client)
    assert reading.method == "web" and reading.pages == 2
    assert "Jane Decor Interiors" in reading.text and "hidden" not in reading.text
    assert "Roller blind | Shade | KES 7800" in reading.text
    assert "Blackout curtains\nKES 4,500 per metre" in reading.text
    assert "About us" not in reading.text  # not a shop page


def test_a_shopify_shop_is_read_from_its_product_list():
    products = {"products": [
        {"title": "Garden speaker", "vendor": "Sauti", "product_type": "Audio",
         "variants": [{"price": "68000.00"}, {"price": "72000.00"}]},
    ]}  # fmt: skip
    client = _client({
        "/": (200, "text/html", HOME),
        "/products.json": (200, "application/json", json.dumps(products)),
    })  # fmt: skip
    reading = websites.read_site("https://sauti.example", client)
    assert reading.method == "shopify"
    assert "Garden speaker | Sauti | Audio | from 68000.00" in reading.text


def test_a_woocommerce_shop_is_read_from_its_product_list():
    products = [{"name": "Wall paint 20L", "categories": [{"name": "Paint"}],
                 "short_description": "<p>Matt finish</p>",
                 "prices": {"price": "1250000", "currency_code": "KES",
                            "currency_minor_unit": 2}}]  # fmt: skip
    client = _client({
        "/": (200, "text/html", HOME),
        "/wp-json/wc/store/v1/products": (200, "application/json", json.dumps(products)),
    })  # fmt: skip
    reading = websites.read_site("https://paint.example", client)
    assert reading.method == "woocommerce"
    assert "Wall paint 20L | Paint | Matt finish | KES 12,500" in reading.text


def test_robots_txt_and_private_addresses_are_respected():
    client = _client({
        "/": (200, "text/html", HOME),
        "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /"),
    })  # fmt: skip
    with pytest.raises(AnalysisError, match="asks bots not to read it"):
        websites.read_site("https://janedecor.example", client)
    client = _client({"/": (302, "", "http://localhost/admin")})
    with pytest.raises(AnalysisError, match="could not be opened"):
        websites.read_site("https://janedecor.example", client)


def test_only_public_addresses_count():
    assert not REAL_PUBLIC("127.0.0.1") and not REAL_PUBLIC("192.168.1.10")
    assert not REAL_PUBLIC("169.254.169.254") and not REAL_PUBLIC(None)
    assert REAL_PUBLIC("8.8.8.8")


async def test_website_command_finds_the_partner_and_sends_a_sheet(factory, context, monkeypatch):
    with session_scope(factory) as s:
        sync_categories(s)
        for name, links in (
            ("Jane", [{"kind": "website", "url": "https://janedecor.example"}]),
            ("Otieno", [{"kind": "whatsapp_catalog", "url": "https://wa.me/c/254722111333"}]),
        ):
            draft = partners.PartnerDraft(
                message=name, name=name, company=f"{name} Co", area=None, trade="other",
                services=[], rates=[], note=None,
                phone=sites.find_phones("0722111333" if name == "Otieno" else "0790001776"),
                links=links,
            )  # fmt: skip
            partners.save(s, draft, date(2026, 10, 21))
    context.bot_data["db"] = factory
    context.bot_data["drafter"] = SimpleNamespace(
        transport=FakeTransport(text(json.dumps(PRODUCTS)))
    )
    context.user_data = {}
    read = []

    def read_site(url):
        read.append(url)
        return brochures.Reading("Garden Rock Speakers KES 68,000", 2, "web")

    monkeypatch.setattr(websites, "read_site", read_site)

    update, status = _update(ALLOWED_ID, "/website jane")
    update.effective_chat.send_document = AsyncMock()
    await website_handlers.website_command(update, context)
    assert read == ["https://janedecor.example"]
    assert "Jane Co: read 2 page(s) on janedecor.example" in status.edit_text.await_args.args[0]
    sheet = update.effective_chat.send_document.await_args
    assert sheet.kwargs["filename"] == "catalog_Jane_Co.xlsx"

    update, _ = _update(ALLOWED_ID, "/website otieno")
    await website_handlers.website_command(update, context)
    reply = update.effective_message.reply_text.await_args.args[0]
    assert reply.startswith("Otieno Co has a WhatsApp catalogue") and "screen-record" in reply
