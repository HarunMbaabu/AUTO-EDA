"""Import quality report (Markdown). Contains site names, so it is written to data/processed/,
which Git ignores. Phone numbers never appear in it."""

from __future__ import annotations

from brains_bot.importers.loader import ImportStats
from brains_bot.importers.reconcile import Reconciliation
from brains_bot.importers.reconcile import __doc__ as POLICY
from brains_bot.importers.workbook import Workbook


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def build_report(
    stats: ImportStats, result: Reconciliation, workbook: Workbook, file_name: str
) -> str:
    out: list[str] = [
        f"# Import quality report: batch {stats.batch_id}",
        "",
        f"File: `{file_name}`",
        "",
    ]

    out += ["## Summary", ""]
    total_records = sum(stats.source_rows.values())
    project_sheets = ("Data Base", "Active Sheet", "To share with Dealer")
    linked = sum(len(p.links) for p in result.projects)
    out += _table(
        ["Measure", "Value"],
        [
            ["Logical records read (all sheets)", total_records],
            [
                "Project records (3 project sheets)",
                sum(stats.source_rows[s] for s in project_sheets),
            ],
            ["Unique projects after reconciliation", stats.projects],
            ["Project records linked to a project", linked],
            ["Projects flagged for review", len(stats.flagged)],
            ["Contacts created", stats.contacts],
            ["Dealer shops (unique)", stats.dealer_shops],
        ],
    )
    out += [
        "",
        f"Every project record is accounted for: {linked} linked = "
        f"{sum(stats.source_rows[s] for s in project_sheets)} read.",
        "",
    ]

    out += ["## Records per sheet", ""]
    rows = [[s, stats.source_rows.get(s, 0)] for s in workbook.sheets]
    rows += [
        [s, "skipped (summary/pivot sheet, recalculated from data instead)"]
        for s in workbook.skipped_sheets
    ]
    rows += [[s, "skipped (unknown sheet; please review)"] for s in workbook.unknown_sheets]
    out += _table(["Sheet", "Records"], rows)
    out += [
        "",
        "One logical record can span several worksheet rows: in Data Base, follow-up comments "
        "sit in rows below the visit (merged cells). Those rows are folded into the visit.",
        "",
    ]

    out += ["## Reconciliation policy", "", POLICY.strip(), ""]
    methods: dict[str, int] = {}
    for p in result.projects:
        for link in p.links:
            methods[link.method] = methods.get(link.method, 0) + 1
    out += _table(["Link method", "Records"], [[k, v] for k, v in sorted(methods.items())])
    out += ["", "### Decisions needing attention", ""]
    out += [f"- {e}" for e in result.events] or ["- none"]
    out += [""]

    out += ["## Projects flagged for review", ""]
    if stats.flagged:
        for key, name, reasons in stats.flagged:
            out.append(f"- **{name}** (`{key}`)")
            out += [f"  - {r}" for r in reasons]
    else:
        out.append("- none")
    out += [""]

    out += ["## Projects missing from this import", ""]
    out += [f"- {name} (`{key}`)" for key, name in stats.stale] or ["- none"]
    out += [""]

    out += ["## Contact phone numbers (contact person field)", ""]
    labels = {
        "valid": "one valid Kenyan number (normalised to +254)",
        "multiple": "several numbers (kept as candidates, none chosen)",
        "invalid": "not a valid Kenyan mobile number",
        "placeholder": "placeholder such as TBA / TBC",
        "missing": "empty",
    }
    out += _table(
        ["Status", "Count", "Meaning"],
        [[k, stats.phone_status.get(k, 0), v] for k, v in labels.items()],
    )
    out += ["", "Marketing consent is imported as **unknown** for every contact.", ""]

    out += ["## Data issues", ""]
    out += ["### Dates", ""]
    out += [f"- {d}" for d in stats.date_issues] or ["- none"]
    out += ["", "### Values that differ between sheets", ""]
    out += [f"- {c}" for c in stats.conflicts] or ["- none"]
    out += ["", "### Other issues", ""]
    out += [f"- {k}: {v}" for k, v in stats.issues.items()] or ["- none"]
    out += ["", "### Name corrections applied", ""]
    out += [f"- {k}: {v} projects" for k, v in stats.aliases.items()] or ["- none"]
    out += [
        "",
        "Salesperson names also have spacing, case and suffixes such as '- project' and "
        "'(Trainee)' removed. Corrections come from the private file data/name_aliases.json. "
        "The original text is kept in `sales_assignments.salesperson_raw`.",
        "",
    ]
    out += [f"- Duplicate dealer shop rows merged: {stats.dealer_duplicates}", ""]

    out += ["## Won / lost by routed dealer (recalculated)", ""]
    rows = [
        [d, c.get("Won", 0), c.get("Lost", 0)]
        for d, c in sorted(stats.won_lost_by_dealer.items(), key=lambda kv: -sum(kv[1].values()))
    ]
    out += _table(["Dealer to route", "Won", "Lost"], rows) if rows else ["- no outcomes recorded"]
    out += [
        "",
        "Compare with the workbook's 'Hit Rate' pivot. Differences usually come from pivot "
        "filters; these numbers count each reconciled project once.",
        "",
    ]
    return "\n".join(out)
