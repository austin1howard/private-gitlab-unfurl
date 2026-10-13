import pytest

from gitlab_unfurl.urls import LinkKind, parse_link

BASE = "https://gitlab.example.com"


def test_merge_request():
    link = parse_link(f"{BASE}/group/sub/project/-/merge_requests/42", BASE)
    assert link is not None
    assert link.kind is LinkKind.MERGE_REQUEST
    assert link.namespace_path == "group/sub/project"
    assert link.mr_iid == 42


@pytest.mark.parametrize(
    "suffix",
    ["/diffs", "/commits", "/pipelines", "?tab=overview", "#note_123", "/diffs#abc"],
)
def test_merge_request_with_subpages_query_and_fragment(suffix):
    link = parse_link(f"{BASE}/group/project/-/merge_requests/7{suffix}", BASE)
    assert link.kind is LinkKind.MERGE_REQUEST
    assert link.namespace_path == "group/project"
    assert link.mr_iid == 7


def test_merge_request_list_is_a_page():
    link = parse_link(f"{BASE}/group/project/-/merge_requests", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.namespace_path == "group/project"
    assert link.resource == ("merge_requests",)


def test_merge_request_non_numeric_is_a_page():
    link = parse_link(f"{BASE}/group/project/-/merge_requests/new", BASE)
    assert link.kind is LinkKind.PAGE


def test_commit():
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    link = parse_link(f"{BASE}/group/project/-/commit/{sha}", BASE)
    assert link.kind is LinkKind.COMMIT
    assert link.namespace_path == "group/project"
    assert link.commit_sha == sha


def test_commit_short_sha_and_uppercase_normalized():
    link = parse_link(f"{BASE}/g/p/-/commit/ABCDEF1?view=parallel", BASE)
    assert link.kind is LinkKind.COMMIT
    assert link.commit_sha == "abcdef1"


def test_commit_too_short_is_a_page():
    link = parse_link(f"{BASE}/g/p/-/commit/abc12", BASE)
    assert link.kind is LinkKind.PAGE


def test_commits_list_is_a_page():
    link = parse_link(f"{BASE}/g/p/-/commits/main", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.resource == ("commits", "main")


def test_legacy_merge_request_route():
    link = parse_link(f"{BASE}/group/project/merge_requests/12", BASE)
    assert link.kind is LinkKind.MERGE_REQUEST
    assert link.namespace_path == "group/project"
    assert link.mr_iid == 12


def test_legacy_commit_route():
    link = parse_link(f"{BASE}/group/sub/project/commit/deadbeef", BASE)
    assert link.kind is LinkKind.COMMIT
    assert link.namespace_path == "group/sub/project"
    assert link.commit_sha == "deadbeef"


def test_legacy_marker_needs_a_project_path():
    # A single segment can't be a project, so this is a nested namespace, not a legacy MR route.
    link = parse_link(f"{BASE}/group/merge_requests/12", BASE)
    assert link.kind is LinkKind.NAMESPACE


@pytest.mark.parametrize(
    ("path", "namespace"),
    [
        ("/group/project", "group/project"),
        ("/group/project/", "group/project"),
        ("/group", "group"),
        ("/a/b/c/d", "a/b/c/d"),
        ("/group/project.git", "group/project"),
    ],
)
def test_namespace_home_pages(path, namespace):
    link = parse_link(f"{BASE}{path}", BASE)
    assert link.kind is LinkKind.NAMESPACE
    assert link.namespace_path == namespace


def test_explicit_group_url():
    link = parse_link(f"{BASE}/groups/parent/child", BASE)
    assert link.kind is LinkKind.GROUP
    assert link.namespace_path == "parent/child"


def test_groups_root_is_a_page():
    link = parse_link(f"{BASE}/groups", BASE)
    assert link.kind is LinkKind.PAGE


def test_group_dash_route_is_group_only_page():
    link = parse_link(f"{BASE}/groups/parent/-/epics/3", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.namespace_path == "parent"
    assert link.group_only is True
    assert link.resource == ("epics", "3")


def test_group_dash_route_never_matches_merge_request():
    link = parse_link(f"{BASE}/groups/parent/-/merge_requests/3", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.group_only is True


def test_project_page_with_dash_route():
    link = parse_link(f"{BASE}/group/project/-/issues/5", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.namespace_path == "group/project"
    assert link.group_only is False
    assert link.resource == ("issues", "5")


def test_dash_route_without_namespace():
    link = parse_link(f"{BASE}/-/ide/project/g/p", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.namespace_path is None


@pytest.mark.parametrize("path", ["/dashboard/merge_requests", "/explore", "/users/sign_in"])
def test_reserved_top_level_paths_are_pages(path):
    link = parse_link(f"{BASE}{path}", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.namespace_path is None


def test_root_is_a_page():
    link = parse_link(f"{BASE}/", BASE)
    assert link.kind is LinkKind.PAGE
    assert link.namespace_path is None


def test_url_encoded_path_is_decoded():
    link = parse_link(f"{BASE}/my%20group/project", BASE)
    assert link.namespace_path == "my group/project"


def test_host_match_is_case_insensitive_and_scheme_agnostic():
    link = parse_link("http://GitLab.Example.com/g/p", BASE)
    assert link is not None


def test_url_is_preserved():
    url = f"{BASE}/g/p/-/merge_requests/1#note_5"
    assert parse_link(url, BASE).url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/group/project",
        "https://gitlab.example.com.evil.com/group/project",
        "ftp://gitlab.example.com/group/project",
        "not a url",
        "",
        "https://gitlab.example.com:99999/g/p",
    ],
)
def test_foreign_or_invalid_urls(url):
    assert parse_link(url, BASE) is None


def test_base_url_with_port():
    base = "https://gitlab.example.com:8443"
    assert parse_link("https://gitlab.example.com:8443/g/p", base) is not None
    assert parse_link("https://gitlab.example.com/g/p", base) is None


def test_base_url_with_relative_root():
    base = "https://example.com/gitlab"
    link = parse_link("https://example.com/gitlab/g/p/-/merge_requests/3", base)
    assert link.kind is LinkKind.MERGE_REQUEST
    assert link.namespace_path == "g/p"
    assert parse_link("https://example.com/other/g/p", base) is None
    assert parse_link("https://example.com/gitlabx/g/p", base) is None
    assert parse_link("https://example.com/gitlab", base).kind is LinkKind.PAGE
