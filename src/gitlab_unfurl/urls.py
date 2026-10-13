"""Classify GitLab URLs into the kinds of things we know how to unfurl."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit

_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")

# Top-level paths that are GitLab routes rather than user, group, or project namespaces.
_RESERVED_TOP_LEVEL = frozenset(
    {
        "-",
        ".well-known",
        "admin",
        "api",
        "assets",
        "dashboard",
        "explore",
        "help",
        "import",
        "jwt",
        "oauth",
        "profile",
        "projects",
        "public",
        "search",
        "snippets",
        "uploads",
        "users",
    }
)


class LinkKind(StrEnum):
    MERGE_REQUEST = "merge_request"
    COMMIT = "commit"
    # A namespace home page. Could be a project or a group; resolved against the API.
    NAMESPACE = "namespace"
    # An explicit /groups/<path> URL.
    GROUP = "group"
    # Anything else. `namespace_path` is set when the page lives under a project or group.
    PAGE = "page"


@dataclass(frozen=True)
class ParsedLink:
    url: str
    kind: LinkKind
    namespace_path: str | None = None
    resource: tuple[str, ...] = ()
    group_only: bool = False
    mr_iid: int | None = None
    commit_sha: str | None = None


def parse_link(url: str, base_url: str) -> ParsedLink | None:
    """Parse ``url`` relative to the GitLab instance at ``base_url``.

    Returns ``None`` when the URL does not belong to that instance.
    """
    try:
        parts = urlsplit(url.strip())
        base = urlsplit(base_url)
        port, base_port = parts.port, base.port
    except ValueError:
        return None

    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.hostname.lower() != (base.hostname or "").lower():
        return None
    if base_port is not None and port != base_port:
        return None

    path = unquote(parts.path)
    base_path = base.path.rstrip("/")
    if base_path:
        if path != base_path and not path.startswith(base_path + "/"):
            return None
        path = path[len(base_path) :]

    segments = [s for s in path.split("/") if s]
    if not segments:
        return ParsedLink(url, LinkKind.PAGE)

    if "-" in segments:
        return _parse_dash_route(url, segments)

    if segments[0] == "groups":
        if len(segments) == 1:
            return ParsedLink(url, LinkKind.PAGE, resource=tuple(segments))
        return ParsedLink(url, LinkKind.GROUP, namespace_path="/".join(segments[1:]))

    if segments[0] in _RESERVED_TOP_LEVEL:
        return ParsedLink(url, LinkKind.PAGE, resource=tuple(segments))

    legacy = _parse_legacy_route(url, segments)
    if legacy is not None:
        return legacy

    if segments[-1].endswith(".git"):
        segments[-1] = segments[-1].removesuffix(".git")
    return ParsedLink(url, LinkKind.NAMESPACE, namespace_path="/".join(segments))


def _parse_dash_route(url: str, segments: list[str]) -> ParsedLink:
    """Parse modern routes like ``group/project/-/merge_requests/1``."""
    idx = segments.index("-")
    namespace, rest = segments[:idx], segments[idx + 1 :]
    group_only = False
    if namespace and namespace[0] == "groups":
        namespace = namespace[1:]
        group_only = True
    if not namespace:
        return ParsedLink(url, LinkKind.PAGE, resource=tuple(rest))

    namespace_path = "/".join(namespace)
    if not group_only and len(rest) >= 2:
        if rest[0] == "merge_requests" and rest[1].isdigit():
            return ParsedLink(
                url,
                LinkKind.MERGE_REQUEST,
                namespace_path=namespace_path,
                mr_iid=int(rest[1]),
            )
        if rest[0] == "commit" and _SHA_RE.fullmatch(rest[1]):
            return ParsedLink(
                url,
                LinkKind.COMMIT,
                namespace_path=namespace_path,
                commit_sha=rest[1].lower(),
            )
    return ParsedLink(
        url,
        LinkKind.PAGE,
        namespace_path=namespace_path,
        resource=tuple(rest),
        group_only=group_only,
    )


def _parse_legacy_route(url: str, segments: list[str]) -> ParsedLink | None:
    """Parse pre-``/-/`` routes like ``group/project/merge_requests/1``.

    A project path always has at least two segments, so the marker must come
    at index 2 or later.
    """
    for idx in range(2, len(segments) - 1):
        marker, value = segments[idx], segments[idx + 1]
        namespace_path = "/".join(segments[:idx])
        if marker == "merge_requests" and value.isdigit():
            return ParsedLink(
                url, LinkKind.MERGE_REQUEST, namespace_path=namespace_path, mr_iid=int(value)
            )
        if marker == "commit" and _SHA_RE.fullmatch(value):
            return ParsedLink(
                url, LinkKind.COMMIT, namespace_path=namespace_path, commit_sha=value.lower()
            )
    return None
