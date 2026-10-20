"""Plain-text message formatting. Spreadsheet values are untrusted, so no Markdown/HTML."""

from __future__ import annotations

from datetime import date

from brains_bot.importers.normalize import mask_phone
from brains_bot.services.catalog import needs_retrofit, price_text
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
        line += (
            "\n    Phone: not a usable number in the source (mistyped?). Send the right one "
            "with /newsite as a follow-up"
        )
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

    audio = truncate(d.audio_status, NOTE_PREVIEW) if d.audio_status else "not recorded yet"
    out.append(f"Audio status: {audio}")

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
        out += ["", "Sales tracking"]
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


def category_list(categories) -> str:
    from brains_bot.services.stages import STAGES

    if not categories:
        return "No categories are set up yet. Run: uv run brains-bot-categories"
    lines = ["Categories", ""]
    for c in categories:
        catalog = "verified catalog" if c.has_verified_catalog else "no catalog yet"
        lines.append(f"{c.key}: {c.name}")
        lines.append(f"    Best time: {STAGES[c.best_from]} to {STAGES[c.best_to]} · {catalog}")
    lines += ["", "Use: /analyze <project number> <category>, e.g. /analyze 205 audio"]
    return truncate("\n".join(lines), MAX_MESSAGE)


FIT_LABELS = {
    "strong": "Strong",
    "possible": "Possible",
    "weak": "Weak",
    "insufficient_evidence": "Not enough evidence",
}
TIMING_LABELS = {
    "unknown": "Unknown",
    "early": "Early",
    "in_window": "Good time",
    "retrofit_only": "Late: retrofit only",
    "too_late": "Too late",
}


def _age(days: int | None) -> str:
    if days is None:
        return ""
    if days < 60:
        return f", {days} days ago"
    return f", about {round(days / 30)} months ago"


def analysis(ctx, result, note: str) -> str:
    stage, timing, category = ctx.stage, ctx.timing, ctx.category
    out = [
        f"Analysis: Project #{ctx.project_id} · {category.name}",
        f"Fit: {FIT_LABELS.get(result.fit, result.fit)}",
        result.summary,
        "",
        f"Timing: {TIMING_LABELS.get(timing.status, timing.status)}",
    ]
    if stage.stage:
        out.append(
            f"Last recorded stage: {stage.label} "
            f"(note from {fmt_date(stage.recorded_on)}{_age(timing.age_days)})"
        )
        out.append(f'    "{truncate(stage.evidence or "", 200)}"')
    else:
        out.append("Last recorded stage: not recorded in the notes")
    out.append(timing.explanation)
    if timing.stale:
        out.append("⚠ This stage was recorded a while ago. Confirm the current stage first.")
    if result.timing_comment:
        out.append(result.timing_comment)

    if result.recorded_facts:
        out += ["", "Recorded facts"]
        out += [f"- {f.fact} ({f.source})" for f in result.recorded_facts]
    if result.possible_needs:
        out += ["", "Possible needs (inferred, not confirmed)"]
        out += [f"- {p.need} [{p.confidence}]: {p.reasoning}" for p in result.possible_needs]
    if result.unknowns:
        out += ["", "Unknowns"]
        out += [f"- {u}" for u in result.unknowns]
    out += ["", "Suggested next step", result.suggested_next_step]
    if ctx.products:
        out += ["", "Catalog options that fit this stage"]
        for p in ctx.products:
            how = " · retrofit" if needs_retrofit(p, stage.stage) else ""
            supplier = f" · {p.supplier}" if p.supplier else ""
            out.append(f"- {p.name}: {price_text(p)}{how}{supplier}")
    if ctx.products:
        catalog = " Products and prices are from the verified catalog; confirm before quoting."
    elif category.has_verified_catalog:
        catalog = " No catalog product fits this stage."
    else:
        catalog = " No products or prices are suggested until a verified catalog is loaded."
    out += ["", f"Based on the imported workbook, not verified current status.{catalog}", note]
    return truncate("\n".join(out), MAX_MESSAGE)
