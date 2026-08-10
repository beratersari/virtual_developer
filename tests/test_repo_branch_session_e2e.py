"""E2E: same git repo + work branch resumes OpenCode session (CLI path).

1. First issue on repo@feature/shared creates ses_shared and binds it.
2. Second issue on the same repo+branch starts with that session_id
   (CLI would pass ``--session``).
3. Dashboard DELETE /api/opencode-sessions/{id} clears the bind.
4. Third issue starts cold (no session_id).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def _params(repo: str, source: str) -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: develop\n"
        "Mode: build\n"
        "{params}\n"
    )


@pytest.mark.asyncio
async def test_e2e_cli_same_repo_branch_resumes_then_dashboard_reset(
    tmp_path,
    monkeypatch,
    fake_jira,
    reporter,
    isolate_jira_agent_artifacts,
):
    monkeypatch.chdir(tmp_path)
    repo = "https://gitlab.com/acme/app.git"
    branch = "feature/shared"
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = MagicMock(return_value=None)
    proc._snapshot_delivery_baseline = MagicMock()

    mock_git = MagicMock()
    mock_git.remote_url = repo
    mock_git.work_branch = branch
    mock_git.source_branch = branch
    mock_git.target_branch = "develop"
    mock_git.get_working_directory.return_value = tmp_path
    mock_git.ensure_on_work_branch.return_value = True

    runner = AgentRunner(working_directory=tmp_path)
    seen: List[Dict[str, Any]] = []
    next_sid = {"n": 0}

    async def fake_run(task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append(
            {
                "issue_key": task.issue_key,
                "session_id": task.session_id,
                "attempt": kwargs.get("attempt_number"),
            }
        )
        next_sid["n"] += 1
        sid = task.session_id or f"ses_new_{next_sid['n']}"
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": f"Session: {sid}\ndone\n",
            "stderr": "",
            "session_file": str(tmp_path / f"{task.issue_key}.log"),
            "opencode_session_id": sid,
            "progress": 100,
        }

    monkeypatch.setattr(runner, "run_agent", fake_run)
    from tests.test_opencode_sessions import _make_session_db

    session_db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_new_1", "directory": str(tmp_path), "title": "KAN-A: x"}],
    )
    monkeypatch.setattr(
        "src.opencode_sessions._default_db_path", lambda: session_db
    )

    async def fake_prepare(state):
        proc._contexts[state.issue_key] = {"git": mock_git, "runner": runner}
        proc.git_manager = mock_git
        proc.agent_runner = runner
        return mock_git

    monkeypatch.setattr(proc, "_prepare_git_workspace", fake_prepare)

    live = MagicMock()
    live.agent_task_timeout_seconds = 30
    live.agent_task_max_retries = 0
    live.default_agent = "atlas"
    monkeypatch.setattr("src.config.get_settings", lambda: live)

    async def run_issue(key: str) -> None:
        sm.create_state(key, f"work {key}", _params(repo, branch))
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.opencode_cli = "opencode"
            s.opencode_run_mode = "cli"
            s.default_model = "opencode/x"
            s.agent_task_timeout_seconds = 30
            s.agent_task_max_retries = 0
            s.agent_task_retry_delay_seconds = 0
            s.agent_task_retry_backoff_multiplier = 1.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            await proc._start_execution_workflow(sm.get_state(key))

    await run_issue("KAN-A")
    assert seen[0]["session_id"] is None
    first_sid = "ses_new_1"
    bound = binds.get(repo, branch, "develop")
    assert bound is not None
    assert bound["session_id"] == first_sid
    assert bound["issue_key"] == "KAN-A"
    assert bound.get("working_directory")
    # Second issue continues because both jobs use the same clone folder
    # (stable temp dir) and OpenCode still lists ses_new_1 under that dir.

    # CLI argv would include --session for a real run_agent
    cmd = runner._build_command(
        AgentTask(
            description="check",
            prompt="do it",
            agent="atlas",
            issue_key="KAN-B",
            session_id=first_sid,
        ),
        tmp_path / "cmd.log",
    )
    assert "--session" in cmd
    assert first_sid in cmd

    await run_issue("KAN-B")
    assert seen[1]["issue_key"] == "KAN-B"
    assert seen[1]["session_id"] == first_sid
    assert binds.get(repo, branch, "develop")["issue_key"] == "KAN-B"

    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)
    listing = client.get("/api/opencode-sessions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["sessions"][0]["session_id"] == first_sid
    bind_id = body["sessions"][0]["bind_id"]

    reset = client.delete(f"/api/opencode-sessions/{bind_id}")
    assert reset.status_code == 200
    assert reset.json()["ok"] is True
    empty = client.get("/api/opencode-sessions").json()
    assert empty["total"] == 0
    tomb = binds.get(repo, branch, "develop")
    assert tomb is not None
    assert not (tomb.get("session_id") or "").strip()
    assert first_sid in (tomb.get("forgotten_session_ids") or [])

    # Tombstone remains so discovery cannot rebind; reset again is idempotent.
    gone = client.delete(f"/api/opencode-sessions/{bind_id}")
    assert gone.status_code == 200

    await run_issue("KAN-C")
    assert seen[2]["issue_key"] == "KAN-C"
    assert seen[2]["session_id"] is None
    rebound = binds.get(repo, branch, "develop")
    assert rebound is not None
    assert rebound["session_id"] == "ses_new_3"
    assert rebound["session_id"] != first_sid

    st_a = sm.get_state("KAN-A")
    st_b = sm.get_state("KAN-B")
    assert st_a is not None and st_a.status == TaskStatus.COMPLETED
    assert st_b is not None and st_b.status == TaskStatus.COMPLETED
