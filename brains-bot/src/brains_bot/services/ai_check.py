"""Command line: uv run brains-bot-check-ai  (checks the configured AI provider's connection)."""

from __future__ import annotations

import os
import sys

from brains_bot.config import load_env_file
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekAnalyzer


def main() -> int:
    load_env_file()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        model = os.environ.get("DEEPSEEK_MODEL", "").strip() or DEFAULT_MODEL
        base = os.environ.get("DEEPSEEK_BASE_URL", "").strip() or DEFAULT_BASE_URL
        try:
            models = DeepSeekAnalyzer(key, model, base).check()
        except AnalysisError as exc:
            print(f"FAILED (DeepSeek): {exc}")
            return 1
        if model not in models:
            print(f"FAILED (DeepSeek): model '{model}' is not available. Available: {models}")
            return 1
        print(f"OK: DeepSeek key accepted; model '{model}' is available.")
        return 0
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        from brains_bot.services import anthropic_check

        return anthropic_check.main()
    print(
        "No AI provider configured: set DEEPSEEK_API_KEY (or ANTHROPIC_API_KEY and CLAUDE_MODEL)."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
