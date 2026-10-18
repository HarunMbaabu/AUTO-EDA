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
    assert commands == {"start", "help", "myid", "health"}
    assert app.bot_data["settings"] is settings
    assert app.error_handlers
