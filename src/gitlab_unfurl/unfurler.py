"""Turn a GitLab URL into a Slack unfurl payload."""

from __future__ import annotations

import html
import logging

from gitlab_unfurl import formatting
from gitlab_unfurl.errors import (
    AccessDeniedError,
    AuthenticationError,
    GitLabError,
)
from gitlab_unfurl.formatting import Unfurl
from gitlab_unfurl.gitlab import GitLabClient
from gitlab_unfurl.telemetry import Metrics
from gitlab_unfurl.urls import LinkKind, ParsedLink, parse_link

log = logging.getLogger(__name__)


class Unfurler:
    def __init__(self, client: GitLabClient, metrics: Metrics, *, fetch_page_titles: bool = True):
        self._client = client
        self._metrics = metrics
        self._fetch_page_titles = fetch_page_titles

    def unfurl(self, url: str) -> Unfurl | None:
        """Build an unfurl for ``url``. Returns None if the URL is not on our GitLab instance."""
        parsed = parse_link(html.unescape(url), self._client.base_url)
        if parsed is None:
            return None

        # Refined once we know whether a namespace is a project or a group.
        link_type = _initial_link_type(parsed)
        try:
            link_type, payload = self._build(parsed)
        except AccessDeniedError as exc:
            self._metrics.record_access_error(link_type, exc.reason)
            self._metrics.record_unfurl(link_type, "denied")
            log.info("access denied for %s (%s)", url, exc.reason)
            return formatting.denied_unfurl()
        except AuthenticationError as exc:
            self._metrics.record_access_error(link_type, f"token_{exc.reason}")
            self._metrics.record_unfurl(link_type, "denied")
            log.warning("GitLab token %s; cannot unfurl %s", exc.reason, url)
            return formatting.denied_unfurl()
        except GitLabError as exc:
            # API down, rate limited, or an unexpected status. Already counted by the client.
            self._metrics.record_unfurl(link_type, "error")
            log.warning("GitLab error while unfurling %s: %s", url, exc)
            return formatting.denied_unfurl()
        except Exception:
            self._metrics.record_unfurl(link_type, "error")
            log.exception("unexpected error unfurling %s", url)
            return formatting.denied_unfurl()

        self._metrics.record_unfurl(link_type, "success")
        return payload

    def _build(self, parsed: ParsedLink) -> tuple[str, Unfurl]:
        url = parsed.url
        match parsed.kind:
            case LinkKind.MERGE_REQUEST:
                assert parsed.namespace_path and parsed.mr_iid is not None
                mr = self._client.get_merge_request(parsed.namespace_path, parsed.mr_iid)
                return "merge_request", formatting.merge_request_unfurl(mr, url)
            case LinkKind.COMMIT:
                assert parsed.namespace_path and parsed.commit_sha
                commit = self._client.get_commit(parsed.namespace_path, parsed.commit_sha)
                return "commit", formatting.commit_unfurl(commit, parsed.namespace_path, url)
            case LinkKind.GROUP:
                assert parsed.namespace_path
                group = self._client.get_group(parsed.namespace_path)
                return "group", formatting.group_unfurl(group, url)
            case LinkKind.NAMESPACE:
                assert parsed.namespace_path
                try:
                    kind, data = self._resolve_namespace(parsed.namespace_path, group_only=False)
                except AccessDeniedError:
                    # A top-level path that is not a visible project or group may be a user profile.
                    user_page = self._user_page(parsed)
                    if user_page is None:
                        raise
                    return "page", user_page
                if kind == "project":
                    return "project", formatting.project_unfurl(data, url)
                return "group", formatting.group_unfurl(data, url)
            case _:
                return "page", self._page(parsed)

    def _resolve_namespace(self, path: str, *, group_only: bool) -> tuple[str, dict]:
        """Look ``path`` up as a project, then as a group. Raises AccessDeniedError if neither."""
        if not group_only:
            try:
                return "project", self._client.get_project(path)
            except AccessDeniedError:
                pass
        return "group", self._client.get_group(path)

    def _user_page(self, parsed: ParsedLink) -> Unfurl | None:
        assert parsed.namespace_path
        if "/" in parsed.namespace_path:
            return None
        user = self._client.find_user(parsed.namespace_path)
        if user is None:
            return None
        name = user.get("name") or parsed.namespace_path
        title = f"{name} (@{user.get('username') or parsed.namespace_path}) · GitLab"
        return formatting.title_unfurl(parsed.url, title)

    def _page(self, parsed: ParsedLink) -> Unfurl:
        """Title-only unfurl for any other GitLab page."""
        context_name = None
        if parsed.namespace_path:
            # Confirms the service account can see the containing project or group.
            kind, data = self._resolve_namespace(
                parsed.namespace_path, group_only=parsed.group_only
            )
            if kind == "project":
                context_name = data.get("name_with_namespace") or data.get("path_with_namespace")
            else:
                context_name = data.get("full_name") or data.get("full_path")

        title = self._client.fetch_page_title(parsed.url) if self._fetch_page_titles else None
        if not title:
            title = _fallback_title(parsed.resource, context_name)
        return formatting.title_unfurl(parsed.url, title)


def _initial_link_type(parsed: ParsedLink) -> str:
    """The `link.type` metric attribute to use if the lookup fails.

    "namespace" is a home-page link that never resolved to a project or a group.
    """
    return {
        LinkKind.MERGE_REQUEST: "merge_request",
        LinkKind.COMMIT: "commit",
        LinkKind.GROUP: "group",
        LinkKind.NAMESPACE: "namespace",
    }.get(parsed.kind, "page")


def _fallback_title(resource: tuple[str, ...], context_name: str | None) -> str:
    """Approximate GitLab's own "<Page> · <Project> · GitLab" title from the URL path."""
    parts = []
    if resource:
        parts.append(" / ".join(seg.replace("_", " ") for seg in resource).capitalize())
    if context_name:
        parts.append(context_name)
    parts.append("GitLab")
    return " · ".join(parts)
