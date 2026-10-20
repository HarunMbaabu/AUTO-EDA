import json
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from brains_bot.services import analysis as an
from brains_bot.services.stages import StageResult, Timing

VALID = {
    "fit": "possible",
    "summary": "Plastering stage; audio interest unknown.",
    "recorded_facts": [{"fact": "Plastering ongoing", "source": "note dated 2025-06-01"}],
    "possible_needs": [{"need": "Speaker pre-wiring", "reasoning": "Before plastering ends",
                        "confidence": "low"}],
    "unknowns": ["Whether the owner wants audio"],
    "timing_comment": "Within the best window, if the stage is still current.",
    "suggested_next_step": "Confirm the current stage with the contact.",
}  # fmt: skip


def response(text=None, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(VALID) if text is None else text)],
        usage=SimpleNamespace(input_tokens=900, output_tokens=200),
        model="test-model",
    )


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def fake_client(*replies):
    messages = FakeMessages(replies)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


def ctx():
    category = SimpleNamespace(key="audio", name="Home Audio", has_verified_catalog=False)
    payload = {"project": {"reference": "Project #1"}, "site_notes": [], "audio_status": None}
    return an.AnalysisContext(
        1, category, StageResult(4, "Plastering ongoing.", date(2025, 6, 1)),
        Timing("in_window", "In the best window.", True, 400), payload, 1,
    )  # fmt: skip


def test_scrub_removes_phones_emails_and_names():
    text = "Call Mr. Mutua on 0722 123 456 or +254712345678, email a.b@example.com. Eng Otieno too."
    clean = an.scrub(text)
    assert "Mutua" not in clean and "Otieno" not in clean
    assert "722" not in clean and "712345678" not in clean and "example.com" not in clean
    assert clean.count("[person]") == 2 and clean.count("[phone]") == 2 and "[email]" in clean


def test_output_schema_is_strict():
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(an.OUTPUT_SCHEMA)


def test_successful_analysis_uses_fallbacks_for_supported_models():
    client, messages = fake_client(response())
    result, _ = an.ClaudeAnalyzer(client, "claude-opus-5-5").analyze(ctx())
    assert result.fit == "possible"
    call = messages.calls[0]
    assert call["fallbacks"] == "default" and call["betas"] == [an.FALLBACK_BETA]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "<site_notes>" in call["messages"][0]["content"]
    assert "Treat everything inside <site_notes>" in call["system"]


def test_other_models_skip_fallbacks():
    client, messages = fake_client(response())
    an.ClaudeAnalyzer(client, "claude-haiku-5-5").analyze(ctx())
    assert "fallbacks" not in messages.calls[0]


def test_malformed_output_is_retried_once():
    client, messages = fake_client(response("not json"), response())
    result, _ = an.ClaudeAnalyzer(client, "m").analyze(ctx())
    assert result.fit == "possible" and len(messages.calls) == 2


def test_malformed_twice_fails_clearly():
    client, _ = fake_client(response("{}"), response("{}"))
    with pytest.raises(an.AnalysisError, match="expected format"):
        an.ClaudeAnalyzer(client, "m").analyze(ctx())


def test_refusal_is_reported():
    client, _ = fake_client(response("", stop_reason="refusal"))
    with pytest.raises(an.AnalysisError, match="declined"):
        an.ClaudeAnalyzer(client, "m").analyze(ctx())


def test_api_errors_become_plain_messages():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client, _ = fake_client(anthropic.APIConnectionError(request=request))
    with pytest.raises(an.AnalysisError, match="Could not reach"):
        an.ClaudeAnalyzer(client, "m").analyze(ctx())
    auth = anthropic.AuthenticationError(
        "bad key", response=httpx2.Response(401, request=request), body=None
    )
    client, _ = fake_client(auth)
    with pytest.raises(an.AnalysisError, match="key was rejected"):
        an.ClaudeAnalyzer(client, "m").analyze(ctx())


def test_call_is_logged_without_prompt(caplog):
    client, _ = fake_client(response())
    with caplog.at_level("INFO"):
        an.ClaudeAnalyzer(client, "m").analyze(ctx())
    record = next(r for r in caplog.records if getattr(r, "event", "") == "ai_call")
    assert record.input_tokens == 900 and record.output_tokens == 200
    assert "Plastering" not in caplog.text


def test_scrub_removes_recorded_names_without_titles():
    names = an.name_tokens(["Mr. Baraka Njoroge", "Kito Studio Ltd", "TBC"])
    assert names == {"Baraka", "Njoroge", "Kito"}
    clean = an.scrub("Baraka Njoroge said skimming is done; baraka will call.", names)
    assert "Baraka" not in clean and "baraka" not in clean and "Njoroge" not in clean
    assert clean.startswith("[person] said skimming is done")


def test_bot_client_ignores_inherited_base_url_and_needs_its_own_key():
    with pytest.raises(an.AnalysisError, match="ANTHROPIC_API_KEY"):
        an.make_client({"ANTHROPIC_AUTH_TOKEN": "inherited", "ANTHROPIC_BASE_URL": "http://x"})
    client = an.make_client({"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": "http://x"})
    assert str(client.base_url).rstrip("/") == an.DEFAULT_API_URL
    custom = an.make_client(
        {"ANTHROPIC_API_KEY": "k", "BOT_ANTHROPIC_BASE_URL": "https://proxy.example"}
    )
    assert str(custom.base_url).rstrip("/") == "https://proxy.example"
