from types import SimpleNamespace

from brains_bot.services import anthropic_check


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="pong")])


def test_missing_model_exits_without_calling_api(monkeypatch, capsys):
    monkeypatch.setattr(anthropic_check, "load_dotenv", lambda: None)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert anthropic_check.main(client=object()) == 2
    assert "CLAUDE_MODEL" in capsys.readouterr().out


def test_success_with_fake_client(monkeypatch, capsys):
    monkeypatch.setattr(anthropic_check, "load_dotenv", lambda: None)
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")
    client = SimpleNamespace(messages=FakeMessages())
    assert anthropic_check.main(client=client) == 0
    assert client.messages.kwargs["model"] == "test-model"
    assert "OK" in capsys.readouterr().out
