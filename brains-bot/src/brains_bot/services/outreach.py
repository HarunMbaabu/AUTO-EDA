"""WhatsApp outreach drafts: the AI writes, a person reviews and sends.

The bot never sends anything to a customer. A draft gets a WhatsApp link (wa.me) that opens
the message in the operator's own WhatsApp; the operator presses send there. Tapping the
button in the bot records the draft as sent, which is what the dashboard counts.

Data note: the contact's first name, the site name, area, stage and catalog options go to the
AI provider; the phone number never does (it is only used locally to build the link).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brains_bot.database.models import (
    Category,
    Contact,
    OutreachDraft,
    Project,
    ProjectContact,
    ProjectNote,
)
from brains_bot.services import catalog
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.chat import no_contact_details, plain_text
from brains_bot.services.stages import detect_stage

log = logging.getLogger(__name__)

MAX_DRAFT_CHARS = 700
CONTACT_ROLES = ("contact_person", "owner")
_TITLES = {"mr", "mrs", "ms", "miss", "madam", "dr", "eng", "arch", "prof"}

DRAFT_PROMPT = """\
You write short WhatsApp messages for a sales person at {company}, a Kenyan company that \
installs home audio, home cinema and smart home systems. The sales person will read your \
draft, may edit it, and sends it themselves.

Write one message to the contact named in the data, about their building project and the \
product category given. Rules:
- 60 to 110 words, warm and respectful, plain text, no Markdown, at most one emoji.
- Greet them by the name given (keep their title, e.g. "Mr. Richard"). If no name is given, \
use "Hello".
- Mention their project by name or area so they know it is personal.
- Say why now is a sensible time for this category, using only the stage and timing given. \
Never claim to know their plans, budget or preferences.
- You may mention at most two products from catalog_options by their exact names, with \
"from" prices only if given. Never invent products, prices, discounts or deadlines.
- End with a simple question, such as whether they would be open to a short call or a site \
visit.
- Sign off with: {signature}
Reply with the message text only.\
"""


@dataclass
class DraftTarget:
    project: Project
    contact: Contact | None
    category: Category


def first_name(full: str | None) -> str | None:
    """'Mr. Richard & Madam Dorcas Anita' -> 'Mr. Richard'; placeholders -> None."""
    if not full:
        return None
    first = re.split(r"\s*(?:&|/|,|\band\b)\s*", full.strip(), maxsplit=1)[0].strip()
    words = first.replace(".", ". ").split()
    if not words or first.lower() in {"tbc", "tba", "n/a", "na", "not specified"}:
        return None
    if words[0].rstrip(".").lower() in _TITLES and len(words) > 1:
        return f"{words[0].capitalize()} {words[1]}"
    return words[0]


def pick_contact(session: Session, project_id: int) -> Contact | None:
    """The contact person, else the owner; one with a usable phone number beats one without."""
    links = session.execute(
        select(ProjectContact.role, Contact)
        .join(Contact, Contact.id == ProjectContact.contact_id)
        .where(ProjectContact.project_id == project_id, ProjectContact.role.in_(CONTACT_ROLES))
    ).all()
    if not links:
        return None
    usable = ("valid", "multiple")
    links.sort(key=lambda rc: (rc[1].phone_status not in usable, CONTACT_ROLES.index(rc[0])))
    return links[0][1]


def whatsapp_url(phone_e164: str, text: str) -> str:
    digits = re.sub(r"\D", "", phone_e164)
    return f"https://wa.me/{digits}?text={quote(text)}"


def draft_request(session: Session, target: DraftTarget, today: date) -> dict[str, Any]:
    notes = list(
        session.execute(
            select(ProjectNote.observed_date, ProjectNote.text).where(
                ProjectNote.project_id == target.project.id
            )
        )
    )
    stage = detect_stage(notes)
    products = (
        catalog.suggestions(session, target.category.key, stage.stage, limit=3)
        if target.category.has_verified_catalog
        else []
    )
    return {
        "today": today.isoformat(),
        "contact_name": first_name(target.contact.name if target.contact else None),
        "project": {
            "name": no_contact_details(target.project.name),
            "area": no_contact_details(target.project.area),
            "city": no_contact_details(target.project.city),
        },
        "construction_stage": {
            "last_recorded_stage": stage.label,
            "recorded_on": stage.recorded_on.isoformat() if stage.recorded_on else None,
        },
        "category": {"name": target.category.name, "description": target.category.description},
        "catalog_options": [
            {
                "name": no_contact_details(p.name),
                "price": no_contact_details(catalog.price_text(p)),
                "sku": p.sku,
            }
            for p in products
        ],
    }


class Drafter:
    """Writes drafts through a chat-completions provider (Mistral or DeepSeek)."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.model = transport.model

    def write(self, request: dict[str, Any]) -> str:
        company = os.environ.get("OUTREACH_COMPANY", "").strip() or "Intelligent Home"
        signature = os.environ.get("OUTREACH_SIGNATURE", "").strip() or f"The {company} team"
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": DRAFT_PROMPT.format(company=company, signature=signature),
                },
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "max_tokens": 400,
            "stream": False,
        }
        data = self.transport._send("POST", "/chat/completions", json=body)
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        log.info(
            "draft call",
            extra={
                "event": "draft_call",
                "model": data.get("model", self.model),
                "stop_reason": choice.get("finish_reason"),
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            },
        )
        text = plain_text(no_contact_details((choice.get("message") or {}).get("content") or ""))
        text = text.strip().strip('"').strip()
        if not text:
            raise AnalysisError("the AI returned an empty draft")
        return text[:MAX_DRAFT_CHARS]


def create_draft(
    session: Session, drafter: Drafter, target: DraftTarget, actor: int | None, today: date
) -> OutreachDraft:
    request = draft_request(session, target, today)
    text = drafter.write(request)
    draft = OutreachDraft(
        project_id=target.project.id,
        category_key=target.category.key,
        contact_id=target.contact.id if target.contact else None,
        text=text,
        # Only the products the message actually names, for the dashboard's "top packages".
        product_skus=[
            p["sku"] for p in request["catalog_options"] if p["name"].lower() in text.lower()
        ],
        model=drafter.model,
        created_by=actor,
    )
    session.add(draft)
    session.flush()
    return draft


def mark_sent(session: Session, draft_id: int, actor: int | None) -> OutreachDraft | None:
    draft = session.get(OutreachDraft, draft_id)
    if draft is not None and draft.sent_at is None:
        draft.sent_at, draft.sent_by = func.now(), actor
        session.flush()
        session.refresh(draft)
    return draft


def mark_replied(session: Session, draft_id: int) -> OutreachDraft | None:
    draft = session.get(OutreachDraft, draft_id)
    if draft is not None and draft.replied_at is None:
        draft.replied_at = func.now()
        session.flush()
        session.refresh(draft)
    return draft
