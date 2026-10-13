import signal
from typing import ClassVar

import pytest

from gitlab_unfurl import __main__ as entry

ENV = {
    "SLACK_BOT_TOKEN": "xoxb-1",
    "SLACK_APP_TOKEN": "xapp-1",
    "GITLAB_URL": "https://gitlab.example.com",
    "GITLAB_TOKEN": "glpat-1",
    "HEALTH_CHECK_INTERVAL": "3600",
    "OTEL_METRICS_EXPORTER": "none",
}


@pytest.fixture
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    # Don't leave our SIGTERM handler installed for the rest of the test run.
    monkeypatch.setattr(signal, "signal", lambda *_args: None)


def test_missing_config_exits_2(monkeypatch, capsys):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    assert entry.main() == 2
    assert "missing required environment variables" in capsys.readouterr().err


class FakeHandler:
    instances: ClassVar[list["FakeHandler"]] = []

    def __init__(self, app, app_token):
        self.app = app
        self.app_token = app_token
        FakeHandler.instances.append(self)

    def start(self):
        raise KeyboardInterrupt


class FakeChecker:
    def __init__(self, client, interval):
        self.interval = interval
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_main_wires_everything_and_cleans_up(env, monkeypatch):
    checkers = []
    created = {}

    def make_checker(client, interval):
        checker = FakeChecker(client, interval)
        checkers.append(checker)
        return checker

    def fake_create_app(bot_token, unfurler, metrics):
        created["bot_token"] = bot_token
        return "app"

    FakeHandler.instances.clear()
    monkeypatch.setattr(entry, "SocketModeHandler", FakeHandler)
    monkeypatch.setattr(entry, "HealthChecker", make_checker)
    monkeypatch.setattr(entry, "create_app", fake_create_app)

    assert entry.main() == 0
    assert created["bot_token"] == "xoxb-1"
    assert FakeHandler.instances[0].app == "app"
    assert FakeHandler.instances[0].app_token == "xapp-1"
    assert checkers[0].interval == 3600
    assert checkers[0].started and checkers[0].stopped


def test_main_shuts_down_meter_provider(env, monkeypatch):
    class Provider:
        shut = False

        def shutdown(self):
            Provider.shut = True

    monkeypatch.setattr(entry, "configure_meter_provider", Provider)
    monkeypatch.setattr(entry, "SocketModeHandler", FakeHandler)
    monkeypatch.setattr(entry, "HealthChecker", FakeChecker)
    monkeypatch.setattr(entry, "create_app", lambda *a: "app")
    assert entry.main() == 0
    assert Provider.shut
