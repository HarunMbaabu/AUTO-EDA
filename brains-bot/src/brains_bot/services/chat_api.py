"""Opportunity analysis through OpenAI-style chat-completions APIs (Mistral, DeepSeek).

Same contract as the Claude analyzer in analysis.py: the same minimal, scrubbed project
context, the same instructions and the same validated answer structure. These APIs offer a JSON
mode but no guaranteed schema, so the schema is given in the instructions and every answer is
validated, with one retry.

Data note: requests go to the provider's servers. Only the scrubbed context built in
analysis.py is sent (no site, owner or contact names, phone numbers or emails).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
from pydantic import ValidationError

from brains_bot.services.analysis import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    AnalysisContext,
    AnalysisError,
    AnalysisResult,
    _user_message,
)

log = logging.getLogger(__name__)

MAX_TOKENS = 4000
TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Provider:
    key: str  # "mistral" | "deepseek"
    label: str
    key_env: str
    model_env: str
    url_env: str
    default_model: str
    default_url: str


PROVIDERS = {
    "mistral": Provider(
        "mistral", "Mistral", "MISTRAL_API_KEY", "MISTRAL_MODEL", "MISTRAL_BASE_URL",
        "mistral-large-latest", "https://api.mistral.ai/v1",
    ),
    "deepseek": Provider(
        "deepseek", "DeepSeek", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
        "deepseek-v4-pro", "https://api.deepseek.com",
    ),
}  # fmt: skip

JSON_INSTRUCTIONS = (
    "\nReply with one JSON object only, no other text. It must match this JSON Schema exactly "
    "(all fields required, no extra fields):\n" + json.dumps(OUTPUT_SCHEMA, separators=(",", ":"))
)


class ChatAnalyzer:
    def __init__(
        self,
        provider: Provider,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.provider = provider
        self.model = model or provider.default_model
        self._base = (base_url or provider.default_url).rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx.Client(timeout=TIMEOUT_SECONDS)

    @classmethod
    def from_env(cls, provider: Provider, env: Mapping[str, str] | None = None) -> ChatAnalyzer:
        env = os.environ if env is None else env
        return cls(
            provider,
            env[provider.key_env].strip(),
            env.get(provider.model_env, "").strip() or None,
            env.get(provider.url_env, "").strip() or None,
        )

    def _fail(self, response: httpx.Response) -> None:
        name, status = self.provider.label, response.status_code
        if status == 401:
            raise AnalysisError(f"The {name} API key was rejected.")
        if status == 402:
            raise AnalysisError(f"The {name} account has insufficient balance.")
        if status == 404:
            raise AnalysisError(f"{name} model '{self.model}' was not found.")
        if status == 429:
            raise AnalysisError(f"{name} is rate limited right now. Try again shortly.")
        raise AnalysisError(f"The {name} API returned an error ({status}).")

    def _send(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        name = self.provider.label
        try:
            response = self._http.request(
                method, self._base + path, headers=self._headers, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise AnalysisError(f"{name} took too long to answer. Try again.") from exc
        except httpx.HTTPError as exc:
            raise AnalysisError(f"Could not reach the {name} API.") from exc
        if response.status_code >= 400:
            self._fail(response)
        try:
            return response.json()
        except ValueError as exc:
            raise AnalysisError(f"{name} returned an unreadable response.") from exc

    def analyze(self, ctx: AnalysisContext) -> tuple[AnalysisResult, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + JSON_INSTRUCTIONS},
                {"role": "user", "content": _user_message(ctx)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
        last_problem = "no response"
        for _attempt in range(2):  # one retry if the output does not validate
            data = self._send("POST", "/chat/completions", json=body)
            choice = (data.get("choices") or [{}])[0]
            usage = data.get("usage") or {}
            finish = choice.get("finish_reason")
            log.info(
                f"{self.provider.key} call",
                extra={
                    "event": "ai_call",
                    "model": data.get("model", self.model),
                    "stop_reason": finish,
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "request_id": data.get("id"),
                },
            )
            if finish == "content_filter":
                raise AnalysisError(f"{self.provider.label} declined to analyse this project.")
            if finish in ("length", "model_length"):
                last_problem = "the answer was cut off"
                continue
            text = (choice.get("message") or {}).get("content") or ""
            try:
                result = AnalysisResult.model_validate_json(text)
            except ValidationError:
                last_problem = "the answer was not in the expected format"
                continue
            meta = SimpleNamespace(
                model=data.get("model", self.model),
                usage=SimpleNamespace(
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                ),
            )
            return result, meta
        raise AnalysisError(f"{self.provider.label}'s answer could not be used: {last_problem}.")

    def check(self) -> list[str]:
        """List the models the key can use (no tokens used). Raises AnalysisError on failure."""
        return [m.get("id", "") for m in self._send("GET", "/models").get("data", [])]


def select_provider(env: Mapping[str, str] | None = None) -> Provider | None:
    """AI_PROVIDER picks explicitly (mistral, deepseek); otherwise the first provider with a key,
    Mistral before DeepSeek. Returns None when neither applies (Claude may still be used)."""
    env = os.environ if env is None else env
    wanted = env.get("AI_PROVIDER", "").strip().lower()
    if wanted:
        provider = PROVIDERS.get(wanted)
        return provider if provider and env.get(provider.key_env, "").strip() else None
    for provider in PROVIDERS.values():
        if env.get(provider.key_env, "").strip():
            return provider
    return None
