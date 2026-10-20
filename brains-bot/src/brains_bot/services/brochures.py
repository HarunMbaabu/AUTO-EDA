"""Supplier and partner brochures (PDFs and photos): read them into the catalog, and keep a
library of them to forward to customers.

Reading: a PDF with a text layer is read locally; scanned or pictorial pages and photos go to
Mistral's document reader (OCR). The AI then lists the products it sees. Nothing goes live
from a brochure directly: the bot returns a catalog spreadsheet for a person to check and send
back, so misread prices never reach drafts.

Library: the file itself stays on Telegram's servers; the bot keeps its file id, the supplier,
categories and a title, and sends it again on request.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from brains_bot.database.models import Brochure, CatalogProduct, Category
from brains_bot.importers.normalize import name_key, redact_contact_details
from brains_bot.services import catalog
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

OCR_MODEL = "mistral-ocr-latest"
MAX_PAGES = 30
MIN_TEXT_PER_PAGE = 80  # fewer characters than this per page: a pictorial or scanned PDF
MIN_PICTURE_BYTES = 15_000
TRANSCRIBE_PROMPT = (
    "Transcribe all the text in this brochure page: product names, descriptions, prices and "
    "units exactly as written. Plain text only."
)
MAX_TEXT_CHARS = 24_000
MAX_PRODUCTS = 80
LIBRARY_LIMIT = 10

EXTRACT_PROMPT = """\
You read the text of a supplier's brochure or price list for a Kenyan company that sells and \
installs building products and home technology. Reply with one JSON object with exactly these \
keys:
- "supplier": the company the brochure is from, or null
- "title": a short title for the brochure (e.g. "Outdoor speakers 2026"), at most 8 words
- "categories": the category keys the brochure covers, from: {categories}
- "products": up to {limit} products or packages, each {{"category": a category key from the \
list, "name": as written, "brand": or null, "description": one short line, "price": the price \
exactly as written (e.g. "KES 68,000", "from 42,500") or null if none is shown, "price_unit": \
e.g. "each", "pair", "per sqm", "package (from)", or null}}
Contact details were removed and appear as [phone] or [email]. Use only what the text says: \
never invent products or prices. A product with no price shown gets null.\
"""


@dataclass
class Reading:
    text: str
    pages: int
    method: str  # "text" (read locally) | "ocr" (Mistral's document reader) | "vision"


@dataclass
class ProductDraft:
    category: str
    name: str
    brand: str | None
    description: str
    price: str | None
    price_unit: str | None


@dataclass
class BrochureInfo:
    supplier: str | None
    title: str
    categories: list[str]
    products: list[ProductDraft] = field(default_factory=list)


def _ocr(transport: Any, document: dict[str, Any]) -> list[str]:
    data = transport._send("POST", "/ocr", json={"model": OCR_MODEL, "document": document})
    pages = data.get("pages") or []
    log.info("ocr call", extra={"event": "ocr_call", "command": str(len(pages))})
    return [p.get("markdown") or "" for p in pages[:MAX_PAGES]]


def _look(transport: Any, image: bytes, mime: str) -> str:
    """Transcribe one picture with the chat model (Mistral's models read images)."""
    url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    body = {
        "model": transport.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                    {"type": "image_url", "image_url": url},
                ],
            }
        ],
        "max_tokens": 2000,
        "stream": False,
    }
    data = transport._send("POST", "/chat/completions", json=body)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _pictures(reader: Any) -> list[tuple[bytes, str]]:
    """The pictures inside a PDF's pages (pictorial brochures are mostly one per page)."""
    found = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            images = list(page.images)
        except Exception:
            log.warning("pdf picture skipped", extra={"event": "brochure_error"})
            continue
        for image in images:
            data = image.data
            if len(data) < MIN_PICTURE_BYTES:  # logos and icons
                continue
            mime = "image/png" if image.name.lower().endswith(".png") else "image/jpeg"
            found.append((data, mime))
    return found[:MAX_PAGES]


def _need_mistral(transport: Any) -> None:
    if getattr(getattr(transport, "provider", None), "key", None) != "mistral":
        raise AnalysisError("reading scanned brochures and photos needs MISTRAL_API_KEY")


def read_document(transport: Any, data: bytes, mime: str) -> Reading:
    """The brochure's text. PDFs with a text layer never leave the bot for this step; pictures
    go to Mistral's document reader, or, if that is not available on the account, to the chat
    model one picture at a time."""
    if mime == "application/pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages[:MAX_PAGES]]
        except Exception as exc:  # a damaged or encrypted PDF
            raise AnalysisError("that PDF could not be opened") from exc
        if pages and sum(len(p.strip()) for p in pages) / len(pages) >= MIN_TEXT_PER_PAGE:
            return Reading("\n\n".join(pages), len(reader.pages), "text")
        _need_mistral(transport)
        encoded = base64.b64encode(data).decode()
        try:
            document = {"type": "document_url", "document_url": f"data:{mime};base64,{encoded}"}
            return Reading("\n\n".join(_ocr(transport, document)), len(reader.pages), "ocr")
        except AnalysisError:
            pictures = _pictures(reader)
            if not pictures:
                raise AnalysisError(
                    "this PDF has no text or pictures I can read; send photos of its pages"
                ) from None
            text = "\n\n".join(_look(transport, image, kind) for image, kind in pictures)
            return Reading(text, len(reader.pages), "vision")
    if mime.startswith("image/"):
        _need_mistral(transport)
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        try:
            return Reading("\n\n".join(_ocr(transport, {"type": "image_url", "image_url": url})),
                           1, "ocr")  # fmt: skip
        except AnalysisError:
            return Reading(_look(transport, data, mime), 1, "vision")
    raise AnalysisError("send a PDF or a photo")


def _clean(value: Any, limit: int = 200) -> str | None:
    text = re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""
    if not text or text.lower() in ("null", "none", "n/a"):
        return None
    return redact_contact_details(text)[:limit]


def extract_products(
    transport: Any, text: str, categories: dict[str, str], hint: str | None = None
) -> BrochureInfo:
    """Supplier, title, categories and products from the brochure's text."""
    content = redact_contact_details(text)[:MAX_TEXT_CHARS]
    if hint:
        content = f"(Note from the sales team: {redact_contact_details(hint)})\n\n{content}"
    body = {
        "model": transport.model,
        "messages": [
            {
                "role": "system",
                "content": EXTRACT_PROMPT.format(
                    categories=", ".join(f'"{k}" ({v})' for k, v in categories.items()),
                    limit=MAX_PRODUCTS,
                ),
            },
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4000,
        "stream": False,
    }
    data = transport._send("POST", "/chat/completions", json=body)
    raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnalysisError("the AI's answer could not be read; please try again") from exc
    if not isinstance(answer, dict):
        answer = {}
    products = []
    for item in answer.get("products") or []:
        if not isinstance(item, dict) or item.get("category") not in categories:
            continue
        name = _clean(item.get("name"), 120)
        if name:
            products.append(
                ProductDraft(item["category"], name, _clean(item.get("brand"), 80),
                             _clean(item.get("description")) or "", _clean(item.get("price"), 40),
                             _clean(item.get("price_unit"), 40))
            )  # fmt: skip
    found = [k for k in answer.get("categories") or [] if k in categories]
    for p in products:
        if p.category not in found:
            found.append(p.category)
    return BrochureInfo(
        supplier=_clean(answer.get("supplier"), 120),
        title=_clean(answer.get("title"), 80) or "Brochure",
        categories=found,
        products=products[:MAX_PRODUCTS],
    )


def _install_stage(category: Category | None) -> str:
    """A starting guess from the category's best window; the person checks it."""
    if category is None or category.best_to >= 7:
        return "any time"
    return "first fix" if category.best_to <= 4 else "finishing"


def _price_cell(price: str | None) -> str:
    """'KES 68,000' -> '68000'; text the catalog cannot read is left for the person to fix."""
    if not price:
        return ""
    try:
        value = catalog.parse_price(price)
    except ValueError:
        return price
    return f"{value:.0f}" if value is not None else ""


def catalog_sheet(
    session: Session, info: BrochureInfo, supplier: str, source: str, path: Path
) -> Path:
    """A catalog spreadsheet for the person to check and send back: this supplier's products
    already in the catalog (so loading the file keeps them) plus the brochure's products."""
    categories = {c.key: c for c in session.scalars(select(Category))}
    wb = Workbook()
    ws = wb.active
    ws.title = catalog.SHEET
    ws.append(list(catalog.COLUMNS))
    ws.append(["Category key (see /categories)", "Your product code", "Product name"])
    key = catalog.supplier_key(supplier)
    existing = [
        p
        for p in session.scalars(select(CatalogProduct).where(CatalogProduct.active))
        if catalog.supplier_key(p.supplier) == key
    ]
    for p in existing:
        ws.append([
            p.category_key, p.sku, p.name, p.brand, p.supplier, p.description,
            f"{p.price_kes:.0f}" if p.price_kes is not None else "", p.price_unit,
            p.install_stage, "yes" if p.retrofit_ok else "no", p.available,
            p.price_checked.isoformat() if p.price_checked else None, p.notes,
        ])  # fmt: skip
    taken = {s.lower() for s in session.scalars(select(CatalogProduct.sku))}
    prefix = (re.sub(r"[^A-Z0-9]", "", supplier.upper())[:6] or "SUP") + "-"
    number = 0
    for product in info.products:
        number += 1
        while f"{prefix}{number:03d}".lower() in taken:
            number += 1
        unit = product.price_unit or ""
        if product.price and product.price.lower().startswith("from") and "from" not in unit:
            unit = f"{unit} (from)".strip()
        ws.append([
            product.category, f"{prefix}{number:03d}", product.name, product.brand, supplier,
            product.description, _price_cell(product.price), unit or None,
            _install_stage(categories.get(product.category)), "no", None, None,
            f"From brochure {source}: check price and install stage",
        ])  # fmt: skip
    widths = (14, 12, 34, 12, 18, 40, 12, 16, 12, 10, 10, 12, 40)
    for column, width in zip("ABCDEFGHIJKLM", widths, strict=True):
        ws.column_dimensions[column].width = width
    wb.save(path)
    return path


def save(
    session: Session,
    *,
    file_id: str,
    file_unique_id: str,
    kind: str,
    file_name: str | None,
    mime: str | None,
    size: int | None,
    title: str,
    supplier: str | None,
    categories: list[str],
    pages: int | None,
    contact_id: int | None = None,
    actor: int | None = None,
) -> Brochure:
    """Add to the library; the same file sent again updates its entry."""
    brochure = session.scalar(select(Brochure).where(Brochure.file_unique_id == file_unique_id))
    if brochure is None:
        brochure = Brochure(file_unique_id=file_unique_id, created_by=actor)
        session.add(brochure)
    brochure.file_id, brochure.kind, brochure.file_name = file_id, kind, file_name
    brochure.mime, brochure.size, brochure.title = mime, size, title
    brochure.supplier = supplier or brochure.supplier
    brochure.category_keys = list(dict.fromkeys([*(brochure.category_keys or []), *categories]))
    brochure.pages = pages
    brochure.contact_id = contact_id or brochure.contact_id
    session.flush()
    return brochure


def library(
    session: Session, keys: list[str] | None = None, text: str | None = None, limit: int = 0
) -> tuple[list[Brochure], int]:
    """(brochures, total) for these categories and words in title, supplier or file name."""
    query = select(Brochure)
    if keys:
        query = query.where(or_(*[Brochure.category_keys.contains([k]) for k in keys]))
    for word in (text or "").split():
        pattern = svc.like_pattern(word)
        query = query.where(
            or_(
                Brochure.title.ilike(pattern, escape="\\"),
                Brochure.supplier.ilike(pattern, escape="\\"),
                Brochure.file_name.ilike(pattern, escape="\\"),
            )
        )
    found = list(session.scalars(query.order_by(Brochure.created_at.desc(), Brochure.id.desc())))
    return (found[:limit] if limit else found), len(found)


def label(brochure: Brochure) -> str:
    who = f" · {brochure.supplier}" if brochure.supplier else ""
    pages = f", {brochure.pages} pages" if brochure.pages and brochure.pages > 1 else ""
    kind = (
        "photo"
        if brochure.kind == "photo"
        else "PDF"
        if brochure.mime == "application/pdf"
        else "file"
    )
    return f"{brochure.title}{who} ({kind}{pages})"


def supplier_from(caption: str | None) -> str | None:
    """A caption like 'Sauti Sound' or 'catalog Sauti Sound' names the supplier."""
    words = (caption or "").split()
    while words and name_key(words[0]) in ("catalog", "catalogue", "brochure", "partner"):
        words = words[1:]
    return " ".join(words)[:120] or None
