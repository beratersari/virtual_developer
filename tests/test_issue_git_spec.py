"""Issue git template parsing ({params} block + Mode)."""

from src.issue_git_spec import (
    IssueGitConfigError,
    parse_issue_git_spec,
    parse_issue_mode,
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
Mode: plan
{params}

Acceptance: do the thing
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert err is None
    assert spec is not None
    assert spec.repository_url.endswith("repo.git")
    assert spec.source_branch == "develop"
    assert spec.target_branch == "main"
    assert spec.mode == "plan"
    assert spec.model is None


def test_parse_optional_model():
    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Target branch: main
Mode: build
Model: opencode/hy3-free
{params}
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert err is None
    assert spec is not None
    assert spec.model == "opencode/hy3-free"


def test_upsert_params_model_inserts_and_replaces():
    from src.issue_git_spec import upsert_params_model

    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Target branch: main
Mode: build
{params}
"""
    one = upsert_params_model(desc, "opencode/mimo-v2.5-free")
    spec, err = parse_issue_git_spec("", one)
    assert err is None
    assert spec is not None
    assert spec.model == "opencode/mimo-v2.5-free"
    two = upsert_params_model(one, "opencode/hy3-free")
    spec2, err2 = parse_issue_git_spec("", two)
    assert err2 is None
    assert spec2 is not None
    assert spec2.model == "opencode/hy3-free"
    assert two.lower().count("model:") == 1


def test_params_required():
    """Fields outside {params} are ignored."""
    desc = (
        "Repository: https://gitlab.com/a/b.git\n"
        "Source branch: develop\n"
        "Mode: plan\n"
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
        "Mode: build\n"
        "{params}\n\n"
        "More acceptance notes."
    )
    out = strip_params_block(desc)
    assert "Do the calculator work." in out
    assert "More acceptance notes." in out
    assert "{params}" not in out
    assert "Repository:" not in out
    spec, err = parse_issue_git_spec("s", desc)
    assert err is None and spec is not None
    assert spec.mode == "build"


def test_target_defaults_to_source_when_omitted():
    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Mode: plan
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
Mode: execute
{params}
"""
    spec, err = parse_issue_git_spec("", desc)
    assert err is None
    assert spec is not None
    assert "gitlab.com" in spec.repository_url
    assert spec.source_branch == "feature/team-base"
    assert spec.target_branch == "main"
    assert spec.mode == "build"


def test_jira_issue_key_brackets_in_branch_parse():
    """Cloud visual editor wraps keys: feature/[KAN-7] must stay a git ref."""
    spec, err = parse_issue_git_spec(
        "KANe",
        (
            "{params}\n"
            "Repository: https://gitlab.com/beratersari0/test_project.git\n"
            "Source branch: feature/[KAN-7]\n"
            "Target branch: main\n"
            "Mode: build\n"
            "{params}\n"
        ),
    )
    assert err is None, err
    assert spec is not None
    assert spec.source_branch == "feature/KAN-7"
    assert spec.target_branch == "main"


def test_jira_issue_key_wiki_link_in_branch_parse():
    """[KAN-7|browse-url] must become KAN-7, not a URL glued onto the branch."""
    spec, err = parse_issue_git_spec(
        "KANe",
        (
            "{params}\n"
            "Repository: https://gitlab.com/beratersari0/test_project.git\n"
            "Source branch: feature/[KAN-7|https://beratersari0.atlassian.net/browse/KAN-7]\n"
            "Target branch: develop\n"
            "Mode: build\n"
            "{params}\n"
        ),
    )
    assert err is None, err
    assert spec is not None
    assert spec.source_branch == "feature/KAN-7"
    assert spec.target_branch == "develop"


def test_jira_wiki_inside_params():
    """Jira wiki links and same-line fields inside {params}."""
    desc = (
        "h3. Summary\n\nKANe\n\n"
        "{params}\n"
        "Repository: \n\n"
        "[https://gitlab.com/beratersari0/test_project.git|"
        "https://gitlab.com/beratersari0/test_project.git|smart-card]\n\n"
        "Source branch: develop Target branch: feature/KAN-4\n"
        "Mode: plan\n"
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
    desc = (
        "{params}\nRepository: https://g.com/a/b.git\n"
        "Source branch: main\nMode: plan\n{params}"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert err is None
    assert spec is not None
    assert spec.source_branch == "main"


def test_missing_fields_inside_params():
    desc = "{params}\nRepository: https://gitlab.example.com/g/r.git\nMode: plan\n{params}"
    spec, err = parse_issue_git_spec("hello", desc)
    assert spec is None
    assert err is not None
    assert "Source branch" in err


def test_mode_defaults_to_build():
    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Target branch: main
{params}
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert err is None
    assert spec is not None
    assert spec.mode == "build"


def test_invalid_mode_rejected():
    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Target branch: main
Mode: banana
{params}
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert spec is None
    assert err is not None
    assert "Mode" in err


def test_parse_issue_mode_helper():
    assert parse_issue_mode("", "no params") is None
    assert (
        parse_issue_mode(
            "",
            "{params}\nRepository: https://g.com/a/b.git\n"
            "Source branch: develop\nMode: BUILD\n{params}",
        )
        == "build"
    )
    assert (
        parse_issue_mode(
            "",
            "{params}\nRepository: https://g.com/a/b.git\n"
            "Source branch: develop\n{params}",
        )
        == "build"
    )


def test_invalid_url():
    desc = (
        "{params}\nRepository: not-a-url\nSource branch: develop\nMode: plan\n{params}\n"
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
        "Mode: plan\n"
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
