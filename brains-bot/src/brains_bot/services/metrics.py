"""Numbers for the dashboard. Read-only, computed from the same tables the bot writes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from brains_bot.database.models import (
    CatalogProduct,
    Category,
    Contact,
    OpportunityAssessment,
    OutreachDraft,
    Project,
    Quote,
)
from brains_bot.services.stages import (
    STALE_AFTER_DAYS,
    CategoryWindow,
    StageResult,
    assess_timing,
    detect_stage,
)

TIMING_ORDER = ("in_window", "early", "retrofit_only", "too_late", "unknown")
TIMING_LABELS = {
    "in_window": "Best time now",
    "early": "Coming up",
    "retrofit_only": "Retrofit only",
    "too_late": "Too late",
    "unknown": "Stage unknown",
}
LIST_LIMIT = 15


@dataclass
class Lead:
    project_id: int
    project: str
    area: str | None
    category: str
    stage: str
    recorded_on: date | None
    age_days: int | None
    stale: bool
    categories: list[str] = field(default_factory=list)


@dataclass
class Crm:
    contacts: int
    reachable_contacts: int
    projects: int
    pipeline: list[dict[str, Any]]  # per category: counts per timing status
    best_now: list[Lead]
    coming_up: list[Lead]
    stage_counts: list[tuple[str, int]]


@dataclass
class BotStats:
    drafts: int
    sent: int
    replied: int
    response_rate: float | None
    analyses: int
    pitched: list[dict[str, Any]]  # per category: analyses, drafts, sent
    most_pitched: str | None
    fit_counts: list[tuple[str, int]]
    recent_drafts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Revenue:
    pipeline_value: Decimal
    won_value: Decimal
    lost_value: Decimal
    open_quotes: int
    won: int
    lost: int
    win_rate: float | None
    top_packages: list[dict[str, Any]]


def project_stages(session: Session) -> list[tuple[Project, StageResult]]:
    projects = session.scalars(select(Project).options(selectinload(Project.notes)))
    return [(p, detect_stage([(n.observed_date, n.text) for n in p.notes])) for p in projects]


def _categories(session: Session) -> list[Category]:
    return list(session.scalars(select(Category).where(Category.active).order_by(Category.id)))


def crm(session: Session, today: date) -> Crm:
    contacts = session.scalar(select(func.count(Contact.id))) or 0
    reachable = (
        session.scalar(
            select(func.count(Contact.id)).where(Contact.phone_status.in_(("valid", "multiple")))
        )
        or 0
    )
    rows = project_stages(session)
    categories = _categories(session)
    pipeline, best_now, coming_up = [], [], []
    for category in categories:
        window = CategoryWindow(
            category.name, category.best_from, category.best_to, category.retrofit_to
        )
        counts: Counter[str] = Counter()
        for project, stage in rows:
            timing = assess_timing(window, stage, today)
            counts[timing.status] += 1
            lead = Lead(project.id, project.name, project.area, category.name, stage.label,
                        stage.recorded_on, timing.age_days, timing.stale)  # fmt: skip
            if timing.status == "in_window":
                best_now.append(lead)
            elif stage.stage is not None and stage.stage == category.best_from - 1:
                coming_up.append(lead)
        pipeline.append(
            {
                "key": category.key,
                "name": category.name,
                "counts": {s: counts.get(s, 0) for s in TIMING_ORDER},
                "leads": counts.get("in_window", 0) + counts.get("early", 0),
            }
        )

    def freshest(leads: list[Lead]) -> list[Lead]:
        """One row per project (its categories joined), most recent notes first."""
        by_project: dict[int, Lead] = {}
        for lead in leads:
            if lead.project_id in by_project:
                by_project[lead.project_id].categories.append(lead.category)
            else:
                lead.categories = [lead.category]
                by_project[lead.project_id] = lead
        rows = sorted(by_project.values(), key=lambda x: x.recorded_on or date.min, reverse=True)
        return rows[:LIST_LIMIT]

    stage_counts = Counter(stage.label for _, stage in rows)
    return Crm(
        contacts=contacts,
        reachable_contacts=reachable,
        projects=len(rows),
        pipeline=pipeline,
        best_now=freshest(best_now),
        coming_up=freshest(coming_up),
        stage_counts=sorted(stage_counts.items(), key=lambda kv: -kv[1]),
    )


def bot_stats(session: Session) -> BotStats:
    names = dict(session.execute(select(Category.key, Category.name)).all())
    drafts = session.scalar(select(func.count(OutreachDraft.id))) or 0
    sent = (
        session.scalar(
            select(func.count(OutreachDraft.id)).where(OutreachDraft.sent_at.is_not(None))
        )
        or 0
    )
    replied = (
        session.scalar(
            select(func.count(OutreachDraft.id)).where(OutreachDraft.replied_at.is_not(None))
        )
        or 0
    )
    analyses = dict(
        session.execute(
            select(OpportunityAssessment.category_key, func.count()).group_by(
                OpportunityAssessment.category_key
            )
        ).all()
    )
    drafted = dict(
        session.execute(
            select(OutreachDraft.category_key, func.count()).group_by(OutreachDraft.category_key)
        ).all()
    )
    sent_by = dict(
        session.execute(
            select(OutreachDraft.category_key, func.count())
            .where(OutreachDraft.sent_at.is_not(None))
            .group_by(OutreachDraft.category_key)
        ).all()
    )
    # Drafts weigh more than analyses: a draft is an actual pitch.
    keys = sorted(
        set(analyses) | set(drafted), key=lambda k: -(analyses.get(k, 0) + drafted.get(k, 0) * 2)
    )
    pitched = [
        {"key": k, "name": names.get(k, k), "analyses": analyses.get(k, 0),
         "drafts": drafted.get(k, 0), "sent": sent_by.get(k, 0)}
        for k in keys
    ]  # fmt: skip
    fits = Counter(r["fit"] for r in session.scalars(select(OpportunityAssessment.result)) if r)
    recent = session.execute(
        select(OutreachDraft, Project.name)
        .join(Project, Project.id == OutreachDraft.project_id)
        .order_by(OutreachDraft.id.desc())
        .limit(10)
    ).all()
    return BotStats(
        drafts=drafts,
        sent=sent,
        replied=replied,
        response_rate=(replied / sent) if sent else None,
        analyses=sum(analyses.values()),
        pitched=pitched,
        most_pitched=pitched[0]["name"] if pitched else None,
        fit_counts=sorted(fits.items(), key=lambda kv: -kv[1]),
        recent_drafts=[
            {
                "id": d.id,
                "project_id": d.project_id,
                "project": name,
                "category": names.get(d.category_key, d.category_key),
                "created_at": d.created_at,
                "sent": d.sent_at is not None,
                "replied": d.replied_at is not None,
            }
            for d, name in recent
        ],  # fmt: skip
    )


def revenue(session: Session) -> Revenue:
    def total(status: str) -> tuple[Decimal, int]:
        value, count = session.execute(
            select(func.coalesce(func.sum(Quote.amount_kes), 0), func.count(Quote.id)).where(
                Quote.status == status
            )
        ).one()
        return Decimal(value), count

    (open_value, open_count), (won_value, won), (lost_value, lost) = (
        total("open"), total("won"), total("lost")
    )  # fmt: skip
    names = dict(session.execute(select(CatalogProduct.sku, CatalogProduct.name)).all())
    quoted = Counter(s for s in session.scalars(select(Quote.product_sku)) if s)
    pitched: Counter[str] = Counter()
    for skus in session.scalars(select(OutreachDraft.product_skus)):
        pitched.update(skus or [])
    top = [
        {"sku": sku, "name": names.get(sku, sku), "pitched": pitched.get(sku, 0),
         "quoted": quoted.get(sku, 0)}
        for sku in sorted(set(pitched) | set(quoted), key=lambda s: -(pitched[s] + quoted[s] * 2))
    ][:10]  # fmt: skip
    closed = won + lost
    return Revenue(
        pipeline_value=open_value,
        won_value=won_value,
        lost_value=lost_value,
        open_quotes=open_count,
        won=won,
        lost=lost,
        win_rate=(won / closed) if closed else None,
        top_packages=top,
    )


__all__ = ["STALE_AFTER_DAYS", "TIMING_LABELS", "TIMING_ORDER", "bot_stats", "crm", "revenue"]
