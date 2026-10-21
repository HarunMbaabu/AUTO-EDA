"""Partners: the people and companies we give jobs to (interior designers, painters,
installers, other suppliers), with the work they do and their approximate rates.

Partners are contacts (see services/contacts.py) marked as partners, so the same phone number
is the same person whether they were met on a site or added here. They are added by pasting a
note with /partner; the AI reads names, categories and rates, the bot reads phone numbers and
dates itself, and nothing is stored before Save. Links to share with customers (a website, a
WhatsApp catalogue) are read by the bot too: a wa.me link holds a phone number, so links never
go to the AI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from brains_bot.database.models import Category, Contact, PartnerRate
from brains_bot.importers import normalize as n
from brains_bot.services import catalog, contacts, sites
from brains_bot.services import project_service as svc
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

LIST_LIMIT = 25
# A web address: with http(s)://, or starting www. or wa.me/ (as pasted from WhatsApp or a
# brochure).
_LINK = re.compile(r"\b(?:https?://|www\.|wa\.me/|wa\.link/)[^\s<>()\"']+", re.I)
_WHATSAPP_HOSTS = {"wa.me", "api.whatsapp.com", "www.whatsapp.com", "whatsapp.com", "wa.link"}
LINK_LABELS = {
    "website": "Website",
    "whatsapp_catalog": "WhatsApp catalogue",
    "whatsapp": "WhatsApp",
}
EXTRACT_PROMPT = """\
A sales person in Kenya pasted a note about service providers they can give jobs to (interior \
designers, painters, installers, suppliers, ...). You get JSON with "context" (any intro text) \
and "items" (one entry per provider). Reply with one JSON object {{"partners": [...]}} holding \
exactly {count} objects, one per item, in the same order, each with exactly these keys:
- "name": the person's name as written, or null
- "company": the company name if stated, or null
- "area": where they are based or work, if stated, or null
- "trade": one of {trades}, or null
- "categories": the work they do, as a list of these category keys: {categories}
- "rates": a list of their rates, each {{"category": a category key or null, "job": what the \
rate is for in a few words, "amount": the amount as written (e.g. "1,500", "50k"), \
"amount_max": the upper amount if a range is given, else null, "unit": e.g. "per sqm", \
"per day", "per room", "per job", or null}}; [] if no rate is stated
- "note": anything else useful, in one short sentence, or null
Contact details were removed and appear as [phone], [email] or [link]. Use only what the text \
says; never invent rates or categories.\
"""


@dataclass
class RateDraft:
    category: str | None
    job: str
    amount: Decimal | None
    amount_max: Decimal | None
    unit: str | None


@dataclass
class PartnerDraft:
    message: str
    name: str | None
    company: str | None
    area: str | None
    trade: str | None
    services: list[str]
    rates: list[RateDraft]
    note: str | None
    phone: n.PhoneResult
    noted_on: date | None = None
    existing_contact_id: int | None = None
    existing_name: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return " · ".join(x for x in (self.name, self.company) if x) or "no name"


def find_links(text: str) -> list[dict[str, str]]:
    """Every website or WhatsApp link in the text (map links left out), each once, as
    {"kind": "website" | "whatsapp_catalog" | "whatsapp", "url": ...}."""
    links: dict[str, dict[str, str]] = {}
    for m in _LINK.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if sites._MAP_LINK.match(url):
            continue
        if not url.lower().startswith("http"):
            url = "https://" + url
        parts = urlsplit(url)
        host, path = (parts.hostname or "").lower(), parts.path.lower()
        if not host or "." not in host:
            continue
        if host in _WHATSAPP_HOSTS:
            catalog_link = path.startswith("/c/") or "catalog" in path
            kind = "whatsapp_catalog" if catalog_link else "whatsapp"
        else:
            kind = "website"
        links.setdefault(url.lower(), {"kind": kind, "url": url})
    return list(links.values())


def without_links(text: str) -> str:
    return _LINK.sub("[link]", text)


def link_lines(links: list[dict[str, Any]], indent: str = "   ") -> list[str]:
    return [f"{indent}{LINK_LABELS.get(x.get('kind'), 'Link')}: {x.get('url')}" for x in links]


def _amount(value: Any) -> Decimal | None:
    try:
        return catalog.parse_price(value)
    except ValueError:
        return None


def extract(
    transport: Any,
    message: str,
    categories: dict[str, str],
    today: date | None = None,
    single: bool = False,
) -> list[PartnerDraft]:
    """One draft per provider in the message (a numbered list, or the whole message). With
    single=True the whole text is one provider (e.g. their brochure, whose lists are not
    separate providers)."""
    today = today or date.today()
    message = message.strip()[: sites.MAX_MESSAGE_CHARS]
    intro, items = ("", [message]) if single else sites.split_report(message)
    payload = {
        "context": sites._for_ai(without_links(intro)) or None,
        "items": [sites._for_ai(without_links(item)) for item in items],
    }
    body = {
        "model": transport.model,
        "messages": [
            {
                "role": "system",
                "content": EXTRACT_PROMPT.format(
                    count=len(items),
                    trades=", ".join(
                        f'"{key}"' for key in contacts.TRADES if key not in contacts.GENERIC
                    ),
                    categories=", ".join(f'"{k}" ({v})' for k, v in categories.items()),
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 300 + 250 * len(items),
        "stream": False,
    }
    data = transport._send("POST", "/chat/completions", json=body)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    try:
        answer = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AnalysisError("the AI's answer could not be read; please try again") from exc
    found = answer.get("partners") if isinstance(answer, dict) else None
    if not isinstance(found, list):
        found = [answer] if isinstance(answer, dict) and len(items) == 1 else []
    log.info("partners extracted", extra={"event": "partner_extract", "model": data.get("model")})

    report_date = sites.date_in_text(intro or message, today, relative=True)
    drafts = []
    for i, item in enumerate(items):
        fields = found[i] if i < len(found) and isinstance(found[i], dict) else {}
        services = [c for c in (fields.get("categories") or []) if c in categories]
        rates = []
        for rate in fields.get("rates") or []:
            if not isinstance(rate, dict):
                continue
            category = rate.get("category") if rate.get("category") in categories else None
            amount = _amount(rate.get("amount"))
            job = sites._clean(rate.get("job"), 120) or (categories.get(category or "") or "job")
            if amount is None and not sites._clean(rate.get("job")):
                continue
            rates.append(
                RateDraft(category, job, amount, _amount(rate.get("amount_max")),
                          sites._clean(rate.get("unit"), 60))
            )  # fmt: skip
            if category and category not in services:
                services.append(category)
        drafts.append(
            PartnerDraft(
                message=item,
                name=sites._without_trade(sites._clean(fields.get("name"))),
                company=sites._clean(fields.get("company")),
                area=sites._clean(fields.get("area")),
                trade=contacts.trade_key(sites._clean(fields.get("trade"))),
                services=services,
                rates=rates,
                note=sites._clean(fields.get("note"), 300),
                phone=sites.find_phones(item),
                noted_on=(sites.date_in_text(item, today) if intro else None) or report_date,
                links=find_links(item),
            )
        )
    return drafts


def match_existing(session: Session, drafts: list[PartnerDraft]) -> None:
    """A known phone number adds to that person's entry instead of creating a new one."""
    for draft in drafts:
        known = contacts.by_phone(session, draft.phone.numbers)
        if known is not None:
            draft.existing_contact_id, draft.existing_name = known.id, known.name


def save(
    session: Session, draft: PartnerDraft, today: date, actor: int | None = None
) -> tuple[Contact, bool]:
    on = draft.noted_on or today
    contact, is_new = contacts.save_contact(
        session, draft.name, draft.phone, draft.trade or "other", draft.company, draft.area,
        draft.message, on, actor,
    )  # fmt: skip
    contact.partner = True
    contact.services = list(dict.fromkeys([*(contact.services or []), *draft.services]))
    known = {x["url"].lower() for x in contact.links or []}
    contact.links = [
        *(contact.links or []),
        *(x for x in draft.links if x["url"].lower() not in known),
    ]
    for rate in draft.rates:
        session.add(
            PartnerRate(
                contact_id=contact.id, category_key=rate.category, job=rate.job,
                amount_kes=rate.amount, amount_max_kes=rate.amount_max, unit=rate.unit,
                noted_on=on, created_by=actor,
            )
        )  # fmt: skip
    session.flush()
    return contact, is_new


def format_rate(rate: RateDraft | PartnerRate) -> str:
    low = rate.amount if isinstance(rate, RateDraft) else rate.amount_kes
    high = rate.amount_max if isinstance(rate, RateDraft) else rate.amount_max_kes
    if low is None:
        price = "rate not stated"
    else:
        price = f"KES {low:,.0f}" + (f"–{high:,.0f}" if high else "")
    job = rate.job
    if rate.unit and job.lower().endswith(" " + rate.unit.lower()):
        job = job[: -len(rate.unit) - 1]  # "design per sqm" + "per sqm"
    return f"{job}: {price}" + (f" {rate.unit}" if rate.unit and low is not None else "")


@dataclass
class Partner:
    contact: Contact
    rates: list[PartnerRate] = field(default_factory=list)


def category_keys(session: Session, wanted: str | None) -> list[str] | None:
    """A category key ('interior_design'), a category name or a group ('Finishes & Interiors',
    or just 'finishes') -> the category keys it covers; None if it is neither."""
    if not wanted:
        return None
    from brains_bot.services.categories import active_categories, resolve_category

    key = n.name_key(wanted)
    groups: dict[str, list[str]] = {}
    for c in active_categories(session):
        for group in c.groups or []:
            groups.setdefault(group, []).append(c.key)
    for group, keys in groups.items():
        if key and (n.name_key(group) == key or n.name_key(group).startswith(key + " ")):
            return keys
    category = resolve_category(session, wanted)
    return [category.key] if category else None


def directory(
    session: Session, keys: list[str] | None = None, text: str | None = None, limit: int = 0
) -> tuple[list[Partner], int]:
    """(partners, total) doing any of these categories, matching the words, with their rates
    (newest first). limit=0 means all."""
    query = select(Contact).where(Contact.partner.is_(True))
    if keys:
        rated = select(PartnerRate.contact_id).where(PartnerRate.category_key.in_(keys))
        query = query.where(
            or_(*[Contact.services.contains([k]) for k in keys], Contact.id.in_(rated))
        )
    for word in (text or "").split():
        pattern = svc.like_pattern(word)
        query = query.where(
            or_(
                Contact.name.ilike(pattern, escape="\\"),
                Contact.company.ilike(pattern, escape="\\"),
                Contact.area.ilike(pattern, escape="\\"),
            )
        )
    found = list(session.scalars(query.order_by(Contact.name)))
    total = len(found)
    shown = found[:limit] if limit else found
    rates = session.scalars(
        select(PartnerRate)
        .where(PartnerRate.contact_id.in_([c.id for c in shown]))
        .order_by(PartnerRate.noted_on.desc().nulls_last(), PartnerRate.id.desc())
    )
    by_id = {c.id: Partner(c) for c in shown}
    for rate in rates:
        by_id[rate.contact_id].rates.append(rate)
    return list(by_id.values()), total


def category_names(session: Session) -> dict[str, str]:
    return {
        c.key: c.name
        for c in session.scalars(select(Category).where(Category.active).order_by(Category.id))
    }
