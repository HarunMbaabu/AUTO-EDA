"""/website: read a partner's or supplier's website for products and prices, into a catalog
spreadsheet to check and send back (like a brochure)."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from brains_bot.bot.brochure_handlers import send_sheet
from brains_bot.bot.formatting import MAX_MESSAGE, truncate
from brains_bot.bot.handlers import _user_id, restricted
from brains_bot.bot.project_handlers import DB_ERROR, _guard_db, _with_session
from brains_bot.services import brochures, partners, websites
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

NOT_AVAILABLE = "Reading websites needs MISTRAL_API_KEY or DEEPSEEK_API_KEY to be set."
USAGE = (
    "Send /website with a partner's name or a web address, e.g. /website Jane Decor or "
    "/website www.janedecor.co.ke. I read the products and prices and send you a catalog "
    "spreadsheet to check; nothing goes live until you send it back.\n"
    "For a WhatsApp catalogue: open it in WhatsApp, screen-record while scrolling slowly "
    "through it, and send me the video."
)
RECORD = (
    "has a WhatsApp catalogue but no website. Open the catalogue in WhatsApp, screen-record "
    "while scrolling slowly through it, and send me the video (as a video, not a file); I'll "
    "read the products and prices from it."
)


def _target(session: Session, text: str) -> tuple[str | None, str | None, str]:
    """(url, supplier, message): the website to read, or a message saying why not."""
    links = [x for x in partners.find_links(text) if x["kind"] == "website"]
    if links:
        url = links[0]["url"]
        found, _ = partners.directory(session)
        for p in found:  # a partner's own site: their name goes on the products
            if any(x["url"].lower() == url.lower() for x in p.contact.links or []):
                return url, p.contact.company or p.contact.name, ""
        return url, None, ""
    found, total = partners.directory(session, text=text)
    if not found:
        return None, None, f"No partner matches {text!r}. Try a web address instead."
    with_site = [p for p in found if any(x["kind"] == "website" for x in p.contact.links or [])]
    if len(with_site) == 1:
        c = with_site[0].contact
        url = next(x["url"] for x in c.links if x["kind"] == "website")
        return url, c.company or c.name, ""
    if len(with_site) > 1:
        names = "\n".join(
            f"- {p.contact.name}" + (f" ({p.contact.company})" if p.contact.company else "")
            for p in with_site[:8]
        )
        return None, None, f"Several partners match; add more of the name:\n{names}"
    who = found[0].contact.company or found[0].contact.name if total == 1 else "That partner"
    if any(x["kind"] == "whatsapp_catalog" for p in found for x in p.contact.links or []):
        return None, None, f"{who} {RECORD}"
    return (
        None,
        None,
        (
            f"{who} has no website saved. Send /website <web address>, or add their website with "
            "/partner (same phone number)."
        ),
    )


@restricted("website")
async def website_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_db(update, context):
        return
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        await update.effective_message.reply_text(USAGE)
        return
    drafter = context.bot_data.get("drafter")
    if drafter is None:
        await update.effective_message.reply_text(NOT_AVAILABLE)
        return
    actor = _user_id(update)

    def find(session: Session) -> tuple[str | None, str | None, str]:
        found = _target(session, text)
        svc.record_audit(session, actor, "website_read", target=found[0])
        return found

    try:
        url, supplier, message = await _with_session(context, find)
    except Exception:
        log.exception("website lookup failed", extra={"event": "db_error"})
        await update.effective_message.reply_text(DB_ERROR)
        return
    if url is None:
        await update.effective_message.reply_text(truncate(message, MAX_MESSAGE))
        return
    status = await update.effective_message.reply_text(f"Reading {url} … this can take a minute.")
    try:
        reading = await asyncio.to_thread(websites.read_site, url)
        if not reading.text.strip():
            raise AnalysisError("I found no text on that website")
        names = await _with_session(context, partners.category_names)
        hint = f"Products from the website of {supplier}" if supplier else None
        info = await asyncio.to_thread(
            brochures.extract_products, drafter.transport, reading.text, names, hint
        )
    except AnalysisError as exc:
        await status.edit_text(f"I couldn't read that: {exc}")
        return
    except Exception:
        log.exception("website reading failed", extra={"event": "website_error"})
        await status.edit_text("Something went wrong reading that website. Please try again.")
        return
    host = urlsplit(url).hostname or url
    supplier = supplier or info.supplier or host.removeprefix("www.")
    how = {"shopify": "its shop's product list", "woocommerce": "its shop's product list"}.get(
        reading.method, f"{reading.pages} page(s)"
    )
    summary = f"{supplier}: read {how} on {host}."
    if not info.products:
        await status.edit_text(
            summary + "\n\nI found no products with names on it. If the site only shows its "
            "products after loading, send screenshots or a PDF price list instead."
        )
        return
    await send_sheet(update, context, status.edit_text, info, supplier, f"website {host}", summary)
