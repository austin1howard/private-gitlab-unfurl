"""Errors raised by the GitLab client."""

from __future__ import annotations


class GitLabError(Exception):
    """Base class for GitLab API failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AccessDeniedError(GitLabError):
    """The token works but cannot see this resource.

    GitLab answers 404 rather than 403 for private items the token cannot see.
    """

    @property
    def reason(self) -> str:
        return "forbidden" if self.status_code == 403 else "not_found"


class AuthenticationError(GitLabError):
    """The token was rejected (401). ``reason`` is ``expired``, ``revoked``, or ``invalid``."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message, status_code=401)
        self.reason = reason


class ApiUnavailableError(GitLabError):
    """GitLab could not answer: timeout, connection failure, 5xx, rate limit, or bad response."""

    def __init__(self, message: str, *, reason: str, status_code: int | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.reason = reason
