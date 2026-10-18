"""Command line: uv run brains-bot-check-ai  (checks the configured AI provider's connection)."""

from __future__ import annotations

import os
import sys

from brains_bot.config import load_env_file
from brains_bot.services.analysis import AnalysisError
from brains_bot.services.chat_api import ChatAnalyzer, select_provider


def main() -> int:
    load_env_file()
    provider = select_provider()
    if provider is not None:
        analyzer = ChatAnalyzer.from_env(provider)
        try:
            models = analyzer.check()
        except AnalysisError as exc:
            print(f"FAILED ({provider.label}): {exc}")
            return 1
        if analyzer.model not in models:
            print(
                f"FAILED ({provider.label}): model '{analyzer.model}' is not available. "
                f"Set {provider.model_env} to one of: {sorted(models)}"
            )
            return 1
        print(f"OK: {provider.label} key accepted; model '{analyzer.model}' is available.")
        return 0
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        from brains_bot.services import anthropic_check

        return anthropic_check.main()
    print("No AI provider configured: set MISTRAL_API_KEY or DEEPSEEK_API_KEY "
          "(or ANTHROPIC_API_KEY and CLAUDE_MODEL).")  # fmt: skip
    return 2


if __name__ == "__main__":
    sys.exit(main())
