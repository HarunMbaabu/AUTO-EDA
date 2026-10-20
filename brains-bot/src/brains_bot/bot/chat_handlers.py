"""Chat mode: plain messages (not commands) answered from the project database."""

from __future__ import annotations

import logging
import time
from datetime import date

from sqlalchemy.orm import Session
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from brains_bot.bot import site_handlers
from brains_bot.bot.formatting import truncate
from brains_bot.bot.handlers import _user_id, restricted
from brains_bot.bot.project_handlers import _guard_db, _with_session
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.chat import HISTORY_MESSAGES, ProjectTools

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_REPLY_CHARS = 4000  # Telegram's limit is 4096
HISTORY_TTL_SECONDS = 30 * 60  # follow-ups refer back at most this far

NOT_AVAILABLE = (
    "Chat needs MISTRAL_API_KEY or DEEPSEEK_API_KEY to be set. "
    "Commands still work: send /help for the list."
)


def _history(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    data = context.user_data
    if time.monotonic() - data.get("chat_at", 0) > HISTORY_TTL_SECONDS:
        data["chat_history"] = []
    return data.setdefault("chat_history", [])


@restricted("chat")
async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Message text is never logged: it may contain customer data.
    if not await _guard_db(update, context):
        return
    assistant = context.bot_data.get("assistant")
    if assistant is None:
        await update.effective_message.reply_text(NOT_AVAILABLE)
        return
    if context.user_data.pop(site_handlers.AWAITING, False):
        # The message after a bare /newsite is the site report, not a question.
        await site_handlers.handle_report(update, context, update.effective_message.text or "")
        return
    question = (update.effective_message.text or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except Exception:
        log.debug("typing indicator failed", exc_info=True)

    history = _history(context)
    actor, analyzer = _user_id(update), context.bot_data.get("analyzer")

    def work(session: Session) -> str:
        tools = ProjectTools(session, actor, date.today(), analyzer)
        answer = assistant.answer(tools, list(history), question)
        svc.record_audit(session, actor, "chat", projects=sorted(tools.projects_seen)[:50])
        return answer

    try:
        answer = await _with_session(context, work)
    except AnalysisError as exc:
        await update.effective_message.reply_text(f"Sorry, I couldn't answer that: {exc}")
        return
    except Exception:
        log.exception("chat failed", extra={"event": "chat_error"})
        await update.effective_message.reply_text(
            "Sorry, something went wrong answering that. Please try again."
        )
        return

    history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    del history[:-HISTORY_MESSAGES]
    context.user_data["chat_at"] = time.monotonic()
    await update.effective_message.reply_text(truncate(answer, MAX_REPLY_CHARS))


@restricted("newchat")
async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["chat_history"] = []
    await update.effective_message.reply_text(
        "Started a new conversation. Ask me about the projects in plain words."
    )
