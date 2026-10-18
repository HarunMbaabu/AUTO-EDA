import pytest

from brains_bot.config import ConfigError, load_env_file, load_settings, parse_user_ids
from tests.conftest import FAKE_TOKEN


def test_parse_user_ids():
    assert parse_user_ids("1, 22 ,333,") == frozenset({1, 22, 333})
    assert parse_user_ids("") == frozenset()
    assert parse_user_ids(None) == frozenset()


@pytest.mark.parametrize("raw", ["abc", "12,-5", "1.5", "@user"])
def test_parse_user_ids_rejects_non_numeric(raw):
    with pytest.raises(ConfigError):
        parse_user_ids(raw)


def test_missing_token_is_an_error():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_settings({})


def test_load_settings_plural_and_singular_allowlist():
    s = load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_ALLOWED_USER_IDS": "5,6"})
    assert s.allowed_user_ids == frozenset({5, 6})
    s = load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_ALLOWED_USER_ID": "7"})
    assert s.allowed_user_ids == frozenset({7})


def test_empty_allowlist_denies_everyone():
    s = load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
    assert not s.is_allowed(1)
    assert not s.is_allowed(None)


def test_invalid_log_level():
    with pytest.raises(ConfigError):
        load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "LOG_LEVEL": "LOUD"})


def test_token_not_in_repr():
    s = load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
    assert FAKE_TOKEN not in repr(s)


def test_load_env_file_fills_unset_and_empty_but_keeps_set_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\nLOG_LEVEL=DEBUG\nCLAUDE_MODEL=file-model\n")
    env = {"TELEGRAM_BOT_TOKEN": "", "LOG_LEVEL": "WARNING"}
    assert load_env_file([tmp_path / "missing.env", env_file], env) == env_file
    assert env["TELEGRAM_BOT_TOKEN"] == "from-file"  # empty value was filled
    assert env["LOG_LEVEL"] == "WARNING"  # non-empty value wins
    assert env["CLAUDE_MODEL"] == "file-model"  # unset value was filled


def test_load_env_file_returns_none_when_missing(tmp_path):
    assert load_env_file([tmp_path / ".env"], {}) is None
