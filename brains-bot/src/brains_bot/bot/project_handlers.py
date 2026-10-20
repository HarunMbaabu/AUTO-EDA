"""Project lookup commands: /projects, /search, /project."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from brains_bot.bot import formatting
from brains_bot.bot.handlers import (
    NOT_AUTHORIZED,
    _is_authorized,
    _log_command,
    _user_id,
    restricted,
)
from brains_bot.database.session import session_scope
from brains_bot.services import project_service as svc

log = logging.getLogger(__name__)

NO_DATABASE = "The project database is not configured yet (DATABASE_URL is not set)."
DB_ERROR = "Sorry, I couldn't reach the project database. Please try again later."
PAGE_CALLBACK = re.compile(r"^projects:(\d{1,5})$")


async def _with_session[T](context: ContextTypes.DEFAULT_TYPE, work: Callable[[Session], T]) -> T:
    factory: sessionmaker[Session] = context.bot_data["db"]

    def run() -> T:
        with session_scope(factory) as session:
            return work(session)

    return await asyncio.to_thread(run)


async def _guard_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.bot_data.get("db") is None:
        await update.effective_message.reply_text(NO_DATABASE)
        return False
    return True


def _pager(page: int, pages: int) -> InlineKeyboardMarkup | None:
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀ Previous", callback_data=f"projects:{page - 1}"))
    if page < pages:
        buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"projects:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def _project_page(
    context: ContextTypes.DEFAULT_TYPE, actor: int | None, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    def work(session: Session) -> tuple[list[svc.ProjectSummary], int, int]:
        items, pages = svc.list_projects(session, page)
        current = min(max(page, 1), pages)
        svc.record_audit(session, actor, "project_list", page=current)
        return items, pages, current

    items, pages, current = await _with_session(context, work)
    text = formatting.project_list(f"Projects (page {current} of {pages})", items)
    return text, _pager(current, pages)


@restricted("projects")
async def projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    try:
        text, markup = await _project_page(context, _user_id(update), page)
    except Exception:
        log.exception("project list failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(text, reply_markup=markup)


async def projects_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "projects_page", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = PAGE_CALLBACK.match(query.data or "")
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    try:
        text, markup = await _project_page(context, _user_id(update), int(match.group(1)))
    except Exception:
        log.exception("project page failed", extra={"event": "db_error"})
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=markup)


@restricted("search")
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    term = " ".join(context.args or []).strip()
    if len(term) < 2 or len(term) > 100:
        await update.effective_message.reply_text(
            "Usage: /search <text>\nExample: /search Karen\n"
            "Searches site names, areas, cities, addresses and the names of owners, "
            "contractors, architects, companies and contact people (2 to 100 characters)."
        )
        return
    actor = _user_id(update)

    def work(session: Session) -> list[svc.ProjectSummary]:
        found = svc.search_projects(session, term)
        # The search term itself is not stored: it may contain a person's name.
        svc.record_audit(session, actor, "project_search", results=len(found))
        return found

    try:
        found = await _with_session(context, work)
    except Exception:
        log.exception("project search failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    more = (
        f"\n\nShowing the first {svc.SEARCH_LIMIT}. Try a more specific search."
        if len(found) >= svc.SEARCH_LIMIT
        else ""
    )
    title = f"Search results ({len(found)})"
    await update.effective_message.reply_text(formatting.project_list(title, found, more))


@restricted("project")
async def project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    raw = (context.args or [""])[0].lstrip("#")
    if not raw.isdigit() or len(raw) > 9:
        await update.effective_message.reply_text(
            "Usage: /project <number>\nExample: /project 12\n"
            "Find numbers with /projects or /search."
        )
        return
    project_id, actor = int(raw), _user_id(update)

    def work(session: Session) -> svc.ProjectDetail | None:
        detail = svc.get_project(session, project_id)
        svc.record_audit(
            session, actor, "project_view", target=f"project:{project_id}", found=bool(detail)
        )
        return detail

    try:
        detail: Any = await _with_session(context, work)
    except Exception:
        log.exception("project view failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    if detail is None:
        await update.effective_message.reply_text(f"No project #{project_id}.")
        return
    await update.effective_message.reply_text(
        formatting.project_detail(detail), disable_web_page_preview=True
    )
