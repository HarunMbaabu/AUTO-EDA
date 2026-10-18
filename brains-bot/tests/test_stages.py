from datetime import date

import pytest

from brains_bot.services.stages import CategoryWindow, StageResult, assess_timing, detect_stage

AUDIO = CategoryWindow("Home Audio", best_from=1, best_to=4, retrofit_to=7)
PAINT = CategoryWindow("Painting", best_from=4, best_to=6, retrofit_to=None)


def test_detects_most_advanced_stage_with_evidence_and_date():
    notes = [
        (date(2025, 3, 1), "Foundation done. Walling to start next month."),
        (date(2025, 6, 1), "Roofing complete. Plastering ongoing on the ground floor."),
    ]
    result = detect_stage(notes)
    assert result.stage == 4
    assert result.label == "plastering"
    assert result.evidence == "Plastering ongoing on the ground floor."
    assert result.recorded_on == date(2025, 6, 1)


@pytest.mark.parametrize(
    ("text", "stage"),
    [
        ("Interior skimming and two coats undercoat done.", 6),
        ("Skimming ongoing.", 5),
        ("The project was handed over in August.", 7),
        ("The site is on its early stages, at foundation.", 1),
        ("Second floor slab cast.", 2),
        ("Landscaping and gate works.", None),
    ],
)
def test_stage_patterns(text, stage):
    assert detect_stage([(date(2025, 1, 1), text)]).stage == stage


def test_no_notes():
    result = detect_stage([])
    assert result.stage is None and result.label == "not recorded"


@pytest.mark.parametrize(
    ("window", "stage", "status"),
    [
        (PAINT, 2, "early"),
        (PAINT, 5, "in_window"),
        (PAINT, 7, "too_late"),
        (AUDIO, 3, "in_window"),
        (AUDIO, 6, "retrofit_only"),
        (AUDIO, None, "unknown"),
    ],
)
def test_timing_status(window, stage, status):
    recorded = date(2025, 9, 1) if stage else None
    timing = assess_timing(window, StageResult(stage, "x", recorded), date(2025, 10, 1))
    assert timing.status == status


def test_old_stage_is_marked_stale():
    timing = assess_timing(AUDIO, StageResult(3, "x", date(2025, 3, 1)), date(2026, 10, 18))
    assert timing.stale and timing.age_days == 596
    fresh = assess_timing(AUDIO, StageResult(3, "x", date(2026, 9, 1)), date(2026, 10, 18))
    assert not fresh.stale
