"""Entry point: ``gitlab-unfurl`` or ``python -m gitlab_unfurl``."""

from __future__ import annotations

import logging
import signal
import sys

from opentelemetry import metrics as otel_metrics
from slack_bolt.adapter.socket_mode import SocketModeHandler

from gitlab_unfurl import __version__
from gitlab_unfurl.app import create_app
from gitlab_unfurl.config import ConfigError, Settings
from gitlab_unfurl.gitlab import GitLabClient
from gitlab_unfurl.health import HealthChecker, HealthState
from gitlab_unfurl.telemetry import METER_NAME, Metrics, configure_meter_provider
from gitlab_unfurl.unfurler import Unfurler

log = logging.getLogger("gitlab_unfurl")


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"gitlab-unfurl: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Let SIGTERM (docker stop, Kubernetes) run the cleanup below.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    provider = configure_meter_provider()
    health = HealthState()
    metrics = Metrics(otel_metrics.get_meter(METER_NAME, __version__), health)
    client = GitLabClient(
        settings.gitlab_url,
        settings.gitlab_token,
        health=health,
        metrics=metrics,
        timeout=settings.gitlab_timeout,
        ca_bundle=settings.gitlab_ca_bundle,
    )
    checker = HealthChecker(client, settings.health_check_interval)
    unfurler = Unfurler(client, metrics, fetch_page_titles=settings.fetch_page_titles)
    app = create_app(settings.slack_bot_token, unfurler, metrics)

    log.info("starting gitlab-unfurl %s for %s", __version__, settings.gitlab_url)
    checker.start()
    try:
        SocketModeHandler(app, settings.slack_app_token).start()
    except KeyboardInterrupt:
        pass
    finally:
        checker.stop()
        client.close()
        if provider is not None:
            provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
