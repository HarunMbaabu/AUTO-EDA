import logging

from brains_bot.bot import handlers
from tests.conftest import ALLOWED_ID, STRANGER_ID, make_update, reply_of


async def test_start_authorized(context):
    update = make_update(ALLOWED_ID)
    await handlers.start(update, context)
    assert "You are authorized" in reply_of(update)


async def test_start_unauthorized(context):
    update = make_update(STRANGER_ID)
    await handlers.start(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_help_hides_private_commands_from_strangers(context):
    update = make_update(STRANGER_ID)
    await handlers.help_command(update, context)
    text = reply_of(update)
    assert "/myid" in text
    assert "/health" not in text


async def test_help_shows_private_commands_to_authorized(context):
    update = make_update(ALLOWED_ID)
    await handlers.help_command(update, context)
    assert "/health" in reply_of(update)


async def test_myid_works_for_anyone(context):
    update = make_update(STRANGER_ID)
    await handlers.myid(update, context)
    assert str(STRANGER_ID) in reply_of(update)


async def test_myid_without_user(context):
    update = make_update(None)
    await handlers.myid(update, context)
    assert "couldn't" in reply_of(update)


async def test_health_authorized(context):
    update = make_update(ALLOWED_ID)
    await handlers.health(update, context)
    text = reply_of(update)
    assert text.startswith("OK")
    assert "0h 1m" in text


async def test_health_denied_for_strangers(context):
    update = make_update(STRANGER_ID)
    await handlers.health(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_health_denied_without_user(context):
    update = make_update(None)
    await handlers.health(update, context)
    assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_text_and_unknown_command_denied_for_strangers(context):
    for handler in (handlers.text_message, handlers.unknown_command):
        update = make_update(STRANGER_ID)
        await handler(update, context)
        assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_text_message_content_is_not_logged(context, caplog):
    update = make_update(ALLOWED_ID)
    update.effective_message.text = "Call Mr Mwangi on 0712345678"
    with caplog.at_level(logging.INFO):
        await handlers.text_message(update, context)
    assert "Mwangi" not in caplog.text
    assert "0712345678" not in caplog.text


async def test_project_commands_denied_for_strangers(context):
    from brains_bot.bot import project_handlers

    for handler in (project_handlers.projects, project_handlers.search, project_handlers.project):
        update = make_update(STRANGER_ID)
        await handler(update, context)
        assert reply_of(update) == handlers.NOT_AUTHORIZED


async def test_project_commands_without_database(context):
    from brains_bot.bot import project_handlers

    context.bot_data["db"] = None
    update = make_update(ALLOWED_ID)
    context.args = ["1"]
    await project_handlers.project(update, context)
    assert reply_of(update) == project_handlers.NO_DATABASE


async def test_project_usage_messages(context):
    from brains_bot.bot import project_handlers

    context.bot_data["db"] = object()  # never reached: input is rejected first
    for handler, args in ((project_handlers.project, ["abc"]), (project_handlers.search, ["x"])):
        update = make_update(ALLOWED_ID)
        context.args = args
        await handler(update, context)
        assert reply_of(update).startswith("Usage:")
