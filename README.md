# private-gitlab-unfurl

A Slack app that shows previews for links to a private GitLab instance. It reads GitLab with a single service account token, so anything that account can see gets a preview, for everyone in the channel.

It connects to Slack with [Socket Mode](https://api.slack.com/apis/socket-mode). The app only makes outbound connections (to Slack and to GitLab), so it can run inside your network next to GitLab without exposing a public endpoint.

## What gets unfurled

| Link | Preview |
| --- | --- |
| Merge request (`group/project/-/merge_requests/42`, including `/diffs`, `#note_…`, etc.) | Title, reference, status (open/draft/merged/closed), author, source and target branch, reviewers, assignees, pipeline status, last update, description excerpt, labels, comment and file counts |
| Commit (`group/project/-/commit/<sha>`) | Title, short SHA, project, author, date, +/- line counts, pipeline status, rest of the commit message |
| Project home (`group/project`) | Name, description, visibility, default branch, stars, forks, open issues, last activity, topics |
| Group home (`group/sub` or `groups/group/sub`) | Name, description, path, visibility, created date |
| Anything else | Page title only |
| Anything the service account can't see | `unable to show gitlab preview` |

The same "unable to show gitlab preview" message is used when the token is broken or GitLab is down. The reason shows up in metrics and logs, not in Slack.

Older URLs without the `/-/` segment (`group/project/merge_requests/42`) are also recognized.

### How "anything else" titles work

GitLab does not accept API tokens on its HTML pages, so the app can't read the real `<title>` of a private page. For other links it:

1. Checks that the service account can see the project or group the page belongs to. If not, it shows "unable to show gitlab preview".
2. Fetches the page without credentials. If that returns a real title (public pages), it uses it.
3. Otherwise it builds a title from the URL and the project or group name, for example `Issues / 5 · Group / Project · GitLab`.

Set `FETCH_PAGE_TITLES=false` to skip step 2 if every page on your instance needs sign-in.

A single-segment link that isn't a visible project or group (`gitlab.example.com/jane`) is looked up as a user and shown as `Jane Doe (@jane) · GitLab`.

## Setup

### 1. GitLab token

1. Create a [service account](https://docs.gitlab.com/user/profile/service_accounts/) (GitLab Premium/Ultimate). On Free tiers, a group access token or a dedicated bot user's personal access token works the same way.
2. Add the account to the groups or projects you want previews for. Use **Reporter**: Guests can't read code or commits in private projects.
3. Create a personal access token for it with the **`read_api`** scope. That's all it needs.
4. Note the expiry date. The app reports days until expiry as a metric (see below) so you can alert before it lapses.

### 2. Slack app

1. Edit `slack-app-manifest.yml` and replace `gitlab.example.com` with your GitLab hostname.
2. Go to <https://api.slack.com/apps>, choose **Create New App → From an app manifest**, and paste the manifest.
3. Under **Basic Information → App-Level Tokens**, create a token with the `connections:write` scope. This is `SLACK_APP_TOKEN` (`xapp-…`).
4. Under **Install App**, install it to your workspace. The **Bot User OAuth Token** is `SLACK_BOT_TOKEN` (`xoxb-…`).

If links don't unfurl in a private channel, invite the app to that channel.

### 3. Run it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --no-dev
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=glpat-...
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
uv run gitlab-unfurl
```

To install it elsewhere, build a wheel with `uv build` and `pip install dist/*.whl`, then run `gitlab-unfurl` (or `python -m gitlab_unfurl`).

The process handles `SIGTERM` and flushes metrics on shutdown.

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `SLACK_BOT_TOKEN` | yes | | Bot token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | yes | | App-level token with `connections:write` (`xapp-…`) |
| `GITLAB_URL` | yes | | Base URL of your GitLab, e.g. `https://gitlab.example.com`. Include the path if GitLab runs under one (`https://example.com/gitlab`) |
| `GITLAB_TOKEN` | yes | | Service account token with `read_api` |
| `GITLAB_TIMEOUT` | no | `10` | Seconds per GitLab request |
| `GITLAB_CA_BUNDLE` | no | | Path to a PEM CA bundle, for GitLab behind an internal CA |
| `HEALTH_CHECK_INTERVAL` | no | `300` | Seconds between background token/API checks. `0` checks once at startup only |
| `FETCH_PAGE_TITLES` | no | `true` | Try to read real page titles for "anything else" links |
| `LOG_LEVEL` | no | `INFO` | Python log level |

OpenTelemetry uses the standard variables:

| Variable | Default | Description |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP/HTTP collector endpoint. `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, etc. also work |
| `OTEL_METRICS_EXPORTER` | `otlp` | `otlp`, `console` (prints to stdout, handy for debugging), or `none` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` | Export interval in ms |
| `OTEL_SERVICE_NAME` | `gitlab-unfurl` | `service.name` resource attribute |
| `OTEL_RESOURCE_ATTRIBUTES` | | Extra resource attributes, e.g. `deployment.environment=prod` |
| `OTEL_SDK_DISABLED` | | `true` turns metrics off |

## Metrics

| Metric | Type | Attributes | Meaning |
| --- | --- | --- | --- |
| `gitlab_unfurl.unfurls` | counter | `link.type`, `outcome` | Every GitLab link processed. `outcome` is `success`, `denied` (access or token problem), or `error` (GitLab down or unexpected failure) |
| `gitlab_unfurl.access_errors` | counter | `link.type`, `reason` | Links that couldn't be shown because of access. `reason` is `not_found`, `forbidden`, `token_expired`, `token_revoked`, or `token_invalid` |
| `gitlab_unfurl.gitlab.token_valid` | gauge | `reason` (when 0) | `1` if GitLab accepts the token, `0` if it's expired, revoked, or invalid. When this is 0, every unfurl fails |
| `gitlab_unfurl.gitlab.api_up` | gauge | | `1` if the GitLab API is answering, `0` on timeouts, connection failures, 5xx, or non-JSON responses |
| `gitlab_unfurl.gitlab.token_expires_in_days` | gauge | | Days until the token expires, if GitLab reports an expiry |
| `gitlab_unfurl.gitlab.api_errors` | counter | `endpoint`, `reason`, `http.response.status_code` | Failed API requests (excluding per-item 403/404). `reason` is `timeout`, `connection_error`, `server_error`, `invalid_response`, `rate_limited`, `token_expired`, `token_revoked`, `token_invalid`, or `unexpected_status` |
| `gitlab_unfurl.gitlab.request.duration` | histogram (s) | `endpoint`, `http.response.status_code` | GitLab API latency |
| `gitlab_unfurl.slack.errors` | counter | `error` | Failed `chat.unfurl` calls, by Slack error code |

`link.type` is one of `merge_request`, `commit`, `project`, `group`, `page`, or `namespace` (a home-page link that failed before it could be identified as a project or group).

The gauges are refreshed by every GitLab request and by the background health check, so they stay current even when nobody is posting links. GitLab returns 404 for private items a token can't see, so 403/404 count as per-link access errors and don't affect `token_valid` or `api_up`.

Suggested alerts:

- `gitlab_unfurl.gitlab.token_valid == 0`: token expired or revoked, all previews are broken
- `gitlab_unfurl.gitlab.api_up == 0` for 5+ minutes: GitLab API is down or unreachable
- `gitlab_unfurl.gitlab.token_expires_in_days < 14`: rotate the token soon
- Rising `gitlab_unfurl.access_errors{reason="not_found"}`: people are sharing links from projects the service account isn't a member of

Metric names may be rewritten by your backend (Prometheus, for example, turns them into `gitlab_unfurl_gitlab_token_valid`).

## Security notes

- Previews are visible to everyone who can see the message. Someone who can't open a private project in GitLab will still see its preview if a link is posted where they can read it. Only grant the service account access to projects whose titles and descriptions are fine to share with your whole Slack workspace.
- That includes Slack Connect channels shared with other organizations.
- The token is only sent to the GitLab API. Page title fetches are unauthenticated and are discarded if they redirect to a different host or a sign-in page.

## Development

```sh
uv sync
uv run pytest          # unit tests with coverage report (e2e tests are skipped)
uv run ruff check .
uv run ruff format .
```

### End-to-end tests

`tests/e2e/` runs the app against a real GitLab CE container (`compose.e2e.yml`). Slack is the only part that's faked: the tests patch `WebClient` so `chat.unfurl` calls are captured instead of sent.

Requires Docker with about 4 GB of free memory.

```sh
scripts/e2e.sh                     # boot GitLab, run tests, remove the container
KEEP_GITLAB=1 scripts/e2e.sh       # leave GitLab running for faster re-runs
uv run pytest -m e2e tests/e2e     # run against an already-running container
docker compose -f compose.e2e.yml down -v   # stop it when done
```

The first boot takes 3 to 5 minutes. With `KEEP_GITLAB=1`, later runs take about 15 seconds.

What the suite does:

- Creates two root tokens with known values via `gitlab-rails runner` (`tests/e2e/seed_tokens.rb`): an admin token for seeding, and one that's already expired. This step is skipped when they already exist.
- Seeds, under a random suffix so runs don't collide: a private group, subgroup, and project with a branch, commit, merge request, and issue; a public project; a private project the bot can't see; and a bot user who is a Reporter on the private group, with a working `read_api` token and a revoked one.
- Checks previews for each link type, access-denied cases, expired, revoked, and invalid tokens, an unreachable API, the background health check (including token expiry), and a full `link_shared` event through the Bolt app.

| Variable | Default | Description |
| --- | --- | --- |
| `GITLAB_IMAGE` | `gitlab/gitlab-ce:19.4.2-ce.0` | GitLab image to test against |
| `E2E_GITLAB_PORT` | `8929` | Host port for GitLab |
| `E2E_GITLAB_URL` | `http://localhost:8929` | URL the tests use. Change this together with `E2E_GITLAB_PORT` |
| `E2E_GITLAB_CONTAINER` | `gitlab-unfurl-e2e` | Container name, used for `docker exec` |

Layout:

```
src/gitlab_unfurl/
  __main__.py    entry point, wiring, shutdown
  app.py         Slack Bolt app and link_shared handler
  config.py      environment settings
  urls.py        classifies GitLab URLs
  gitlab.py      GitLab API client; updates health state and API metrics
  errors.py      client exceptions
  unfurler.py    URL -> Slack unfurl, records unfurl/access metrics
  formatting.py  Block Kit payloads
  health.py      token/API health state and background checker
  telemetry.py   OpenTelemetry setup and instruments
tests/           unit tests (no network; GitLab is faked with httpx.MockTransport)
tests/e2e/       end-to-end tests against the GitLab container
compose.e2e.yml  GitLab CE container for the e2e tests
scripts/e2e.sh   runs the e2e suite
```
