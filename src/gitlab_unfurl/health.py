"""Tracks whether the GitLab API is reachable and the token still works."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from gitlab_unfurl.errors import AccessDeniedError, GitLabError

if TYPE_CHECKING:
    from gitlab_unfurl.gitlab import GitLabClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthSnapshot:
    # None means "not yet known" (no request has completed).
    token_valid: bool | None
    token_failure_reason: str | None
    api_up: bool | None
    token_expires_at: date | None

    def token_expires_in_days(self, today: date | None = None) -> int | None:
        if self.token_expires_at is None:
            return None
        today = today or datetime.now(UTC).date()
        return (self.token_expires_at - today).days


class HealthState:
    """Thread-safe record of the latest GitLab health signals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token_valid: bool | None = None
        self._token_failure_reason: str | None = None
        self._api_up: bool | None = None
        self._token_expires_at: date | None = None

    def record_success(self) -> None:
        """The API answered with an authenticated response (2xx, 403, or 404)."""
        with self._lock:
            if self._token_valid is False:
                log.info("GitLab token is working again")
            if self._api_up is False:
                log.info("GitLab API is reachable again")
            self._token_valid = True
            self._token_failure_reason = None
            self._api_up = True

    def record_auth_failure(self, reason: str) -> None:
        """The API rejected the token (401). The API itself is up."""
        with self._lock:
            if self._token_valid is not False:
                log.error("GitLab token rejected (%s); all unfurls will fail", reason)
            if self._api_up is False:
                log.info("GitLab API is reachable again")
            self._token_valid = False
            self._token_failure_reason = reason
            self._api_up = True

    def record_api_down(self) -> None:
        """The API was unreachable or returned a server error."""
        with self._lock:
            if self._api_up is not False:
                log.error("GitLab API is unavailable")
            self._api_up = False

    def set_token_expiry(self, expires_at: date | None) -> None:
        with self._lock:
            self._token_expires_at = expires_at

    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            return HealthSnapshot(
                token_valid=self._token_valid,
                token_failure_reason=self._token_failure_reason,
                api_up=self._api_up,
                token_expires_at=self._token_expires_at,
            )


class HealthChecker:
    """Periodically probes GitLab so health metrics stay fresh when nobody is posting links."""

    def __init__(self, client: GitLabClient, interval: float) -> None:
        self._client = client
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> None:
        try:
            info = self._client.get_token_info()
        except AccessDeniedError:
            # Older GitLab, or a token type without a /personal_access_tokens/self entry.
            try:
                self._client.get_current_user()
            except GitLabError as exc:
                log.debug("health check failed: %s", exc)
            return
        except GitLabError as exc:
            # The client has already updated HealthState and metrics.
            log.debug("health check failed: %s", exc)
            return

        self._client.health.set_token_expiry(_parse_date(info.get("expires_at")))

    def start(self) -> None:
        if self._interval <= 0:
            self.check_once()
            return
        self._thread = threading.Thread(target=self._run, name="gitlab-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception:
                log.exception("unexpected error in GitLab health check")
            if self._stop.wait(self._interval):
                return


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
