"""DeepSeek-backed opportunity analysis.

Same contract as the Claude analyzer in analysis.py: the same minimal, scrubbed project
context, the same instructions and the same validated answer structure. DeepSeek's API is
OpenAI-style (POST /chat/completions). It has a JSON mode but no schema enforcement, so the
schema is given in the instructions and every answer is validated, with one retry.

Data note: requests go to DeepSeek's servers. Only the scrubbed context built in analysis.py
is sent (no site, owner or contact names, phone numbers or emails).
"""

from __future__ import annotations

import json
import logging
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

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
MAX_TOKENS = 4000
TIMEOUT_SECONDS = 120

JSON_INSTRUCTIONS = (
    "\nReply with one JSON object only, no other text. It must match this JSON Schema exactly "
    "(all fields required, no extra fields):\n" + json.dumps(OUTPUT_SCHEMA, separators=(",", ":"))
)


class DeepSeekAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx.Client(timeout=TIMEOUT_SECONDS)

    def _post(self, ctx: AnalysisContext) -> dict[str, Any]:
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
        try:
            response = self._http.post(self._url, json=body, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise AnalysisError("DeepSeek took too long to answer. Try again.") from exc
        except httpx.HTTPError as exc:
            raise AnalysisError("Could not reach the DeepSeek API.") from exc
        status = response.status_code
        if status == 401:
            raise AnalysisError("The DeepSeek API key was rejected.")
        if status == 402:
            raise AnalysisError("The DeepSeek account has insufficient balance.")
        if status == 404:
            raise AnalysisError(
                f"DeepSeek model '{self.model}' was not found. Check DEEPSEEK_MODEL."
            )
        if status == 429:
            raise AnalysisError("DeepSeek is rate limited right now. Try again shortly.")
        if status >= 400:
            raise AnalysisError(f"The DeepSeek API returned an error ({status}).")
        try:
            return response.json()
        except ValueError as exc:
            raise AnalysisError("DeepSeek returned an unreadable response.") from exc

    def analyze(self, ctx: AnalysisContext) -> tuple[AnalysisResult, Any]:
        last_problem = "no response"
        for _attempt in range(2):  # one retry if the output does not validate
            data = self._post(ctx)
            choice = (data.get("choices") or [{}])[0]
            usage = data.get("usage") or {}
            finish = choice.get("finish_reason")
            log.info(
                "deepseek call",
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
                raise AnalysisError("DeepSeek declined to analyse this project.")
            if finish == "length":
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
        raise AnalysisError(f"DeepSeek's answer could not be used: {last_problem}.")

    def check(self) -> list[str]:
        """List available models (no tokens used). Raises AnalysisError on failure."""
        url = self._url.rsplit("/chat/completions", 1)[0] + "/models"
        try:
            response = self._http.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise AnalysisError("Could not reach the DeepSeek API.") from exc
        if response.status_code == 401:
            raise AnalysisError("The DeepSeek API key was rejected.")
        if response.status_code >= 400:
            raise AnalysisError(f"The DeepSeek API returned an error ({response.status_code}).")
        return [m.get("id", "") for m in response.json().get("data", [])]
