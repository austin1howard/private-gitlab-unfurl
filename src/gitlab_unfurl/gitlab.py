"""Minimal GitLab REST client that feeds health state and metrics."""

from __future__ import annotations

import html
import logging
import re
import ssl
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from gitlab_unfurl import __version__
from gitlab_unfurl.errors import (
    AccessDeniedError,
    ApiUnavailableError,
    AuthenticationError,
    GitLabError,
)
from gitlab_unfurl.health import HealthState
from gitlab_unfurl.telemetry import Metrics

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_PAGE_BYTES = 512 * 1024
_SIGN_IN_PATHS = ("/users/sign_in", "/users/auth")
_USER_AGENT = f"gitlab-unfurl/{__version__}"


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        health: HealthState,
        metrics: Metrics,
        timeout: float = 10.0,
        ca_bundle: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.health = health
        self._metrics = metrics
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
        )
        self._api = httpx.Client(
            base_url=f"{self.base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token, "User-Agent": _USER_AGENT},
            timeout=timeout,
            verify=verify,
            transport=transport,
        )
        # Web pages are fetched without the token: GitLab does not accept API tokens
        # for HTML routes, and a redirect to an SSO host must not receive it.
        self._web = httpx.Client(
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
            verify=verify,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._api.close()
        self._web.close()

    # -- API endpoints --------------------------------------------------------

    def get_project(self, path: str) -> dict[str, Any]:
        return self._get(f"/projects/{_enc(path)}", endpoint="project")

    def get_group(self, path: str) -> dict[str, Any]:
        return self._get(
            f"/groups/{_enc(path)}", endpoint="group", params={"with_projects": "false"}
        )

    def get_merge_request(self, project_path: str, iid: int) -> dict[str, Any]:
        return self._get(
            f"/projects/{_enc(project_path)}/merge_requests/{iid}", endpoint="merge_request"
        )

    def get_commit(self, project_path: str, sha: str) -> dict[str, Any]:
        return self._get(
            f"/projects/{_enc(project_path)}/repository/commits/{_enc(sha)}", endpoint="commit"
        )

    def find_user(self, username: str) -> dict[str, Any] | None:
        users = self._get("/users", endpoint="users", params={"username": username})
        return users[0] if isinstance(users, list) and users else None

    def get_token_info(self) -> dict[str, Any]:
        return self._get("/personal_access_tokens/self", endpoint="token_self")

    def get_current_user(self) -> dict[str, Any]:
        return self._get("/user", endpoint="user")

    # -- Web pages ------------------------------------------------------------

    def fetch_page_title(self, url: str) -> str | None:
        """Return the HTML <title> of ``url``, or None if it is unavailable or behind sign-in."""
        base_host = urlsplit(self.base_url).hostname
        try:
            with self._web.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                if resp.url.host != base_host or resp.url.path.startswith(_SIGN_IN_PATHS):
                    return None
                if "html" not in resp.headers.get("content-type", "html"):
                    return None
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if b"</title>" in body.lower() or len(body) >= _MAX_PAGE_BYTES:
                        break
        except httpx.HTTPError as exc:
            log.debug("could not fetch page title for %s: %s", url, exc)
            return None

        match = _TITLE_RE.search(body.decode("utf-8", errors="replace"))
        if not match:
            return None
        title = " ".join(html.unescape(match.group(1)).split())
        if not title or title.lower().startswith("sign in"):
            return None
        return title

    # -- Internals ------------------------------------------------------------

    def _get(self, path: str, *, endpoint: str, params: dict[str, str] | None = None) -> Any:
        start = time.monotonic()
        try:
            resp = self._api.get(path, params=params)
        except httpx.TimeoutException as exc:
            self._metrics.record_request_duration(endpoint, time.monotonic() - start, None)
            raise self._unavailable(
                endpoint, "timeout", None, f"GitLab request timed out: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            self._metrics.record_request_duration(endpoint, time.monotonic() - start, None)
            raise self._unavailable(
                endpoint, "connection_error", None, f"could not reach GitLab: {exc}"
            ) from exc

        status = resp.status_code
        self._metrics.record_request_duration(endpoint, time.monotonic() - start, status)

        if 200 <= status < 300:
            try:
                data = resp.json()
            except ValueError:
                raise self._unavailable(
                    endpoint, "invalid_response", status, "GitLab returned a non-JSON response"
                ) from None
            self.health.record_success()
            return data

        if status == 401:
            reason = _auth_failure_reason(resp)
            self.health.record_auth_failure(reason)
            self._metrics.record_api_error(endpoint, f"token_{reason}", status)
            raise AuthenticationError(f"GitLab rejected the token ({reason})", reason=reason)

        if status in (403, 404):
            # An authenticated "no" means the token and API are both fine.
            self.health.record_success()
            raise AccessDeniedError(f"GitLab returned {status} for {path}", status_code=status)

        if status == 429:
            self._metrics.record_api_error(endpoint, "rate_limited", status)
            raise ApiUnavailableError(
                "GitLab rate limit hit", reason="rate_limited", status_code=status
            )

        if status >= 500:
            raise self._unavailable(endpoint, "server_error", status, f"GitLab returned {status}")

        self._metrics.record_api_error(endpoint, "unexpected_status", status)
        raise GitLabError(f"unexpected GitLab status {status} for {path}", status_code=status)

    def _unavailable(
        self, endpoint: str, reason: str, status: int | None, message: str
    ) -> ApiUnavailableError:
        self.health.record_api_down()
        self._metrics.record_api_error(endpoint, reason, status)
        return ApiUnavailableError(message, reason=reason, status_code=status)


def _enc(value: str) -> str:
    return quote(value, safe="")


def _auth_failure_reason(resp: httpx.Response) -> str:
    """Work out why GitLab returned 401 from the body or WWW-Authenticate header."""
    text = resp.headers.get("www-authenticate", "")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        text += " " + " ".join(
            str(body.get(key, "")) for key in ("error", "error_description", "message")
        )
    text = text.lower()
    if "expired" in text:
        return "expired"
    if "revoked" in text:
        return "revoked"
    return "invalid"
