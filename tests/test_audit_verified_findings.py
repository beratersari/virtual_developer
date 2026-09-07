"""Proof tests for findings verified against current source + real job history.

These encode the *correct* behaviour. A failure means the production bug is
still present. No live network.

Evidence sources (do not treat comments as gospel):
- ``.jira-agent/jobs`` error strings from KAN-7 / KAN-5 (wiki-mangled branches)
- ``src/issue_git_spec.py`` branch regex + ``_expand_links``
- ``src/dashboard/service.py`` ``build_queue`` / job session overlay
- ``src/orchestrator/agent_runner.py`` ``max_retries`` handling
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.issue_git_spec import parse_issue_git_spec
from src.jira.client import JiraClient


REAL_GITLAB = "https://gitlab.com/beratersari0/test_project.git"


def _params(*, repo=REAL_GITLAB, source="develop", target="main", mode="build") -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        f"Mode: {mode}\n"
        "{params}\n"
    )


def test_jira_issue_key_brackets_in_branch_must_parse():
    """Cloud auto-links issue keys. Stored wiki is often ``feature/[KAN-7]``.

    Production job on KAN-7 failed with:
    ``Parsed value: feature/[KAN-7]`` — the parser must strip the brackets
    and keep ``feature/KAN-7``.
    """
    spec, err = parse_issue_git_spec(
        "KANe",
        _params(source="feature/[KAN-7]", target="main"),
    )
    assert err is None, err
    assert spec is not None
    assert spec.source_branch == "feature/KAN-7"
    assert spec.target_branch == "main"
    assert spec.repository_url == REAL_GITLAB


def test_jira_issue_key_wiki_link_in_branch_must_parse():
    """``[KAN-7|https://.../browse/KAN-7]`` must not become a URL-as-branch."""
    spec, err = parse_issue_git_spec(
        "KANe",
        _params(
            source="feature/[KAN-7|https://beratersari0.atlassian.net/browse/KAN-7]",
            target="develop",
        ),
    )
    assert err is None, err
    assert spec is not None
    assert spec.source_branch == "feature/KAN-7"
    assert spec.target_branch == "develop"


def test_smart_link_repo_from_real_kan7_description_still_parses():
    """Exact {params} shape stored on live KAN-7 (Jira smart-link)."""
    desc = (
        "{params}\n"
        "Repository: [https://gitlab.com/beratersari0/test_project.git|"
        "https://gitlab.com/beratersari0/test_project.git|smart-link] \n"
        "Source branch: feature/KAN-1905\n"
        "Target branch: main\n"
        "Mode: build\n"
        "Model: muse-spark-1.2-contributor-free\n"
        "Backend: codex\n"
        "\n"
        "{params}\n"
        "\n"
        "12+18\n"
    )
    spec, err = parse_issue_git_spec("KANe", desc)
    assert err is None, err
    assert spec is not None
    assert spec.repository_url == REAL_GITLAB
    assert spec.source_branch == "feature/KAN-1905"
    assert spec.target_branch == "main"
    assert spec.mode == "build"
    assert spec.backend == "codex"


def test_create_issue_payload_uses_fields_wrapper():
    """Jira REST v2 requires ``{\"fields\": {...}}``. Regression of logical issue."""
    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            s.jira_email = ""
            c = JiraClient()
            c.client = http
            c.resolve_issuetype_ref = MagicMock(return_value={"name": "Task"})
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"key": "P-1"}
            resp.text = ""
            resp.raise_for_status = MagicMock()
            http.post.return_value = resp
            c.create_issue("PROJ", "sum", "desc")
            payload = http.post.call_args.kwargs.get("json") or {}
            assert "fields" in payload
            assert payload["fields"]["project"]["key"] == "PROJ"


def test_max_retries_zero_runs_once():
    """Callers passing max_retries=0 must not fall through to settings=5."""
    import asyncio

    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    runner = AgentRunner()
    calls = {"n": 0}

    async def fail(*_a, **_k):
        calls["n"] += 1
        return {
            "task_id": "t",
            "returncode": 1,
            "stdout": "",
            "stderr": "err",
            "session_file": None,
            "opencode_session_id": None,
            "progress": 0,
        }

    async def run():
        with patch.object(runner, "run_agent", side_effect=fail):
            with patch("src.orchestrator.agent_runner.settings") as s:
                s.agent_task_max_retries = 5
                s.agent_task_retry_delay_seconds = 0.01
                s.agent_task_retry_backoff_multiplier = 1.0
                s.agent_task_retry_on_timeout = True
                s.agent_task_retry_on_error = True
                s.agent_task_max_incomplete_retries = 0
                task = AgentTask(description="d", prompt="p", agent="a")
                await runner.run_agent_with_retry(task, max_retries=0)

    asyncio.run(run())
    assert calls["n"] == 1, f"max_retries=0 should run once, ran {calls['n']}"


def test_progress_percent_clamped():
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner()
    assert 0 <= runner._parse_progress("Progress: 150%") <= 100


def test_queue_count_must_not_double_count_listed_rows():
    """Jobs nav badge uses queued_count. build_queue currently adds
    list_items(limit=500) *and* increments again per returned row.
    Three waiting items must report 3, not 6.
    """
    from src.dashboard.service import build_queue
    from src.state.queue_store import WorkQueueStore

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        store = WorkQueueStore(queue_dir=Path(td))
        for i in range(3):
            store.enqueue(
                source="jira",
                issue_key=f"AUD-{i}",
                summary=f"s{i}",
                message="m",
                lock_key=f"lock_{i}",
            )
        payload = build_queue(store=store)
        waiting = [i for i in payload.items if i.status == "queued"]
        assert len(waiting) == 3
        assert payload.queued_count == 3, (
            f"queued_count={payload.queued_count} for 3 queued rows "
            "(double-count in build_queue)"
        )


def test_adf_cloud_description_must_still_parse_params():
    """Cloud can return description as ADF. Processor does str(dict),
    which destroys {params}. The intake path must unwrap text nodes.
    """
    from src.dashboard.service import _jira_plain_text
    from src.issue_git_spec import parse_issue_git_spec

    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "{params}"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": f"Repository: {REAL_GITLAB}",
                    }
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Source branch: develop"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Target branch: main"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Mode: build"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "{params}"}],
            },
        ],
    }
    # Current processor path
    broken = str(adf)
    spec_broken, err_broken = parse_issue_git_spec("s", broken)
    # Dashboard already unwraps ADF — intake must do the same
    unwrapped = _jira_plain_text(adf)
    spec, err = parse_issue_git_spec("s", unwrapped)
    assert spec is not None and err is None, (
        f"ADF unwrap must yield a valid spec; got err={err!r} text={unwrapped!r}"
    )
    assert spec.repository_url.startswith("https://gitlab.com/beratersari0/")
    # If someone wires processor to str(dict), this documents the failure mode
    assert spec_broken is None, "str(ADF) accidentally parsed — update this test"
    assert err_broken is not None


def test_frontend_copy_matches_run_scoped_and_waiting_plan():
    """Static UI contracts for the operator-facing copy/filter fixes."""
    from pathlib import Path

    root = Path("web/src")
    poll = (root / "pages/poll/PollPage.tsx").read_text(encoding="utf-8")
    assert "This cycle" in poll
    assert ">Queued<" not in poll
    settings = (root / "pages/settings/SettingsPage.tsx").read_text(encoding="utf-8")
    assert "written to .env" in settings
    assert "Saved here, not to .env" not in settings
    status = (root / "util/status.ts").read_text(encoding="utf-8")
    active = status.split("case 'active':", 1)[1].split("case 'queue':", 1)[0]
    assert "plan_ready" not in active
    jobs_table = (root / "pages/jobs/JobsTable.tsx").read_text(encoding="utf-8")
    job_overview = (root / "pages/jobs/JobOverview.tsx").read_text(encoding="utf-8")
    issue_detail = (root / "pages/issues/IssueDetailPage.tsx").read_text(encoding="utf-8")
    assert "progress_percentage" not in jobs_table
    assert 'label="Progress"' not in job_overview
    assert 'label="Progress"' not in issue_detail

