"""Fixtures that seed a real GitLab (see compose.e2e.yml) for the e2e tests.

Each test session creates its own groups, projects, and bot user under a random suffix,
so the suite can be re-run against the same container without cleanup.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from gitlab_unfurl.gitlab import GitLabClient
from gitlab_unfurl.health import HealthState
from gitlab_unfurl.telemetry import Metrics
from gitlab_unfurl.unfurler import Unfurler

GITLAB_URL = os.environ.get("E2E_GITLAB_URL", "http://localhost:8929").rstrip("/")
CONTAINER = os.environ.get("E2E_GITLAB_CONTAINER", "gitlab-unfurl-e2e")
ADMIN_TOKEN = "glpat-e2eadmin00000000000001"
EXPIRED_TOKEN = "glpat-e2eexpired000000000001"
SEED_TOKENS_SCRIPT = Path(__file__).with_name("seed_tokens.rb")

REPORTER = 20


@dataclass(frozen=True)
class Seed:
    suffix: str
    group: str
    subgroup: str
    project: str
    mr_iid: int
    commit_sha: str
    issue_iid: int
    public_project: str
    public_issue_iid: int
    secret_project: str
    secret_commit_sha: str
    bot_username: str
    bot_name: str
    bot_token: str
    revoked_token: str

    def url(self, path: str) -> str:
        return f"{GITLAB_URL}/{path.lstrip('/')}"


@dataclass
class Harness:
    unfurler: Unfurler
    client: GitLabClient
    health: HealthState
    reader: InMemoryMetricReader
    metrics: Metrics


def _enc(path: str) -> str:
    return quote(path, safe="")


def _wait_for_gitlab(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{GITLAB_URL}/users/sign_in", timeout=5).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(2)
    pytest.fail(
        f"GitLab is not reachable at {GITLAB_URL} ({last_error}). "
        "Start it with scripts/e2e.sh or `docker compose -f compose.e2e.yml up -d --wait`."
    )


def _token_works(token: str) -> bool:
    return httpx.get(
        f"{GITLAB_URL}/api/v4/user", headers={"PRIVATE-TOKEN": token}, timeout=10
    ).is_success


def _ensure_root_tokens() -> None:
    """Create the admin and expired tokens with gitlab-rails, unless a previous run already did."""
    expired = httpx.get(
        f"{GITLAB_URL}/api/v4/user", headers={"PRIVATE-TOKEN": EXPIRED_TOKEN}, timeout=10
    )
    if _token_works(ADMIN_TOKEN) and "expired" in expired.text:
        return
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            f"E2E_ADMIN_TOKEN={ADMIN_TOKEN}",
            "-e",
            f"E2E_EXPIRED_TOKEN={EXPIRED_TOKEN}",
            CONTAINER,
            "gitlab-rails",
            "runner",
            "-",
        ],
        input=SEED_TOKENS_SCRIPT.read_bytes(),
        check=True,
        timeout=600,
    )
    assert _token_works(ADMIN_TOKEN), "admin token was not created"


def _wait_until_visible(path: str, token: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{GITLAB_URL}/api/v4{path}", headers={"PRIVATE-TOKEN": token}, timeout=10)
        if resp.is_success:
            return
        time.sleep(1)
    pytest.fail(f"bot still can't see {path} after {timeout}s")


class Admin:
    """Thin wrapper over the GitLab API using the root token."""

    def __init__(self) -> None:
        self.http = httpx.Client(
            base_url=f"{GITLAB_URL}/api/v4",
            headers={"PRIVATE-TOKEN": ADMIN_TOKEN},
            timeout=60,
        )

    def request(self, method: str, path: str, **kwargs) -> dict | list:
        resp = self.http.request(method, path, **kwargs)
        if resp.is_error:
            raise AssertionError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def post(self, endpoint: str, /, **data) -> dict:
        return self.request("POST", endpoint, json=data)

    def group(self, path: str, visibility: str, parent_id: int | None = None) -> dict:
        data = {"name": path, "path": path, "visibility": visibility}
        if parent_id:
            data["parent_id"] = parent_id
        return self.post("/groups", **data)

    def project(self, group: dict, path: str, visibility: str, **extra) -> dict:
        return self.post(
            "/projects",
            name=path,
            path=path,
            namespace_id=group["id"],
            visibility=visibility,
            initialize_with_readme=True,
            **extra,
        )

    def issue(self, project: dict, title: str) -> dict:
        return self.post(f"/projects/{project['id']}/issues", title=title)

    def head_sha(self, project: dict) -> str:
        commits = self.request("GET", f"/projects/{project['id']}/repository/commits")
        return commits[0]["id"]


@pytest.fixture(scope="session")
def seed() -> Iterator[Seed]:
    _wait_for_gitlab()
    _ensure_root_tokens()
    admin = Admin()
    s = uuid.uuid4().hex[:6]

    group = admin.group(f"e2e-{s}", "private")
    admin.group("sub", "private", parent_id=group["id"])
    project = admin.project(
        group, "app", "private", description="Demo app for unfurl tests", topics=["python", "slack"]
    )
    pid = project["id"]
    admin.post(f"/projects/{pid}/repository/branches", branch="feature", ref="main")
    commit = admin.post(
        f"/projects/{pid}/repository/commits",
        branch="feature",
        commit_message="Add feature module\n\nExplains why the feature exists.",
        actions=[{"action": "create", "file_path": "feature.py", "content": "print('hi')\n"}],
    )
    mr = admin.post(
        f"/projects/{pid}/merge_requests",
        source_branch="feature",
        target_branch="main",
        title="Add feature module",
        description="This MR adds the feature module.",
        labels="backend,e2e",
    )
    issue = admin.issue(project, "Something is broken")

    public_group = admin.group(f"e2e-{s}-public", "public")
    public_project = admin.project(public_group, "site", "public")
    public_issue = admin.issue(public_project, "Public bug report")

    secret_group = admin.group(f"e2e-{s}-secret", "private")
    secret_project = admin.project(secret_group, "hidden", "private")

    bot_username = f"unfurl-bot-{s}"
    bot = admin.post(
        "/users",
        username=bot_username,
        name="Unfurl Bot",
        email=f"{bot_username}@example.com",
        force_random_password=True,
        skip_confirmation=True,
    )
    admin.post(f"/groups/{group['id']}/members", user_id=bot["id"], access_level=REPORTER)
    expires = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    bot_token = admin.post(
        f"/users/{bot['id']}/personal_access_tokens",
        name="unfurl",
        scopes=["read_api"],
        expires_at=expires,
    )
    revoked = admin.post(
        f"/users/{bot['id']}/personal_access_tokens",
        name="revoked",
        scopes=["read_api"],
        expires_at=expires,
    )
    admin.request("DELETE", f"/personal_access_tokens/{revoked['id']}")
    # Group membership reaches projects through a Sidekiq job, so wait for it.
    _wait_until_visible(f"/projects/{pid}", bot_token["token"])

    yield Seed(
        suffix=s,
        group=group["full_path"],
        subgroup=f"{group['full_path']}/sub",
        project=project["path_with_namespace"],
        mr_iid=mr["iid"],
        commit_sha=commit["id"],
        issue_iid=issue["iid"],
        public_project=public_project["path_with_namespace"],
        public_issue_iid=public_issue["iid"],
        secret_project=secret_project["path_with_namespace"],
        secret_commit_sha=admin.head_sha(secret_project),
        bot_username=bot_username,
        bot_name="Unfurl Bot",
        bot_token=bot_token["token"],
        revoked_token=revoked["token"],
    )
    admin.http.close()


@pytest.fixture
def make_harness() -> Iterator[Callable[..., Harness]]:
    created: list[tuple[GitLabClient, MeterProvider]] = []

    def factory(token: str, *, base_url: str = GITLAB_URL, **unfurler_kwargs) -> Harness:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        health = HealthState()
        metrics = Metrics(provider.get_meter("e2e"), health)
        client = GitLabClient(base_url, token, health=health, metrics=metrics, timeout=30)
        created.append((client, provider))
        return Harness(
            Unfurler(client, metrics, **unfurler_kwargs), client, health, reader, metrics
        )

    yield factory
    for client, provider in created:
        client.close()
        provider.shutdown()


@pytest.fixture
def bot(seed: Seed, make_harness) -> Harness:
    """Harness using the bot's working token, the way the app runs in production."""
    return make_harness(seed.bot_token)
