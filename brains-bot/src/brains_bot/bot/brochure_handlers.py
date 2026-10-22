"""Brochures sent to the bot (PDFs, photos, and screen recordings of a WhatsApp catalogue):
read them into the catalog, keep them in the library, or add them to a partner; /brochures finds
them again to forward to customers."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from brains_bot.bot import partner_handlers
from brains_bot.bot.formatting import truncate
from brains_bot.bot.handlers import (
    NOT_AUTHORIZED,
    _is_authorized,
    _log_command,
    _user_id,
    restricted,
)
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.database.models import Brochure
from brains_bot.services import brochures, partners
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

FILE_CALLBACK = re.compile(r"^broc:(catalog|save|partner|cancel):([A-Za-z0-9_-]{6,20})$")
SEND_CALLBACK = re.compile(r"^brochure:(one|cat):([a-z0-9_]{1,50})$")
PENDING = "pending_brochures"
MAX_BYTES = 20 * 1024 * 1024  # Telegram's download limit for bots
NOT_AVAILABLE = "Reading brochures needs MISTRAL_API_KEY to be set."
UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
SENT_PER_TAP = 3


def _file_of(update: Update) -> dict[str, Any] | None:
    message = update.effective_message
    if message.video:  # a screen recording of a WhatsApp catalogue
        video = message.video
        return {"file_id": video.file_id, "file_unique_id": video.file_unique_id,
                "kind": "video", "file_name": video.file_name, "mime": "video/mp4",
                "size": video.file_size}  # fmt: skip
    if message.photo:
        photo = message.photo[-1]  # the largest size
        return {"file_id": photo.file_id, "file_unique_id": photo.file_unique_id,
                "kind": "photo", "file_name": None, "mime": "image/jpeg",
                "size": photo.file_size}  # fmt: skip
    document = message.document
    if document is None:
        return None
    mime = document.mime_type or ""
    name = document.file_name or ""
    if name.lower().endswith(".pdf"):
        mime = "application/pdf"
    if mime != "application/pdf" and not mime.startswith(("image/", "video/")):
        return None
    return {"file_id": document.file_id, "file_unique_id": document.file_unique_id,
            "kind": "video" if mime.startswith("video/") else "document",
            "file_name": name or None, "mime": mime, "size": document.file_size}  # fmt: skip


def is_brochure(update: Update) -> bool:
    return _file_of(update) is not None


@restricted("brochure")
async def file_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A PDF or photo: ask what it is."""
    if not await _guard_db(update, context):
        return
    found = _file_of(update)
    if found is None:
        return
    if found["size"] and found["size"] > MAX_BYTES:
        await update.effective_message.reply_text(
            "That file is larger than 20 MB, which Telegram won't let me download."
            + (
                " Send the recording as a video (not as a file) so Telegram makes it smaller, "
                "or record a shorter part of the catalogue."
                if found["kind"] == "video"
                else ""
            )
        )
        return
    found["caption"] = (update.effective_message.caption or "").strip()[:200]
    token = secrets.token_urlsafe(9)
    context.user_data.setdefault(PENDING, {})[token] = found
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Read products into the catalog",
                               callback_data=f"broc:catalog:{token}")],
         [InlineKeyboardButton("📚 Save as a brochure only", callback_data=f"broc:save:{token}")],
         [InlineKeyboardButton("📇 A partner's brochure (add the partner)",
                               callback_data=f"broc:partner:{token}")],
         [InlineKeyboardButton("✖ Cancel", callback_data=f"broc:cancel:{token}")]]
    )  # fmt: skip
    await update.effective_message.reply_text(
        "What is this file?\n"
        "- Catalog: I read the products and prices and send you a spreadsheet to check; "
        "nothing goes live until you send it back.\n"
        "- Brochure only: I keep it to forward to customers (/brochures).\n"
        "- Partner: I add the company as a partner with the work they do and any rates.\n"
        "Tip: a caption with the supplier's name (e.g. 'Sauti Sound') helps."
        + (
            "\nA screen recording takes a minute or two to read: I look at it frame by frame."
            if found["kind"] == "video"
            else ""
        ),
        reply_markup=buttons,
    )


async def _read(context: ContextTypes.DEFAULT_TYPE, found: dict[str, Any]) -> brochures.Reading:
    drafter = context.bot_data.get("drafter")
    telegram_file = await context.bot.get_file(found["file_id"])
    data = bytes(await telegram_file.download_as_bytearray())
    if found["kind"] == "video":
        return await asyncio.to_thread(brochures.read_video, drafter.transport, data)
    return await asyncio.to_thread(brochures.read_document, drafter.transport, data, found["mime"])


def _save_kwargs(found: dict[str, Any]) -> dict[str, Any]:
    return {k: found[k] for k in ("file_id", "file_unique_id", "kind", "file_name", "mime",
                                  "size")}  # fmt: skip


async def file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "brochure_button", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = FILE_CALLBACK.match(query.data or "")
    pending = context.user_data.get(PENDING, {})
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    action, token = match.groups()
    found = pending.pop(token, None)
    if found is None:
        await query.answer("This has expired. Send the file again.", show_alert=True)
        return
    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled: nothing was saved.")
        return
    drafter = context.bot_data.get("drafter")
    if drafter is None:
        await query.answer(NOT_AVAILABLE, show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("Reading the brochure… this can take a minute.")
    actor, hint = _user_id(update), found["caption"] or None
    if found["kind"] == "video":
        hint = f"{hint}. {brochures.VIDEO_HINT}" if hint else brochures.VIDEO_HINT
    try:
        reading = await _read(context, found)
        if not reading.text.strip():
            raise AnalysisError("no text could be read from it")
        if action == "partner":
            await _as_partner(update, context, found, reading, token)
            return
        names = await _with_session(context, partners.category_names)
        info = await asyncio.to_thread(
            brochures.extract_products, drafter.transport, reading.text, names, hint
        )
    except AnalysisError as exc:
        pending[token] = found  # let them try again
        await query.edit_message_text(f"I couldn't read that: {exc}")
        return
    except Exception:
        log.exception("brochure reading failed", extra={"event": "brochure_error"})
        pending[token] = found
        await query.edit_message_text("Something went wrong reading that. Please try again.")
        return
    supplier = brochures.supplier_from(hint) or info.supplier or "Unknown supplier"

    def store(session: Session) -> Brochure:
        saved = brochures.save(session, **_save_kwargs(found), title=info.title,
                               supplier=supplier, categories=info.categories,
                               pages=reading.pages, actor=actor)  # fmt: skip
        svc.record_audit(session, actor, f"brochure_{action}", target=f"brochure:{saved.id}")
        return saved

    try:
        saved = await _with_session(context, store)
    except Exception:
        log.exception("saving brochure failed", extra={"event": "db_error"})
        await query.edit_message_text(DB_ERROR)
        return
    covered = ", ".join(names.get(k, k) for k in info.categories) or "no category recognised"
    summary = (
        f"Saved to the library: {brochures.label(saved)}\nCategories: {covered}\n"
        f"Find it with /brochures {info.categories[0] if info.categories else supplier}."
    )
    if action == "save":
        await query.edit_message_text(summary)
        return
    if not info.products:
        await query.edit_message_text(summary + "\n\nI found no products in it to add.")
        return
    what = {"video": "catalogue recording", "photo": "photo"}.get(found["kind"], "brochure")
    source = f"{what} {found['file_name'] or ''}".strip()
    await send_sheet(update, context, query.edit_message_text, info, supplier, source, summary)


async def send_sheet(update, context, edit, info, supplier, source, summary) -> None:
    """The products as a catalog spreadsheet to check and send back; edit() updates the status
    message."""
    priced = sum(1 for p in info.products if p.price)

    with tempfile.TemporaryDirectory() as tmp:
        name = UNSAFE_NAME_CHARS.sub("_", f"catalog_{supplier}.xlsx")
        path = Path(tmp) / name

        def build(session: Session) -> None:
            brochures.catalog_sheet(session, info, supplier, source, path)

        await _with_session(context, build)
        await edit(
            f"{summary}\n\nRead {len(info.products)} products ({priced} with a price). "
            "The spreadsheet is below."
        )
        with path.open("rb") as handle:
            await update.effective_chat.send_document(
                handle,
                filename=name,
                caption=truncate(
                    f"{supplier}: {len(info.products)} products from the {source}, plus any "
                    f"{supplier} products already in the catalog. Check each price, unit and "
                    "install stage (the AI can misread numbers in pictures), fix or delete "
                    "rows, then send this file back to me to load it. Only "
                    f"{supplier}'s products are replaced.",
                    1000,
                ),
            )


async def _as_partner(update, context, found, reading, token) -> None:
    """The brochure's company becomes a partner: the usual /partner preview, then Save."""
    drafter = context.bot_data["drafter"]
    names = await _with_session(context, partners.category_names)
    text = f"{found['caption']}\n{reading.text}" if found["caption"] else reading.text
    if found["kind"] == "video":
        text = f"{brochures.VIDEO_HINT}\n{text}"
    drafts = await asyncio.to_thread(
        partners.extract, drafter.transport, text, names, date.today(), True
    )

    def match(session: Session) -> None:
        partners.match_existing(session, drafts)

    await _with_session(context, match)
    for draft in drafts:
        draft.message = f"Brochure: {found['file_name'] or 'photo'}"
    found["pages"] = reading.pages
    partner_handlers.remember(context, token, drafts, brochure=found)
    await update.callback_query.edit_message_text(
        partner_handlers.preview(drafts, names),
        reply_markup=partner_handlers.save_buttons(token, len(drafts)),
    )


@restricted("brochures")
async def brochures_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    args = [a for a in (context.args or []) if a.strip()]
    actor = _user_id(update)

    def work(session: Session):
        keys, words = None, args
        for take in (3, 2, 1):
            if len(args) >= take:
                found_keys = partners.category_keys(session, " ".join(args[:take]))
                if found_keys:
                    keys, words = found_keys, args[take:]
                    break
        found, total = brochures.library(session, keys, " ".join(words), brochures.LIBRARY_LIMIT)
        svc.record_audit(session, actor, "brochures_list", results=total)
        return [(b.id, brochures.label(b)) for b in found], total

    try:
        found, total = await _with_session(context, work)
    except Exception:
        log.exception("brochure list failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    if not found:
        await update.effective_message.reply_text(
            "No brochures found. Send me a supplier's PDF, a photo of their brochure or a screen "
            "recording of their WhatsApp catalogue to add one. Search with /brochures "
            "<category, group or supplier>, e.g. /brochures audio."
        )
        return
    more = (
        f"\nShowing {len(found)} of {total}; add a word to narrow it down."
        if total > len(found)
        else ""
    )
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"📎 {text}"[:60], callback_data=f"brochure:one:{bid}")]
         for bid, text in found]
    )  # fmt: skip
    await update.effective_message.reply_text(
        f"Brochures ({total}). Tap one to get the file, then forward it to the customer.{more}",
        reply_markup=buttons,
    )


async def send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send brochure files: one by id, or up to three for a category (from under a draft)."""
    query = update.callback_query
    authorized = _is_authorized(update, context)
    _log_command(update, "brochure_send", authorized)
    if not authorized:
        await query.answer(NOT_AUTHORIZED[:190], show_alert=True)
        return
    match = SEND_CALLBACK.match(query.data or "")
    if not match or context.bot_data.get("db") is None:
        await query.answer()
        return
    what, value = match.groups()

    def work(session: Session) -> list[tuple[str, str, str]]:
        if what == "one":
            found = [session.get(Brochure, int(value))] if value.isdigit() else []
        else:
            found, _ = brochures.library(session, [value], limit=SENT_PER_TAP)
        return [(b.kind, b.file_id, brochures.label(b)) for b in found if b is not None]

    try:
        files = await _with_session(context, work)
    except Exception:
        log.exception("brochure send failed", extra={"event": "db_error"})
        await query.answer(DB_ERROR[:190], show_alert=True)
        return
    if not files:
        await query.answer("No brochure found.", show_alert=True)
        return
    await query.answer()
    for kind, file_id, caption in files:
        if kind == "photo":
            await update.effective_chat.send_photo(file_id, caption=caption)
        elif kind == "video":
            await update.effective_chat.send_video(file_id, caption=caption)
        else:
            await update.effective_chat.send_document(file_id, caption=caption)


def draft_button(category_key: str, count: int) -> list[InlineKeyboardButton]:
    """The row under a draft that sends the category's brochures, if there are any."""
    if not count:
        return []
    return [
        InlineKeyboardButton(
            f"📎 Brochures ({min(count, SENT_PER_TAP)})",
            callback_data=f"brochure:cat:{category_key}",
        )
    ]
