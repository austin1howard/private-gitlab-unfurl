import json
import threading
from unittest.mock import MagicMock

from slack_bolt.request import BoltRequest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from gitlab_unfurl.app import create_app, handle_link_shared

from .conftest import GITLAB_URL, metric_value

MR = {"iid": 1, "title": "Hello", "references": {"full": "g/p!1"}}
MR_URL = f"{GITLAB_URL}/g/p/-/merge_requests/1"


def event(**overrides):
    base = {
        "type": "link_shared",
        "channel": "C123",
        "message_ts": "1700000000.000100",
        "links": [{"domain": "gitlab.example.com", "url": MR_URL}],
    }
    return {**base, **overrides}


def test_unfurls_by_channel_and_ts(unfurler, fake_gitlab, metrics):
    fake_gitlab.add_api("/projects/g%2Fp/merge_requests/1", MR)
    slack = MagicMock()
    handle_link_shared(event(), slack, unfurler, metrics)
    slack.chat_unfurl.assert_called_once()
    kwargs = slack.chat_unfurl.call_args.kwargs
    assert kwargs["channel"] == "C123"
    assert kwargs["ts"] == "1700000000.000100"
    assert "unfurl_id" not in kwargs
    assert list(kwargs["unfurls"]) == [MR_URL]
    assert "Hello" in json.dumps(kwargs["unfurls"][MR_URL])


def test_composer_links_use_unfurl_id(unfurler, fake_gitlab, metrics):
    fake_gitlab.add_api("/projects/g%2Fp/merge_requests/1", MR)
    slack = MagicMock()
    handle_link_shared(
        event(channel="COMPOSER", message_ts="U1-abc", unfurl_id="UF1", source="composer"),
        slack,
        unfurler,
        metrics,
    )
    kwargs = slack.chat_unfurl.call_args.kwargs
    assert kwargs["unfurl_id"] == "UF1"
    assert kwargs["source"] == "composer"
    assert "channel" not in kwargs


def test_multiple_links_deduplicated_and_foreign_skipped(unfurler, fake_gitlab, metrics):
    fake_gitlab.add_api("/projects/g%2Fp/merge_requests/1", MR)
    links = [
        {"url": MR_URL},
        {"url": MR_URL},
        {"url": "https://other.example.com/x"},
        {"url": f"{GITLAB_URL}/secret/project"},
        {},
    ]
    slack = MagicMock()
    handle_link_shared(event(links=links), slack, unfurler, metrics)
    unfurls = slack.chat_unfurl.call_args.kwargs["unfurls"]
    assert list(unfurls) == [MR_URL, f"{GITLAB_URL}/secret/project"]
    assert fake_gitlab.paths().count("/api/v4/projects/g%2Fp/merge_requests/1") == 1


def test_no_matching_links_skips_slack_call(unfurler, metrics):
    slack = MagicMock()
    handle_link_shared(
        event(links=[{"url": "https://other.example.com/x"}]), slack, unfurler, metrics
    )
    handle_link_shared(event(links=None), slack, unfurler, metrics)
    slack.chat_unfurl.assert_not_called()


def test_slack_error_is_counted(unfurler, fake_gitlab, metrics, metric_reader):
    fake_gitlab.add_api("/projects/g%2Fp/merge_requests/1", MR)
    slack = MagicMock()
    slack.chat_unfurl.side_effect = SlackApiError(
        "failed", MagicMock(get=lambda key, default=None: {"error": "cannot_unfurl_url"}.get(key))
    )
    handle_link_shared(event(), slack, unfurler, metrics)
    assert metric_value(metric_reader, "gitlab_unfurl.slack.errors", error="cannot_unfurl_url") == 1


def test_bolt_app_routes_link_shared_events(unfurler, fake_gitlab, metrics, monkeypatch):
    fake_gitlab.add_api("/projects/g%2Fp/merge_requests/1", MR)
    calls = []
    done = threading.Event()

    def auth_test(self, **kwargs):
        # Bolt authorizes the first request with auth.test; answer it locally.
        data = {"ok": True, "team_id": "T1", "user_id": "U1", "bot_id": "B1", "user": "bot"}
        return SlackResponse(
            client=self,
            http_verb="POST",
            api_url="https://slack.com/api/auth.test",
            req_args={},
            data=data,
            headers={},
            status_code=200,
        )

    def chat_unfurl(self, **kwargs):
        calls.append(kwargs)
        done.set()

    # Bolt builds a fresh WebClient per request, so patch the class.
    monkeypatch.setattr(WebClient, "auth_test", auth_test)
    monkeypatch.setattr(WebClient, "chat_unfurl", chat_unfurl)

    app = create_app("xoxb-test", unfurler, metrics, token_verification_enabled=False)
    body = {
        "type": "event_callback",
        "team_id": "T1",
        "api_app_id": "A1",
        "event": event(),
        "event_id": "Ev1",
        "event_time": 1700000000,
    }
    response = app.dispatch(BoltRequest(body=body, mode="socket_mode"))
    assert response.status == 200
    assert done.wait(5)
    assert list(calls[0]["unfurls"]) == [MR_URL]
