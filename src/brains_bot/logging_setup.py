"""Structured (JSON-lines) logging with secret and phone-number redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Telegram bot token: <bot id>:<35-char secret>. No leading \b: in API URLs it follows "bot".
    re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}"),
    # Anthropic API keys
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    # Kenyan mobile numbers: +2547..., 2547..., 07..., 01...
    re.compile(r"(?<!\d)(?:\+?254|0)[17]\d{8}(?!\d)"),
]

# Fields a caller may attach with `extra=` that are copied into the JSON output.
_EXTRA_FIELDS = (
    "event", "user_id", "command", "authorized", "allowlist_size",
    "model", "stop_reason", "input_tokens", "output_tokens", "request_id",
)  # fmt: skip


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in _EXTRA_FIELDS:
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Redact the fully serialised line so nothing slips through via args or tracebacks.
        return redact(json.dumps(payload, default=str), self._secrets)


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # HTTP client loggers include request URLs, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "httpx2", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
