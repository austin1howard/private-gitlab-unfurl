from gitlab_unfurl import formatting as f

URL = "https://gitlab.example.com/g/p/-/merge_requests/1"


def texts(payload):
    out = []
    for block in payload["blocks"]:
        if "text" in block:
            out.append(block["text"]["text"])
        out.extend(x["text"] for x in block.get("fields", []))
        out.extend(x["text"] for x in block.get("elements", []))
    return out


def joined(payload):
    return "\n".join(texts(payload))


def test_escape():
    assert f.escape("a & <b> c") == "a &amp; &lt;b&gt; c"


def test_truncate():
    assert f.truncate("  short  ") == "short"
    long = "x" * 400
    out = f.truncate(long, 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_link_escapes_label():
    assert f.link("https://x", "a <b>") == "<https://x|a &lt;b&gt;>"


def test_slack_date():
    assert f.slack_date("2024-01-02T03:04:05.000Z") == (
        "<!date^1704164645^{date_short_pretty} at {time}|2024-01-02T03:04:05.000Z>"
    )
    assert f.slack_date(None) is None
    assert f.slack_date("") is None
    assert f.slack_date("not-a-date") == "not-a-date"


def test_denied_unfurl_text_is_exact():
    payload = f.denied_unfurl()
    assert texts(payload) == ["unable to show gitlab preview"]


def test_title_unfurl():
    payload = f.title_unfurl("https://x/y", "Hello & bye")
    assert texts(payload) == ["*<https://x/y|Hello &amp; bye>*"]


class TestMergeRequest:
    MR = {
        "iid": 12,
        "title": "Add <feature>",
        "state": "opened",
        "draft": False,
        "web_url": URL,
        "references": {"full": "g/p!12"},
        "author": {"name": "Jane Doe", "username": "jane"},
        "source_branch": "feat",
        "target_branch": "main",
        "reviewers": [{"name": "Rev One"}, {"username": "rev2"}],
        "assignees": [{"name": "Ann"}],
        "head_pipeline": {"status": "running"},
        "updated_at": "2024-01-02T03:04:05Z",
        "description": "Long description " * 50,
        "labels": ["bug", "backend"],
        "user_notes_count": 3,
        "changes_count": "7",
    }

    def test_full(self):
        payload = f.merge_request_unfurl(self.MR, URL)
        text = joined(payload)
        assert f"*<{URL}|Add &lt;feature&gt;>*\n`g/p!12`" in text
        assert "*Status*\nOpen" in text
        assert "*Author*\nJane Doe" in text
        assert "`feat` → `main`" in text
        assert "*Reviewers*\nRev One, rev2" in text
        assert "*Assignees*\nAnn" in text
        assert "*Pipeline*\nrunning" in text
        assert "*Updated*\n<!date^" in text
        assert "Labels: bug, backend · 3 comments · 7 files changed" in text
        description = payload["blocks"][1]["text"]["text"]
        assert len(description) <= f.DESCRIPTION_MAX_CHARS

    def test_minimal(self):
        payload = f.merge_request_unfurl({"iid": 3}, URL)
        text = joined(payload)
        assert f"*<{URL}|!3>*\n`!3`" in text
        assert "*Status*\nUnknown" in text
        assert "*Author*\nunknown" in text
        assert len(payload["blocks"]) == 1

    def test_draft(self):
        mr = {**self.MR, "draft": True}
        assert "*Status*\nDraft" in joined(f.merge_request_unfurl(mr, URL))

    def test_merged_and_single_comment(self):
        mr = {**self.MR, "state": "merged", "user_notes_count": 1}
        text = joined(f.merge_request_unfurl(mr, URL))
        assert "*Status*\nMerged" in text
        assert "1 comment ·" in text

    def test_draft_flag_ignored_when_merged(self):
        mr = {**self.MR, "state": "merged", "draft": True}
        assert "*Status*\nMerged" in joined(f.merge_request_unfurl(mr, URL))


class TestCommit:
    COMMIT = {
        "id": "abcdef1234567890",
        "short_id": "abcdef12",
        "title": "Fix bug",
        "message": "Fix bug\n\nDetails & more",
        "author_name": "Sam",
        "authored_date": "2024-01-02T03:04:05Z",
        "stats": {"additions": 10, "deletions": 2},
        "last_pipeline": {"status": "success"},
        "web_url": "https://gitlab.example.com/g/p/-/commit/abcdef1234567890",
    }

    def test_full(self):
        text = joined(f.commit_unfurl(self.COMMIT, "g/p", "u"))
        assert "|Fix bug>*\n`abcdef12` in `g/p`" in text
        assert "*Author*\nSam" in text
        assert "*Authored*\n<!date^" in text
        assert "*Changes*\n+10 / -2" in text
        assert "*Pipeline*\nsuccess" in text
        assert "Details &amp; more" in text

    def test_single_line_message_has_no_body(self):
        commit = {**self.COMMIT, "message": "Fix bug"}
        assert len(f.commit_unfurl(commit, "g/p", "u")["blocks"]) == 1

    def test_message_not_starting_with_title_shown_in_full(self):
        commit = {**self.COMMIT, "title": "Fix b...", "message": "Fix bug with long line"}
        assert "Fix bug with long line" in joined(f.commit_unfurl(commit, "g/p", "u"))

    def test_minimal(self):
        payload = f.commit_unfurl({"id": "0123456789abcdef"}, "g/p", "https://u")
        text = joined(payload)
        assert "*<https://u|Commit>*\n`01234567` in `g/p`" in text
        assert "*Author*\nunknown" in text


class TestProject:
    PROJECT = {
        "name_with_namespace": "Group / Project",
        "web_url": "https://gitlab.example.com/group/project",
        "description": "A project",
        "visibility": "internal",
        "default_branch": "main",
        "star_count": 4,
        "forks_count": 1,
        "open_issues_count": 9,
        "last_activity_at": "2024-01-02T03:04:05Z",
        "topics": ["python", "slack"],
    }

    def test_full(self):
        text = joined(f.project_unfurl(self.PROJECT, "u"))
        assert "*<https://gitlab.example.com/group/project|Group / Project>*\nA project" in text
        assert "*Visibility*\nInternal" in text
        assert "*Default branch*\n`main`" in text
        assert "*Stars*\n4" in text
        assert "*Forks*\n1" in text
        assert "*Open issues*\n9" in text
        assert "*Last activity*\n<!date^" in text
        assert "Topics: python, slack" in text

    def test_minimal(self):
        payload = f.project_unfurl({}, "https://u")
        text = joined(payload)
        assert "*<https://u|Project>*" in text
        assert "Open issues" not in text
        assert len(payload["blocks"]) == 1


class TestGroup:
    def test_full(self):
        group = {
            "full_name": "Parent / Child",
            "full_path": "parent/child",
            "web_url": "https://gitlab.example.com/groups/parent/child",
            "description": "Team stuff",
            "visibility": "private",
            "created_at": "2024-01-02T03:04:05Z",
        }
        text = joined(f.group_unfurl(group, "u"))
        assert "|Parent / Child>*\nTeam stuff" in text
        assert "*Path*\n`parent/child`" in text
        assert "*Visibility*\nPrivate" in text
        assert "*Created*\n<!date^" in text

    def test_minimal(self):
        payload = f.group_unfurl({}, "https://u")
        assert texts(payload) == ["*<https://u|Group>*"]
        assert "fields" not in payload["blocks"][0]


def test_section_caps_fields_at_ten():
    block = f._section("x", [f._field(str(i), "v") for i in range(15)])
    assert len(block["fields"]) == 10
