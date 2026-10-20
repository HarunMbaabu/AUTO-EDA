"""The dashboard web app (FastAPI, server-rendered pages).

Run with `uv run brains-bot-dashboard`. It reads the same database as the bot. Every page
except the login page needs a Telegram login from an allowlisted user.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from brains_bot.config import Settings, load_env_file, load_settings
from brains_bot.database.models import (
    CatalogProduct,
    Category,
    OpportunityAssessment,
    OutreachDraft,
    Project,
    Quote,
)
from brains_bot.database.session import make_engine, make_session_factory, session_scope
from brains_bot.importers.normalize import mask_phone
from brains_bot.services import followups, metrics, opportunities
from brains_bot.services import project_service as svc
from brains_bot.services.stages import detect_stage

from .auth import verify_login

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
PAGE_SIZE = 25
QUOTE_STATUSES = ("open", "won", "lost")


class LoginRequired(Exception):
    pass


def _bot_username(settings: Settings) -> str:
    if name := os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"):
        return name
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe", timeout=10
        )
        return response.json()["result"]["username"]
    except Exception:
        log.warning("could not look up the bot username", extra={"event": "dashboard_config"})
        return ""


def _session_secret(settings: Settings) -> str:
    if secret := os.environ.get("DASHBOARD_SECRET", "").strip():
        return secret
    # Stable fallback for local use; production sets DASHBOARD_SECRET (render.yaml generates it).
    key = settings.telegram_bot_token.encode()
    return hmac.new(key, b"dashboard-session", hashlib.sha256).hexdigest()


def money(value: Any) -> str:
    if value is None:
        return "—"
    amount = Decimal(value)
    if amount >= 1_000_000:
        return f"KES {amount / 1_000_000:.2f}M".replace(".00M", "M")
    return f"KES {amount:,.0f}"


def create_app(
    settings: Settings | None = None,
    factory: sessionmaker[Session] | None = None,
    bot_username: str | None = None,
) -> FastAPI:
    if settings is None:
        load_env_file()
        settings = load_settings()
    if factory is None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        factory = make_session_factory(make_engine(settings.database_url))
    username = _bot_username(settings) if bot_username is None else bot_username

    app = FastAPI(title="Brains Bot dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(settings),
        session_cookie="brains_dashboard",
        max_age=7 * 24 * 3600,
        same_site="lax",
        https_only=os.environ.get("DASHBOARD_HTTPS_ONLY", "1") != "0",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = money
    templates.env.filters["phone"] = mask_phone
    templates.env.globals["timing_labels"] = metrics.TIMING_LABELS

    def db():
        with session_scope(factory) as session:
            yield session

    def user(request: Request) -> dict[str, Any]:
        uid = request.session.get("uid")
        if uid is None or not settings.is_allowed(uid):
            raise LoginRequired
        return {"id": uid, "name": request.session.get("name", "")}

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        context = {"csrf": request.session["csrf"], "today": date.today(), **context}
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def check_csrf(request: Request, token: str) -> None:
        if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(status_code=400, detail="Form expired. Reload the page.")

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", media_type="text/plain")

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, error: str | None = None):
        return render(
            request, "login.html", {"bot_username": username, "error": error, "user": None}
        )

    @app.get("/auth/telegram")
    def auth_telegram(request: Request):
        data = dict(request.query_params)
        uid = verify_login(data, settings.telegram_bot_token)
        if uid is None:
            return RedirectResponse("/login?error=invalid", status_code=303)
        if not settings.is_allowed(uid):
            log.warning(
                "dashboard login denied", extra={"event": "dashboard_denied", "user_id": uid}
            )
            return RedirectResponse("/login?error=denied", status_code=303)
        request.session.clear()
        request.session.update(uid=uid, name=data.get("first_name", ""))
        log.info("dashboard login", extra={"event": "dashboard_login", "user_id": uid})
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, me=Depends(user), session: Session = Depends(db)):
        crm = metrics.crm(session, date.today())
        bot = metrics.bot_stats(session)
        revenue = metrics.revenue(session)
        charts = {
            "pipeline": {
                "labels": [p["name"] for p in crm.pipeline],
                "series": [
                    {
                        "key": s,
                        "label": metrics.TIMING_LABELS[s],
                        "values": [p["counts"][s] for p in crm.pipeline],
                    }
                    for s in ("in_window", "retrofit_only", "early")
                ],  # fmt: skip
            },
            "pitched": {
                "labels": [p["name"] for p in bot.pitched],
                "series": [
                    {
                        "key": "analyses",
                        "label": "Analyses",
                        "values": [p["analyses"] for p in bot.pitched],
                    },
                    {
                        "key": "drafts",
                        "label": "Drafts",
                        "values": [p["drafts"] for p in bot.pitched],
                    },
                ],  # fmt: skip
            },
        }
        unknown = crm.pipeline[0]["counts"]["unknown"] if crm.pipeline else 0
        today = date.today()
        top = opportunities.rank(session, today, limit=5)
        waiting = followups.awaiting_reply(session, today)
        to_chase = followups.quotes_to_chase(session, today)
        return render(
            request,
            "overview.html",
            {"user": me, "crm": crm, "bot": bot, "revenue": revenue, "charts": charts,
             "unknown_stage": unknown, "top": top, "waiting": waiting, "to_chase": to_chase,
             "reply_days": followups.REPLY_WAIT_DAYS, "quote_days": followups.QUOTE_WAIT_DAYS},
        )  # fmt: skip

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities_page(
        request: Request, category: str = "", me=Depends(user), session: Session = Depends(db)
    ):
        categories = list(
            session.scalars(select(Category).where(Category.active).order_by(Category.id))
        )
        chosen = next((c for c in categories if c.key == category), None)
        items = opportunities.rank(session, date.today(), chosen.key if chosen else None, 50)
        return render(
            request,
            "opportunities.html",
            {"user": me, "items": items, "categories": categories, "chosen": chosen},
        )

    @app.get("/projects", response_class=HTMLResponse)
    def projects(
        request: Request,
        q: str = "",
        page: int = 1,
        me=Depends(user),
        session: Session = Depends(db),
    ):
        query = svc.ordered_projects()
        if q.strip():
            query = query.where(svc.search_clause(q))
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 1), pages)
        rows = []
        for p in session.scalars(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)):
            stage = detect_stage([(n.observed_date, n.text) for n in p.notes])
            rows.append({"project": p, "stage": stage})
        return render(
            request,
            "projects.html",
            {"user": me, "rows": rows, "q": q, "page": page, "pages": pages, "total": total},
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project(
        request: Request, project_id: int, me=Depends(user), session: Session = Depends(db)
    ):
        detail = svc.get_project(session, project_id, note_limit=20)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such project")
        svc.record_audit(
            session, me["id"], "dashboard_project_view", target=f"project:{project_id}"
        )
        project = session.get(Project, project_id)
        stage = detect_stage([(n.observed_date, n.text) for n in project.notes])
        categories = list(
            session.scalars(select(Category).where(Category.active).order_by(Category.id))
        )
        names = {c.key: c.name for c in categories}
        products = list(
            session.scalars(
                select(CatalogProduct)
                .where(CatalogProduct.active)
                .order_by(CatalogProduct.category_key, CatalogProduct.price_kes)
            )
        )
        analyses = session.scalars(
            select(OpportunityAssessment)
            .where(OpportunityAssessment.project_id == project_id)
            .order_by(OpportunityAssessment.id.desc())
        )
        drafts = session.scalars(
            select(OutreachDraft)
            .where(OutreachDraft.project_id == project_id)
            .order_by(OutreachDraft.id.desc())
        )
        quotes = session.scalars(
            select(Quote).where(Quote.project_id == project_id).order_by(Quote.id.desc())
        )
        return render(
            request,
            "project.html",
            {
                "user": me,
                "d": detail,
                "stage": stage,
                "categories": categories,
                "category_names": names,
                "products": products,
                "product_names": {p.sku: p.name for p in products},
                "analyses": list(analyses),
                "drafts": list(drafts),
                "quotes": list(quotes),
            },
        )

    @app.post("/projects/{project_id}/quotes")
    def add_quote(
        request: Request,
        project_id: int,
        category: str = Form(...),
        amount: str = Form(...),
        product_sku: str = Form(""),
        note: str = Form(""),
        csrf: str = Form(""),
        me=Depends(user),
        session: Session = Depends(db),
    ):
        check_csrf(request, csrf)
        if session.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="No such project")
        if session.scalar(select(Category.id).where(Category.key == category)) is None:
            raise HTTPException(status_code=400, detail="Unknown category")
        try:
            value = Decimal(amount.replace(",", "").strip())
        except InvalidOperation:
            raise HTTPException(status_code=400, detail="The amount must be a number") from None
        if value <= 0:
            raise HTTPException(status_code=400, detail="The amount must be more than zero")
        sku = product_sku.strip() or None
        if sku:
            product_category = session.scalar(
                select(CatalogProduct.category_key).where(CatalogProduct.sku == sku)
            )
            if product_category is None:
                raise HTTPException(status_code=400, detail="Unknown product")
            if product_category != category:
                raise HTTPException(
                    status_code=400, detail="That package belongs to a different category"
                )
        quote = Quote(project_id=project_id, category_key=category, product_sku=sku,
                      amount_kes=value, note=note.strip()[:500], created_by=me["id"])  # fmt: skip
        session.add(quote)
        session.flush()
        svc.record_audit(
            session, me["id"], "quote_created", target=f"project:{project_id}", quote=quote.id
        )
        return RedirectResponse(f"/projects/{project_id}#quotes", status_code=303)

    @app.post("/quotes/{quote_id}/status")
    def quote_status(
        request: Request,
        quote_id: int,
        status: str = Form(...),
        csrf: str = Form(""),
        me=Depends(user),
        session: Session = Depends(db),
    ):
        check_csrf(request, csrf)
        quote = session.get(Quote, quote_id)
        if quote is None or status not in QUOTE_STATUSES:
            raise HTTPException(status_code=404, detail="No such quote")
        quote.status = status
        quote.closed_at = None if status == "open" else func.now()
        svc.record_audit(
            session,
            me["id"],
            f"quote_{status}",
            target=f"project:{quote.project_id}",
            quote=quote.id,
        )
        return RedirectResponse(f"/projects/{quote.project_id}#quotes", status_code=303)

    return app
