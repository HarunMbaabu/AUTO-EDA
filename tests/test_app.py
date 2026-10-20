from telegram.ext import CommandHandler

from brains_bot.bot.app import build_application


def test_build_application_registers_commands(settings):
    # Building does not contact Telegram; no network is needed.
    app = build_application(settings)
    commands = {
        name
        for handler in app.handlers[0]
        if isinstance(handler, CommandHandler)
        for name in handler.commands
    }
    assert commands == {
        "start", "help", "myid", "health", "projects", "search", "project", "categories", "analyze",
        "analyse", "import", "newchat", "draft", "top", "followups", "reminders", "newsite",
        "contacts", "repeats", "merge", "partner", "partners",
    }  # fmt: skip
    assert app.bot_data["settings"] is settings
    assert app.bot_data["db"] is None  # no DATABASE_URL in the test settings
    assert app.bot_data["analyzer"] is None  # no ANTHROPIC_API_KEY or CLAUDE_MODEL
    assert app.bot_data["assistant"] is None
    assert app.bot_data["drafter"] is None
    assert app.error_handlers
