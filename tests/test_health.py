import logging
import threading
from datetime import date

import httpx

from gitlab_unfurl.health import HealthChecker, HealthSnapshot, HealthState, _parse_date

from .conftest import metric_value


class TestHealthState:
    def test_initially_unknown(self):
        snap = HealthState().snapshot()
        assert snap == HealthSnapshot(None, None, None, None)

    def test_success(self):
        state = HealthState()
        state.record_success()
        snap = state.snapshot()
        assert snap.token_valid is True
        assert snap.api_up is True

    def test_auth_failure_then_recovery(self, caplog):
        caplog.set_level(logging.INFO)
        state = HealthState()
        state.record_auth_failure("expired")
        snap = state.snapshot()
        assert snap.token_valid is False
        assert snap.token_failure_reason == "expired"
        assert snap.api_up is True
        assert "token rejected (expired)" in caplog.text

        state.record_success()
        snap = state.snapshot()
        assert snap.token_valid is True
        assert snap.token_failure_reason is None
        assert "token is working again" in caplog.text

    def test_api_down_then_recovery(self, caplog):
        caplog.set_level(logging.INFO)
        state = HealthState()
        state.record_api_down()
        assert state.snapshot().api_up is False
        assert "API is unavailable" in caplog.text
        state.record_success()
        assert state.snapshot().api_up is True
        assert "API is reachable again" in caplog.text

    def test_repeated_failures_log_once(self, caplog):
        caplog.set_level(logging.INFO)
        state = HealthState()
        for _ in range(3):
            state.record_api_down()
        for _ in range(3):
            state.record_auth_failure("revoked")
        assert caplog.text.count("API is unavailable") == 1
        assert caplog.text.count("token rejected") == 1

    def test_token_expiry_days(self):
        state = HealthState()
        state.set_token_expiry(date(2030, 1, 11))
        assert state.snapshot().token_expires_in_days(today=date(2030, 1, 1)) == 10
        assert HealthState().snapshot().token_expires_in_days() is None


def test_parse_date():
    assert _parse_date("2030-01-02") == date(2030, 1, 2)
    assert _parse_date("2030-01-02T00:00:00Z") == date(2030, 1, 2)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("garbage") is None
    assert _parse_date(123) is None


class TestHealthChecker:
    def test_records_token_expiry(self, client, fake_gitlab, health):
        fake_gitlab.add_api("/personal_access_tokens/self", {"expires_at": "2030-06-01"})
        HealthChecker(client, 0).check_once()
        snap = health.snapshot()
        assert snap.token_expires_at == date(2030, 6, 1)
        assert snap.token_valid is True
        assert snap.api_up is True

    def test_token_without_expiry(self, client, fake_gitlab, health):
        health.set_token_expiry(date(2030, 1, 1))
        fake_gitlab.add_api("/personal_access_tokens/self", {"expires_at": None})
        HealthChecker(client, 0).check_once()
        assert health.snapshot().token_expires_at is None

    def test_falls_back_to_current_user(self, client, fake_gitlab, health):
        fake_gitlab.add_api("/user", {"id": 1})
        HealthChecker(client, 0).check_once()
        assert fake_gitlab.paths() == ["/api/v4/personal_access_tokens/self", "/api/v4/user"]
        assert health.snapshot().token_valid is True

    def test_fallback_failure_is_swallowed(self, client, fake_gitlab, health):
        fake_gitlab.add_api("/user", status=503, text="down")
        HealthChecker(client, 0).check_once()
        assert health.snapshot().api_up is False

    def test_expired_token(self, client, fake_gitlab, health, metric_reader):
        fake_gitlab.add_api(
            "/personal_access_tokens/self",
            {"error": "invalid_token", "error_description": "Token is expired."},
            status=401,
        )
        HealthChecker(client, 0).check_once()
        assert health.snapshot().token_valid is False
        assert (
            metric_value(metric_reader, "gitlab_unfurl.gitlab.token_valid", reason="expired") == 0
        )

    def test_api_down(self, client, fake_gitlab, health, metric_reader):
        fake_gitlab.raise_for("/api/v4/personal_access_tokens/self", httpx.ConnectError("x"))
        HealthChecker(client, 0).check_once()
        assert health.snapshot().api_up is False
        assert metric_value(metric_reader, "gitlab_unfurl.gitlab.api_up") == 0

    def test_start_with_zero_interval_checks_once_synchronously(self, client, fake_gitlab):
        fake_gitlab.add_api("/personal_access_tokens/self", {})
        checker = HealthChecker(client, 0)
        checker.start()
        assert checker._thread is None
        assert len(fake_gitlab.requests) == 1
        checker.stop()

    def test_background_thread_runs_and_stops(self, client, fake_gitlab):
        called = threading.Event()

        def handler(_request):
            called.set()
            return httpx.Response(200, json={})

        fake_gitlab.add_handler("/api/v4/personal_access_tokens/self", handler)
        checker = HealthChecker(client, 3600)
        checker.start()
        assert called.wait(5)
        checker.stop()
        assert not checker._thread.is_alive()

    def test_background_thread_survives_unexpected_errors(self, client, monkeypatch, caplog):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("bug")

        checker = HealthChecker(client, 0.01)
        monkeypatch.setattr(checker, "check_once", boom)
        checker.start()
        deadline = threading.Event()
        for _ in range(200):
            if len(calls) >= 2:
                break
            deadline.wait(0.01)
        checker.stop()
        assert len(calls) >= 2
        assert "unexpected error in GitLab health check" in caplog.text
