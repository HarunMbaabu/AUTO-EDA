"""Chat mode: plain questions answered from the project database.

The model (Mistral or DeepSeek, through the chat-completions API) never sees the database. It
asks for data through a few tools that the bot runs locally, then writes the answer from what
they return. Tool results may include site and people's names, but never phone numbers or
email addresses: those are dropped from structured fields and scrubbed from free text.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from brains_bot.database.models import CatalogProduct, Contact, Project, ProjectContact
from brains_bot.importers.normalize import redact_contact_details
from brains_bot.logging_setup import redact
from brains_bot.services import analysis as an
from brains_bot.services import catalog, contacts, followups, opportunities, partners
from brains_bot.services import project_service as svc
from brains_bot.services.categories import active_categories, resolve_category
from brains_bot.services.chat_api import ChatAnalyzer
from brains_bot.services.stages import StageResult, detect_stage

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
MAX_ANSWER_TOKENS = 1500
MAX_RESULTS = 25
NOTE_CHARS = 600
HISTORY_MESSAGES = 6  # earlier user/assistant turns kept for follow-up questions

STAGE_KEYS = {
    "foundation": 1, "structure": 2, "roofing": 3, "plastering": 4, "skimming": 5,
    "painting": 6, "complete": 7,
}  # fmt: skip
STAGE_NAMES = {number: key for key, number in STAGE_KEYS.items()}
NOT_RECORDED = "not_recorded"

_MARKDOWN = re.compile(r"\*\*|__|^#{1,6}\s+|`", re.MULTILINE)


def plain_text(text: str) -> str:
    """The bot sends plain text, so Markdown markers would show up literally."""
    return _MARKDOWN.sub("", text)


def no_contact_details(text: str | None) -> str | None:
    """Free text with phone numbers and email addresses removed; names are kept."""
    if text is None:
        return None
    return redact_contact_details(redact(text).replace("[REDACTED]", "[phone]"))


SYSTEM_PROMPT = """\
You are Brains Bot, an assistant for a sales team that follows up building projects in Kenya \
(site visits, construction progress, product opportunities such as home audio). Today is {today}.

Answer only from the tool results. Never guess or invent projects, names, dates, stages or \
numbers; if the tools return nothing relevant, say so plainly. Use the tools for every question \
about projects, even follow-ups. Construction stages are detected from dated site notes, so give \
the date a stage was recorded and say when it may be out of date.

Write plain text for Telegram: short sentences or simple "- " lists, no Markdown, no tables. \
Refer to projects as "#<number> <name>" so the user can open one with /project <number>. \
Phone numbers are never available to you; for contact details tell the user to use \
/project <number>. Mention products and prices only from find_products or analyze_opportunity \
results, with their exact names and prices, and end with a short line such as "Confirm \
prices before quoting." Never invent products, brands, specifications or prices. \
For people met for future work (contractors, interior designers, developers, ...) use \
find_contacts; for people and companies we give jobs to, and their approximate rates, use \
find_partners. For numbers tell the user to send /contacts or /partners.\
"""

_FILTERS = {
    "text": {
        "type": "string",
        "description": "Words to match in site name, area, city, address or people's names",
    },
    "area": {"type": "string", "description": "Area or estate, e.g. Ruiru, Kitengela"},
    "city": {"type": "string", "description": "City, e.g. Nairobi"},
    "stages": {
        "type": "array",
        "items": {"type": "string", "enum": [*STAGE_KEYS, NOT_RECORDED]},
        "description": "Only projects whose latest recorded stage is one of these. "
        "'past plastering' = skimming, painting, complete",
    },
    "visited_from": {"type": "string", "description": "Earliest site visit date, YYYY-MM-DD"},
    "visited_to": {"type": "string", "description": "Latest site visit date, YYYY-MM-DD"},
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_projects",
            "description": "Find projects matching filters, most recent site visit first. "
            "Returns the total and up to 25 projects with their latest recorded stage.",
            "parameters": {"type": "object", "properties": _FILTERS},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_projects",
            "description": "Count projects matching the same filters, grouped by city, area, "
            "stage or visit_month (YYYY-MM).",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "string",
                        "enum": ["city", "area", "stage", "visit_month"],
                    },
                    **_FILTERS,
                },
                "required": ["group_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project",
            "description": "Details of one project: location, visit date, people and their "
            "roles, latest stage and the most recent site notes.",
            "parameters": {
                "type": "object",
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_opportunity",
            "description": "Assess one project for a product category: fit, timing, unknowns "
            "and a suggested next step. Use list_categories for the category keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer"},
                    "category": {"type": "string", "description": "Category key, e.g. audio"},
                },
                "required": ["project_id", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_opportunities",
            "description": "The best projects to pitch now, ranked by a points score with the "
            "reasons (timing, how recent the stage note is, WhatsApp number, AI fit, recent "
            "drafts). Use for 'who should I pitch', 'best leads', 'what to do this week'. Also "
            "returns follow-ups: sent drafts with no reply and open quotes to chase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Category key, e.g. audio"},
                    "limit": {"type": "integer", "description": "How many, at most 15"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_products",
            "description": "Products and packages from the verified catalog, cheapest first. "
            "With project_id, only those that still fit that project's recorded stage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Category key, e.g. audio"},
                    "text": {
                        "type": "string",
                        "description": "Words in name, brand or description",
                    },
                    "project_id": {"type": "integer"},
                    "max_price_kes": {"type": "number"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_contacts",
            "description": "The contacts directory: people by trade (contractors, interior "
            "designers, developers, architects, ...) with company, area, their sites and the "
            "latest dated note. Use for 'which contractors do we know in Migaa'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trade": {"type": "string", "enum": list(contacts.TRADES)},
                    "text": {
                        "type": "string",
                        "description": "Words in name, company, area or their sites' areas",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_partners",
            "description": "Partners: the people and companies we give jobs to (interior "
            "designers, painters, installers, other suppliers), the work they do and their "
            "approximate rates. Use for 'who can do interior design in Kiambu' or 'what does "
            "our painter charge'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "A category key or group, e.g. interior_design, "
                        "audio, 'Finishes & Interiors'",
                    },
                    "text": {"type": "string", "description": "Words in name, company or area"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "The product categories that can be analysed, with their keys.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class _Row:
    project: Project
    stage: StageResult


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _stage_key(stage: StageResult) -> str:
    return STAGE_NAMES[stage.stage] if stage.stage else NOT_RECORDED


class ProjectTools:
    """Runs the tools against the database for one chat turn."""

    def __init__(
        self,
        session: Session,
        actor: int | None,
        today: date,
        analyzer: Any = None,
    ) -> None:
        self.session = session
        self.actor = actor
        self.today = today
        self.analyzer = analyzer
        self.projects_seen: set[int] = set()

    def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "find_projects": self.find_projects,
            "count_projects": self.count_projects,
            "get_project": self.get_project,
            "analyze_opportunity": self.analyze_opportunity,
            "list_categories": self.list_categories,
            "find_products": self.find_products,
            "rank_opportunities": self.rank_opportunities,
            "find_contacts": self.find_contacts,
            "find_partners": self.find_partners,
        }
        if name not in handlers:
            return {"error": f"unknown tool {name}"}
        try:
            return handlers[name](**arguments)
        except TypeError:
            return {"error": "invalid arguments"}

    def _rows(self, filters: dict[str, Any]) -> list[_Row]:
        query = svc.ordered_projects().options(selectinload(Project.notes))
        if text := (filters.get("text") or "").strip():
            query = query.where(svc.search_clause(text))
        for column, key in ((Project.area, "area"), (Project.city, "city")):
            if value := (filters.get(key) or "").strip():
                pattern = svc.like_pattern(value)
                query = query.where(
                    or_(
                        column.ilike(pattern, escape="\\"),
                        Project.address.ilike(pattern, escape="\\"),
                    )
                    if key == "area"
                    else column.ilike(pattern, escape="\\")
                )
        if start := _parse_date(filters.get("visited_from")):
            query = query.where(Project.site_visit_date >= start)
        if end := _parse_date(filters.get("visited_to")):
            query = query.where(Project.site_visit_date <= end)
        rows = [
            _Row(p, detect_stage([(n.observed_date, n.text) for n in p.notes]))
            for p in self.session.scalars(query)
        ]
        if wanted := set(filters.get("stages") or []):
            rows = [r for r in rows if _stage_key(r.stage) in wanted]
        return rows

    def _brief(self, row: _Row) -> dict[str, Any]:
        p = row.project
        return {
            "id": p.id,
            "name": no_contact_details(p.name),
            "area": no_contact_details(p.area),
            "city": no_contact_details(p.city),
            "site_visit_date": _iso(p.site_visit_date),
            "stage": row.stage.label,
            "stage_recorded_on": _iso(row.stage.recorded_on),
        }

    def find_contacts(self, trade: str | None = None, text: str | None = None) -> dict[str, Any]:
        entries, total = contacts.directory(self.session, contacts.trade_key(trade), text)
        for e in entries:
            self.projects_seen.update(pid for pid, _ in e.projects)
        return {
            "total": total,
            "contacts": [
                {
                    "name": no_contact_details(e.contact.name),
                    "trade": contacts.label(e.contact.trade),
                    "company": no_contact_details(e.contact.company),
                    "area": no_contact_details(e.contact.area),
                    "projects": [no_contact_details(f"#{pid} {name}") for pid, name in e.projects],
                    "latest_note_date": _iso(e.last_note_on),
                    "latest_contact": e.last_how,
                    "latest_note": no_contact_details(e.last_note or "")[:NOTE_CHARS] or None,
                }
                for e in entries
            ],
        }

    def find_partners(self, category: str | None = None, text: str | None = None) -> dict[str, Any]:
        keys = partners.category_keys(self.session, category)
        if category and keys is None:
            return {"error": f"unknown category or group {category!r}; see list_categories"}
        found, total = partners.directory(self.session, keys, text, MAX_RESULTS)
        names = partners.category_names(self.session)
        return {
            "total": total,
            "partners": [
                {
                    "name": no_contact_details(p.contact.name),
                    "company": no_contact_details(p.contact.company),
                    "area": no_contact_details(p.contact.area),
                    "work": [names.get(k, k) for k in p.contact.services or []],
                    "rates": [
                        {
                            "job": no_contact_details(r.job),
                            "category": r.category_key,
                            "approx_kes": float(r.amount_kes) if r.amount_kes is not None else None,
                            "approx_max_kes": float(r.amount_max_kes)
                            if r.amount_max_kes is not None
                            else None,
                            "unit": r.unit,
                            "noted_on": _iso(r.noted_on),
                        }
                        for r in p.rates[:6]
                    ],
                }
                for p in found
            ],
        }

    def find_projects(self, **filters: Any) -> dict[str, Any]:
        rows = self._rows(filters)
        shown = rows[:MAX_RESULTS]
        self.projects_seen.update(r.project.id for r in shown)
        return {
            "total": len(rows),
            "shown": len(shown),
            "projects": [self._brief(r) for r in shown],
        }

    def count_projects(self, group_by: str, **filters: Any) -> dict[str, Any]:
        rows = self._rows(filters)

        def key(r: _Row) -> str:
            if group_by == "stage":
                return r.stage.label
            if group_by == "visit_month":
                return (
                    r.project.site_visit_date.strftime("%Y-%m")
                    if r.project.site_visit_date
                    else "no date"
                )
            value = getattr(r.project, group_by, None)
            if isinstance(value, str) and value.strip():
                return no_contact_details(value.strip())
            return "not recorded"

        groups = Counter(key(r) for r in rows)
        return {"total": len(rows), "group_by": group_by, "groups": dict(groups.most_common(40))}

    def get_project(self, project_id: int) -> dict[str, Any]:
        project = self.session.get(Project, int(project_id))
        if project is None:
            return {"error": f"no project #{project_id}"}
        links = self.session.execute(
            select(ProjectContact.role, Contact.name)
            .join(Contact, Contact.id == ProjectContact.contact_id)
            .where(ProjectContact.project_id == project.id)
        ).all()
        notes = sorted(
            project.notes, key=lambda n: (n.observed_date or date.min, n.id), reverse=True
        )
        stage = detect_stage([(n.observed_date, n.text) for n in notes])
        self.projects_seen.add(project.id)
        return {
            "id": project.id,
            "name": no_contact_details(project.name),
            "area": no_contact_details(project.area),
            "city": no_contact_details(project.city),
            "address": no_contact_details(project.address),
            "site_visit_date": _iso(project.site_visit_date),
            "people": [{"role": role, "name": no_contact_details(name)} for role, name in links],
            "latest_stage": stage.label,
            "stage_recorded_on": _iso(stage.recorded_on),
            "stage_evidence": no_contact_details(stage.evidence),
            "audio_status": no_contact_details(project.audio_status),
            "notes": [
                {"date": _iso(n.observed_date), "text": no_contact_details(n.text)[:NOTE_CHARS]}
                for n in notes[:5]
            ],
            "note_count": len(notes),
            "needs_review": project.needs_review,
            "contact_details": f"phone numbers are not shared here; use /project {project.id}",
        }

    def rank_opportunities(
        self, category: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        key = None
        if category:
            found = resolve_category(self.session, str(category))
            if found is None:
                return {"error": f"unknown category '{category}'; call list_categories"}
            key = found.key
        size = max(1, min(int(limit or 10), 15))
        items = opportunities.rank(self.session, self.today, key, size)
        self.projects_seen.update(o.project_id for o in items)
        return {
            "opportunities": [
                {
                    "project": no_contact_details(f"#{o.project_id} {o.project}"),
                    "area": no_contact_details(o.area),
                    "category": o.category,
                    "category_key": o.category_key,
                    "score": o.score,
                    "stage": o.stage,
                    "stage_recorded_on": _iso(o.recorded_on),
                    "stage_possibly_out_of_date": o.stale,
                    "reasons": o.reasons,
                    "also_worth_pitching": o.also[:3],
                    "draft_command": f"/draft {o.project_id} {o.category_key}",
                }
                for o in items
            ],  # fmt: skip
            "sent_without_reply": [
                {
                    "project": no_contact_details(f"#{w.project_id} {w.project}"),
                    "category": w.category,
                    "days_since_sent": w.days,
                }
                for w in followups.awaiting_reply(self.session, self.today)[:10]
            ],  # fmt: skip
            "open_quotes_to_chase": [
                {
                    "project": no_contact_details(f"#{q.project_id} {q.project}"),
                    "category": q.category,
                    "amount": q.detail,
                    "days_open": q.days,
                }
                for q in followups.quotes_to_chase(self.session, self.today)[:10]
            ],  # fmt: skip
        }

    def find_products(
        self,
        category: str | None = None,
        text: str | None = None,
        project_id: int | None = None,
        max_price_kes: float | None = None,
    ) -> dict[str, Any]:
        if not self.session.scalar(select(CatalogProduct.id).where(CatalogProduct.active).limit(1)):
            return {"error": "no product catalog has been loaded yet"}
        query = select(CatalogProduct).where(CatalogProduct.active)
        if category:
            found = resolve_category(self.session, str(category))
            if found is None:
                return {"error": f"unknown category '{category}'; call list_categories"}
            query = query.where(CatalogProduct.category_key == found.key)
        if words := (text or "").strip():
            pattern = svc.like_pattern(words)
            query = query.where(
                or_(
                    CatalogProduct.name.ilike(pattern, escape="\\"),
                    CatalogProduct.brand.ilike(pattern, escape="\\"),
                    CatalogProduct.description.ilike(pattern, escape="\\"),
                )
            )
        if max_price_kes is not None:
            query = query.where(CatalogProduct.price_kes <= max_price_kes)
        products = list(
            self.session.scalars(
                query.order_by(CatalogProduct.price_kes.asc().nulls_last(), CatalogProduct.id)
            )
        )
        stage = None
        result: dict[str, Any] = {}
        if project_id is not None:
            project = self.session.get(Project, int(project_id))
            if project is None:
                return {"error": f"no project #{project_id}"}
            found_stage = detect_stage([(n.observed_date, n.text) for n in project.notes])
            stage = found_stage.stage
            products = [p for p in products if catalog.fits_stage(p, stage)]
            self.projects_seen.add(project.id)
            result["project_stage"] = found_stage.label
            result["stage_recorded_on"] = _iso(found_stage.recorded_on)
        result["products"] = [catalog.product_summary(p, stage) for p in products[:MAX_RESULTS]]
        result["total"] = len(products)
        return result

    def list_categories(self) -> dict[str, Any]:
        return {
            "categories": [{"key": c.key, "name": c.name} for c in active_categories(self.session)]
        }

    def analyze_opportunity(self, project_id: int, category: str) -> dict[str, Any]:
        if self.analyzer is None:
            return {"error": "AI analysis is not configured"}
        project = self.session.get(Project, int(project_id))
        if project is None:
            return {"error": f"no project #{project_id}"}
        found = resolve_category(self.session, str(category))
        if found is None:
            return {"error": f"unknown category '{category}'; call list_categories"}
        ctx = an.build_context(self.session, project, found, self.today)
        saved = an.recent_assessment(self.session, ctx)
        if saved is not None:
            result = an.AnalysisResult.model_validate(saved.result)
            svc.record_audit(self.session, self.actor, "analysis_reused",
                             target=f"project:{project.id}", category=found.key)  # fmt: skip
        else:
            try:
                result, response = self.analyzer.analyze(ctx)
            except an.AnalysisError as exc:
                return {"error": str(exc)}
            assessment = an.save_assessment(
                self.session, ctx, result, response, self.actor, self.analyzer.model
            )
            svc.record_audit(self.session, self.actor, "analysis_created",
                             target=f"project:{project.id}", category=found.key,
                             assessment=assessment.id)  # fmt: skip
        self.projects_seen.add(project.id)
        return {
            "project": no_contact_details(f"#{project.id} {project.name}"),
            "category": found.name,
            "fit": result.fit,
            "summary": result.summary,
            "timing": ctx.timing.status,
            "timing_explanation": ctx.timing.explanation,
            "stage": ctx.stage.label,
            "stage_recorded_on": _iso(ctx.stage.recorded_on),
            "stage_possibly_out_of_date": ctx.timing.stale,
            "possible_needs": [n.need for n in result.possible_needs],
            "unknowns": result.unknowns,
            "suggested_next_step": result.suggested_next_step,
            "catalog_options": ctx.payload.get("catalog_options", []),
            "full_report": f"/analyze {project.id} {found.key}",
        }


class ChatAssistant:
    """One question in, one answer out, with the model calling ProjectTools as needed."""

    def __init__(self, transport: ChatAnalyzer) -> None:
        self.transport = transport
        self.model = transport.model

    def answer(self, tools: ProjectTools, history: list[dict[str, str]], question: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(today=tools.today.isoformat())},
            # Contact details typed into a question never leave the bot, now or in follow-ups.
            *[
                {"role": m["role"], "content": no_contact_details(m["content"])}
                for m in history[-HISTORY_MESSAGES:]
            ],
            {"role": "user", "content": no_contact_details(question)},
        ]
        for round_number in range(MAX_TOOL_ROUNDS + 1):
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": MAX_ANSWER_TOKENS,
                "stream": False,
            }
            if round_number < MAX_TOOL_ROUNDS:
                body["tools"] = TOOLS
                body["tool_choice"] = "auto"
            data = self.transport._send("POST", "/chat/completions", json=body)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            usage = data.get("usage") or {}
            calls = message.get("tool_calls") or []
            log.info(
                "chat call",
                extra={
                    "event": "chat_call",
                    "model": data.get("model", self.model),
                    "stop_reason": choice.get("finish_reason"),
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "tools": [c.get("function", {}).get("name") for c in calls],
                },
            )
            if not calls:
                text = (message.get("content") or "").strip()
                return (
                    plain_text(no_contact_details(text))
                    or "Sorry, I don't have an answer for that."
                )
            messages.append(
                {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
            )
            for call in calls:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = None
                result = (
                    tools.run(function.get("name", ""), arguments)
                    if isinstance(arguments, dict)
                    else {"error": "arguments were not valid JSON"}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": function.get("name"),
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
        return "Sorry, that question needed too many lookups. Try asking something narrower."
