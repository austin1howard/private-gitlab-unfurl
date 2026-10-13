"""Settings loaded from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    gitlab_url: str
    gitlab_token: str
    gitlab_timeout: float = 10.0
    gitlab_ca_bundle: str | None = None
    health_check_interval: float = 300.0
    fetch_page_titles: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        required = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GITLAB_URL", "GITLAB_TOKEN")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")

        gitlab_url = env["GITLAB_URL"].strip().rstrip("/")
        parts = urlsplit(gitlab_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(f"GITLAB_URL must be an http(s) URL, got {gitlab_url!r}")

        return cls(
            slack_bot_token=env["SLACK_BOT_TOKEN"].strip(),
            slack_app_token=env["SLACK_APP_TOKEN"].strip(),
            gitlab_url=gitlab_url,
            gitlab_token=env["GITLAB_TOKEN"].strip(),
            gitlab_timeout=_float(env, "GITLAB_TIMEOUT", 10.0, minimum=0.1),
            gitlab_ca_bundle=env.get("GITLAB_CA_BUNDLE", "").strip() or None,
            health_check_interval=_float(env, "HEALTH_CHECK_INTERVAL", 300.0, minimum=0.0),
            fetch_page_titles=_bool(env, "FETCH_PAGE_TITLES", True),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")
