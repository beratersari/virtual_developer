"""Real integration proofs for daily-usage defects found in a full-tree review.

No MagicMock / unittest.mock. Each case drives production code over real
HTTP (simulated Jira or the dashboard app), real settings persistence, or
the real OpenCode assessment pipeline.

A failure here is an operator-facing defect: plan→build never starts,
Azure/GitLab on-prem credentials miss, or an unattended job burns its
timeout instead of recovering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.schemas import AzureHostCredentialUpdate, SettingsUpdate
from src.dashboard.service import apply_settings_update
from src.git_manager import GitManager
from src.jira.poller import JiraPoller
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus

pytest_plugins = ["tests.test_live_jira_session_reuse"]

from tests.test_live_jira_session_reuse import TRIGGER, _allow_file_origin


BOT = TRIGGER


def _params(repo: str, *, mode: str = "plan") -> str:
    return (
        "Daily-usage critical e2e.\n"
        "{params}\n"
        f"Repository: {repo}\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        f"Mode: {mode}\n"
        "{params}\n"
    )


def _wire(tmp_path: Path, monkeypatch, board, repo: str):
    _allow_file_origin(monkeypatch)
    work = tmp_path / "run"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(settings, "temp_dir_base", work / ".temp")
    monkeypatch.setattr(settings, "jira_board_id", "1")
    monkeypatch.setattr(settings, "trigger_assignee_names", BOT)
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = board
    proc.reporter = JiraReporter(client=board)

    poller = JiraPoller(
        client=board, interval_seconds=1, board_id="1", state_manager=sm
    )
    pending: list = []
    poller._handler = pending.append
    proc._poller = poller
    return proc, sm, poller, pending, repo


async def _drain(proc: JobProcessor, poller: JiraPoller, pending: list) -> list[str]:
    keys: list[str] = []
    issues = poller.poll_board()
    for issue in issues:
        key = issue["key"]
        poller.process_issue(issue, poller.dispatch_as_update(key))
        poller._seen_issues.add(key)
        keys.append(key)
    while pending:
        event = pending.pop(0)
        await proc.process_event(event)
    return keys


def _comment_text(board, key: str) -> str:
    comments = board.get_comments(key) if hasattr(board, "get_comments") else []
    return "\n".join(str(c.get("body") or c) for c in comments)


def _azure_pr_comment_payload(*, note: str, unique_name: str) -> dict:
    return {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {
                "content": note,
                "author": {
                    "uniqueName": unique_name,
                    "displayName": "Alice",
                },
            },
            "pullRequest": {
                "pullRequestId": 44,
                "title": "feat(KAN-9): fix login",
                "description": "",
                "sourceRefName": "refs/heads/feature/login",
                "targetRefName": "refs/heads/develop",
                "url": "http://tfs.corp:8080/tfs/DefaultCollection/_git/app/pullrequest/44",
                "repository": {
                    "id": "repo-1",
                    "name": "app",
                    "remoteUrl": "http://tfs.corp:8080/tfs/DefaultCollection/_git/app",
                    "project": {"name": "DefaultCollection"},
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Plan → build on the same ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_execute_missing_plan_comments_and_retries(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    """Operator renamed plan_ready → plan_execute but the plan file is gone.

    Daily expectation: Jira gets an ERROR comment, and the next poll still
    tries to implement (or at least does not silently latch forever).
    """
    board, _srv = sim_jira
    proc, sm, poller, pending, repo = _wire(
        tmp_path, monkeypatch, board, "https://gitlab.example.com/acme/app.git"
    )

    created = board.create_issue(
        summary="Implement the plan",
        description=_params(repo, mode="plan"),
        assignee=BOT,
        labels=["plan_execute"],
    )
    key = created["key"] if isinstance(created, dict) else created.get("key")
    board.update_issue(key, fields={"status": "In Progress"})

    sm.create_state(key, "Implement the plan", _params(repo, mode="plan"))
    sm.update_state(key, status=TaskStatus.PLAN_READY, plan_path="")

    await _drain(proc, poller, pending)
    first_comments = _comment_text(board, key)
    live = sm.get_state(key)
    assert live is not None
    assert live.status != TaskStatus.EXECUTING, "must not start a build without a plan"
    assert "error" in first_comments.lower() or "plan" in first_comments.lower(), (
        "plan_execute with no plan file posted nothing the operator can see; "
        f"comments were: {first_comments!r}"
    )

    second = poller.poll_board()
    second_keys = [i["key"] for i in second]
    assert key in second_keys, (
        "poller latched plan_execute after a silent skip; the operator cannot "
        "retry implement without removing and re-adding the label"
    )


@pytest.mark.xfail(
    strict=True,
    reason="To Do rework after failed plan_execute re-plans from Mode: plan",
)
@pytest.mark.asyncio
async def test_failed_plan_execute_todo_rework_implements(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    """After a failed implement, To Do + bot must retry the *build*, not re-plan.

    The ticket still says Mode: plan (documented). plan_execute already
    selected implement. Re-planning loses the operator's implement signal.
    """
    board, _srv = sim_jira
    missing = (tmp_path / "no-such-origin.git").resolve().as_uri()
    proc, sm, poller, pending, _ = _wire(tmp_path, monkeypatch, board, missing)

    created = board.create_issue(
        summary="Failed implement retry",
        description=_params(missing, mode="plan"),
        assignee=BOT,
        labels=["plan_executed"],
    )
    key = created["key"] if isinstance(created, dict) else created.get("key")
    # Stay on To Do so primary rework fires (In Progress transition will move it).

    sm.create_state(key, "Failed implement retry", _params(missing, mode="plan"))
    sm.update_state(
        key,
        status=TaskStatus.ERROR,
        error_message="previous plan_execute timed out",
        metadata={
            "requeue_eligible": True,
            "workflow_type": "execution",
        },
    )

    await _drain(proc, poller, pending)
    live = sm.get_state(key)
    assert live is not None
    wf = str((live.metadata or {}).get("workflow_type") or "")
    assert wf == "execution", (
        f"To Do rework after a failed plan_execute started {wf!r} "
        "(Mode: plan) instead of implementing the existing plan"
    )
    assert live.status != TaskStatus.PLANNING
    assert live.status != TaskStatus.PLAN_READY


# ---------------------------------------------------------------------------
# Settings → clone credentials (on-prem host:port)
# ---------------------------------------------------------------------------


def test_settings_azure_collection_url_keeps_port_for_clone(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Pasting http://tfs.corp:8080/tfs/DefaultCollection must still match clone."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JIRA_HOST=https://jira.example\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: tmp_path / "runtime_settings.json",
    )
    monkeypatch.setattr(settings, "jira_host", "https://jira.onprem.example")
    monkeypatch.setattr(settings, "jira_email", "")
    monkeypatch.setattr(settings, "azure_host_pats", "")
    monkeypatch.setattr(settings, "azure_pat", "")
    monkeypatch.setattr(settings, "azure_allowed_hosts", "")
    if hasattr(settings, "set_azure_host_pat_map"):
        settings.set_azure_host_pat_map({})

    apply_settings_update(
        SettingsUpdate(
            azure_credentials=[
                AzureHostCredentialUpdate(
                    host="http://tfs.corp:8080/tfs/DefaultCollection",
                    pat="onprem-azure-pat",
                )
            ]
        )
    )

    clone_url = "http://tfs.corp:8080/tfs/DefaultCollection/_git/app"
    host = GitManager._host_from_url(clone_url)
    assert host == "tfs.corp:8080"
    found = settings.azure_pat_for_host(host)
    assert found == "onprem-azure-pat", (
        f"Settings saved host map {settings.azure_host_pat_map()!r} "
        f"does not contain clone host {host!r}; daily Azure Server clones "
        "will refuse the PAT"
    )


# ---------------------------------------------------------------------------
# Azure uniqueName DOMAIN\\user
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AZURE_BOT_MENTIONS=DOMAIN\\user is truncated to 'domain'",
)
def test_azure_unique_name_mention_starts_job(monkeypatch):
    """UI says unique name is valid. DOMAIN\\yaver must match @yaver comments."""
    from src.azure.webhook import decide_azure_comment_webhook

    monkeypatch.setattr(settings, "azure_webhook_enabled", True)
    monkeypatch.setattr(settings, "azure_webhook_secret", "hook-secret")
    monkeypatch.setattr(settings, "jira_projects", "KAN")

    decision = decide_azure_comment_webhook(
        _azure_pr_comment_payload(
            note="@yaver please fix the failing test",
            unique_name="CORP\\alice",
        ),
        headers={"X-Azure-Token": "hook-secret"},
        enabled=True,
        secret="hook-secret",
        bot_mentions=["CORP\\yaver"],
        bot_usernames=["CORP\\yaver"],
        jira_project_keys=["KAN"],
    )
    assert decision.accepted, (
        f"Azure uniqueName bot mention was rejected ({decision.reason!r}); "
        "on-prem PR comments never start a job"
    )


@pytest.mark.xfail(
    strict=True,
    reason="DOMAIN\\alice author normalizes to the same token as DOMAIN\\yaver",
)
def test_azure_domain_author_is_not_treated_as_the_bot(monkeypatch):
    """DOMAIN\\alice must not be dropped as 'comment from bot' when bot is DOMAIN\\yaver."""
    from src.azure.webhook import decide_azure_comment_webhook

    decision = decide_azure_comment_webhook(
        _azure_pr_comment_payload(
            note="@yaver please fix the failing test",
            unique_name="CORP\\alice",
        ),
        headers={"X-Azure-Token": "hook-secret"},
        enabled=True,
        secret="hook-secret",
        bot_mentions=["CORP\\yaver"],
        bot_usernames=["CORP\\yaver"],
        jira_project_keys=["KAN"],
    )
    assert decision.reason != "ignored comment from bot user", (
        "every AD user on CORP\\ normalized to 'corp' and was treated as the bot"
    )
    assert decision.accepted, decision.reason


@pytest.mark.xfail(
    strict=True,
    reason="POST /yaver/webhook/azure rejects uniqueName bot mentions",
)
def test_azure_webhook_http_unique_name_is_accepted(monkeypatch):
    """Full dashboard POST /yaver/webhook/azure path (real ASGI, real secret header)."""
    monkeypatch.setattr(settings, "azure_webhook_enabled", True)
    monkeypatch.setattr(settings, "azure_webhook_secret", "hook-secret")
    monkeypatch.setattr(settings, "azure_bot_mentions", "CORP\\yaver")
    monkeypatch.setattr(settings, "jira_projects", "KAN")

    app = create_dashboard_app(processor=None)
    client = TestClient(app)
    response = client.post(
        "/yaver/webhook/azure",
        headers={"X-Azure-Token": "hook-secret"},
        json=_azure_pr_comment_payload(
            note="@yaver please fix the failing test",
            unique_name="CORP\\alice",
        ),
    )
    body = response.json()
    assert response.status_code != 401
    assert body.get("reason") != "bot not mentioned", body
    assert body.get("reason") != "ignored comment from bot user", body


# ---------------------------------------------------------------------------
# OpenCode serve: structured question while status=busy
# ---------------------------------------------------------------------------


def test_busy_question_tool_leaves_compact_wait():
    """Last-turn pending question tool (finish=tool-calls) is a live ask.

    Auto-resume cannot answer a human. Busy compact-wait must leave for the
    unattended nudge instead of burning AGENT_TASK_TIMEOUT_SECONDS.
    """
    from src.opencode_serve import last_turn_is_live_question
    from src.opencode_sessions import assess_session_completeness

    assessment = assess_session_completeness(
        "ses_daily_q",
        messages=[
            {
                "role": "assistant",
                "finish": "tool-calls",
                "parts": [
                    {
                        "type": "tool",
                        "tool": "question",
                        "state": {
                            "status": "pending",
                            "input": {
                                "questions": [
                                    {
                                        "header": "DB",
                                        "question": "Postgres or SQLite?",
                                        "options": ["Postgres", "SQLite"],
                                    }
                                ]
                            },
                        },
                    }
                ],
            }
        ],
        todos=[{"status": "pending"}],
    )
    assert assessment.get("assistant_asked_question") is True
    assert last_turn_is_live_question(assessment) is True, (
        "busy compact-wait treats a pending question tool as mid-work "
        f"(finish={assessment.get('last_finish')!r}); the job will sit until timeout"
    )
