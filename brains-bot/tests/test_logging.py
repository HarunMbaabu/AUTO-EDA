import json
import logging

from brains_bot.logging_setup import REDACTED, JsonFormatter, redact
from tests.conftest import FAKE_TOKEN


def test_redacts_telegram_token_and_api_key():
    text = f"GET https://api.telegram.org/bot{FAKE_TOKEN}/getMe key=sk-ant-api03-abc_DEF-123"
    out = redact(text)
    assert FAKE_TOKEN not in out
    assert "sk-ant" not in out
    assert out.count(REDACTED) == 2


def test_redacts_explicit_secret():
    assert redact("value is hunter2-secret", secrets=["hunter2-secret"]) == f"value is {REDACTED}"


def test_redacts_kenyan_phone_numbers():
    for number in ["+254712345678", "254712345678", "0712345678", "0110123456"]:
        assert redact(f"call {number} now") == f"call {REDACTED} now"
    # Short numeric IDs (e.g. Telegram user IDs) are not touched.
    assert redact("user 123456789") == "user 123456789"


def test_formatter_outputs_json_and_redacts_args_and_tracebacks():
    formatter = JsonFormatter(secrets=[FAKE_TOKEN])
    try:
        raise RuntimeError(f"boom {FAKE_TOKEN}")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "token %s", (FAKE_TOKEN,), sys.exc_info()
        )
    record.event = "test"
    record.user_id = 42
    line = formatter.format(record)
    payload = json.loads(line)
    assert FAKE_TOKEN not in line
    assert payload["event"] == "test"
    assert payload["user_id"] == 42
    assert payload["level"] == "ERROR"
