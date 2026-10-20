"""New projects from a pasted WhatsApp site report (/newsite).

The phone number and map link are found locally. The AI only sees the message with contact
details removed, and returns the site name, area, city and the contact's name and role. The
operator checks the result before anything is saved. The full original message is kept as the
project's first note, so the construction stage is detected from it like any other note.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brains_bot.database.models import (
    Contact,
    MarketingPermission,
    Project,
    ProjectContact,
    ProjectNote,
)
from brains_bot.importers import normalize as n
from brains_bot.services.analysis import AnalysisError

log = logging.getLogger(__name__)

SOURCE_PREFIX = "tg:"  # source_key of projects added from Telegram, never from the workbook
NOTE_FIELD = "WhatsApp report"
ROLES = ("contact_person", "owner", "contractor", "architect")
MAX_MESSAGE_CHARS = 3000
_MAP_LINK = re.compile(
    r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.)?google\.[a-z.]+/maps)\S*", re.I
)

EXTRACT_PROMPT = """\
You read a short site-visit report that a sales person in Kenya pasted from WhatsApp, about a \
building project. Reply with one JSON object only, with exactly these keys:
- "site_name": the project's name if one is stated; otherwise "<person> project" using the \
main person named, or "<area> site"; never invent other names
- "area": the estate, road or neighbourhood, or null
- "city": the town or city if stated or obvious from a well-known area (e.g. Runda -> Nairobi), \
or null
- "contact_name": the person to talk to, as written (keep titles like Mr., Eng.), or null
- "contact_role": one of "contractor", "owner", "architect", "contact_person"
Contact details were removed and appear as [phone] or [email]. Use only what the message says.\
"""


@dataclass
class SiteDraft:
    message: str
    site_name: str
    area: str | None
    city: str | None
    contact_name: str | None
    contact_role: str
    phone: n.PhoneResult
    map_url: str | None


def _clean(value: Any, limit: int = 120) -> str | None:
    text = n.clean_text(value) if value is not None else None
    if not text or n.is_placeholder(text) or text.lower() in {"null", "none", "unknown"}:
        return None
    return n.redact_contact_details(text)[:limit]


def find_phones(text: str) -> n.PhoneResult:
    """Every phone number in the message, normalised like the workbook import does."""
    found = [m.group(0) for m in n.PHONE_IN_TEXT.finditer(text)]
    return n.normalize_phone(" / ".join(found)) if found else n.PhoneResult(raw=None)


def extract(transport: Any, message: str) -> SiteDraft:
    message = message.strip()[:MAX_MESSAGE_CHARS]
    map_match = _MAP_LINK.search(message)
    phone = find_phones(_MAP_LINK.sub(" ", message))
    for_ai = n.redact_contact_details(_MAP_LINK.sub("[map link]", message))
    body = {
        "model": transport.model,
        "messages": [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": for_ai},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
        "stream": False,
    }
    data = transport._send("POST", "/chat/completions", json=body)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    try:
        fields = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AnalysisError("the AI's answer could not be read; please try again") from exc
    if not isinstance(fields, dict):
        fields = {}
    log.info("site extracted", extra={"event": "site_extract", "model": data.get("model")})
    area = _clean(fields.get("area"))
    role = fields.get("contact_role") if fields.get("contact_role") in ROLES else "contact_person"
    return SiteDraft(
        message=message,
        site_name=_clean(fields.get("site_name")) or (f"{area} site" if area else "New site"),
        area=area,
        city=_clean(fields.get("city"), 60),
        contact_name=_clean(fields.get("contact_name")),
        contact_role=role,
        phone=phone,
        map_url=map_match.group(0) if map_match else None,
    )


def _contact(session: Session, draft: SiteDraft, project_key: str) -> Contact:
    name = draft.contact_name or "Contact"
    key_name = n.name_key(name)
    key = (
        f"person:{draft.phone.e164}:{key_name}"
        if draft.phone.e164
        else f"contact_person:{project_key}:{key_name}"
    )
    contact = session.scalar(select(Contact).where(Contact.contact_key == key))
    if contact is None:
        contact = Contact(contact_key=key, name=name)
        session.add(contact)
    if draft.phone.numbers:
        contact.phone_raw = draft.phone.raw
        contact.phone_e164 = draft.phone.e164
        contact.phone_status = draft.phone.status
        contact.phone_candidates = draft.phone.numbers
    session.flush()
    for channel in ("whatsapp", "sms"):
        exists = session.scalar(
            select(MarketingPermission.id).where(
                MarketingPermission.contact_id == contact.id,
                MarketingPermission.channel == channel,
            )
        )
        if exists is None and contact.phone_candidates:
            session.add(MarketingPermission(contact_id=contact.id, channel=channel))
    return contact


def save(session: Session, draft: SiteDraft, today: date) -> Project:
    key = f"{SOURCE_PREFIX}{uuid.uuid4().hex[:12]}"
    project = Project(
        source_key=key,
        name=draft.site_name,
        area=draft.area,
        city=draft.city,
        google_location_url=draft.map_url,
        site_visit_date=today,
    )
    session.add(project)
    session.flush()
    if draft.contact_name or draft.phone.numbers:
        contact = _contact(session, draft, key)
        # The person to talk to is the contact person, whatever their job on site, so drafts
        # and follow-ups find them; a named owner is also recorded as the owner.
        roles = {"contact_person"} | ({"owner"} if draft.contact_role == "owner" else set())
        for role in sorted(roles):
            session.add(ProjectContact(project_id=project.id, contact_id=contact.id, role=role))
    session.add(
        ProjectNote(
            project_id=project.id, field=NOTE_FIELD, text=draft.message, observed_date=today
        )
    )
    session.flush()
    return project
