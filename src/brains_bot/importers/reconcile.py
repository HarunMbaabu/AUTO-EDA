"""Decide which worksheet records describe the same project.

Policy (also printed in the import report):

1. "Data Base" is the primary source. Each of its records becomes one project.
2. A record in "Active Sheet" or "To share with Dealer" links to the Data Base record with the
   same ID only if the two agree on content: a shared phone number, or similar site names.
3. If the IDs disagree on content (IDs were shifted by inserted rows), the record is matched
   by content alone, but only when exactly one project is a strong match. The project is
   flagged for review.
4. Anything else (no match, or several possible matches) becomes its own project, flagged
   for review. Uncertain records are never merged silently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from brains_bot.importers.normalize import name_key, normalize_phone
from brains_bot.importers.workbook import SourceRow

PRIMARY_SHEET = "Data Base"
SUPPLEMENTARY_SHEETS = ("Active Sheet", "To share with Dealer")
SITE = "Name of Potential Site"
PHONE = "Contact Details"

# Words that carry no identity in site names ("Mr. X proposed villa" vs "X project").
_STOPWORDS = {"mr", "mrs", "madam", "dr", "the", "proposed", "villa", "project", "projects"}


def _site_tokens(row: SourceRow) -> str:
    return " ".join(t for t in name_key(str(row.get(SITE) or "")).split() if t not in _STOPWORDS)


def site_similarity(a: SourceRow, b: SourceRow) -> float:
    x, y = _site_tokens(a), _site_tokens(b)
    if not x or not y:
        return 0.0
    if len(min(x, y, key=len)) >= 6 and (x in y or y in x):
        return 1.0
    return SequenceMatcher(None, x, y).ratio()


def phones(row: SourceRow) -> set[str]:
    return set(normalize_phone(row.get(PHONE)).numbers)


def same_project(a: SourceRow, b: SourceRow) -> bool:
    """Lenient check, used when the IDs already agree."""
    return bool(phones(a) & phones(b)) or site_similarity(a, b) >= 0.75


def strong_match(a: SourceRow, b: SourceRow) -> bool:
    """Strict check, used when matching on content alone."""
    pa, pb = phones(a), phones(b)
    similarity = site_similarity(a, b)
    if pa & pb:
        return similarity >= 0.5
    if pa and pb:
        # Both have numbers and none match: a shared name ("Mr. Otieno project") is not enough.
        return False
    if name_key(str(a.get(SITE) or "")) == name_key(str(b.get(SITE) or "")) != "":
        return True  # identical full site name and no conflicting phone numbers
    distinctive = min(len(_site_tokens(a)), len(_site_tokens(b))) >= 8
    return distinctive and similarity >= 0.95


@dataclass
class Link:
    row: SourceRow
    method: str  # primary | source_id | content_match


@dataclass
class ProjectDraft:
    source_key: str
    primary: SourceRow
    links: list[Link] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    def rows(self) -> list[SourceRow]:
        return [link.row for link in self.links]

    def rows_from(self, sheet: str) -> list[SourceRow]:
        return [link.row for link in self.links if link.row.sheet == sheet]


@dataclass
class Reconciliation:
    projects: list[ProjectDraft]
    events: list[str]  # human-readable decisions for the report


def _slug(sheet: str) -> str:
    return {"Data Base": "db", "Active Sheet": "active", "To share with Dealer": "share"}.get(
        sheet, name_key(sheet).replace(" ", "-")
    )


def _content_key(row: SourceRow) -> str:
    digest = hashlib.sha256(name_key(str(row.get(SITE) or "")).encode()).hexdigest()[:10]
    return f"{_slug(row.sheet)}:{row.source_id if row.source_id is not None else 'noid'}:{digest}"


def _where(row: SourceRow) -> str:
    sid = f"ID {row.source_id}" if row.source_id is not None else "no ID"
    return f"{row.sheet} row {row.first_row} ({sid})"


def reconcile(sheets: dict[str, list[SourceRow]]) -> Reconciliation:
    projects: list[ProjectDraft] = []
    events: list[str] = []
    by_id: dict[int, ProjectDraft] = {}

    for row in sheets.get(PRIMARY_SHEET, []):
        sid = row.source_id
        if sid is not None and sid not in by_id:
            draft = ProjectDraft(source_key=f"db:{sid}", primary=row)
            by_id[sid] = draft
        else:
            draft = ProjectDraft(source_key=_content_key(row), primary=row)
            reason = "duplicate ID in Data Base" if sid is not None else "no ID in Data Base"
            draft.review_reasons.append(f"{reason} ({_where(row)})")
            events.append(f"{_where(row)}: {reason}; kept as a separate project")
        draft.links.append(Link(row, "primary"))
        projects.append(draft)

    for sheet in SUPPLEMENTARY_SHEETS:
        linked_in_sheet: dict[str, SourceRow] = {}
        for row in sheets.get(sheet, []):
            target = _match(row, by_id, projects, events)
            if target is None:
                draft = ProjectDraft(source_key=_content_key(row), primary=row)
                draft.links.append(Link(row, "primary"))
                draft.review_reasons.append(f"only found in {_where(row)}")
                projects.append(draft)
                continue
            if target.source_key in linked_in_sheet:
                other = linked_in_sheet[target.source_key]
                target.review_reasons.append(
                    f"two {sheet} records match this project: rows {other.first_row} and "
                    f"{row.first_row}"
                )
                events.append(f"{_where(row)}: second {sheet} record for {target.source_key}")
            linked_in_sheet[target.source_key] = row

    return Reconciliation(projects=projects, events=events)


def _match(
    row: SourceRow,
    by_id: dict[int, ProjectDraft],
    projects: list[ProjectDraft],
    events: list[str],
) -> ProjectDraft | None:
    candidate = by_id.get(row.source_id) if row.source_id is not None else None
    if candidate is not None and same_project(candidate.primary, row):
        candidate.links.append(Link(row, "source_id"))
        return candidate

    matches = [p for p in projects if strong_match(p.primary, row)]
    if len(matches) == 1:
        target = matches[0]
        target.links.append(Link(row, "content_match"))
        id_note = (
            f"its ID points to a different site ({candidate.source_key})"
            if candidate is not None
            else "it has no matching ID"
        )
        target.review_reasons.append(
            f"linked by content to {_where(row)}; {id_note}. Please confirm."
        )
        events.append(f"{_where(row)}: matched by content to {target.source_key} ({id_note})")
        return target

    if len(matches) > 1:
        keys = ", ".join(p.source_key for p in matches)
        events.append(f"{_where(row)}: ambiguous, matches {keys}; kept separate")
    elif candidate is not None:
        events.append(
            f"{_where(row)}: ID matches {candidate.source_key} but content differs; kept separate"
        )
    else:
        events.append(f"{_where(row)}: no match in other sheets; kept as its own project")
    return None
