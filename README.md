# Brains Bot

A human-in-the-loop sales intelligence assistant for authorized sales operators, delivered as a
private Telegram bot. It never sends messages to customers on its own: it prepares, a human
reviews and sends.

**Current status: Phase 3 (Claude analysis).** Product catalogs (Phase 4) are not built yet.

## Commands

| Command            | Who can use it | What it does |
|--------------------|----------------|--------------|
| `/start`           | Anyone         | Welcome message and access check |
| `/help`            | Anyone         | Lists commands (private commands only shown to authorized users) |
| `/myid`            | Anyone         | Shows the caller's numeric Telegram ID, for enrollment |
| `/health`          | Authorized     | Confirms the bot is running, with uptime |
| `/projects [page]` | Authorized     | Lists projects, most recent visit first, with Previous/Next buttons |
| `/search <text>`   | Authorized     | Searches site names, areas, cities, addresses and names of people/companies (not phone numbers) |
| `/project <number>`| Authorized     | One project: location, audio status, people, latest notes, review flags and source rows |
| `/categories`      | Authorized     | Product categories and the construction stages when each is best pitched |
| `/analyze <number> <category> [refresh]` | Authorized | Claude assessment: fit, timing, recorded facts, inferred needs, unknowns, next step |

Contact phone numbers are shown masked (`+254 7XX XXX 678`) together with the marketing consent
status, which the import always sets to **unknown**. Estimated paint start dates are labelled as
historical estimates, not current site status. Every project view and search is recorded in
`audit_events` (who and what, never the search text or any phone number).

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

## Database and workbook import

Requires PostgreSQL 14+. Create a database and user, set `DATABASE_URL` in `.env`, then:

```bash
uv run alembic upgrade head                       # create or update the tables
mkdir -p data/raw                                 # private: ignored by Git
cp "/path/to/Physical Projects Mapping Data 6th March.xlsx" data/raw/
uv run brains-bot-import "data/raw/Physical Projects Mapping Data 6th March.xlsx"
```

The import:

- reads `Data Base` (and `Active Sheet` / `To share with Dealer` when present), finding the
  header row in each sheet, and folds follow-up comment rows (merged cells) into their visit;
- accepts both the full export and the slimmed-down one (`Sr No.`, `Name of Area`,
  `Phone Number (WhatsApp)`, `Audio Status`). Column names are mapped on read, the original
  names are kept for provenance, and the report lists renamed, missing and empty columns.
  In the current file `Audio Status` holds the same site/paint progress notes as the old
  `Comments` column, so by default it is imported as notes, never as evidence of interest in
  audio. Once the column holds real audio information, re-import with
  `--audio-status status --force` to store it as each project's audio status (shown in
  `/project`). The report states which mode was used;
- treats `Data Base` as primary and links the other sheets by ID **only when the content
  agrees** (same phone, or similar site name). IDs in Active Sheet were shifted by inserted
  rows, so ID-only matching would merge different sites. Uncertain records are kept separate
  and flagged for review, never merged silently;
- normalises Kenyan phone numbers to `+254...` only when there is exactly one valid number,
  keeps the original text, and parses US-style text dates, Excel serial dates and `15.10.25`;
- stores notes verbatim, imports marketing consent as `unknown`, and loads the dealer shop
  lists. Pivot sheets (`Sheet1`, `Hit Rate`, `Pivot (2)`) are skipped and recalculated;
- writes a quality report to `data/processed/import_report_batch_<n>.md` (ignored by Git; it
  contains site names but no phone numbers).

Salesperson name corrections (e.g. "Jane" -> "Jane Wanjiru") are read from the private file
`data/name_aliases.json` (ignored by Git, since it holds staff names); see
`name_aliases.example.json` for the format. Every correction applied is listed in the report.

The bot works from the slimmed-down, audio-focused workbook. Paint sales columns (salesperson,
dealer, quotations, won/lost) are optional: when a file has them they are imported and shown,
otherwise those fields stay empty.

Re-running with the same file does nothing; `--force` re-imports without creating duplicates.
Projects that disappear from a newer workbook are flagged, not deleted.

## Claude analysis (`/analyze`)

Set `ANTHROPIC_API_KEY` and `CLAUDE_MODEL` (e.g. `claude-opus-5-5`) in `.env` or the
environment. Without them the bot runs normally and `/analyze` says it is not configured.
Check the connection with:

```bash
uv run brains-bot-check-anthropic
```

How an analysis works:

1. **Categories** come from `src/brains_bot/data/categories.json` (audio plus the cross-sell
   categories: painting & finishes, smart home, cinema add-ons, joinery, outdoor living,
   networking, flooring, interior design). Each has a best-time window in construction stages.
   The bot syncs them at start-up, or run `uv run brains-bot-categories`. Add a category by
   editing the file; no code change.
2. **Stage and timing are computed by the backend**, not by Claude: the most advanced
   construction stage mentioned in the site notes (with the sentence it came from and the
   note date) against the category's window. Stages older than 120 days are flagged as
   possibly out of date.
3. **Claude receives the minimum**: project number, area, city, dates, stage, timing and the
   site notes with phone numbers, emails, titled names (`Mr. X`) and the project's recorded
   owner/contact/architect names replaced. Site names, contact names and phone numbers are
   never sent. Notes are passed as quoted data and the instructions say to ignore any
   instructions inside them.
4. **The answer has a fixed structure** (validated; one retry if it is malformed): fit,
   summary, recorded facts with their source, possible needs marked as inferred with a
   confidence, unknowns, a timing comment and a suggested next step. Claude is told never to
   invent budgets, preferences, products or prices; no product is named until a verified
   catalog exists (Phase 4).
5. **Results are saved** in `opportunity_assessments` with the model and token counts, and
   re-used for 14 days for the same project, category and data import (add `refresh` to run
   again). Calls are logged with model, token counts and request ID only, never the prompt.
   Safety refusals, timeouts, rate limits and bad keys come back as plain messages; on
   supported models, refused requests are retried server-side on Anthropic's recommended
   fallback model.

## Development

```bash
uv sync
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
```

Database tests run only when `TEST_DATABASE_URL` points at an **empty, disposable** PostgreSQL
database (they drop and recreate every table), for example:

```bash
TEST_DATABASE_URL=postgresql://brains:change-me@localhost:5432/brains_bot_test uv run pytest
```

Tests use a small fictional workbook built in `tests/workbook_factory.py`; no real data is
committed.

## Layout

```
src/brains_bot/
├── __main__.py              # entry point: load config, logging, start polling
├── config.py                # environment settings and allowlist parsing
├── logging_setup.py         # JSON logs with redaction
├── bot/
│   ├── app.py               # builds the Telegram application, registers handlers
│   ├── handlers.py          # core commands and the access check
│   ├── project_handlers.py  # /projects, /search, /project
│   ├── analysis_handlers.py # /categories, /analyze
│   └── formatting.py        # plain-text message layout
├── database/
│   ├── models.py            # SQLAlchemy schema
│   └── session.py           # engine and sessions
├── data/
│   └── categories.json      # product categories and their timing windows
├── importers/
│   ├── workbook.py          # reads sheets, finds headers, folds continuation rows
│   ├── normalize.py         # phones, dates, names, amounts
│   ├── reconcile.py         # decides which records are the same project
│   ├── loader.py            # writes to PostgreSQL (idempotent)
│   ├── report.py            # import quality report
│   └── cli.py               # brains-bot-import command
└── services/
    ├── project_service.py   # project queries and audit log
    ├── stages.py            # construction stage detection and category timing
    ├── categories.py        # category sync and lookup
    ├── analysis.py          # Claude context, prompt, structured output, saving
    └── anthropic_check.py   # Claude connectivity check
migrations/                  # Alembic migrations
tests/
```

## Roadmap

1. **Secure Telegram bot.** Done.
2. **Workbook profiling, cleaning and PostgreSQL import; `/projects`, `/search`, `/project`.** Done.
3. **Claude analysis (`/analyze`, `/categories`) with facts kept separate from inferences.** Built;
   tested with simulated Claude responses, awaiting a live check with an API key.
4. Verified product catalogs and grounded recommendations.
5. Reviewable outreach drafts with prefilled WhatsApp/SMS links (manual send only).
6. Opportunity ranking, follow-ups and deployment.

The private workbook must never be committed. Put it in `data/raw/` locally.
