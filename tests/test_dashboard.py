import time
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from brains_bot.config import Settings
from brains_bot.dashboard.app import create_app, money
from brains_bot.dashboard.auth import sign_login, verify_login
from brains_bot.database.models import OutreachDraft, Project, Quote
from brains_bot.database.session import session_scope
from brains_bot.services import metrics
from tests.conftest import ALLOWED_ID, FAKE_TOKEN, STRANGER_ID
from tests.test_outreach import _seed


def _login_params(user_id, token=FAKE_TOKEN, age=0):
    fields = {"id": str(user_id), "first_name": "Ann", "auth_date": str(int(time.time()) - age)}
    return sign_login(fields, token)


def test_verify_login():
    good = _login_params(ALLOWED_ID)
    assert verify_login(good, FAKE_TOKEN) == ALLOWED_ID
    assert verify_login({**good, "first_name": "Eve"}, FAKE_TOKEN) is None  # tampered
    assert verify_login(good, "999:other-token") is None
    assert verify_login(_login_params(ALLOWED_ID, age=2 * 86400), FAKE_TOKEN) is None  # too old
    assert verify_login({"id": "1"}, FAKE_TOKEN) is None


def test_money():
    assert money(Decimal("300000")) == "KES 300,000"
    assert money(Decimal("1500000")) == "KES 1.50M"
    assert money(Decimal("4000000")) == "KES 4M"
    assert money(None) == "—"


@pytest.fixture
def client(factory, monkeypatch):
    monkeypatch.setenv("DASHBOARD_HTTPS_ONLY", "0")
    settings = Settings(telegram_bot_token=FAKE_TOKEN, allowed_user_ids=frozenset({ALLOWED_ID}))
    app = create_app(settings, factory, bot_username="test_bot")
    return TestClient(app)


def _log_in(client, user_id=ALLOWED_ID):
    return client.get("/auth/telegram", params=_login_params(user_id), follow_redirects=False)


def test_login_required_and_allowlist(client):
    assert client.get("/healthz").text == "ok"
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    page = client.get("/login").text
    assert 'data-telegram-login="test_bot"' in page

    denied = _log_in(client, STRANGER_ID)
    assert denied.headers["location"] == "/login?error=denied"
    assert "not on the bot's allowlist" in client.get("/login?error=denied").text
    forged = client.get("/auth/telegram", params={**_login_params(ALLOWED_ID), "hash": "0" * 64},
                        follow_redirects=False)  # fmt: skip
    assert forged.headers["location"] == "/login?error=invalid"
    assert client.get("/", follow_redirects=False).status_code == 303

    assert _log_in(client).headers["location"] == "/"
    assert client.get("/").status_code == 200
    client.get("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_pages_quotes_and_metrics(client, factory, tmp_path):
    ids = _seed(factory, tmp_path)
    pid = ids["db:1"]
    with session_scope(factory) as s:
        s.add(OutreachDraft(project_id=pid, category_key="audio", text="Hi", model="m",
                            product_skus=["A-WALL"]))  # fmt: skip
    _log_in(client)

    overview = client.get("/").text
    assert "Cross-sell pipeline by category" in overview and "659" not in overview
    assert "Messages drafted" in overview and "chart-4.4.1.umd.min.js" in overview
    assert "Wall-mounted pair" in overview  # top packages

    listing = client.get("/projects", params={"q": "Kamau"}).text
    assert "Kamau proposed villa" in listing and "Sunrise Apartments" not in listing

    page = client.get(f"/projects/{pid}").text
    assert "Kamau proposed villa" in page and "+254 712 345 678" in page
    assert 'href="https://wa.me/254712345678"' in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    assert client.get("/projects/99999").status_code == 404

    bad = client.post(f"/projects/{pid}/quotes",
                      data={"category": "audio", "amount": "300,000", "csrf": "wrong"})  # fmt: skip
    assert bad.status_code == 400
    added = client.post(
        f"/projects/{pid}/quotes",
        data={"category": "audio", "amount": "300,000", "product_sku": "A-WALL", "csrf": csrf},
        follow_redirects=False,
    )
    assert added.status_code == 303
    not_a_number = {"category": "audio", "amount": "lots", "csrf": csrf}
    assert client.post(f"/projects/{pid}/quotes", data=not_a_number).status_code == 400
    with session_scope(factory) as s:
        quote = s.scalar(select(Quote))
        assert (
            quote.amount_kes == 300000 and quote.status == "open" and quote.created_by == ALLOWED_ID
        )
        quote_id = quote.id
    assert "Wall-mounted pair" in client.get(f"/projects/{pid}").text
    client.post(f"/quotes/{quote_id}/status", data={"status": "won", "csrf": csrf})

    with session_scope(factory) as s:
        revenue = metrics.revenue(s)
        assert revenue.won == 1 and revenue.won_value == 300000 and revenue.win_rate == 1.0
        assert revenue.top_packages[0]["sku"] == "A-WALL"
        bot = metrics.bot_stats(s)
        assert bot.drafts == 1 and bot.sent == 0 and bot.response_rate is None
        crm = metrics.crm(s, date(2025, 10, 1))
        assert crm.projects == 3 and crm.contacts >= 3
        audio = next(p for p in crm.pipeline if p["key"] == "audio")
        assert sum(audio["counts"].values()) == 3
        assert s.get(Project, pid) is not None
    assert "KES 300,000" in client.get("/").text


def test_quote_package_must_match_category(client, factory, tmp_path):
    ids = _seed(factory, tmp_path)
    _log_in(client)
    page = client.get(f"/projects/{ids['db:1']}").text
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    assert '<optgroup label="Home Audio &amp; Multi-room">' in page
    mismatch = {"category": "smart_home", "amount": "1000", "product_sku": "A-WALL", "csrf": csrf}
    response = client.post(f"/projects/{ids['db:1']}/quotes", data=mismatch)
    assert response.status_code == 400 and "different category" in response.text


def test_contacts_page_filters_by_trade(client, factory):
    from brains_bot.importers.normalize import normalize_phone
    from brains_bot.services import contacts

    with session_scope(factory) as s:
        contacts.save_contact(s, "Musa", normalize_phone("0790 004 545"), "contractor",
                              "Example Construction", "Migaa", "Good contact",
                              date(2026, 2, 15))  # fmt: skip
        contacts.save_contact(s, "Jane", normalize_phone("0790001776"), "interior_designer",
                              "Jane Decor Interiors", "Golden palm", "Call back 0790001776",
                              date(2026, 2, 4))  # fmt: skip
    _log_in(client)
    page = client.get("/contacts").text
    assert "Example Construction" in page and "Jane Decor Interiors" in page
    assert 'href="https://wa.me/254790004545"' in page and "+254 790 004 545" in page
    assert "Call back +254 790 001 776" in page  # numbers inside notes are written the same way
    assert "Called on 04 Feb 2026" in page and "Noted on 15 Feb 2026" in page
    only = client.get("/contacts", params={"trade": "contractor", "q": "Migaa"}).text
    assert "Example Construction" in only and "Jane Decor Interiors" not in only


def test_partners_page(client, factory):
    from brains_bot.importers.normalize import normalize_phone
    from brains_bot.services import partners
    from brains_bot.services.categories import sync_categories

    with session_scope(factory) as s:
        sync_categories(s)
        draft = partners.PartnerDraft(
            message="Jane", name="Jane", company="Jane Decor Interiors", area="Kiambu",
            trade="interior_designer", services=["interior_design"],
            rates=[partners.RateDraft("interior_design", "design", 1500, None, "per sqm")],
            note=None, phone=normalize_phone("0790001776"),
            links=[{"kind": "website", "url": "https://janedecor.co.ke"}],
        )  # fmt: skip
        partners.save(s, draft, date(2026, 10, 12))
    _log_in(client)
    page = client.get("/partners", params={"category": "Finishes & Interiors"}).text
    assert "Jane Decor Interiors" in page and "design: KES 1,500 per sqm" in page
    assert 'href="https://wa.me/254790001776"' in page and "+254 790 001 776" in page
    assert '<a href="https://janedecor.co.ke"' in page and ">Website</a>" in page
    assert "Jane Decor Interiors" not in client.get("/partners", params={"category": "audio"}).text
