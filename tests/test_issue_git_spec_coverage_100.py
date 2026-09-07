"""Coverage-focused tests for src/issue_git_spec.py."""

from __future__ import annotations

import pytest

from src.issue_git_spec import (
    IssueGitConfigError,
    _expand_links,
    _extract_repo,
    _looks_like_branch,
    _looks_like_git_url,
    _normalize_branch,
    _normalize_repo_url,
    _params_block_text,
    parse_issue_git_spec,
    parse_issue_mode,
    require_issue_git_spec,
    strip_params_block,
)


def test_mode_required_and_aliases():
    for alias, canonical in [
        ("plan", "plan"),
        ("planning", "plan"),
        ("prometheus", "plan"),
        ("build", "build"),
        ("execute", "build"),
        ("execution", "build"),
        ("atlas", "build"),
        ("implement", "build"),
    ]:
        desc = (
            "{params}\n"
            "Repository: https://gitlab.example.com/g/r.git\n"
            f"Source branch: feature/X\nTarget branch: develop\nMode: {alias}\n"
            "{params}"
        )
        spec, err = parse_issue_git_spec("s", desc)
        assert err is None, err
        assert spec is not None
        assert spec.mode == canonical


def test_invalid_mode_token():
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: develop\n"
        "Mode: banana\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("s", desc)
    assert spec is None
    assert err is not None
    assert "Mode" in err
    assert "banana" in err


def test_missing_repo():
    desc = (
        "{params}\n"
        "Source branch: develop\n"
        "Mode: plan\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("s", desc)
    assert spec is None
    assert "Repository" in (err or "")


def test_invalid_target_branch():
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: feature/ok\n"
        "Target branch: bad..name\n"
        "Mode: plan\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("s", desc)
    assert spec is None
    assert "target branch" in (err or "").lower()


def test_invalid_source_branch_chars():
    """Branch names with .. or leading slash are rejected after parse."""
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: /leading\n"
        "Mode: plan\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("s", desc)
    assert spec is None
    assert err is not None
    assert "branch" in err.lower()


def test_looks_like_branch_edges():
    assert _looks_like_branch("") is False
    assert _looks_like_branch("a" * 256) is False
    assert _looks_like_branch("has space") is False
    assert _looks_like_branch("has\\slash") is False
    assert _looks_like_branch("a..b") is False
    assert _looks_like_branch("/leading") is False
    assert _looks_like_branch("trailing/") is False
    assert _looks_like_branch("feature/ok-1.2") is True


def test_looks_like_git_url_edges():
    assert _looks_like_git_url("") is False
    assert _looks_like_git_url("git@host:group/repo.git") is True
    assert _looks_like_git_url("git@host") is False  # no colon path
    assert _looks_like_git_url("ftp://x/y") is False
    assert _looks_like_git_url("https://host-only") is False  # no path
    assert _looks_like_git_url("https://host/group/repo") is True


def test_normalize_helpers():
    assert _normalize_branch("  refs/heads/main  ") == "main"
    assert _normalize_branch("`feature/x`") == "feature/x"
    assert _normalize_branch("feature/[KAN-7]") == "feature/KAN-7"
    assert (
        _normalize_branch("feature/[KAN-7|https://jira.example/browse/KAN-7]")
        == "feature/KAN-7"
    )
    assert "repo" in _normalize_repo_url("  <https://g.com/a/repo.git>,  ")
    # extract URL from surrounding junk
    assert "gitlab" in _normalize_repo_url("see https://gitlab.com/g/r.git please")


def test_expand_links_jira_and_markdown():
    jira_right = "[label|https://gitlab.com/g/r.git]"
    assert "https://gitlab.com/g/r.git" in _expand_links(jira_right)
    jira_left = "[https://gitlab.com/g/r.git|label]"
    assert "https://gitlab.com/g/r.git" in _expand_links(jira_left)
    jira_neither = "[noturl|also-not]"
    assert "also-not" in _expand_links(jira_neither)
    md = "[click](https://gitlab.com/g/r.git)"
    assert _expand_links(md) == "https://gitlab.com/g/r.git"


def test_extract_repo_fallback_url_token():
    # no Repository: key — first URL in block
    text = "https://gitlab.example.com/group/repo.git\nSource branch: develop"
    url = _extract_repo(text)
    assert url.endswith("repo.git")


def test_extract_repo_multiline_blob():
    text = (
        "Repository: \n"
        "not-a-url-line\n"
        "https://gitlab.example.com/g/r.git\n"
        "Source branch: develop"
    )
    assert "r.git" in _extract_repo(text)


def test_extract_repo_url_in_same_line_junk():
    text = "Repository: see (https://gitlab.example.com/g/r.git) please\nMode: plan"
    assert "r.git" in _extract_repo(text)


def test_params_block_text_and_strip_empty():
    assert strip_params_block("") == ""
    assert strip_params_block(None) == ""  # type: ignore[arg-type]
    assert _params_block_text("", "") is None
    block = _params_block_text(
        "",
        "{params}\nRepository: https://g.com/a/b.git\nMode: plan\n{params}",
    )
    assert block and "Repository" in block


def test_parse_issue_mode_no_mode_field():
    desc = (
        "{params}\nRepository: https://g.com/a/b.git\n"
        "Source branch: develop\n{params}"
    )
    assert parse_issue_mode("", desc) is None
    assert parse_issue_mode("", "") is None


def test_require_issue_git_spec_ok_and_error():
    with pytest.raises(IssueGitConfigError) as ei:
        require_issue_git_spec("x", "no params")
    assert ei.value.user_message

    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: plan\n"
        "{params}"
    )
    spec = require_issue_git_spec("s", desc)
    assert spec.mode == "plan"
    assert spec.target_branch == "main"


def test_gitlab_alias_and_mr_target_alias():
    desc = (
        "{params}\n"
        "GitLab: https://gitlab.example.com/g/r.git\n"
        "Source branch: feature/x\n"
        "MR target: develop\n"
        "Mode: build\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert err is None
    assert spec is not None
    assert spec.target_branch == "develop"
    assert spec.mode == "build"


def test_merge_into_alias():
    desc = (
        "{params}\n"
        "Project URL: https://gitlab.example.com/g/r.git\n"
        "Work branch: feature/y\n"
        "Merge into: main\n"
        "Workflow mode: atlas\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert err is None
    assert spec is not None
    assert spec.source_branch == "feature/y"
    assert spec.target_branch == "main"
    assert spec.mode == "build"


def test_ssh_git_url():
    desc = (
        "{params}\n"
        "Repository: git@gitlab.example.com:group/repo.git\n"
        "Source branch: develop\n"
        "Mode: plan\n"
        "{params}"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert err is None, err
    assert spec is not None
    assert "gitlab.example.com" in spec.repository_url


def test_params_in_summary():
    summary = (
        "{params}\nRepository: https://gitlab.example.com/g/r.git\n"
        "Source branch: main\nMode: plan\n{params}"
    )
    spec, err = parse_issue_git_spec(summary, "body without params")
    assert err is None
    assert spec is not None
