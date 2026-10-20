# Brains Bot: handover

Read this first when picking the project up (a person, or a new Claude Code session). How each
feature works is in `README.md`; this note covers where things are, how to deploy, and what is
open.

## What it is

A private Telegram bot and web dashboard for a Kenyan sales team that follows up building
sites. It keeps the projects from the team's workbook and WhatsApp reports, reads the
construction stage from dated notes, ranks what to pitch and when, writes WhatsApp follow-up
drafts for a person to send, and keeps contacts, partners (people jobs are routed to), supplier
catalogs and brochures. It never messages customers itself.

## Where things are

| What | Where |
|------|-------|
| Code | `brains-bot/` in `HarunMbaabu/AUTO-EDA` (branch `claude/zealous-tesla-myy7wv`) |
| What Render deploys | `vluti/brains-bot` (`main`), a copy of `brains-bot/` made with `git subtree split` |
| Hosting | Render Blueprint (`render.yaml`): worker `brains-bot` (the Telegram bot), web service `brains-bot-dashboard`, Postgres `brains-bot-db` |
| Database changes | Alembic migrations in `migrations/`; Render runs `alembic upgrade head` before each deploy |
| Categories | `src/brains_bot/data/categories.json` (groups, best stages, pairs for drafts); synced when the bot starts |
| AI | Mistral (`ministral-14b-latest`) through the chat-completions API; Mistral's OCR endpoint is not available on the current plan, so brochures fall back to the chat model per page |

Secrets live only in Render (Environment) and a local `.env`, never in git:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `AI_PROVIDER`, `MISTRAL_API_KEY`,
`MISTRAL_MODEL`, `DATABASE_URL` (plus optional DeepSeek / Anthropic keys and the dashboard's
own settings in `render.yaml`). The workbook, catalogs, `.env` and logs are git-ignored.

## Deploying a change

From the repository root, one command at a time:

```
git pull origin claude/zealous-tesla-myy7wv
git branch -D brains-bot-only          # "not found" is fine
git subtree split -P brains-bot -b brains-bot-only
git push https://github.com/vluti/brains-bot.git brains-bot-only:main
```

Render deploys on the push. Never force-push to `vluti/brains-bot` unless its history really
differs (check that the push is a fast-forward first).

## Working on it

```
cd brains-bot
uv sync
uv run ruff format src tests && uv run ruff check src tests
TEST_DATABASE_URL=postgresql+psycopg://.../brains_bot_test uv run pytest -q
```

Tests need a Postgres test database (`TEST_DATABASE_URL`). In a cloud sandbox Postgres may stop
between sessions: `service postgresql start`.

Rules the code keeps:

- Phone numbers and emails never go to the AI (`redact_contact_details`); names may.
- Nothing is saved from a pasted message or a brochure before a person taps Save (or sends the
  checked spreadsheet back).
- Use made-up names and numbers in tests and docs (e.g. `0712 345 678`, `0790...`), never real
  customers'.

## Features (details in README.md)

`/newsite` (WhatsApp reports, several sites per message, dates from the text, follow-ups by phone,
possible repeats by name and area, mistyped numbers flagged) · `/contacts` (directory by trade) ·
`/partner`, `/partners` (people jobs are routed to, with rates) · brochures (send a PDF or photo:
read into a catalog sheet, library, or partner; `/brochures`) · catalogs from several suppliers ·
`/analyze`, `/draft` (follow-ups in the team's style, with brochure buttons) · `/top`,
`/followups [drafts]`, Monday summary with three ready drafts · `/stage`, `/plan` · `/repeats`,
`/merge` · chat mode · dashboard: Overview, Plan, Opportunities, Projects, Contacts, Partners.

## Open items

1. **Make the repositories private** (`HarunMbaabu/AUTO-EDA` and `vluti/brains-bot`). Older
   commits still contain real customer names and numbers that were used as test examples; they
   were replaced in the current code, but only a private repository hides the history.
2. **Review the likely repeats:** `/repeats` found about 23 groups (same number, or same name in
   the same area). Some are real repeats, some are different units of one development; check
   each with `/project` before `/merge`.
3. **Ken (Kiora):** his number was saved as mistyped (`11 digits`); send the right one with
   `/newsite` as a follow-up.
4. **Old stages:** many sites show "confirm"; update them from the dashboard's Plan page or with
   `/stage`, so timing and the plan are right.
5. **Mistral OCR:** if the plan is upgraded, brochures will use Mistral's document reader
   automatically (better for long PDFs); no code change needed.
6. Earlier review notes not yet done: a contact-name cell holding a phone number could reach the
   drafting AI; "Sr No" renumbering in the workbook could attach history to the wrong site; the
   dashboard's contact count includes contacts of projects gone from the workbook.
