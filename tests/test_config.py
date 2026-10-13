import pytest

from gitlab_unfurl.config import ConfigError, Settings

BASE_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-1",
    "SLACK_APP_TOKEN": "xapp-1",
    "GITLAB_URL": "https://gitlab.example.com/",
    "GITLAB_TOKEN": "glpat-1",
}


def test_defaults():
    s = Settings.from_env(BASE_ENV)
    assert s.slack_bot_token == "xoxb-1"
    assert s.slack_app_token == "xapp-1"
    assert s.gitlab_url == "https://gitlab.example.com"
    assert s.gitlab_token == "glpat-1"
    assert s.gitlab_timeout == 10.0
    assert s.gitlab_ca_bundle is None
    assert s.health_check_interval == 300.0
    assert s.fetch_page_titles is True
    assert s.log_level == "INFO"


def test_overrides():
    s = Settings.from_env(
        {
            **BASE_ENV,
            "GITLAB_TIMEOUT": "2.5",
            "GITLAB_CA_BUNDLE": "/etc/ca.pem",
            "HEALTH_CHECK_INTERVAL": "0",
            "FETCH_PAGE_TITLES": "no",
            "LOG_LEVEL": "debug",
        }
    )
    assert s.gitlab_timeout == 2.5
    assert s.gitlab_ca_bundle == "/etc/ca.pem"
    assert s.health_check_interval == 0
    assert s.fetch_page_titles is False
    assert s.log_level == "DEBUG"


def test_reads_os_environ_by_default(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    assert Settings.from_env().gitlab_token == "glpat-1"


def test_missing_required_lists_all():
    with pytest.raises(ConfigError) as exc_info:
        Settings.from_env({"GITLAB_URL": "https://x", "SLACK_BOT_TOKEN": "  "})
    message = str(exc_info.value)
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GITLAB_TOKEN"):
        assert name in message
    assert "GITLAB_URL" not in message


@pytest.mark.parametrize("url", ["gitlab.example.com", "ftp://gitlab.example.com", "https://"])
def test_invalid_gitlab_url(url):
    with pytest.raises(ConfigError, match="GITLAB_URL"):
        Settings.from_env({**BASE_ENV, "GITLAB_URL": url})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITLAB_TIMEOUT", "abc"),
        ("GITLAB_TIMEOUT", "0"),
        ("HEALTH_CHECK_INTERVAL", "-1"),
        ("FETCH_PAGE_TITLES", "maybe"),
    ],
)
def test_invalid_values(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({**BASE_ENV, name: value})


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_truthy(value):
    assert Settings.from_env({**BASE_ENV, "FETCH_PAGE_TITLES": value}).fetch_page_titles is True
