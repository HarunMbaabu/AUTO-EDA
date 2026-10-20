"""Log in with Telegram, restricted to the bot's allowlist.

The Telegram Login Widget sends the user's id, name and an auth_date, signed with a key
derived from the bot token (https://core.telegram.org/widgets/login#checking-authorization).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

MAX_AGE_SECONDS = 24 * 3600


def verify_login(data: Mapping[str, str], bot_token: str, now: float | None = None) -> int | None:
    """The Telegram user id if the login data is genuine and recent, else None."""
    received = data.get("hash", "")
    fields = {k: v for k, v in data.items() if k != "hash"}
    if not received or "id" not in fields or "auth_date" not in fields:
        return None
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        user_id = int(fields["id"])
    except ValueError:
        return None
    return user_id if 0 <= age <= MAX_AGE_SECONDS else None


def sign_login(fields: Mapping[str, str], bot_token: str) -> dict[str, str]:
    """Sign login data the way Telegram does. For tests and local previews only."""
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    return {**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}
