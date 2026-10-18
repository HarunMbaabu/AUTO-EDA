# Brains Bot

A human-in-the-loop sales intelligence assistant for authorized sales operators, delivered as a
private Telegram bot. It never sends messages to customers on its own: it prepares, a human
reviews and sends.

**Current status: Phase 1 (secure Telegram bot).** Data import (Phase 2) and Claude analysis
(Phase 3) are not built yet.

## Phase 1 features

| Command   | Who can use it | What it does |
|-----------|----------------|--------------|
| `/start`  | Anyone         | Welcome message and access check |
| `/help`   | Anyone         | Lists commands (private commands only shown to authorized users) |
| `/myid`   | Anyone         | Shows the caller's numeric Telegram ID, for enrollment |
| `/health` | Authorized     | Confirms the bot is running, with uptime |

Security behaviour:

- **Allowlist, deny by default.** Only Telegram user IDs in `TELEGRAM_ALLOWED_USER_IDS` can use
  private features. An empty list denies everyone.
- **Private chats only.** The bot ignores group chats so private data never lands in a group.
- **Redacted JSON logs.** Bot tokens, Anthropic API keys and Kenyan phone numbers are masked in
  every log line, including tracebacks. Message text is never logged. HTTP client logs (which
  contain the token in URLs) are suppressed below WARNING.
- **Secrets from the environment only.** `.env` is ignored by Git, and so are spreadsheets and
  the `data/raw/` and `data/processed/` folders.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd brains-bot
uv sync                      # installs the bot and dev tools
cp .env.example .env         # then edit .env
```

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token in
   `TELEGRAM_BOT_TOKEN`.
2. Start the bot (below), send it `/myid`, and put the number in `TELEGRAM_ALLOWED_USER_IDS`
   (comma-separate several IDs).
3. Restart the bot. `/health` should now reply `OK`.

## Run

```bash
uv run brains-bot            # or: uv run python -m brains_bot
```

The bot uses long polling, so no public URL is needed. Stop it with Ctrl+C; it shuts down cleanly.

## Optional: check the Claude API connection

Not used by the bot yet. Set `ANTHROPIC_API_KEY` and `CLAUDE_MODEL` in `.env`, then:

```bash
uv sync --extra anthropic
uv run brains-bot-check-anthropic
```

It sends one tiny request and prints `OK` or a specific failure (bad key, unknown model,
network error).

## Development

```bash
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
```

## Layout

```
src/brains_bot/
├── __main__.py              # entry point: load config, logging, start polling
├── config.py                # environment settings and allowlist parsing
├── logging_setup.py         # JSON logs with redaction
├── bot/
│   ├── app.py               # builds the Telegram application, registers handlers
│   └── handlers.py          # command handlers and the access check
└── services/
    └── anthropic_check.py   # optional Claude connectivity check
tests/
```

## Roadmap

1. **Secure Telegram bot.** Done.
2. Workbook profiling, cleaning and PostgreSQL import; `/projects`, `/search`, `/project`.
3. Claude analysis (`/analyze`) with facts kept separate from inferences.
4. Verified product catalogs and grounded recommendations.
5. Reviewable outreach drafts with prefilled WhatsApp/SMS links (manual send only).
6. Opportunity ranking, follow-ups and deployment.

The private workbook must never be committed. Put it in `data/raw/` locally.
