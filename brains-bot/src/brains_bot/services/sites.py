"""New projects and follow-ups from a pasted WhatsApp site report (/newsite).

A report can describe one site or a numbered list of sites ("1. ... 2. ..."), optionally after
an intro line such as "today I'm mapping Riverview, Golden Palm". Each site's phone numbers and
map link are found locally. The AI sees only the text with contact details removed and returns,
per site, the site name, area, city and the contact's name and role, in one call. A site whose
phone number already belongs to a project is treated as a follow-up: its text becomes a new note
on that project instead of a duplicate project. The operator checks everything before saving.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import or_, select
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
MAX_MESSAGE_CHARS = 6000
MAX_SITES = 20
_MAP_LINK = re.compile(
    r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.)?google\.[a-z.]+/maps)\S*", re.I
)
# A list number: at a line start or after a space or full stop, followed by a word. Only an
# ascending run 1, 2, 3 ... counts, so "Riverview 79." or "3 floors" never split a site.
_ITEM_MARK = re.compile(r"(?:^|(?<=[\s.!?…]))(\d{1,2})\s*[.)]\s*(?=[A-Za-z])", re.M)

EXTRACT_PROMPT = """\
A sales person in Kenya pasted a WhatsApp report about building sites they visited. You get \
JSON with "context" (any intro text) and "items" (one entry per site). Reply with one JSON \
object {{"visit_date": ..., "sites": [...]}} where "sites" holds exactly {count} objects, \
one per item, in the same order, each with exactly these keys:
- "site_name": the name the item starts with or states (e.g. "Dennis project", "Pesh Villa"); \
if the item starts with only a person's name, "<that name> project"; never invent names
- "area": the estate, road or neighbourhood stated in the item (e.g. "Riverview 79"), or null
- "city": the town if stated or obvious from a well-known area (e.g. Runda -> Nairobi), or null
- "contact_name": the person to talk to, as written (keep titles), or null if none is named
- "contact_role": one of "contractor", "owner", "architect", "contact_person"
- "role_description": the person's role in a few words as stated (e.g. "paint contractor", \
"caretaker", "interior designer"), or null
- "visit_date": YYYY-MM-DD if this item states its own visit date, else null
Also add a top-level key "visit_date": the date the visits happened if the report states one \
(e.g. "20/10/2026", "13th Oct", "yesterday", "on Monday"), as YYYY-MM-DD, else null. Today is \
{today}; Kenyan dates are written day first.
Contact details were removed and appear as [phone] or [email]. Use only what the text says.\
"""


@dataclass
class SiteDraft:
    message: str  # this site's own text, saved as its note
    site_name: str
    area: str | None
    city: str | None
    contact_name: str | None
    contact_role: str
    role_description: str | None
    phone: n.PhoneResult
    map_url: str | None
    existing_project_id: int | None = None  # set when this is a follow-up on a known project
    existing_name: str | None = None
    visit_date: date | None = None  # stated in the message; None means "today"


def _clean(value: Any, limit: int = 120) -> str | None:
    text = n.clean_text(value) if value is not None else None
    if not text or n.is_placeholder(text) or text.lower() in {"null", "none", "unknown"}:
        return None
    return n.redact_contact_details(text)[:limit]


def find_phones(text: str) -> n.PhoneResult:
    """Every phone number in the text, normalised like the workbook import does."""
    found = [m.group(0) for m in n.PHONE_IN_TEXT.finditer(text)]
    return n.normalize_phone(" / ".join(found)) if found else n.PhoneResult(raw=None)


def split_report(message: str) -> tuple[str, list[str]]:
    """(intro, one text per site). A numbered list of two or more entries is split, even when
    the line breaks were lost in pasting; anything else is a single site with no intro."""
    marks, expected = [], 1
    for m in _ITEM_MARK.finditer(message):
        if int(m.group(1)) == expected:
            marks.append(m)
            expected += 1
    if len(marks) < 2:
        return "", [message.strip()]
    intro = message[: marks[0].start()].strip()
    ends = [m.start() for m in marks[1:]] + [len(message)]
    items = [message[m.end() : end].strip() for m, end in zip(marks, ends, strict=True)]
    return intro, [item for item in items if item][:MAX_SITES]


MAX_AGE_DAYS = 400


def _visit_date(value: Any, today: date) -> date | None:
    """A stated visit date, only if it is a real date that is not in the future or too old."""
    try:
        found = date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None
    if found is None or found > today or (today - found).days > MAX_AGE_DAYS:
        return None
    return found


def _for_ai(text: str) -> str:
    return n.redact_contact_details(_MAP_LINK.sub("[map link]", text))


def extract(transport: Any, message: str, today: date | None = None) -> list[SiteDraft]:
    today = today or date.today()
    message = message.strip()[:MAX_MESSAGE_CHARS]
    intro, items = split_report(message)
    payload = {"context": _for_ai(intro) or None, "items": [_for_ai(item) for item in items]}
    body = {
        "model": transport.model,
        "messages": [
            {
                "role": "system",
                "content": EXTRACT_PROMPT.format(count=len(items), today=today.isoformat()),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 200 + 150 * len(items),
        "stream": False,
    }
    data = transport._send("POST", "/chat/completions", json=body)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    try:
        answer = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AnalysisError("the AI's answer could not be read; please try again") from exc
    report_date = _visit_date(answer.get("visit_date"), today) if isinstance(answer, dict) else None
    found = answer.get("sites") if isinstance(answer, dict) else None
    if not isinstance(found, list):  # a single object is fine for a single site
        found = [answer] if isinstance(answer, dict) and len(items) == 1 else []
    log.info(
        "sites extracted",
        extra={"event": "site_extract", "model": data.get("model"), "command": str(len(items))},
    )

    drafts = []
    for i, item in enumerate(items):
        fields = found[i] if i < len(found) and isinstance(found[i], dict) else {}
        area = _clean(fields.get("area"))
        role = fields.get("contact_role")
        map_match = _MAP_LINK.search(item)
        drafts.append(
            SiteDraft(
                message=item,
                site_name=_clean(fields.get("site_name"))
                or (f"{area} site" if area else "New site"),
                area=area,
                city=_clean(fields.get("city"), 60),
                contact_name=_clean(fields.get("contact_name")),
                contact_role=role if role in ROLES else "contact_person",
                role_description=_clean(fields.get("role_description"), 60),
                phone=find_phones(_MAP_LINK.sub(" ", item)),
                map_url=map_match.group(0) if map_match else None,
                visit_date=_visit_date(fields.get("visit_date"), today) or report_date,
            )
        )
    return drafts


def match_existing(session: Session, drafts: list[SiteDraft]) -> None:
    """Mark drafts whose phone number already belongs to a project as follow-ups on it."""
    for draft in drafts:
        if not draft.phone.numbers:
            continue
        found = session.execute(
            select(Project.id, Project.name)
            .join(ProjectContact, ProjectContact.project_id == Project.id)
            .join(Contact, Contact.id == ProjectContact.contact_id)
            .where(
                or_(
                    Contact.phone_e164.in_(draft.phone.numbers),
                    *[
                        Contact.phone_candidates.contains([number])
                        for number in draft.phone.numbers
                    ],
                )
            )
            .order_by(Project.site_visit_date.desc().nulls_last(), Project.id.desc())
            .limit(1)
        ).first()
        if found is not None:
            draft.existing_project_id, draft.existing_name = found


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


def _note(draft: SiteDraft) -> str:
    who = ", ".join(x for x in (draft.contact_name, draft.role_description) if x)
    return draft.message + (f"\n(Contact: {who})" if who and who not in draft.message else "")


def save(session: Session, draft: SiteDraft, today: date) -> Project:
    """A follow-up adds a dated note to its project; anything else becomes a new project. The
    visit date is the one stated in the message, else today."""
    today = draft.visit_date or today
    if draft.existing_project_id is not None:
        project = session.get(Project, draft.existing_project_id)
        if project is not None:
            session.add(
                ProjectNote(
                    project_id=project.id, field=NOTE_FIELD, text=_note(draft), observed_date=today
                )
            )
            if draft.map_url and not project.google_location_url:
                project.google_location_url = draft.map_url
            session.flush()
            return project
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
        ProjectNote(project_id=project.id, field=NOTE_FIELD, text=_note(draft), observed_date=today)
    )
    session.flush()
    return project
