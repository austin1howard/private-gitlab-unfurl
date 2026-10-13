"""End-to-end tests against a real GitLab CE container. Run with scripts/e2e.sh."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk import WebClient
from slack_sdk.web import SlackResponse

from gitlab_unfurl.app import create_app
from gitlab_unfurl.formatting import DENIED_TEXT
from gitlab_unfurl.health import HealthChecker

from ..conftest import metric_points, metric_value
from .conftest import EXPIRED_TOKEN

pytestmark = pytest.mark.e2e


def all_text(payload: dict) -> str:
    out = []
    for block in payload["blocks"]:
        if "text" in block:
            out.append(block["text"]["text"])
        out.extend(f["text"] for f in block.get("fields", []))
        out.extend(e["text"] for e in block.get("elements", []))
    return "\n".join(out)


def unfurls(harness, link_type: str, outcome: str):
    return metric_value(
        harness.reader, "gitlab_unfurl.unfurls", outcome=outcome, **{"link.type": link_type}
    )


class TestPreviews:
    def test_merge_request(self, seed, bot):
        url = seed.url(f"{seed.project}/-/merge_requests/{seed.mr_iid}")
        text = all_text(bot.unfurler.unfurl(url))
        assert f"|Add feature module>*\n`{seed.project}!{seed.mr_iid}`" in text
        assert "*Status*\nOpen" in text
        assert "*Author*\nAdministrator" in text
        assert "`feature` → `main`" in text
        assert "This MR adds the feature module." in text
        assert "Labels: backend, e2e" in text
        assert unfurls(bot, "merge_request", "success") == 1

    def test_merge_request_subpage_and_fragment(self, seed, bot):
        url = seed.url(f"{seed.project}/-/merge_requests/{seed.mr_iid}/diffs#note_1")
        assert "Add feature module" in all_text(bot.unfurler.unfurl(url))

    def test_commit(self, seed, bot):
        url = seed.url(f"{seed.project}/-/commit/{seed.commit_sha}")
        text = all_text(bot.unfurler.unfurl(url))
        assert f"|Add feature module>*\n`{seed.commit_sha[:8]}` in `{seed.project}`" in text
        assert "*Author*\nAdministrator" in text
        assert "*Changes*\n+1 / -0" in text
        assert "Explains why the feature exists." in text
        assert unfurls(bot, "commit", "success") == 1

    def test_commit_short_sha(self, seed, bot):
        url = seed.url(f"{seed.project}/-/commit/{seed.commit_sha[:8]}")
        assert "Add feature module" in all_text(bot.unfurler.unfurl(url))

    def test_project_home(self, seed, bot):
        text = all_text(bot.unfurler.unfurl(seed.url(seed.project)))
        assert f"e2e-{seed.suffix} / app" in text
        assert "Demo app for unfurl tests" in text
        assert "*Visibility*\nPrivate" in text
        assert "*Default branch*\n`main`" in text
        assert "Topics: python, slack" in text
        assert unfurls(bot, "project", "success") == 1

    def test_group_home(self, seed, bot):
        text = all_text(bot.unfurler.unfurl(seed.url(seed.group)))
        assert f"*Path*\n`{seed.group}`" in text
        assert unfurls(bot, "group", "success") == 1

    def test_subgroup_via_groups_url(self, seed, bot):
        text = all_text(bot.unfurler.unfurl(seed.url(f"groups/{seed.subgroup}")))
        assert f"*Path*\n`{seed.subgroup}`" in text

    def test_subgroup_resolved_from_namespace_url(self, seed, bot):
        text = all_text(bot.unfurler.unfurl(seed.url(seed.subgroup)))
        assert f"*Path*\n`{seed.subgroup}`" in text
        assert unfurls(bot, "group", "success") == 1

    def test_links_in_preview_point_at_gitlab(self, seed, bot):
        url = seed.url(f"{seed.project}/-/merge_requests/{seed.mr_iid}")
        assert f"<{url}|" in all_text(bot.unfurler.unfurl(url))


class TestPageTitles:
    def test_private_page_gets_composed_title(self, seed, bot):
        # GitLab sends unauthenticated requests for private pages to sign-in, so the
        # real <title> is unavailable and the title is built from the URL.
        url = seed.url(f"{seed.project}/-/issues/{seed.issue_iid}")
        text = all_text(bot.unfurler.unfurl(url))
        assert text == f"*<{url}|Issues / {seed.issue_iid} · e2e-{seed.suffix} / app · GitLab>*"
        assert unfurls(bot, "page", "success") == 1

    def test_public_page_uses_real_title(self, seed, bot):
        url = seed.url(f"{seed.public_project}/-/issues/{seed.public_issue_iid}")
        text = all_text(bot.unfurler.unfurl(url))
        assert "Public bug report" in text
        assert "GitLab" in text

    def test_group_page(self, seed, bot):
        url = seed.url(f"groups/{seed.group}/-/milestones")
        text = all_text(bot.unfurler.unfurl(url))
        assert f"Milestones · e2e-{seed.suffix} · GitLab" in text

    def test_user_profile(self, seed, bot):
        text = all_text(bot.unfurler.unfurl(seed.url(seed.bot_username)))
        assert f"{seed.bot_name} (@{seed.bot_username}) · GitLab" in text

    def test_fetch_disabled(self, seed, make_harness):
        harness = make_harness(seed.bot_token, fetch_page_titles=False)
        url = seed.url(f"{seed.public_project}/-/issues/{seed.public_issue_iid}")
        text = all_text(harness.unfurler.unfurl(url))
        assert f"Issues / {seed.public_issue_iid} · e2e-{seed.suffix}-public / site" in text


class TestAccessDenied:
    @pytest.mark.parametrize(
        ("path", "link_type"),
        [
            ("{secret}", "namespace"),
            ("{secret}/-/merge_requests/1", "merge_request"),
            ("{secret}/-/commit/{secret_sha}", "commit"),
            ("{secret}/-/issues/1", "page"),
            ("groups/e2e-{suffix}-secret", "group"),
        ],
    )
    def test_items_the_bot_cannot_see(self, seed, bot, path, link_type):
        url = seed.url(
            path.format(
                secret=seed.secret_project, secret_sha=seed.secret_commit_sha, suffix=seed.suffix
            )
        )
        assert all_text(bot.unfurler.unfurl(url)) == DENIED_TEXT
        assert unfurls(bot, link_type, "denied") == 1
        assert (
            metric_value(
                bot.reader,
                "gitlab_unfurl.access_errors",
                reason="not_found",
                **{"link.type": link_type},
            )
            == 1
        )
        # A per-item "no" doesn't mean the token or API is broken.
        assert bot.health.snapshot().token_valid is True
        assert bot.health.snapshot().api_up is True

    def test_missing_merge_request_in_visible_project(self, seed, bot):
        url = seed.url(f"{seed.project}/-/merge_requests/99999")
        assert all_text(bot.unfurler.unfurl(url)) == DENIED_TEXT


class TestBrokenToken:
    def test_expired_token(self, seed, make_harness):
        harness = make_harness(EXPIRED_TOKEN)
        url = seed.url(f"{seed.project}/-/merge_requests/{seed.mr_iid}")
        assert all_text(harness.unfurler.unfurl(url)) == DENIED_TEXT
        assert (
            metric_value(
                harness.reader,
                "gitlab_unfurl.access_errors",
                reason="token_expired",
                **{"link.type": "merge_request"},
            )
            == 1
        )
        assert metric_points(harness.reader, "gitlab_unfurl.gitlab.token_valid") == [
            ({"reason": "expired"}, 0)
        ]

    def test_revoked_token(self, seed, make_harness):
        harness = make_harness(seed.revoked_token)
        assert all_text(harness.unfurler.unfurl(seed.url(seed.project))) == DENIED_TEXT
        assert metric_points(harness.reader, "gitlab_unfurl.gitlab.token_valid") == [
            ({"reason": "revoked"}, 0)
        ]

    def test_garbage_token(self, seed, make_harness):
        harness = make_harness("glpat-not-a-real-token")
        assert all_text(harness.unfurler.unfurl(seed.url(seed.project))) == DENIED_TEXT
        assert metric_points(harness.reader, "gitlab_unfurl.gitlab.token_valid") == [
            ({"reason": "invalid"}, 0)
        ]


class TestHealth:
    def test_health_check_reports_expiry(self, seed, bot):
        HealthChecker(bot.client, 0).check_once()
        snap = bot.health.snapshot()
        assert snap.token_valid is True
        assert snap.api_up is True
        days = snap.token_expires_in_days(datetime.now(UTC).date())
        assert days in (29, 30)
        assert metric_points(bot.reader, "gitlab_unfurl.gitlab.token_expires_in_days") == [
            ({}, days)
        ]

    def test_health_check_with_expired_token(self, make_harness):
        harness = make_harness(EXPIRED_TOKEN)
        HealthChecker(harness.client, 0).check_once()
        assert harness.health.snapshot().token_valid is False
        assert harness.health.snapshot().token_failure_reason == "expired"

    def test_api_unreachable(self, seed, make_harness):
        # Nothing listens on port 9, so this is GitLab being down from the app's view.
        harness = make_harness(seed.bot_token, base_url="http://127.0.0.1:9")
        url = f"http://127.0.0.1:9/{seed.project}/-/merge_requests/{seed.mr_iid}"
        assert all_text(harness.unfurler.unfurl(url)) == DENIED_TEXT
        assert metric_points(harness.reader, "gitlab_unfurl.gitlab.api_up") == [({}, 0)]
        assert unfurls(harness, "merge_request", "error") == 1


def test_slack_link_shared_event_end_to_end(seed, bot, monkeypatch):
    """A link_shared event goes through Bolt, hits real GitLab, and calls chat.unfurl."""
    calls: list[dict] = []
    done = threading.Event()

    def auth_test(self, **kwargs):
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

    monkeypatch.setattr(WebClient, "auth_test", auth_test)
    monkeypatch.setattr(WebClient, "chat_unfurl", chat_unfurl)

    mr_url = seed.url(f"{seed.project}/-/merge_requests/{seed.mr_iid}")
    secret_url = seed.url(seed.secret_project)
    app = create_app("xoxb-test", bot.unfurler, bot.metrics, token_verification_enabled=False)
    body = {
        "type": "event_callback",
        "team_id": "T1",
        "api_app_id": "A1",
        "event_id": "Ev1",
        "event_time": 1700000000,
        "event": {
            "type": "link_shared",
            "channel": "C1",
            "message_ts": "1700000000.000100",
            "links": [
                {"domain": "localhost", "url": mr_url},
                {"domain": "localhost", "url": secret_url},
                {"domain": "example.com", "url": "https://example.com/"},
            ],
        },
    }
    assert app.dispatch(BoltRequest(body=body, mode="socket_mode")).status == 200
    assert done.wait(30)

    unfurled = calls[0]["unfurls"]
    assert set(unfurled) == {mr_url, secret_url}
    assert "Add feature module" in all_text(unfurled[mr_url])
    assert all_text(unfurled[secret_url]) == DENIED_TEXT
    assert calls[0]["channel"] == "C1"
