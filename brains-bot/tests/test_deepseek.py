import json

import httpx
import pytest

from brains_bot.config import load_settings
from brains_bot.logging_setup import redact
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.deepseek import DeepSeekAnalyzer
from tests.conftest import FAKE_TOKEN
from tests.test_analysis import VALID, ctx


def server(*replies, seen=None):
    """A fake DeepSeek API. Each reply is (status, body dict) or an exception."""
    queue = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def completion(content, finish="stop"):
    return 200, {
        "id": "req-1",
        "model": "deepseek-chat",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 150},
    }


def analyzer(client):
    return DeepSeekAnalyzer("sk-test", "deepseek-chat", "https://api.deepseek.com", client)


def test_successful_analysis_and_request_shape():
    seen = []
    result, meta = analyzer(server(completion(json.dumps(VALID)), seen=seen)).analyze(ctx())
    assert result.fit == "possible"
    assert meta.model == "deepseek-chat" and meta.usage.input_tokens == 800
    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in body["messages"][0]["content"]
    assert "<site_notes>" in body["messages"][1]["content"]


def test_invalid_json_is_retried_once_then_fails():
    ok = analyzer(server(completion("not json"), completion(json.dumps(VALID))))
    assert ok.analyze(ctx())[0].fit == "possible"
    bad = analyzer(server(completion("{}"), completion("{}")))
    with pytest.raises(AnalysisError, match="expected format"):
        bad.analyze(ctx())


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((401, {}), "key was rejected"),
        ((402, {}), "insufficient balance"),
        ((429, {}), "rate limited"),
        ((500, {}), "error \\(500\\)"),
        (httpx.ConnectError("blocked"), "Could not reach"),
        (completion("", finish="content_filter"), "declined"),
    ],
)
def test_errors_become_plain_messages(reply, message):
    with pytest.raises(AnalysisError, match=message):
        analyzer(server(reply)).analyze(ctx())


def test_check_lists_models():
    client = server((200, {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}))
    assert analyzer(client).check() == ["deepseek-chat", "deepseek-reasoner"]


def test_provider_selection():
    base = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN}
    assert load_settings(base).ai_provider is None
    claude = {**base, "ANTHROPIC_API_KEY": "k", "CLAUDE_MODEL": "claude-opus-5-5"}
    assert load_settings(claude).ai_provider == "anthropic"
    assert load_settings({**claude, "DEEPSEEK_API_KEY": "k"}).ai_provider == "deepseek"


def test_deepseek_style_keys_are_redacted():
    assert redact("key sk-0123456789abcdef0123456789abcdef used") == "key [REDACTED] used"
