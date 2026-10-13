import httpx
import pytest

from gitlab_unfurl.formatting import DENIED_TEXT
from gitlab_unfurl.unfurler import Unfurler

from .conftest import GITLAB_URL, metric_value

PROJECT = {
    "id": 1,
    "name_with_namespace": "Group / Project",
    "path_with_namespace": "group/project",
    "web_url": f"{GITLAB_URL}/group/project",
    "visibility": "private",
}
GROUP = {
    "id": 2,
    "full_name": "Group / Sub",
    "full_path": "group/sub",
    "web_url": f"{GITLAB_URL}/groups/group/sub",
}
MR = {
    "iid": 42,
    "title": "Add unfurling",
    "state": "opened",
    "web_url": f"{GITLAB_URL}/group/project/-/merge_requests/42",
    "references": {"full": "group/project!42"},
    "author": {"name": "Jane"},
    "source_branch": "feature",
    "target_branch": "main",
}
COMMIT = {
    "id": "abcdef1234567890",
    "short_id": "abcdef12",
    "title": "Fix the thing",
    "message": "Fix the thing",
    "author_name": "Sam",
    "web_url": f"{GITLAB_URL}/group/project/-/commit/abcdef1234567890",
}


def all_text(payload):
    """Flatten every mrkdwn string in an unfurl payload."""
    out = []
    for block in payload["blocks"]:
        if "text" in block:
            out.append(block["text"]["text"])
        out.extend(f["text"] for f in block.get("fields", []))
        out.extend(e["text"] for e in block.get("elements", []))
    return "\n".join(out)


def is_denied(payload):
    return all_text(payload) == DENIED_TEXT


def unfurls(reader, link_type, outcome):
    return metric_value(
        reader, "gitlab_unfurl.unfurls", outcome=outcome, **{"link.type": link_type}
    )


def access_errors(reader, link_type, reason):
    return metric_value(
        reader, "gitlab_unfurl.access_errors", reason=reason, **{"link.type": link_type}
    )


def test_foreign_url_returns_none(unfurler, metric_reader):
    assert unfurler.unfurl("https://github.com/a/b") is None
    assert metric_value(metric_reader, "gitlab_unfurl.unfurls") is None


def test_merge_request(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/projects/group%2Fproject/merge_requests/42", MR)
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/project/-/merge_requests/42")
    assert "Add unfurling" in all_text(payload)
    assert unfurls(metric_reader, "merge_request", "success") == 1


def test_html_escaped_url_from_slack_is_unescaped_for_parsing(unfurler, fake_gitlab):
    fake_gitlab.add_api("/projects/group%2Fproject/merge_requests/42", MR)
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/project/-/merge_requests/42?a=1&amp;b=2")
    assert not is_denied(payload)


def test_commit(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/projects/group%2Fproject/repository/commits/abcdef1234567890", COMMIT)
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/project/-/commit/abcdef1234567890")
    assert "Fix the thing" in all_text(payload)
    assert unfurls(metric_reader, "commit", "success") == 1


def test_project_home(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/projects/group%2Fproject", PROJECT)
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/project")
    assert "Group / Project" in all_text(payload)
    assert unfurls(metric_reader, "project", "success") == 1


def test_group_home_via_namespace_fallback(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/groups/group%2Fsub", GROUP)
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/sub")
    assert "Group / Sub" in all_text(payload)
    assert fake_gitlab.paths() == ["/api/v4/projects/group%2Fsub", "/api/v4/groups/group%2Fsub"]
    assert unfurls(metric_reader, "group", "success") == 1


def test_explicit_group_url_skips_project_lookup(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/groups/group%2Fsub", GROUP)
    payload = unfurler.unfurl(f"{GITLAB_URL}/groups/group/sub")
    assert "Group / Sub" in all_text(payload)
    assert fake_gitlab.paths() == ["/api/v4/groups/group%2Fsub"]
    assert unfurls(metric_reader, "group", "success") == 1


def test_user_profile_falls_back_to_title(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/users", [{"username": "jane", "name": "Jane Doe"}])
    payload = unfurler.unfurl(f"{GITLAB_URL}/jane")
    assert "Jane Doe (@jane) · GitLab" in all_text(payload)
    assert unfurls(metric_reader, "page", "success") == 1


def test_unknown_single_segment_namespace_is_denied(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/users", [])
    payload = unfurler.unfurl(f"{GITLAB_URL}/secret-group")
    assert is_denied(payload)
    assert unfurls(metric_reader, "namespace", "denied") == 1
    assert access_errors(metric_reader, "namespace", "not_found") == 1


def test_private_nested_namespace_is_denied_without_user_lookup(
    unfurler, fake_gitlab, metric_reader
):
    payload = unfurler.unfurl(f"{GITLAB_URL}/secret/project")
    assert is_denied(payload)
    assert "/api/v4/users" not in fake_gitlab.paths()
    assert access_errors(metric_reader, "namespace", "not_found") == 1


@pytest.mark.parametrize(
    ("url", "api_path", "link_type"),
    [
        (
            f"{GITLAB_URL}/group/project/-/merge_requests/42",
            "/projects/group%2Fproject/merge_requests/42",
            "merge_request",
        ),
        (
            f"{GITLAB_URL}/group/project/-/commit/abcdef12",
            "/projects/group%2Fproject/repository/commits/abcdef12",
            "commit",
        ),
        (f"{GITLAB_URL}/groups/group", "/groups/group", "group"),
    ],
)
@pytest.mark.parametrize(("status", "reason"), [(403, "forbidden"), (404, "not_found")])
def test_access_denied_shows_message_and_counts(
    unfurler, fake_gitlab, metric_reader, url, api_path, link_type, status, reason
):
    fake_gitlab.add_api(api_path, {"message": "nope"}, status=status)
    payload = unfurler.unfurl(url)
    assert is_denied(payload)
    assert unfurls(metric_reader, link_type, "denied") == 1
    assert access_errors(metric_reader, link_type, reason) == 1


def test_expired_token_shows_message_and_counts(unfurler, fake_gitlab, metric_reader, health):
    fake_gitlab.add_api(
        "/projects/group%2Fproject/merge_requests/42",
        {"error": "invalid_token", "error_description": "Token is expired."},
        status=401,
    )
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/project/-/merge_requests/42")
    assert is_denied(payload)
    assert unfurls(metric_reader, "merge_request", "denied") == 1
    assert access_errors(metric_reader, "merge_request", "token_expired") == 1
    assert health.snapshot().token_valid is False


def test_api_down_shows_message_and_counts_error(unfurler, fake_gitlab, metric_reader, health):
    fake_gitlab.add_api("/projects/group%2Fproject", status=502, text="bad gateway")
    payload = unfurler.unfurl(f"{GITLAB_URL}/group/project")
    assert is_denied(payload)
    assert unfurls(metric_reader, "namespace", "error") == 1
    assert metric_value(metric_reader, "gitlab_unfurl.access_errors") is None
    assert health.snapshot().api_up is False


def test_unexpected_gitlab_status_counts_error(unfurler, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/projects/g%2Fp/merge_requests/1", {"message": "?"}, status=400)
    assert is_denied(unfurler.unfurl(f"{GITLAB_URL}/g/p/-/merge_requests/1"))
    assert unfurls(metric_reader, "merge_request", "error") == 1


def test_unexpected_exception_counts_error(client, metrics, metric_reader, monkeypatch):
    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(client, "get_merge_request", boom)
    payload = Unfurler(client, metrics).unfurl(f"{GITLAB_URL}/g/p/-/merge_requests/1")
    assert is_denied(payload)
    assert unfurls(metric_reader, "merge_request", "error") == 1


class TestPages:
    URL = f"{GITLAB_URL}/group/project/-/issues/5"

    def test_uses_html_title_when_available(self, unfurler, fake_gitlab, metric_reader):
        fake_gitlab.add_api("/projects/group%2Fproject", PROJECT)
        fake_gitlab.add("/group/project/-/issues/5", text="<title>Bug (#5) · Issues</title>")
        payload = unfurler.unfurl(self.URL)
        assert all_text(payload) == f"*<{self.URL}|Bug (#5) · Issues>*"
        assert unfurls(metric_reader, "page", "success") == 1

    def test_falls_back_to_composed_title(self, unfurler, fake_gitlab):
        fake_gitlab.add_api("/projects/group%2Fproject", PROJECT)
        fake_gitlab.add(
            "/group/project/-/issues/5",
            status=302,
            text="",
            headers={"location": f"{GITLAB_URL}/users/sign_in"},
        )
        payload = unfurler.unfurl(self.URL)
        assert "Issues / 5 · Group / Project · GitLab" in all_text(payload)

    def test_fetch_disabled_uses_composed_title(self, client, metrics, fake_gitlab):
        fake_gitlab.add_api("/projects/group%2Fproject", PROJECT)
        fake_gitlab.add("/group/project/-/issues/5", text="<title>Real</title>")
        payload = Unfurler(client, metrics, fetch_page_titles=False).unfurl(self.URL)
        assert "Issues / 5 · Group / Project · GitLab" in all_text(payload)
        assert "/group/project/-/issues/5" not in fake_gitlab.paths()

    def test_page_in_private_project_is_denied(self, unfurler, fake_gitlab, metric_reader):
        payload = unfurler.unfurl(self.URL)
        assert is_denied(payload)
        # The HTML page is never fetched when access is denied.
        assert "/group/project/-/issues/5" not in fake_gitlab.paths()
        assert unfurls(metric_reader, "page", "denied") == 1
        assert access_errors(metric_reader, "page", "not_found") == 1

    def test_group_page_uses_group_name(self, unfurler, fake_gitlab):
        fake_gitlab.add_api("/groups/group%2Fsub", GROUP)
        payload = unfurler.unfurl(f"{GITLAB_URL}/groups/group/sub/-/epics/3")
        assert "Epics / 3 · Group / Sub · GitLab" in all_text(payload)
        assert "/api/v4/projects/group%2Fsub" not in fake_gitlab.paths()

    def test_dash_route_on_group_without_groups_prefix(self, unfurler, fake_gitlab):
        fake_gitlab.add_api("/groups/group%2Fsub", GROUP)
        payload = unfurler.unfurl(f"{GITLAB_URL}/group/sub/-/milestones")
        assert "Milestones · Group / Sub · GitLab" in all_text(payload)

    def test_page_without_namespace(self, unfurler, fake_gitlab):
        payload = unfurler.unfurl(f"{GITLAB_URL}/dashboard/merge_requests")
        assert "Dashboard / merge requests · GitLab" in all_text(payload)
        assert not any(p.startswith("/api/") for p in fake_gitlab.paths())

    def test_root_page(self, unfurler, fake_gitlab):
        fake_gitlab.raise_for("/", httpx.ConnectError("x"))
        payload = unfurler.unfurl(f"{GITLAB_URL}/")
        assert all_text(payload) == f"*<{GITLAB_URL}/|GitLab>*"
