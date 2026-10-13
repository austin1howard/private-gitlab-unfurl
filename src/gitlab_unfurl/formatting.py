"""Build Slack Block Kit unfurl payloads from GitLab API responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

DENIED_TEXT = "unable to show gitlab preview"
DESCRIPTION_MAX_CHARS = 300

Unfurl = dict[str, Any]


def escape(text: str) -> str:
    """Escape the characters Slack mrkdwn treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(text: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def link(url: str, label: str) -> str:
    return f"<{url}|{escape(label)}>"


def slack_date(value: str | None) -> str | None:
    """Format an ISO timestamp with Slack's date token so it renders in the reader's timezone."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return escape(value)
    return f"<!date^{int(dt.timestamp())}^{{date_short_pretty}} at {{time}}|{escape(value)}>"


def denied_unfurl() -> Unfurl:
    return {"blocks": [_section(DENIED_TEXT)]}


def title_unfurl(url: str, title: str) -> Unfurl:
    return {"blocks": [_section(f"*{link(url, title)}*")]}


def merge_request_unfurl(mr: dict[str, Any], url: str) -> Unfurl:
    title = mr.get("title") or f"!{mr.get('iid')}"
    reference = (mr.get("references") or {}).get("full") or f"!{mr.get('iid')}"
    header = f"*{link(mr.get('web_url') or url, title)}*\n`{escape(reference)}`"

    fields = [
        _field("Status", _mr_status(mr)),
        _field("Author", _person(mr.get("author"))),
        _field(
            "Branches",
            f"`{escape(mr.get('source_branch', '?'))}` → `{escape(mr.get('target_branch', '?'))}`",
        ),
    ]
    reviewers = _people(mr.get("reviewers"))
    if reviewers:
        fields.append(_field("Reviewers", reviewers))
    assignees = _people(mr.get("assignees"))
    if assignees:
        fields.append(_field("Assignees", assignees))
    pipeline = (mr.get("head_pipeline") or {}).get("status")
    if pipeline:
        fields.append(_field("Pipeline", escape(pipeline.replace("_", " "))))
    updated = slack_date(mr.get("updated_at"))
    if updated:
        fields.append(_field("Updated", updated))

    blocks: list[dict[str, Any]] = [_section(header, fields)]
    description = truncate(mr.get("description") or "")
    if description:
        blocks.append(_section(escape(description)))

    context = []
    labels = mr.get("labels") or []
    if labels:
        context.append("Labels: " + ", ".join(escape(str(label)) for label in labels))
    notes = mr.get("user_notes_count")
    if notes:
        context.append(f"{notes} comment{'s' if notes != 1 else ''}")
    if mr.get("changes_count"):
        context.append(f"{escape(str(mr['changes_count']))} files changed")
    if context:
        blocks.append(_context(" · ".join(context)))
    return {"blocks": blocks}


def commit_unfurl(commit: dict[str, Any], project_path: str, url: str) -> Unfurl:
    title = commit.get("title") or commit.get("short_id") or "Commit"
    short_id = commit.get("short_id") or (commit.get("id") or "")[:8]
    header = (
        f"*{link(commit.get('web_url') or url, title)}*\n"
        f"`{escape(short_id)}` in `{escape(project_path)}`"
    )

    fields = [_field("Author", escape(commit.get("author_name") or "unknown"))]
    authored = slack_date(commit.get("authored_date") or commit.get("created_at"))
    if authored:
        fields.append(_field("Authored", authored))
    stats = commit.get("stats") or {}
    if stats:
        changes = f"+{stats.get('additions', 0)} / -{stats.get('deletions', 0)}"
        fields.append(_field("Changes", changes))
    pipeline = (commit.get("last_pipeline") or {}).get("status")
    if pipeline:
        fields.append(_field("Pipeline", escape(pipeline.replace("_", " "))))

    blocks: list[dict[str, Any]] = [_section(header, fields)]
    body = _commit_body(commit.get("message") or "", title)
    if body:
        blocks.append(_section(escape(truncate(body))))
    return {"blocks": blocks}


def project_unfurl(project: dict[str, Any], url: str) -> Unfurl:
    name = project.get("name_with_namespace") or project.get("path_with_namespace") or "Project"
    text = f"*{link(project.get('web_url') or url, name)}*"
    description = truncate(project.get("description") or "")
    if description:
        text += "\n" + escape(description)

    fields = []
    if project.get("visibility"):
        fields.append(_field("Visibility", escape(project["visibility"].capitalize())))
    if project.get("default_branch"):
        fields.append(_field("Default branch", f"`{escape(project['default_branch'])}`"))
    fields.append(_field("Stars", str(project.get("star_count", 0))))
    fields.append(_field("Forks", str(project.get("forks_count", 0))))
    if "open_issues_count" in project:
        fields.append(_field("Open issues", str(project["open_issues_count"])))
    activity = slack_date(project.get("last_activity_at"))
    if activity:
        fields.append(_field("Last activity", activity))

    blocks: list[dict[str, Any]] = [_section(text, fields)]
    topics = project.get("topics") or []
    if topics:
        blocks.append(_context("Topics: " + ", ".join(escape(str(t)) for t in topics)))
    return {"blocks": blocks}


def group_unfurl(group: dict[str, Any], url: str) -> Unfurl:
    name = group.get("full_name") or group.get("full_path") or "Group"
    text = f"*{link(group.get('web_url') or url, name)}*"
    description = truncate(group.get("description") or "")
    if description:
        text += "\n" + escape(description)

    fields = []
    if group.get("full_path"):
        fields.append(_field("Path", f"`{escape(group['full_path'])}`"))
    if group.get("visibility"):
        fields.append(_field("Visibility", escape(group["visibility"].capitalize())))
    created = slack_date(group.get("created_at"))
    if created:
        fields.append(_field("Created", created))
    return {"blocks": [_section(text, fields)]}


def _mr_status(mr: dict[str, Any]) -> str:
    state = (mr.get("state") or "unknown").replace("_", " ")
    status = {"opened": "Open"}.get(state, state.capitalize())
    if state == "opened" and (mr.get("draft") or mr.get("work_in_progress")):
        status = "Draft"
    return escape(status)


def _person(user: dict[str, Any] | None) -> str:
    if not user:
        return "unknown"
    return escape(user.get("name") or user.get("username") or "unknown")


def _people(users: list[dict[str, Any]] | None) -> str:
    return ", ".join(_person(u) for u in users or [])


def _commit_body(message: str, title: str) -> str:
    """Return the commit message without its first line (the title)."""
    first, _, rest = message.strip().partition("\n")
    return rest.strip() if first.strip() == title.strip() else message.strip()


def _section(text: str, fields: list[dict[str, str]] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if fields:
        block["fields"] = fields[:10]  # Slack's limit per section
    return block


def _field(label: str, value: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
