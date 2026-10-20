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
from brains_bot.services.categories import category_definitions
from brains_bot.services.chat import no_contact_details, plain_text
from brains_bot.services.stages import STAGES, assess_timing, detect_stage

log = logging.getLogger(__name__)

MAX_DRAFT_CHARS = 700
CONTACT_ROLES = ("contact_person", "owner")
_TITLES = {"mr", "mrs", "ms", "miss", "madam", "dr", "eng", "arch", "prof"}

DRAFT_PROMPT = """\
You write short WhatsApp follow-up messages for a sales person at {company}, a Kenyan company \
that supplies and installs home technology (audio, home cinema, smart home, security, solar), \
finishes and interiors (paint, waterproofing, flooring, lighting, joinery, curtains), exterior \
works (paving, gardens, fencing, outdoor audio) and utilities (water, air conditioning). The \
sales person reads your draft, may edit it, and sends it themselves.

Write one message to the contact named in the data, about their building project and the \
category given. Make it feel like a personal follow-up from someone who knows the site:
- Open with their name (keep a title, e.g. "Mr. Richard"; "Hello" if no name is given) and a \
hook from the project: the site name or area, and what latest_site_note says about progress \
("since the Westlands villa is almost finished", "now that the walls are drying"). Use only \
facts in the data; never invent progress, compliments or details of the site, and never \
mention a building step as still to come when the stage says it is done (a skimmed house \
already has its roof).
- Mention a plan or event from latest_site_note ("the client visits on Saturday") only if \
days_ago is 7 or less; older plans may have passed.
- If previous_message is given, this is a follow-up: refer back to it naturally ("following \
up on the painting").
- Then the pitch, using pitch_angle, and one clear reason why now, taken only from why_now \
(e.g. cabling is cheaper before plastering, waterproofing must go in before tiling).
- You may suggest one item from goes_well_with as an option that works with it ("we can \
integrate it with the audio system"). Never write "your" before anything from \
goes_well_with (not "your home cinema", not "your future smart home"): they may not have it. \
Offer it as an option instead ("and if you are planning a home cinema, it can power that too").
- You may name at most two products from catalog_options, by their exact names, with "from" \
prices only if given. Never invent products, prices, discounts or deadlines.
- End with one easy next step as a question: a free site visit, a short call, or sending a \
quote or photos of similar work.
- 50 to 100 words, warm and confident, plain text, no Markdown, at most one emoji.
- Sign off on its own last line with: {signature}
Style examples (for tone only; not facts about this customer):
"Hi John, since the Westlands villa is almost finished, have you considered a solar backup? It \
keeps your new home cinema and smart home running even during power cuts, and we can \
integrate it with the audio system."
"Hi Sarah, following up on the painting. Now that the walls are drying, it is the right time \
to look at acoustic panels for the media room, so we can finish the interior design."
"Hi David, the landscaping looks great. Don't forget we can add weatherproof outdoor speakers \
so you can enjoy music on the new patio."
Reply with the message text only.\
"""
# How to pitch each group, after the sales team's own examples.
PITCH_ANGLES = {
    "Tech & Entertainment": "high-value upgrade: comfort, entertainment and peace of mind, "
    "and everything working together (e.g. solar backup keeps the cinema and smart home on)",
    "Finishes & Interiors": "tie it to the build timeline: what the current stage means for "
    "the next finish, so the work is done once and done right",
    "Exterior & Landscaping": "upsell on the finished outdoor space: how they will enjoy it "
    "(music on the patio, a secure gate, a neat driveway)",
    "Utilities": "reliability and running costs: water, power and cooling they can count on",
}
NOTE_CHARS = 300


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


def why_now(category: Category, stage, today: date) -> str:
    """The timing argument for this stage only, so the AI cannot borrow advice for a step
    that is already done (e.g. "before plastering" on a skimmed house)."""
    timing = assess_timing(category, stage, today)
    if timing.status == "in_window":
        return category.timing_note
    if timing.status == "early":
        return (
            f"It is early: a good time to plan it, so it is ready when the build reaches "
            f"{STAGES[category.best_from]}."
        )
    if timing.status == "retrofit_only":
        return (
            "The ideal stage for wiring or built-in work has passed: pitch options that can be "
            "added now without disturbing the finished work. Do not mention earlier building steps."
        )
    return "Do not mention construction timing; focus on the benefit for their home."


def _names(session: Session) -> dict[str, str]:
    return dict(session.execute(select(Category.key, Category.name)).all())


def draft_request(session: Session, target: DraftTarget, today: date) -> dict[str, Any]:
    notes = list(
        session.execute(
            select(ProjectNote.observed_date, ProjectNote.text)
            .where(ProjectNote.project_id == target.project.id)
            .order_by(ProjectNote.id)
        )
    )
    stage = detect_stage(notes)
    latest = max(notes, key=lambda n: n[0] or date.min, default=None)
    definition = category_definitions().get(target.category.key, {})
    names = _names(session)
    pairs = [names[k] for k in definition.get("pairs_with", []) if k in names]
    previous = session.execute(
        select(OutreachDraft.category_key, OutreachDraft.sent_at, OutreachDraft.replied_at)
        .where(OutreachDraft.project_id == target.project.id, OutreachDraft.sent_at.is_not(None))
        .order_by(OutreachDraft.sent_at.desc())
        .limit(1)
    ).first()
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
            "stage_number": stage.stage,
            "last_recorded_stage": stage.label,
            "recorded_on": stage.recorded_on.isoformat() if stage.recorded_on else None,
        },
        "latest_site_note": (
            {
                "date": latest[0].isoformat() if latest[0] else None,
                "days_ago": (today - latest[0]).days if latest[0] else None,
                "text": no_contact_details(latest[1])[:NOTE_CHARS],
            }
            if latest
            else None
        ),
        "category": {"name": target.category.name, "description": target.category.description},
        "pitch_angle": PITCH_ANGLES.get((target.category.groups or [""])[0], ""),
        "why_now": why_now(target.category, stage, today),
        "goes_well_with": pairs[:3],
        "previous_message": (
            {
                "about": names.get(previous[0], previous[0]),
                "sent_on": previous[1].date().isoformat(),
                "they_replied": previous[2] is not None,
            }
            if previous
            else None
        ),
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
        text = self._write(request)
        problem = draft_problem(text, request)
        if problem:  # one rewrite with the mistake pointed out
            text = self._write(request, fix=f"Rewrite the message: {problem}")
        # Whatever is left: "your future smart home" -> "a smart home".
        for item in request.get("goes_well_with") or []:
            text = re.sub(
                rf"\byour\s+(?:future\s+|new\s+)?({re.escape(item)})",
                r"a \1",
                text,
                flags=re.IGNORECASE,
            )
        return text

    def _write(self, request: dict[str, Any], fix: str | None = None) -> str:
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
                *([{"role": "user", "content": fix}] if fix else []),
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
        if signature.lower() not in text.lower():
            text = f"{text}\n\n{signature}"
        elif not text.endswith(f"\n{signature}"):  # keep the sign-off on its own last line
            head = text[: text.lower().rindex(signature.lower())].rstrip()
            text = f"{head}\n\n{signature}"
        return text[:MAX_DRAFT_CHARS]


# Words for building steps; a draft must not talk about one as still to come once it is done.
_STEP_WORDS = {1: r"\bfoundation", 3: r"\broof", 4: r"\bplaster"}


def draft_problem(text: str, request: dict[str, Any]) -> str | None:
    """What a draft claims that the data does not support, or None."""
    lower = text.lower()
    for item in request.get("goes_well_with") or []:
        if re.search(rf"\byour\s+(?:\w+\s+){{0,2}}{re.escape(item.lower())}", lower):
            return (
                f'it says "your {item}", but they may not have one. Offer it as an option '
                f'instead ("if you are planning ...").'
            )
    stage = (request.get("construction_stage") or {}).get("stage_number")
    note = ((request.get("latest_site_note") or {}).get("text") or "").lower()
    for number, pattern in _STEP_WORDS.items():
        if stage and stage > number and re.search(pattern, lower) and not re.search(pattern, note):
            word = pattern.removeprefix(r"\b")
            return f"it mentions the {word}, which is already done at this stage; leave it out."
    return None


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
