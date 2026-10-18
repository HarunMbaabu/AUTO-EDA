"""Construction stage detection from site notes, and category timing.

Deterministic on purpose: the backend works out the stage and timing, and Claude is told the
result rather than guessing it. Every detection keeps the sentence it came from so an operator
can check it, and stages recorded long ago are marked as possibly out of date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

STAGES = {
    1: "foundation / early works",
    2: "structure / walling",
    3: "roofing",
    4: "plastering",
    5: "skimming / finishing prep",
    6: "painting under way",
    7: "complete / handed over",
}

# Most advanced first. A note mentioning several stages counts as the most advanced one.
_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (7, re.compile(r"handed over|hand-?over done|project (is )?(complete|finished)|"
                   r"(house|building) (is )?complete|occupied|moved in", re.I)),
    (6, re.compile(r"paint(ing)? (work )?(has )?(started|commenced|ongoing|in progress|done)|"
                   r"\bpainted\b|undercoat|top ?coats? (applied|done)", re.I)),
    (5, re.compile(r"\bskim", re.I)),
    (4, re.compile(r"plaster", re.I)),
    (3, re.compile(r"\broof", re.I)),
    (2, re.compile(r"walling|superstructure|\bslab\b|\bcolumns\b|storey|"
                   r"(first|second|ground) floor", re.I)),
    (1, re.compile(r"foundation|excavat|early stages?|ground ?breaking|site clearing", re.I)),
]  # fmt: skip

STALE_AFTER_DAYS = 120


@dataclass(frozen=True)
class StageResult:
    stage: int | None
    evidence: str | None  # the sentence the stage was detected from
    recorded_on: date | None  # date of the note it came from

    @property
    def label(self) -> str:
        return STAGES[self.stage] if self.stage else "not recorded"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def detect_stage(notes: list[tuple[date | None, str]]) -> StageResult:
    """Most advanced stage mentioned in any note, with the sentence and note date."""
    best = StageResult(None, None, None)
    for recorded_on, text in notes:
        for sentence in _sentences(text):
            for stage, pattern in _PATTERNS:
                if pattern.search(sentence):
                    newer = (recorded_on or date.min) > (best.recorded_on or date.min)
                    if best.stage is None or stage > best.stage or (stage == best.stage and newer):
                        best = StageResult(stage, sentence[:300], recorded_on)
                    break
    return best


@dataclass(frozen=True)
class CategoryWindow:
    name: str
    best_from: int
    best_to: int
    retrofit_to: int | None


@dataclass(frozen=True)
class Timing:
    status: str  # unknown | early | in_window | retrofit_only | too_late
    explanation: str
    stale: bool
    age_days: int | None


def _window(c: CategoryWindow) -> str:
    return f"{STAGES[c.best_from]} to {STAGES[c.best_to]}"


def assess_timing(category: CategoryWindow, stage: StageResult, today: date) -> Timing:
    age = (today - stage.recorded_on).days if stage.recorded_on else None
    stale = age is not None and age > STALE_AFTER_DAYS
    best = _window(category)
    if stage.stage is None:
        return Timing(
            "unknown",
            f"The construction stage is not recorded. Best window for {category.name}: {best}.",
            stale,
            age,
        )
    if stage.stage < category.best_from:
        status, text = "early", f"Early: the best window ({best}) has not started yet; plan ahead."
    elif stage.stage <= category.best_to:
        status, text = "in_window", f"In the best window ({best})."
    elif category.retrofit_to and stage.stage <= category.retrofit_to:
        status, text = "retrofit_only", f"Past the best window ({best}); retrofit options only."
    else:
        status, text = "too_late", f"Past the window for {category.name} ({best})."
    return Timing(status, text, stale, age)
