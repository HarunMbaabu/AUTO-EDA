#!/usr/bin/env bash
# Prepare a fresh cloud/dev container: PostgreSQL, Python deps, tables, categories and (if the
# private workbook is in data/raw/) the import. Safe to run more than once.
# Usage: bash scripts/cloud_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

DB_USER=brains
DB_NAME=brains_bot
DB_PASS="${BRAINS_DB_PASSWORD:-brains_dev}"
WORKBOOK="data/raw/Physical Projects Mapping Data 6th March.xlsx"

echo "== PostgreSQL"
service postgresql start >/dev/null
# The password reaches psql as a variable (:'pw' quotes it), never through a shell string,
# so quotes, $ or backticks in a custom password are taken literally.
export BRAINS_SETUP_PW="$DB_PASS"
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'\"" | grep -q 1 \
  || echo "CREATE ROLE $DB_USER LOGIN PASSWORD :'pw';" \
    | su postgres -c 'psql -q -v pw="$BRAINS_SETUP_PW"'
unset BRAINS_SETUP_PW
for db in "$DB_NAME" "${DB_NAME}_test"; do
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$db'\"" | grep -q 1 \
    || su postgres -c "psql -qc \"CREATE DATABASE $db OWNER $DB_USER\""
done

echo "== .env"
touch .env && chmod 600 .env
if ! grep -q '^DATABASE_URL=.' .env; then  # missing or blank (e.g. copied from .env.example)
  sed -i '/^DATABASE_URL=$/d' .env
  URL_PASS=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASS")
  echo "DATABASE_URL=postgresql://$DB_USER:$URL_PASS@localhost:5432/$DB_NAME" >> .env
fi
grep -q '^CLAUDE_MODEL=' .env || echo "CLAUDE_MODEL=claude-opus-5-5" >> .env
grep -q '^LOG_LEVEL=' .env || echo "LOG_LEVEL=INFO" >> .env
for var in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USER_IDS ANTHROPIC_API_KEY; do
  if [ -n "${!var:-}" ] || grep -q "^$var=." .env; then echo "  $var: set"; else echo "  $var: MISSING"; fi
done

echo "== Python dependencies, tables, categories"
uv sync -q
uv run alembic upgrade head
uv run brains-bot-categories >/dev/null && echo "  categories synced"

if [ -f "$WORKBOOK" ]; then
  echo "== Import"
  uv run brains-bot-import "$WORKBOOK"
else
  echo "== Import skipped: put the workbook at '$WORKBOOK' and run:"
  echo "   uv run brains-bot-import \"$WORKBOOK\""
fi
echo "Done. Start the bot with: uv run brains-bot"
