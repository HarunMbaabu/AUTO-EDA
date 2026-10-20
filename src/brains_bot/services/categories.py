"""Category definitions: sync from data/categories.json and look them up."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brains_bot.database.models import Category
from brains_bot.importers.normalize import name_key

FIELDS = ("name", "groups", "description", "best_from", "best_to", "retrofit_to", "timing_note")


def load_definitions() -> list[dict[str, Any]]:
    text = resources.files("brains_bot.data").joinpath("categories.json").read_text()
    definitions = json.loads(text)["categories"]
    for d in definitions:
        if not (1 <= d["best_from"] <= d["best_to"] <= 7):
            raise ValueError(f"category {d['key']!r} has an invalid stage window")
    return definitions


@cache
def category_definitions() -> dict[str, dict[str, Any]]:
    """key -> definition from data/categories.json (e.g. its pairs_with list)."""
    return {d["key"]: d for d in load_definitions()}


def sync_categories(session: Session, definitions: list[dict[str, Any]] | None = None) -> int:
    """Upsert categories from the definitions file. Categories removed from the file are
    deactivated, not deleted, so saved analyses keep their meaning. Catalog status is not
    touched: it is set when a verified catalog is loaded (Phase 4)."""
    definitions = load_definitions() if definitions is None else definitions
    keys = {d["key"] for d in definitions}
    for d in definitions:
        category = session.scalar(select(Category).where(Category.key == d["key"]))
        if category is None:
            category = Category(key=d["key"])
            session.add(category)
        for field in FIELDS:
            setattr(category, field, d.get(field))
        category.timing_note = d.get("timing_note") or ""
        category.groups = d.get("groups") or []
        category.active = True
    for category in session.scalars(select(Category).where(Category.key.not_in(keys))):
        category.active = False
    session.flush()
    return len(definitions)


def grouped(categories: list[Category]) -> list[tuple[str, list[Category]]]:
    """[(group, categories)] in the order the groups first appear; ungrouped ones last."""
    out: dict[str, list[Category]] = {}
    for c in categories:  # groups in the order of each category's main (first) group
        out.setdefault((c.groups or ["Other"])[0], [])
    for c in categories:
        for group in c.groups or ["Other"]:
            out[group].append(c)
    return list(out.items())


def active_categories(session: Session) -> list[Category]:
    return list(session.scalars(select(Category).where(Category.active).order_by(Category.id)))


def resolve_category(session: Session, text: str) -> Category | None:
    """Match by key ('smart_home'), name ('Smart Home Automation') or an unambiguous prefix
    of either ('smart', 'interior')."""
    wanted = name_key(text)
    if not wanted:
        return None
    categories = active_categories(session)
    for c in categories:
        if wanted in (name_key(c.key), name_key(c.name)):
            return c
    matches = [
        c
        for c in categories
        if name_key(c.key).startswith(wanted) or name_key(c.name).startswith(wanted)
    ]
    return matches[0] if len(matches) == 1 else None
