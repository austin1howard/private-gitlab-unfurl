import ssl

import httpx
import pytest

from gitlab_unfurl.errors import (
    AccessDeniedError,
    ApiUnavailableError,
    AuthenticationError,
    GitLabError,
)
from gitlab_unfurl.gitlab import GitLabClient

from .conftest import GITLAB_URL, TOKEN, metric_value


def test_sends_token_and_encodes_project_path(client, fake_gitlab):
    fake_gitlab.add_api("/projects/group%2Fsub%2Fproject", {"id": 1})
    assert client.get_project("group/sub/project") == {"id": 1}
    request = fake_gitlab.requests[0]
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert request.headers["User-Agent"].startswith("gitlab-unfurl/")


def test_endpoints_hit_expected_paths(client, fake_gitlab):
    fake_gitlab.add_api("/groups/a%2Fb", {"id": 2})
    fake_gitlab.add_api("/projects/a%2Fb/merge_requests/9", {"iid": 9})
    fake_gitlab.add_api("/projects/a%2Fb/repository/commits/abc1234", {"id": "abc1234"})
    fake_gitlab.add_api("/personal_access_tokens/self", {"id": 3})
    fake_gitlab.add_api("/user", {"id": 4})
    fake_gitlab.add_api("/users", [{"username": "jane"}])

    assert client.get_group("a/b") == {"id": 2}
    assert client.get_merge_request("a/b", 9) == {"iid": 9}
    assert client.get_commit("a/b", "abc1234") == {"id": "abc1234"}
    assert client.get_token_info() == {"id": 3}
    assert client.get_current_user() == {"id": 4}
    assert client.find_user("jane") == {"username": "jane"}
    assert fake_gitlab.requests[0].url.params["with_projects"] == "false"
    assert fake_gitlab.requests[-1].url.params["username"] == "jane"


def test_find_user_returns_none_when_empty(client, fake_gitlab):
    fake_gitlab.add_api("/users", [])
    assert client.find_user("ghost") is None


def test_success_marks_healthy_and_records_duration(client, fake_gitlab, health, metric_reader):
    fake_gitlab.add_api("/projects/g%2Fp", {"id": 1})
    client.get_project("g/p")
    snap = health.snapshot()
    assert snap.token_valid is True
    assert snap.api_up is True
    assert (
        metric_value(
            metric_reader,
            "gitlab_unfurl.gitlab.request.duration",
            endpoint="project",
            **{"http.response.status_code": 200},
        )
        == 1
    )


@pytest.mark.parametrize(
    ("body", "headers", "reason"),
    [
        (
            {
                "error": "invalid_token",
                "error_description": "Token is expired. You can either do re-authorization "
                "or token refresh.",
            },
            None,
            "expired",
        ),
        ({"error": "invalid_token", "error_description": "Token was revoked."}, None, "revoked"),
        ({"message": "401 Unauthorized"}, None, "invalid"),
        (
            None,
            {
                "WWW-Authenticate": 'Bearer error="invalid_token", '
                'error_description="Token is expired"'
            },
            "expired",
        ),
    ],
)
def test_401_raises_authentication_error_with_reason(
    client, fake_gitlab, health, metric_reader, body, headers, reason
):
    if body is None:
        fake_gitlab.add_api("/projects/g%2Fp", status=401, text="nope", headers=headers)
    else:
        fake_gitlab.add_api("/projects/g%2Fp", body, status=401, headers=headers)

    with pytest.raises(AuthenticationError) as exc_info:
        client.get_project("g/p")

    assert exc_info.value.reason == reason
    assert exc_info.value.status_code == 401
    snap = health.snapshot()
    assert snap.token_valid is False
    assert snap.token_failure_reason == reason
    assert snap.api_up is True
    assert (
        metric_value(metric_reader, "gitlab_unfurl.gitlab.api_errors", reason=f"token_{reason}")
        == 1
    )


@pytest.mark.parametrize(("status", "reason"), [(403, "forbidden"), (404, "not_found")])
def test_403_and_404_are_access_denied_but_healthy(
    client, fake_gitlab, health, metric_reader, status, reason
):
    fake_gitlab.add_api("/projects/g%2Fp", {"message": "x"}, status=status)
    with pytest.raises(AccessDeniedError) as exc_info:
        client.get_project("g/p")
    assert exc_info.value.reason == reason
    assert health.snapshot().token_valid is True
    assert health.snapshot().api_up is True
    assert metric_value(metric_reader, "gitlab_unfurl.gitlab.api_errors") is None


def test_access_denied_after_token_failure_restores_token_valid(client, fake_gitlab, health):
    health.record_auth_failure("expired")
    fake_gitlab.add_api("/projects/g%2Fp", status=404, json={})
    with pytest.raises(AccessDeniedError):
        client.get_project("g/p")
    assert health.snapshot().token_valid is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_marks_api_down(client, fake_gitlab, health, metric_reader, status):
    fake_gitlab.add_api("/projects/g%2Fp", status=status, text="<html>bad gateway</html>")
    with pytest.raises(ApiUnavailableError) as exc_info:
        client.get_project("g/p")
    assert exc_info.value.reason == "server_error"
    assert exc_info.value.status_code == status
    assert health.snapshot().api_up is False
    assert (
        metric_value(
            metric_reader,
            "gitlab_unfurl.gitlab.api_errors",
            reason="server_error",
            endpoint="project",
            **{"http.response.status_code": status},
        )
        == 1
    )


def test_timeout_marks_api_down(client, fake_gitlab, health, metric_reader):
    fake_gitlab.raise_for("/api/v4/projects/g%2Fp", httpx.ReadTimeout("slow"))
    with pytest.raises(ApiUnavailableError) as exc_info:
        client.get_project("g/p")
    assert exc_info.value.reason == "timeout"
    assert health.snapshot().api_up is False
    assert metric_value(metric_reader, "gitlab_unfurl.gitlab.api_errors", reason="timeout") == 1
    # Duration is still recorded, without a status code.
    points = metric_value(metric_reader, "gitlab_unfurl.gitlab.request.duration")
    assert points == 1


def test_connection_error_marks_api_down(client, fake_gitlab, health, metric_reader):
    fake_gitlab.raise_for("/api/v4/projects/g%2Fp", httpx.ConnectError("refused"))
    with pytest.raises(ApiUnavailableError) as exc_info:
        client.get_project("g/p")
    assert exc_info.value.reason == "connection_error"
    assert health.snapshot().api_up is False
    assert (
        metric_value(metric_reader, "gitlab_unfurl.gitlab.api_errors", reason="connection_error")
        == 1
    )


def test_non_json_success_is_invalid_response(client, fake_gitlab, health):
    fake_gitlab.add_api("/projects/g%2Fp", text="<html>SSO login</html>")
    with pytest.raises(ApiUnavailableError) as exc_info:
        client.get_project("g/p")
    assert exc_info.value.reason == "invalid_response"
    assert health.snapshot().api_up is False


def test_429_is_rate_limited_and_does_not_mark_api_down(client, fake_gitlab, health, metric_reader):
    fake_gitlab.add_api("/projects/g%2Fp", {"message": "slow down"}, status=429)
    with pytest.raises(ApiUnavailableError) as exc_info:
        client.get_project("g/p")
    assert exc_info.value.reason == "rate_limited"
    assert health.snapshot().api_up is None
    assert (
        metric_value(metric_reader, "gitlab_unfurl.gitlab.api_errors", reason="rate_limited") == 1
    )


def test_unexpected_status_raises_generic_error(client, fake_gitlab, metric_reader):
    fake_gitlab.add_api("/projects/g%2Fp", {"message": "bad"}, status=400)
    with pytest.raises(GitLabError) as exc_info:
        client.get_project("g/p")
    assert type(exc_info.value) is GitLabError
    assert exc_info.value.status_code == 400
    assert (
        metric_value(metric_reader, "gitlab_unfurl.gitlab.api_errors", reason="unexpected_status")
        == 1
    )


def test_ca_bundle_builds_ssl_context(health, metrics, monkeypatch):
    seen = {}
    real_create_default_context = ssl.create_default_context

    def fake_create_default_context(cafile=None):
        seen["cafile"] = cafile
        return real_create_default_context()

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)
    c = GitLabClient(GITLAB_URL, TOKEN, health=health, metrics=metrics, ca_bundle="/etc/ca.pem")
    c.close()
    assert seen["cafile"] == "/etc/ca.pem"


def test_base_url_trailing_slash_stripped(health, metrics):
    c = GitLabClient(GITLAB_URL + "/", TOKEN, health=health, metrics=metrics)
    c.close()
    assert c.base_url == GITLAB_URL


class TestFetchPageTitle:
    URL = f"{GITLAB_URL}/g/p/-/issues/1"

    def test_returns_title(self, client, fake_gitlab):
        fake_gitlab.add(
            "/g/p/-/issues/1",
            text="<html><head><title>  Bug &amp; fix\n (#1) · Issues · G / P · GitLab </title>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
        assert client.fetch_page_title(self.URL) == "Bug & fix (#1) · Issues · G / P · GitLab"

    def test_does_not_send_token(self, client, fake_gitlab):
        fake_gitlab.add("/g/p/-/issues/1", text="<title>x</title>")
        client.fetch_page_title(self.URL)
        assert "PRIVATE-TOKEN" not in fake_gitlab.requests[0].headers

    def test_sign_in_redirect_returns_none(self, client, fake_gitlab):
        fake_gitlab.add(
            "/g/p/-/issues/1",
            status=302,
            text="",
            headers={"location": f"{GITLAB_URL}/users/sign_in"},
        )
        fake_gitlab.add("/users/sign_in", text="<title>Sign in · GitLab</title>")
        assert client.fetch_page_title(self.URL) is None

    def test_sign_in_title_returns_none(self, client, fake_gitlab):
        fake_gitlab.add("/g/p/-/issues/1", text="<title>Sign in · GitLab</title>")
        assert client.fetch_page_title(self.URL) is None

    def test_redirect_to_other_host_returns_none(self, client, fake_gitlab):
        fake_gitlab.add(
            "/g/p/-/issues/1",
            status=302,
            text="",
            headers={"location": "https://sso.example.com/login"},
        )
        fake_gitlab.add("/login", text="<title>Corporate SSO</title>")
        assert client.fetch_page_title(self.URL) is None

    def test_non_200_returns_none(self, client, fake_gitlab):
        fake_gitlab.add("/g/p/-/issues/1", status=404, text="<title>Not Found</title>")
        assert client.fetch_page_title(self.URL) is None

    def test_non_html_returns_none(self, client, fake_gitlab):
        fake_gitlab.add(
            "/g/p/-/issues/1", text="<title>x</title>", headers={"content-type": "application/json"}
        )
        assert client.fetch_page_title(self.URL) is None

    def test_missing_title_returns_none(self, client, fake_gitlab):
        fake_gitlab.add("/g/p/-/issues/1", text="<html><body>hi</body></html>")
        assert client.fetch_page_title(self.URL) is None

    def test_empty_title_returns_none(self, client, fake_gitlab):
        fake_gitlab.add("/g/p/-/issues/1", text="<title>   </title>")
        assert client.fetch_page_title(self.URL) is None

    def test_network_error_returns_none_and_does_not_touch_api_health(
        self, client, fake_gitlab, health
    ):
        fake_gitlab.raise_for("/g/p/-/issues/1", httpx.ConnectError("refused"))
        assert client.fetch_page_title(self.URL) is None
        assert health.snapshot().api_up is None
