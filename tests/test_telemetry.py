from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

from gitlab_unfurl import telemetry
from gitlab_unfurl.telemetry import configure_meter_provider

from .conftest import metric_points, metric_value


class TestGauges:
    def test_no_observations_until_health_known(self, metrics, metric_reader):
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.token_valid") == []
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.api_up") == []
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.token_expires_in_days") == []

    def test_healthy(self, metrics, metric_reader, health):
        health.record_success()
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.token_valid") == [({}, 1)]
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.api_up") == [({}, 1)]

    def test_token_invalid_carries_reason(self, metrics, metric_reader, health):
        health.record_auth_failure("revoked")
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.token_valid") == [
            ({"reason": "revoked"}, 0)
        ]

    def test_api_down(self, metrics, metric_reader, health):
        health.record_api_down()
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.api_up") == [({}, 0)]

    def test_token_expiry(self, metrics, metric_reader, health):
        health.set_token_expiry((datetime.now(UTC) + timedelta(days=30)).date())
        assert metric_points(metric_reader, "gitlab_unfurl.gitlab.token_expires_in_days") == [
            ({}, 30)
        ]


def test_counters(metrics, metric_reader):
    metrics.record_unfurl("commit", "success")
    metrics.record_unfurl("commit", "success")
    metrics.record_access_error("project", "forbidden")
    metrics.record_api_error("user", "timeout", None)
    metrics.record_slack_error("cannot_unfurl_url")
    assert metric_value(metric_reader, "gitlab_unfurl.unfurls", **{"link.type": "commit"}) == 2
    assert metric_points(metric_reader, "gitlab_unfurl.access_errors") == [
        ({"link.type": "project", "reason": "forbidden"}, 1)
    ]
    assert metric_points(metric_reader, "gitlab_unfurl.gitlab.api_errors") == [
        ({"endpoint": "user", "reason": "timeout"}, 1)
    ]
    assert metric_points(metric_reader, "gitlab_unfurl.slack.errors") == [
        ({"error": "cannot_unfurl_url"}, 1)
    ]


class TestConfigureMeterProvider:
    @pytest.fixture(autouse=True)
    def capture_global(self, monkeypatch):
        self.installed = []
        self.exporters = []
        real_reader = telemetry.PeriodicExportingMetricReader

        def reader(exporter):
            self.exporters.append(exporter)
            return real_reader(exporter)

        monkeypatch.setattr(telemetry.otel_metrics, "set_meter_provider", self.installed.append)
        monkeypatch.setattr(telemetry, "PeriodicExportingMetricReader", reader)

    def test_disabled_by_sdk_flag(self):
        assert configure_meter_provider({"OTEL_SDK_DISABLED": "true"}) is None
        assert self.installed == []

    def test_disabled_by_exporter_none(self):
        assert configure_meter_provider({"OTEL_METRICS_EXPORTER": "none"}) is None

    def test_console_exporter(self):
        provider = configure_meter_provider({"OTEL_METRICS_EXPORTER": "console"})
        try:
            assert isinstance(provider, MeterProvider)
            assert self.installed == [provider]
            assert isinstance(self.exporters[0], ConsoleMetricExporter)
            attrs = provider._sdk_config.resource.attributes
            assert attrs["service.name"] == "gitlab-unfurl"
            assert "service.version" in attrs
        finally:
            provider.shutdown()

    def test_otlp_is_default(self, monkeypatch):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        monkeypatch.setenv("OTEL_SERVICE_NAME", "custom-name")
        provider = configure_meter_provider({"OTEL_SERVICE_NAME": "custom-name"})
        try:
            assert isinstance(self.exporters[0], OTLPMetricExporter)
            assert provider._sdk_config.resource.attributes["service.name"] == "custom-name"
        finally:
            provider.shutdown(timeout_millis=100)

    def test_unknown_exporter(self):
        with pytest.raises(ValueError, match="unsupported"):
            configure_meter_provider({"OTEL_METRICS_EXPORTER": "prometheus"})
