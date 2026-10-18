"""Optional Anthropic connectivity check. Never called by the bot at startup.

Run with: uv run --extra anthropic brains-bot-check-anthropic
"""

from __future__ import annotations

import os
import sys
from typing import Any

from dotenv import load_dotenv

PROMPT = "Reply with the single word: pong"


def check(client: Any, model: str) -> str:
    """Send one tiny request and return the model's text reply."""
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": PROMPT}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def main(client: Any = None) -> int:
    load_dotenv()
    model = os.environ.get("CLAUDE_MODEL", "").strip()
    if not model:
        print("CLAUDE_MODEL is not set. Add it to .env (e.g. CLAUDE_MODEL=claude-opus-5-5).")
        return 2

    try:
        import anthropic
    except ImportError:
        print("The anthropic package is not installed. Run: uv sync --extra anthropic")
        return 2

    if client is None:
        client = anthropic.Anthropic(timeout=30.0, max_retries=1)

    try:
        reply = check(client, model)
    except anthropic.AuthenticationError:
        print("FAILED: the API key was rejected. Check ANTHROPIC_API_KEY.")
        return 1
    except anthropic.PermissionDeniedError:
        print("FAILED: the API key does not have permission for this request.")
        return 1
    except anthropic.NotFoundError:
        print(f"FAILED: model '{model}' was not found. Check CLAUDE_MODEL.")
        return 1
    except anthropic.RateLimitError:
        print("FAILED: rate limited. Try again shortly.")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"FAILED: API returned HTTP {exc.status_code}.")
        return 1
    except anthropic.APIConnectionError:
        print("FAILED: could not reach the Anthropic API. Check the network connection.")
        return 1

    print(f"OK: model '{model}' responded ({len(reply)} characters).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
