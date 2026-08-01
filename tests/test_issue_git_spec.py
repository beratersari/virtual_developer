"""Issue git template parsing ({params} block)."""

from src.issue_git_spec import (
    IssueGitConfigError,
    parse_issue_git_spec,
    require_issue_git_spec,
    strip_params_block,
)


def test_parse_happy_path():
    desc = """
Some task text here.

{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Target branch: main
{params}

Acceptance: do the thing
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert err is None
    assert spec is not None
    assert spec.repository_url.endswith("repo.git")
    assert spec.source_branch == "develop"
    assert spec.target_branch == "main"


def test_params_required():
    """Fields outside {params} are ignored."""
    desc = (
        "Repository: https://gitlab.com/a/b.git\n"
        "Source branch: develop\n"
    )
    spec, err = parse_issue_git_spec("sum", desc)
    assert spec is None
    assert err is not None
    assert "{params}" in err


def test_strip_params_block_keeps_task_text():
    desc = (
        "Do the calculator work.\n\n"
        "{params}\n"
        "Repository: https://gitlab.com/u/r.git\n"
        "Source branch: feature/X\n"
        "Target branch: main\n"
        "{params}\n\n"
        "More acceptance notes."
    )
    out = strip_params_block(desc)
    assert "Do the calculator work." in out
    assert "More acceptance notes." in out
    assert "{params}" not in out
    assert "Repository:" not in out
    # Still parseable from original (strip does not mutate source)
    spec, err = parse_issue_git_spec("s", desc)
    assert err is None and spec is not None


def test_target_defaults_to_source_when_omitted():
    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
{params}
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert err is None
    assert spec is not None
    assert spec.source_branch == "develop"
    assert spec.target_branch == "develop"


def test_parse_aliases():
    desc = """
{params}
Repo: https://gitlab.com/a/b.git
Work branch: feature/team-base
Base branch: main
{params}
"""
    spec, err = parse_issue_git_spec("", desc)
    assert err is None
    assert spec is not None
    assert "gitlab.com" in spec.repository_url
    assert spec.source_branch == "feature/team-base"
    assert spec.target_branch == "main"


def test_jira_wiki_inside_params():
    """Jira wiki links and same-line fields inside {params}."""
    desc = (
        "h3. Summary\n\nKANe\n\n"
        "{params}\n"
        "Repository: \n\n"
        "[https://gitlab.com/beratersari0/test_project.git|"
        "https://gitlab.com/beratersari0/test_project.git|smart-card]\n\n"
        "Source branch: develop Target branch: feature/KAN-4\n"
        "{params}\n"
        "h3. Acceptance\n10 + 3\n"
    )
    spec, err = parse_issue_git_spec("KANe", desc)
    assert err is None, err
    assert spec is not None
    assert spec.repository_url == "https://gitlab.com/beratersari0/test_project.git"
    assert spec.source_branch == "develop"
    assert spec.target_branch == "feature/KAN-4"


def test_escaped_jira_code_braces_still_ok():
    """Wiki may store braces oddly; unescaped {params} is the contract."""
    desc = "{params}\nRepository: https://g.com/a/b.git\nSource branch: main\n{params}"
    spec, err = parse_issue_git_spec("", desc)
    assert err is None
    assert spec is not None
    assert spec.source_branch == "main"


def test_missing_fields_inside_params():
    desc = "{params}\nRepository: https://gitlab.example.com/g/r.git\n{params}"
    spec, err = parse_issue_git_spec("hello", desc)
    assert spec is None
    assert err is not None
    assert "Source branch" in err


def test_invalid_url():
    desc = (
        "{params}\nRepository: not-a-url\nSource branch: develop\n{params}\n"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert spec is None
    assert err is not None
    assert "invalid" in err.lower()


def test_invalid_branch():
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: bad..name\n"
        "{params}\n"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert spec is None
    assert err is not None
    assert "branch" in err.lower()


def test_require_raises():
    try:
        require_issue_git_spec("x", "y")
        assert False, "expected raise"
    except IssueGitConfigError as e:
        assert e.user_message
        assert "{params}" in e.user_message
