"""Opportunity ranking: which project to pitch which category, and why.

A transparent points score, not an AI guess, so every rank can be explained and checked:

  timing for the category   best window +40, coming up +20, retrofit only +15, unknown +5
  stage note age            within 4 months +20, within a year +10
  reachable on WhatsApp     +15
  saved AI analysis fit     strong +25, possible +15, weak -15
  catalog product fits      +5
  drafted in the last 30 days  -25 (recently pitched)
  lost quote for it         -20

Projects that are too late for a category, already have an open or won quote for it, or are
no longer in the latest workbook are left out: they are no longer opportunities. An AI fit only
counts when the analysis was made on the project's current data.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brains_bot.database.models import (
    CatalogProduct,
    Category,
    Contact,
    OpportunityAssessment,
    OutreachDraft,
    ProjectContact,
    Quote,
    SourceRecord,
)
from brains_bot.importers.loader import STALE_REASON
from brains_bot.services import catalog
from brains_bot.services.metrics import project_stages
from brains_bot.services.stages import CategoryWindow, assess_timing

TIMING_POINTS = {"in_window": 40, "early": 20, "retrofit_only": 15, "unknown": 5}
FIT_POINTS = {"strong": 25, "possible": 15, "weak": -15}
FRESH_DAYS, RECENT_DAYS = 120, 365
REACHABLE_POINTS, CATALOG_POINTS = 15, 5
RECENT_DRAFT_DAYS, RECENT_DRAFT_POINTS = 30, -25
LOST_QUOTE_POINTS = -20
TIMING_TEXT = {
    "in_window": "best time for it now",
    "early": "its best window is coming up",
    "retrofit_only": "retrofit options only",
    "unknown": "stage not recorded",
}


@dataclass
class Opportunity:
    project_id: int
    project: str
    area: str | None
    category_key: str
    category: str
    score: int
    timing: str
    stage: str
    recorded_on: date | None
    stale: bool
    reachable: bool
    reasons: list[str] = field(default_factory=list)
    also: list[str] = field(default_factory=list)  # other categories for the same project


def _reachable_projects(session: Session) -> set[int]:
    return set(
        session.scalars(
            select(ProjectContact.project_id)
            .join(Contact, Contact.id == ProjectContact.contact_id)
            .where(
                ProjectContact.role.in_(("contact_person", "owner")),
                Contact.phone_status.in_(("valid", "multiple")),
            )
        )
    )


def _latest_fits(session: Session) -> dict[tuple[int, str], str]:
    """The AI fit per project and category, from analyses that still describe the current
    data: made on the project's latest import and after the category was last changed."""
    latest_batch = (
        select(SourceRecord.project_id, func.max(SourceRecord.batch_id).label("batch_id"))
        .group_by(SourceRecord.project_id)
        .subquery()
    )
    rows = session.execute(
        select(
            OpportunityAssessment.project_id,
            OpportunityAssessment.category_key,
            OpportunityAssessment.result,
        )
        .join(latest_batch, latest_batch.c.project_id == OpportunityAssessment.project_id)
        .join(Category, Category.key == OpportunityAssessment.category_key)
        .where(
            OpportunityAssessment.data_batch_id == latest_batch.c.batch_id,
            OpportunityAssessment.created_at >= Category.updated_at,
        )
        .order_by(OpportunityAssessment.id)
    )
    return {(pid, key): (result or {}).get("fit", "") for pid, key, result in rows}


def _last_drafts(session: Session) -> dict[tuple[int, str], datetime]:
    rows = session.execute(
        select(
            OutreachDraft.project_id, OutreachDraft.category_key, func.max(OutreachDraft.created_at)
        ).group_by(OutreachDraft.project_id, OutreachDraft.category_key)
    )
    return {(project_id, key): created for project_id, key, created in rows}


def rank(
    session: Session,
    today: date,
    category_key: str | None = None,
    limit: int = 10,
) -> list[Opportunity]:
    """Best opportunities first. Without a category, one row per project (its best category,
    with the other worthwhile categories listed)."""
    query = select(Category).where(Category.active).order_by(Category.id)
    if category_key:
        query = query.where(Category.key == category_key)
    categories = list(session.scalars(query))
    products: dict[str, list[CatalogProduct]] = defaultdict(list)
    for p in session.scalars(select(CatalogProduct).where(CatalogProduct.active)):
        products[p.category_key].append(p)
    reachable = _reachable_projects(session)
    fits = _latest_fits(session)
    drafts = _last_drafts(session)
    quotes: dict[tuple[int, str], set[str]] = defaultdict(set)
    for project_id, key, status in session.execute(
        select(Quote.project_id, Quote.category_key, Quote.status)
    ):
        quotes[(project_id, key)].add(status)
    recent_cutoff = today - timedelta(days=RECENT_DRAFT_DAYS)

    found: list[Opportunity] = []
    for project, stage in project_stages(session):
        if STALE_REASON in (project.review_reasons or []):
            continue  # no longer in the latest workbook: not something to pitch
        for category in categories:
            pair = (project.id, category.key)
            if quotes[pair] & {"open", "won"}:
                continue
            window = CategoryWindow(
                category.name, category.best_from, category.best_to, category.retrofit_to
            )
            timing = assess_timing(window, stage, today)
            if timing.status == "too_late":
                continue
            score = TIMING_POINTS[timing.status]
            reasons = [f"{TIMING_TEXT[timing.status]} (+{score})"]
            if timing.age_days is not None and timing.age_days <= FRESH_DAYS:
                score += 20
                reasons.append(f"stage noted {timing.age_days} days ago (+20)")
            elif timing.age_days is not None and timing.age_days <= RECENT_DAYS:
                score += 10
                reasons.append(f"stage noted {round(timing.age_days / 30)} months ago (+10)")
            if project.id in reachable:
                score += REACHABLE_POINTS
                reasons.append(f"has a WhatsApp number (+{REACHABLE_POINTS})")
            fit = fits.get(pair)
            if fit in FIT_POINTS:
                score += FIT_POINTS[fit]
                reasons.append(f"AI analysis: {fit} fit ({FIT_POINTS[fit]:+d})")
            stage_known = stage.stage is not None
            if stage_known and any(
                catalog.fits_stage(p, stage.stage) for p in products[category.key]
            ):
                score += CATALOG_POINTS
                reasons.append(f"a catalog product fits the stage (+{CATALOG_POINTS})")
            last = drafts.get(pair)
            if last is not None and last.date() >= recent_cutoff:
                score += RECENT_DRAFT_POINTS
                reasons.append(f"drafted on {last:%d %b} ({RECENT_DRAFT_POINTS})")
            if "lost" in quotes[pair]:
                score += LOST_QUOTE_POINTS
                reasons.append(f"a quote was lost ({LOST_QUOTE_POINTS})")
            found.append(
                Opportunity(
                    project.id,
                    project.name,
                    project.area,
                    category.key,
                    category.name,
                    score,
                    timing.status,
                    stage.label,
                    stage.recorded_on,
                    timing.stale,
                    project.id in reachable,
                    reasons,
                )  # fmt: skip
            )

    found.sort(key=lambda o: (-o.score, -(o.recorded_on or date.min).toordinal(), o.project_id))
    if category_key:
        return found[:limit]
    best: dict[int, Opportunity] = {}
    for opportunity in found:
        if opportunity.project_id not in best:
            best[opportunity.project_id] = opportunity
        elif opportunity.score >= best[opportunity.project_id].score - 10:
            best[opportunity.project_id].also.append(opportunity.category)
    return list(best.values())[:limit]
