"""/categories and /analyze."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from brains_bot.bot import formatting
from brains_bot.bot.handlers import _user_id, restricted
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.database.models import Project
from brains_bot.services import analysis as an
from brains_bot.services import project_service as svc
from brains_bot.services.categories import active_categories, resolve_category

log = logging.getLogger(__name__)

NOT_CONFIGURED = (
    "Claude analysis is not configured yet. The administrator needs to set ANTHROPIC_API_KEY "
    "and CLAUDE_MODEL."
)


@restricted("categories")
async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    try:
        items = await _with_session(context, active_categories)
    except Exception:
        log.exception("category list failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    await update.effective_message.reply_text(formatting.category_list(items))


@restricted("analyze")
async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = list(context.args or [])
    refresh = bool(args) and args[-1].lower() == "refresh"
    if refresh:
        args = args[:-1]
    raw_id = args[0].lstrip("#") if args else ""
    if len(args) < 2 or not raw_id.isdigit() or len(raw_id) > 9:
        await update.effective_message.reply_text(
            "Usage: /analyze <project number> <category> [refresh]\n"
            "Example: /analyze 205 audio\nSee /categories for the list."
        )
        return
    analyzer: an.ClaudeAnalyzer | None = context.bot_data.get("analyzer")
    if analyzer is None:
        await update.effective_message.reply_text(NOT_CONFIGURED)
        return
    project_id, category_text, actor = int(raw_id), " ".join(args[1:]), _user_id(update)

    def prepare(session: Session):
        project = session.get(Project, project_id)
        category = resolve_category(session, category_text)
        if project is None or category is None:
            return project, category, None, None
        ctx = an.build_context(session, project, category, date.today())
        saved = None if refresh else an.recent_assessment(session, ctx)
        return project, category, ctx, saved

    try:
        project, category, ctx, saved = await _with_session(context, prepare)
    except Exception:
        log.exception("analysis preparation failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    if project is None:
        await update.effective_message.reply_text(f"No project #{project_id}.")
        return
    if category is None:
        await update.effective_message.reply_text(
            f"I don't know the category '{category_text}'. See /categories for the list."
        )
        return

    if saved is not None:
        result = an.AnalysisResult.model_validate(saved.result)
        note = f"Saved analysis from {formatting.fmt_date(saved.created_at.date())}. "
        note += f"Add 'refresh' to run it again. Model: {saved.model}."
        await _record(context, actor, project_id, category.key, "analysis_reused")
        await update.effective_message.reply_text(formatting.analysis(ctx, result, note))
        return

    status = await update.effective_message.reply_text(
        f"Analysing project #{project_id} for {category.name}. This can take up to a minute…"
    )
    try:
        result, response = await _run_in_thread(analyzer, ctx)
    except an.AnalysisError as exc:
        await status.edit_text(f"Analysis failed: {exc}")
        return
    except Exception:
        log.exception("analysis failed", extra={"event": "analysis_error"})
        await status.edit_text("Analysis failed because of an unexpected error. Please try again.")
        return

    def save(session: Session):
        assessment = an.save_assessment(session, ctx, result, response, actor, analyzer.model)
        svc.record_audit(
            session, actor, "analysis_created", target=f"project:{project_id}",
            category=category.key, assessment=assessment.id,
        )  # fmt: skip
        return assessment

    try:
        assessment = await _with_session(context, save)
        model = assessment.model
    except Exception:
        log.exception("saving analysis failed", extra={"event": "db_error"})
        model = analyzer.model
    note = f"New analysis. Model: {model}."
    await status.edit_text(formatting.analysis(ctx, result, note))


async def _run_in_thread(analyzer: an.ClaudeAnalyzer, ctx: an.AnalysisContext):
    import asyncio

    return await asyncio.to_thread(analyzer.analyze, ctx)


async def _record(context, actor, project_id, category_key, action) -> None:
    def work(session: Session) -> None:
        svc.record_audit(
            session, actor, action, target=f"project:{project_id}", category=category_key
        )

    try:
        await _with_session(context, work)
    except Exception:
        log.exception("audit write failed", extra={"event": "db_error"})
