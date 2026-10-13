"""OpenTelemetry metrics setup and instruments."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

from gitlab_unfurl import __version__
from gitlab_unfurl.health import HealthState

log = logging.getLogger(__name__)

METER_NAME = "gitlab_unfurl"
DEFAULT_SERVICE_NAME = "gitlab-unfurl"


def configure_meter_provider(env: Mapping[str, str] | None = None) -> MeterProvider | None:
    """Install a global MeterProvider based on standard OTEL_* environment variables.

    ``OTEL_METRICS_EXPORTER`` picks the exporter: ``otlp`` (default), ``console``, or ``none``.
    The OTLP exporter reads ``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_HEADERS``, etc.
    Returns ``None`` when metrics are disabled.
    """
    env = os.environ if env is None else env
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        log.info("OpenTelemetry disabled via OTEL_SDK_DISABLED")
        return None

    exporter_name = env.get("OTEL_METRICS_EXPORTER", "otlp").strip().lower() or "otlp"
    exporter: MetricExporter
    if exporter_name == "none":
        log.info("OpenTelemetry metrics export disabled via OTEL_METRICS_EXPORTER=none")
        return None
    if exporter_name == "console":
        exporter = ConsoleMetricExporter()
    elif exporter_name == "otlp":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        exporter = OTLPMetricExporter()
    else:
        raise ValueError(f"unsupported OTEL_METRICS_EXPORTER {exporter_name!r}")

    attributes: dict[str, str] = {"service.version": __version__}
    if not env.get("OTEL_SERVICE_NAME"):
        attributes[SERVICE_NAME] = DEFAULT_SERVICE_NAME
    provider = MeterProvider(
        resource=Resource.create(attributes),
        metric_readers=[PeriodicExportingMetricReader(exporter)],
    )
    otel_metrics.set_meter_provider(provider)
    return provider


class Metrics:
    """All instruments the app records. Construct with any Meter (tests use an in-memory one)."""

    def __init__(self, meter: Meter, health: HealthState) -> None:
        self._health = health
        self._unfurls = meter.create_counter(
            "gitlab_unfurl.unfurls",
            unit="{link}",
            description="Links processed, by link type and outcome (success, denied, error).",
        )
        self._access_errors = meter.create_counter(
            "gitlab_unfurl.access_errors",
            unit="{link}",
            description="Links that could not be shown because GitLab denied access.",
        )
        self._api_errors = meter.create_counter(
            "gitlab_unfurl.gitlab.api_errors",
            unit="{request}",
            description="GitLab API requests that failed for reasons other than per-item access.",
        )
        self._request_duration = meter.create_histogram(
            "gitlab_unfurl.gitlab.request.duration",
            unit="s",
            description="GitLab API request latency.",
        )
        self._slack_errors = meter.create_counter(
            "gitlab_unfurl.slack.errors",
            unit="{request}",
            description="Failed Slack chat.unfurl calls.",
        )
        meter.create_observable_gauge(
            "gitlab_unfurl.gitlab.token_valid",
            callbacks=[self._observe_token_valid],
            description="1 if the GitLab token is accepted, 0 if expired, revoked, or invalid.",
        )
        meter.create_observable_gauge(
            "gitlab_unfurl.gitlab.api_up",
            callbacks=[self._observe_api_up],
            description="1 if the GitLab API is reachable, 0 if it is down.",
        )
        meter.create_observable_gauge(
            "gitlab_unfurl.gitlab.token_expires_in_days",
            unit="d",
            callbacks=[self._observe_token_expiry],
            description="Days until the GitLab token expires, when GitLab reports an expiry.",
        )

    def record_unfurl(self, link_type: str, outcome: str) -> None:
        self._unfurls.add(1, {"link.type": link_type, "outcome": outcome})

    def record_access_error(self, link_type: str, reason: str) -> None:
        self._access_errors.add(1, {"link.type": link_type, "reason": reason})

    def record_api_error(self, endpoint: str, reason: str, status_code: int | None) -> None:
        attrs: dict[str, str | int] = {"endpoint": endpoint, "reason": reason}
        if status_code is not None:
            attrs["http.response.status_code"] = status_code
        self._api_errors.add(1, attrs)

    def record_request_duration(
        self, endpoint: str, seconds: float, status_code: int | None
    ) -> None:
        attrs: dict[str, str | int] = {"endpoint": endpoint}
        if status_code is not None:
            attrs["http.response.status_code"] = status_code
        self._request_duration.record(seconds, attrs)

    def record_slack_error(self, error: str) -> None:
        self._slack_errors.add(1, {"error": error})

    def _observe_token_valid(self, _options: CallbackOptions) -> Iterable[Observation]:
        snap = self._health.snapshot()
        if snap.token_valid is None:
            return []
        if snap.token_valid:
            return [Observation(1)]
        return [Observation(0, {"reason": snap.token_failure_reason or "invalid"})]

    def _observe_api_up(self, _options: CallbackOptions) -> Iterable[Observation]:
        snap = self._health.snapshot()
        if snap.api_up is None:
            return []
        return [Observation(1 if snap.api_up else 0)]

    def _observe_token_expiry(self, _options: CallbackOptions) -> Iterable[Observation]:
        days = self._health.snapshot().token_expires_in_days()
        return [] if days is None else [Observation(days)]
