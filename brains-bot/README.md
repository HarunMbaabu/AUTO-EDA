# Brains Bot

A human-in-the-loop sales intelligence assistant for authorized sales operators, delivered as a
private Telegram bot. It never sends messages to customers on its own: it prepares, a human
reviews and sends.

**Current status:** all planned phases built and deployed on Render with Mistral: lookup,
analysis, chat, catalog, WhatsApp drafts, opportunity ranking, weekly follow-ups and the web
dashboard.

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
| `/analyze <number> <category> [refresh]` | Authorized | AI assessment: fit, timing, recorded facts, inferred needs, unknowns, next step |
| `/draft <number> <category>` | Authorized | AI-drafted WhatsApp message to the project's contact; **[Send via WhatsApp]** opens it in your WhatsApp (you press send), **[They replied]** records a reply |
| `/top [category]`  | Authorized     | Best opportunities to pitch, with the reasons for each score |
| `/followups`       | Authorized     | This week's summary: who to pitch, sent drafts with no reply, open quotes to chase |
| `/reminders on\|off` | Authorized   | The weekly summary, sent Mondays at 08:00 Nairobi time (on by default) |
| `/newsite <message>` | Authorized | Add a project by pasting a WhatsApp site report; check what was understood, then tap Save |
| Any plain message | Authorized | Chat mode: ask in your own words, e.g. "which projects in Ruiru are at painting stage?" |
| `/newchat`         | Authorized     | Starts a new conversation (forgets earlier questions) |
| `/import` + send the `.xlsx` | Authorized | Loads or updates the data from a workbook sent to the bot as a file (caption `force` re-imports the same file) |

An uploaded workbook is saved only to a temporary folder for the import and deleted right after;
the import is the same as `brains-bot-import` (max 20 MB, Telegram's limit for bots).

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

## Quick setup in a fresh cloud or dev container

```bash
bash scripts/cloud_setup.sh   # PostgreSQL, dependencies, tables, categories, import
uv run brains-bot
```

The script is safe to re-run. It reports which secrets are missing (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_IDS`, `ANTHROPIC_API_KEY`); set them in the environment settings or in
`.env`. The private workbook has to be put in `data/raw/` by hand.

## Run

```bash
uv run brains-bot            # or: uv run python -m brains_bot
```

The bot uses long polling, so no public URL is needed. Stop it with Ctrl+C; it shuts down cleanly.

## Deploy on Render (always on)

`render.yaml` is a Render Blueprint: a background worker (Starter plan) built from the
`Dockerfile`, plus a PostgreSQL database (Basic 256 MB plan). Each deploy runs
`alembic upgrade head` first; categories are synced when the bot starts.

1. In Render: **New > Blueprint**, connect the GitHub repository that holds this project.
   Render reads `render.yaml` from the repository root, so this works when `brains-bot/` is the
   root (e.g. `vluti/brains-bot`). For a repository where the project sits in `brains-bot/`
   (AUTO-EDA), create the database and a **Background Worker** by hand instead, with
   **Root Directory** `brains-bot`, **Runtime** Docker, **Pre-Deploy Command**
   `alembic upgrade head` and the environment variables listed in `render.yaml`
   (`DATABASE_URL` = the database's Internal Database URL).
2. Enter the secrets Render asks for: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`,
   `MISTRAL_API_KEY`.
3. Stop every other copy of the bot first (dev machine, cloud session). Telegram allows one
   long-polling connection per token; two copies make both fail with "Conflict" errors.
4. Load the data: send the workbook to the bot in Telegram as a file (see `/import`). Do the
   same whenever a newer workbook arrives. Alternatively, allow your IP in the database's
   **Access Control** and run `brains-bot-import` from your own machine with
   `DATABASE_URL` set to the **External Database URL**.

Logs are in the worker's **Logs** tab. Pushing to the connected branch redeploys
automatically.

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

## Adding sites from WhatsApp (`/newsite`)

Paste a site report after `/newsite` (or send `/newsite`, then paste the message):

```
/newsite This a project at Runda mumwe Riaka they have finished on the building phase ...
the contractor is Maina 0721462244. https://maps.app.goo.gl/...
```

The bot finds the phone number(s) and the Google Maps link itself; the AI only sees the message
with contact details removed and returns the site name, area, city and the contact's name and
role. The bot shows what it understood with **✅ Save** / **✖ Cancel**; nothing is stored before
Save. A saved site becomes a normal project: the person named is its contact person (usable by
`/draft`), the whole message is its first note (so the stage is read from it), and the visit
date is today. Sites added this way are not flagged when a newer workbook is uploaded, because
they were never in it. Mention the stage in plain words (foundation, walling, roofing,
plastering, skimming, painting, handed over) so timing and ranking can use it.

## WhatsApp drafts (`/draft`)

`/draft 205 audio` writes a short, personal WhatsApp message to the project's contact person
(or owner) about that category, naming at most two catalog products that fit the recorded
stage. The bot **never sends anything**: tapping **📤 Send via WhatsApp** records the draft as
sent and shows an **Open WhatsApp** link (`wa.me`) that opens the message in your own
WhatsApp, where you can edit it and press send. A contact with several numbers gets one button
per number. **✅ They replied** records a reply, which feeds the dashboard's response rate.

Marketing consent is imported as *unknown*, so every draft reminds you to send only to people
who agreed to hear from you. The AI receives the contact's first name, the site name, area,
stage and catalog options; phone numbers are only used locally to build the link.
`OUTREACH_COMPANY` and `OUTREACH_SIGNATURE` set the company name and sign-off (default:
Intelligent Home).

## Opportunity ranking and follow-ups

Every project gets a points score per category, so the ranking can always be explained:

| Signal | Points |
|--------|--------|
| Timing for the category | best window +40, coming up +20, retrofit only +15, stage unknown +5 |
| Age of the stage note | within 4 months +20, within a year +10 |
| A WhatsApp number on record | +15 |
| Saved AI analysis | strong fit +25, possible +15, weak -15 |
| A catalog product fits the recorded stage | +5 |
| Drafted in the last 30 days | -25 |
| A lost quote for it | -20 |

Projects too late for a category, or with an open or won quote for it, are left out. `/top`
shows one row per project (its best category, others listed); `/top audio` ranks one
category. Each line shows its reasons and the `/draft` command to act on it. The dashboard's
**Opportunities** page shows the same ranking, and chat answers "who should I pitch this
week?" from it.

**Follow-ups** are sent drafts with no reply after 5 days and open quotes older than 14 days.
Every Monday at 08:00 (Nairobi) the bot sends each allowlisted user a summary: the top five
opportunities, drafts awaiting a reply and quotes to chase. `/reminders off` stops it for you;
`/followups` shows it any time.

## Dashboard (web)

A private website, the "Command Center", reading the same database as the bot:

- **Clients & pipeline:** contacts (and how many have a WhatsApp number), projects, the
  cross-sell pipeline per category (best time now / retrofit only / coming up, from each
  project's recorded stage and the category's window), the projects to pitch now and those
  entering a window next. Stages come from dated notes; old ones are flagged "confirm".
- **Bot performance:** messages drafted, sent via WhatsApp, response rate, most pitched
  category, analyses and drafts per category, latest drafts.
- **Revenue:** value of open quotes, won and lost value, win rate, top packages (named in
  drafts and quoted). Quotes are recorded on each project's page (category, package, amount)
  and marked won or lost there.

The workbook has no paint start dates, quote values or outcomes, so timelines use the recorded
construction stage, and revenue comes from the quotes you record.

**Login:** "Log in with Telegram", accepted only for the IDs in `TELEGRAM_ALLOWED_USER_IDS`.
Telegram's signature is checked with the bot token; sessions last 7 days.

Run locally: `DASHBOARD_HTTPS_ONLY=0 uv run brains-bot-dashboard` (port 8000, or `$PORT`).

**On Render**, `render.yaml` adds a web service `brains-bot-dashboard` (Starter plan) from the
same image, sharing the database and taking the bot token and allowlist from the bot's
service. After the first deploy:

1. Note the dashboard's address, e.g. `https://brains-bot-dashboard.onrender.com`.
2. In Telegram, send `/setdomain` to **@BotFather**, choose the bot and enter that address.
   The login button only works on the domain set there.
3. Open the address and log in with Telegram.

## Product catalog

The verified catalog is a spreadsheet with a `Catalog` sheet, one row per product or package:

| Column | Meaning |
|--------|---------|
| `category` | A category key (`/categories`), e.g. `audio`, `cinema_addons`, `smart_home` |
| `sku` | Your unique product code |
| `name`, `brand`, `supplier`, `description` | Shown to the sales team; `supplier` e.g. Intelligent Home or Prime Audio Solutions |
| `price_kes`, `price_unit` | `300000`, `300,000`, `300K` or `1.5M`; blank = "to be scoped". Put `from` in the unit (e.g. `package (from)`) for starting prices |
| `install_stage` | `pre-wiring` or `first fix` (before plastering ends), `finishing` (until painting), `any time` |
| `retrofit_ok` | `yes` if it can still be added after its install stage |
| `available`, `price_checked`, `notes` | Optional |

Send the file to the bot in Telegram (as with the workbook). The whole file is checked first:
one bad row rejects it with a list of what to fix, so a half-loaded catalog never goes live.
Loading replaces the previous catalog; products left out are hidden, not deleted. Categories
with products are marked as having a verified catalog.

`/analyze` then lists **catalog options that fit the project's recorded stage** (normal
installs first, then retrofit options, cheapest first) and the AI may mention only those, by
their catalog name and price. In chat, `find_products` answers questions such as "what audio
products would suit #205?" or "what's our cheapest smart home option?". Catalog data contains
no customer information.

## Chat mode

Any message that is not a command is answered from the project database. The AI provider
(Mistral or DeepSeek; Claude is not used for chat) never queries the database itself: it calls
six tools that the bot runs locally (`find_projects`, `count_projects`, `get_project`,
`analyze_opportunity`, `find_products`, `list_categories`) and writes the answer from what they return.

- **What leaves the bot:** your question, the last three questions and answers (for follow-ups,
  kept for 30 minutes or until `/newchat`), and tool results: project, area, city and people's
  names, visit dates, detected stages and site notes. **Phone numbers and email addresses are
  never sent**: they are left out of tool results, scrubbed from notes, and scrubbed from the
  answer too. For contact details the bot points to `/project <number>`.
- Stages come from the same note-based detection as `/analyze`; projects whose notes mention no
  stage count as "not recorded".
- Questions are not logged; each answer records a `chat` audit event with the project numbers
  it looked at.

## AI analysis (`/analyze`)

The operator chose **Mistral** as the provider (the original spec named Claude). Supported
providers, in the order they are picked when several keys are set:

| Provider | Key | Model setting (default) | Host to allow |
|---|---|---|---|
| Mistral | `MISTRAL_API_KEY` | `MISTRAL_MODEL` (`ministral-14b-latest`) | `api.mistral.ai` |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` (`deepseek-v4-pro`) | `api.deepseek.com` |
| Claude | `ANTHROPIC_API_KEY` | `CLAUDE_MODEL` (required) | `api.anthropic.com` |

Set `AI_PROVIDER=mistral|deepseek|anthropic` to choose explicitly; if the chosen provider has
no key, analysis stays off rather than silently switching. With no provider, the bot runs
normally and `/analyze` says it is not configured. Check the connection (lists the models your
key can use; no tokens used for Mistral/DeepSeek) with:

```bash
uv run brains-bot-check-ai
```

In a cloud environment with limited network access, allow the provider's host.

**Data protection:** analysis requests go to the chosen provider's servers, which are outside
Kenya. Only the scrubbed context described below is sent, but confirm the
cross-border transfer is acceptable under the Kenya Data Protection Act before real use.

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
3. **The AI receives the minimum**: project number, area, city, dates, stage, timing and the
   site notes with phone numbers, emails, titled names (`Mr. X`) and the project's recorded
   owner/contact/architect names replaced. Site names, contact names and phone numbers are
   never sent. Notes are passed as quoted data and the instructions say to ignore any
   instructions inside them.
4. **The answer has a fixed structure** (validated against a schema; one retry if it is
   malformed; Claude enforces the schema server-side, Mistral and DeepSeek use JSON mode): fit,
   summary, recorded facts with their source, possible needs marked as inferred with a
   confidence, unknowns, a timing comment and a suggested next step. The model is told never to
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
    ├── analysis.py          # AI context, prompt, answer schema, Claude analyzer, saving
    ├── chat_api.py          # Mistral / DeepSeek analyzer (same context and schema)
    ├── ai_check.py          # brains-bot-check-ai
    └── anthropic_check.py   # Claude connectivity check
migrations/                  # Alembic migrations
tests/
```

## Roadmap

1. **Secure Telegram bot.** Done.
2. **Workbook import and project lookup (`/projects`, `/search`, `/project`).** Done.
3. **AI analysis (`/analyze`) and chat mode,** facts kept apart from inferences. Done (Mistral).
4. **Verified product catalog and stage-aware suggestions.** Done.
5. **WhatsApp drafts with manual send (`/draft`).** Done.
6. **Web dashboard (pipeline, bot performance, revenue) with Telegram login.** Done.
7. **Opportunity ranking and weekly follow-up reminders (`/top`, `/followups`).** Done.

The private workbook must never be committed. Put it in `data/raw/` locally.
