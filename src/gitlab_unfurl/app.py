"""Slack Bolt app that answers link_shared events with chat.unfurl."""

from __future__ import annotations

import logging
from typing import Any

from slack_bolt import App
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from gitlab_unfurl.formatting import Unfurl
from gitlab_unfurl.telemetry import Metrics
from gitlab_unfurl.unfurler import Unfurler

log = logging.getLogger(__name__)


def handle_link_shared(
    event: dict[str, Any], client: WebClient, unfurler: Unfurler, metrics: Metrics
) -> None:
    unfurls: dict[str, Unfurl] = {}
    for link in event.get("links") or []:
        url = link.get("url")
        if not url or url in unfurls:
            continue
        payload = unfurler.unfurl(url)
        if payload is not None:
            # The key must be the URL exactly as Slack sent it.
            unfurls[url] = payload

    if not unfurls:
        return

    kwargs: dict[str, Any] = {"unfurls": unfurls}
    if event.get("unfurl_id") and event.get("source"):
        # Links typed in the message composer (before sending) only have an unfurl_id.
        kwargs.update(unfurl_id=event["unfurl_id"], source=event["source"])
    else:
        kwargs.update(channel=event.get("channel"), ts=event.get("message_ts"))

    try:
        client.chat_unfurl(**kwargs)
    except SlackApiError as exc:
        error = str(exc.response.get("error", "unknown")) if exc.response is not None else "unknown"
        metrics.record_slack_error(error)
        log.error("chat.unfurl failed: %s", error)


def create_app(
    bot_token: str,
    unfurler: Unfurler,
    metrics: Metrics,
    *,
    token_verification_enabled: bool = True,
    client: WebClient | None = None,
) -> App:
    app = App(
        token=bot_token,
        client=client,
        token_verification_enabled=token_verification_enabled,
    )

    @app.event("link_shared")
    def _on_link_shared(event: dict[str, Any], client: WebClient) -> None:
        handle_link_shared(event, client, unfurler, metrics)

    return app
