import json
import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from brains_bot.bot import chat_handlers, handlers
from brains_bot.database.models import AuditEvent, Project
from brains_bot.database.session import session_scope
from brains_bot.services.categories import sync_categories
from brains_bot.services.chat import ChatAssistant, ProjectTools, no_contact_details, plain_text
from tests.conftest import ALLOWED_ID, STRANGER_ID, make_update, reply_of
from tests.test_import_db import _import
from tests.workbook_factory import build_slim_workbook

TODAY = date(2025, 10, 1)


class FakeTransport:
    """Plays back chat-completions responses and records the requests."""

    model = "fake-model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def _send(self, method, path, json=None):
        self.requests.append(json)
        return self.responses.pop(0)


def tool_call(name, **arguments):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call{name[:5]}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ]
    }


def text(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


def _tool_results(transport):
    return [
        json.loads(m["content"]) for m in transport.requests[-1]["messages"] if m["role"] == "tool"
    ]


def test_no_contact_details_keeps_names():
    cleaned = no_contact_details("Call Mr Mwangi on 0712 345 678 or +254722111222, a@b.co")
    assert "Mwangi" in cleaned
    assert "0712" not in cleaned and "722111222" not in cleaned and "a@b.co" not in cleaned


def test_plain_text_strips_markdown():
    assert plain_text("### Ruiru\n- **#205** `x` __y__") == "Ruiru\n- #205 x y"


def _seed(factory, tmp_path):
    _import(factory, build_slim_workbook(tmp_path / "slim.xlsx"))
    with session_scope(factory) as s:
        sync_categories(s)
        return {p.source_key: p.id for p in s.scalars(select(Project))}


def test_tools_filter_count_and_details(factory, tmp_path):
    ids = _seed(factory, tmp_path)
    with session_scope(factory) as s:
        tools = ProjectTools(s, ALLOWED_ID, TODAY)
        found = tools.find_projects(city="nairobi")
        assert found["total"] == 2
        assert {p["id"] for p in found["projects"]} == {ids["db:1"], ids["db:3"]}

        past_plastering = tools.find_projects(stages=["skimming", "painting", "complete"])
        assert [p["id"] for p in past_plastering["projects"]] == [ids["db:1"]]
        assert tools.find_projects(stages=["not_recorded"])["total"] == 1
        assert tools.find_projects(text="Kamau")["projects"][0]["id"] == ids["db:1"]
        assert tools.find_projects(visited_from="2025-09-01")["total"] == 1

        counts = tools.count_projects(group_by="city")
        assert counts["groups"] == {"Nairobi": 2, "Eldoret": 1}
        assert tools.count_projects(group_by="stage")["groups"]["foundation / early works"] == 1

        detail = tools.get_project(ids["db:2"])
        assert detail["name"] == "Sunrise Apartments"
        assert {"role": "owner", "name": "Sunrise Ltd"} in detail["people"]
        assert detail["latest_stage"] == "foundation / early works"
        dumped = json.dumps(detail)
        assert "722111222" not in dumped and "733444555" not in dumped
        assert tools.get_project(99999) == {"error": "no project #99999"}
        assert tools.run("nope", {}) == {"error": "unknown tool nope"}
        assert tools.run("get_project", {"wrong": 1}) == {"error": "invalid arguments"}
        assert tools.analyze_opportunity(ids["db:1"], "audio") == {
            "error": "AI analysis is not configured"
        }


def test_assistant_runs_tools_then_answers(factory, tmp_path):
    ids = _seed(factory, tmp_path)
    transport = FakeTransport(
        tool_call("find_projects", area="Lavington"),
        tool_call("get_project", project_id=ids["db:1"]),
        text(f"#{ids['db:1']} Kamau proposed villa is at skimming. Call 0712345678."),
    )
    with session_scope(factory) as s:
        tools = ProjectTools(s, ALLOWED_ID, TODAY)
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        answer = ChatAssistant(transport).answer(tools, history, "What's happening in Lavington?")
    assert "Kamau proposed villa" in answer
    assert "0712345678" not in answer  # scrubbed even if the model writes one
    first = transport.requests[0]
    assert first["tools"] and first["messages"][1:3] == history
    assert "2025-10-01" in first["messages"][0]["content"]
    found, detail = _tool_results(transport)
    assert found["projects"][0]["id"] == ids["db:1"]
    assert detail["latest_stage"] == "skimming / finishing prep"
    assert "712345678" not in json.dumps(transport.requests[-1])
    assert tools.projects_seen == {ids["db:1"]}


def test_assistant_stops_after_too_many_lookups(factory, tmp_path):
    _seed(factory, tmp_path)
    transport = FakeTransport(*[tool_call("list_categories") for _ in range(6)])
    with session_scope(factory) as s:
        answer = ChatAssistant(transport).answer(ProjectTools(s, 1, TODAY), [], "loop")
    assert "too many lookups" in answer
    assert "tools" not in transport.requests[-1]  # the last round must answer in words


def _chat_context(context, factory, transport):
    context.bot_data["db"] = factory
    context.bot_data["assistant"] = ChatAssistant(transport)
    context.bot_data["analyzer"] = None
    context.user_data = {}
    return context


def _chat_update(user_id, message):
    update = make_update(user_id)
    update.effective_message.text = message
    update.effective_chat = SimpleNamespace(send_action=AsyncMock())
    return update


async def test_chat_denied_for_strangers(context):
    update = _chat_update(STRANGER_ID, "hello")
    await chat_handlers.chat_message(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_chat_without_ai_provider(context):
    context.bot_data["db"] = object()
    update = _chat_update(ALLOWED_ID, "hello")
    await chat_handlers.chat_message(update, context)
    assert reply_of(update) == chat_handlers.NOT_AVAILABLE


async def test_chat_message_end_to_end(factory, tmp_path, context, caplog):
    ids = _seed(factory, tmp_path)
    transport = FakeTransport(
        tool_call("find_projects", text="Kamau"),
        text(f"#{ids['db:1']} Kamau proposed villa in Lavington."),
        text("Its stage is skimming."),
    )
    _chat_context(context, factory, transport)
    update = _chat_update(ALLOWED_ID, "Any news on Mr Kamau's villa? call 0712345678")
    with caplog.at_level(logging.INFO):
        await chat_handlers.chat_message(update, context)
    assert "Kamau proposed villa" in reply_of(update)
    assert "Kamau" not in caplog.text and "0712345678" not in caplog.text

    follow_up = _chat_update(ALLOWED_ID, "What stage is it at?")
    await chat_handlers.chat_message(follow_up, context)
    assert reply_of(follow_up) == "Its stage is skimming."
    sent = [m["content"] for m in transport.requests[-1]["messages"]]
    assert "Any news on Mr Kamau's villa? call [phone]" in sent  # remembered, number scrubbed
    assert not any("0712345678" in m for m in sent)

    with session_scope(factory) as s:
        events = s.scalars(select(AuditEvent).where(AuditEvent.action == "chat")).all()
        assert [e.details["projects"] for e in events] == [[ids["db:1"]], []]

    reset = make_update(ALLOWED_ID)
    await chat_handlers.new_chat(reset, context)
    assert context.user_data["chat_history"] == []
