"""Plain-text message formatting. Spreadsheet values are untrusted, so no Markdown/HTML."""

from __future__ import annotations

from datetime import date

from brains_bot.importers.normalize import mask_phone
from brains_bot.services.project_service import ContactView, ProjectDetail, ProjectSummary

MAX_MESSAGE = 4000  # Telegram's limit is 4096 characters
NOTE_PREVIEW = 300
ROLE_LABELS = {
    "owner": "Owner",
    "contractor": "Contractor",
    "architect": "Architect",
    "company": "Company",
    "contact_person": "Contact person",
}


def fmt_date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "not recorded"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def project_line(p: ProjectSummary) -> str:
    place = ", ".join(x for x in (p.area, p.city) if x) or "location not recorded"
    flag = " ⚠" if p.needs_review else ""
    visited = fmt_date(p.site_visit_date)
    return f"#{p.id} {truncate(p.name, 60)}{flag}\n    {truncate(place, 60)} · visited {visited}"


def project_list(title: str, items: list[ProjectSummary], footer: str = "") -> str:
    if not items:
        return f"{title}\n\nNo projects found."
    body = "\n".join(project_line(p) for p in items)
    tail = "\n\nSend /project <number> for details. ⚠ = needs review."
    return truncate(f"{title}\n\n{body}{tail}{footer}", MAX_MESSAGE)


def _contact_line(c: ContactView) -> str:
    label = ROLE_LABELS.get(c.role, c.role)
    line = f"{label}: {c.name}"
    if c.role != "contact_person":
        return line
    if c.phone_status == "valid":
        line += f"\n    Phone: {mask_phone(c.phone_e164)}"
    elif c.phone_status == "multiple":
        line += f"\n    Phone: {c.phone_count} numbers on file; which one to use needs confirming"
    elif c.phone_status in ("invalid", "placeholder"):
        line += "\n    Phone: not a usable number in the source"
    else:
        line += "\n    Phone: none recorded"
    line += f"\n    Marketing consent: {c.consent}"
    return line


def project_detail(d: ProjectDetail) -> str:
    s = d.summary
    out = [f"Project #{s.id}: {s.name}"]
    place = ", ".join(x for x in (s.area, s.city) if x)
    out.append(f"Location: {place or 'not recorded'}")
    if d.address and d.address != s.area:
        out.append(f"Address: {d.address}")
    if d.google_location_url:
        out.append(f"Map: {d.google_location_url}")
    visit = f"Site visit: {fmt_date(s.site_visit_date)}"
    if d.salesperson:
        visit += f" by {d.salesperson}"
    out.append(visit)
    if d.est_paint_start:
        out.append(
            f"Estimated paint start: {fmt_date(d.est_paint_start)} "
            "(estimate recorded at the visit, not a verified current status)"
        )

    if d.contacts:
        out += ["", "People and companies"]
        out += [_contact_line(c) for c in d.contacts]

    tracking = [
        ("Dealer to route", d.dealer_to_route),
        ("SAR", d.sar),
        ("Site visited by dealer", d.site_visited_by_dealer),
        ("Quotation shared", d.quotation_shared),
        ("Quote value", f"KES {d.quote_value_kes:,.0f}" if d.quote_value_kes is not None else None),
        ("Won/lost", d.won_lost),
        ("Supplying dealer", d.supplying_dealer),
        ("Follow-up status", d.tracking_status),
    ]
    tracking = [(k, v) for k, v in tracking if v]
    if tracking:
        out += ["", "Sales tracking (paint)"]
        out += [f"{k}: {v}" for k, v in tracking]

    if d.notes:
        shown = len(d.notes)
        out += ["", f"Notes ({shown} most recent of {d.note_count})"]
        out += [f"- [{fmt_date(day)}] {truncate(text, NOTE_PREVIEW)}" for day, text in d.notes]

    if d.review_reasons:
        out += ["", "⚠ Needs review"]
        out += [f"- {truncate(r, 300)}" for r in d.review_reasons]

    out += ["", "Source"]
    out += [f"- {src}" for src in d.sources]
    return truncate("\n".join(out), MAX_MESSAGE)
