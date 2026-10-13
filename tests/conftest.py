from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from gitlab_unfurl.gitlab import GitLabClient
from gitlab_unfurl.health import HealthState
from gitlab_unfurl.telemetry import Metrics
from gitlab_unfurl.unfurler import Unfurler

GITLAB_URL = "https://gitlab.example.com"
TOKEN = "glpat-test-token"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeGitLab:
    """httpx transport that serves canned responses keyed by method and raw (encoded) path."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self,
        path: str,
        json: Any = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        text: str | None = None,
        method: str = "GET",
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            if text is not None:
                return httpx.Response(
                    status,
                    content=text.encode(),
                    headers={"content-type": "text/html", **(headers or {})},
                )
            return httpx.Response(status, json=json, headers=headers)

        self.routes[(method, path)] = handler

    def add_api(self, path: str, json: Any = None, **kwargs: Any) -> None:
        self.add(f"/api/v4{path}", json, **kwargs)

    def add_handler(self, path: str, handler: Handler, method: str = "GET") -> None:
        self.routes[(method, path)] = handler

    def raise_for(self, path: str, exc: Exception) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise exc

        self.routes[("GET", path)] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        handler = self.routes.get((request.method, path))
        if handler is None:
            return httpx.Response(404, json={"message": "404 Not Found"})
        return handler(request)

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode().split("?", 1)[0] for r in self.requests]


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def meter_provider(metric_reader: InMemoryMetricReader) -> Iterator[MeterProvider]:
    provider = MeterProvider(metric_readers=[metric_reader])
    yield provider
    provider.shutdown()


@pytest.fixture
def health() -> HealthState:
    return HealthState()


@pytest.fixture
def metrics(meter_provider: MeterProvider, health: HealthState) -> Metrics:
    return Metrics(meter_provider.get_meter("test"), health)


@pytest.fixture
def fake_gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def client(
    fake_gitlab: FakeGitLab, health: HealthState, metrics: Metrics
) -> Iterator[GitLabClient]:
    c = GitLabClient(
        GITLAB_URL,
        TOKEN,
        health=health,
        metrics=metrics,
        transport=httpx.MockTransport(fake_gitlab),
    )
    yield c
    c.close()


@pytest.fixture
def unfurler(client: GitLabClient, metrics: Metrics) -> Unfurler:
    return Unfurler(client, metrics)


def metric_points(reader: InMemoryMetricReader, name: str) -> list[tuple[dict[str, Any], Any]]:
    """Return (attributes, value) for every data point of metric ``name``."""
    data = reader.get_metrics_data()
    points: list[tuple[dict[str, Any], Any]] = []
    if data is None:
        return points
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    if value is None:
                        value = point.count  # histogram
                    points.append((dict(point.attributes or {}), value))
    return points


def metric_value(reader: InMemoryMetricReader, name: str, **attrs: Any) -> Any:
    """Sum of data points of ``name`` whose attributes include ``attrs``. None if no match."""
    matches = [
        value
        for point_attrs, value in metric_points(reader, name)
        if all(point_attrs.get(k) == v for k, v in attrs.items())
    ]
    return sum(matches) if matches else None
