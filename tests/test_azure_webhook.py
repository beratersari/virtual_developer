"""Azure DevOps Server 2022.2 PR comment webhook — parse, mention, client, processor, dashboard.

Mirrors GitLab MR-comment intake. GitLab paths must stay unchanged.
"""

from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.azure.keys import (
    azure_issue_key,
    is_azure_issue_key,
    resolve_pr_issue_key,
)
from src.azure.mentions import (
    html_mention_names,
    note_mentions_bot,
    parse_mention_list,
    strip_azure_bot_mentions,
)
from src.azure.webhook import (
    decide_azure_comment_webhook,
    decide_azure_pr_webhook,
    parse_azure_git_url,
    parse_pull_request_url,
)
from src.gitlab.webhook import validate_webhook_token
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def _pr_comment_payload(
    *,
    note: str = "@yaver what does login do?",
    unique_name: str = "DOMAIN\\alice",
    display_name: str = "Alice",
    source: str = "refs/heads/feature/login",
    target: str = "refs/heads/develop",
    comment_id: int = 77,
    title: str = "Add login",
    description: str = "desc",
    pr_id: int = 4,
    status: str = "active",
) -> dict:
    return {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {
                "id": comment_id,
                "parentCommentId": 0,
                "author": {
                    "displayName": display_name,
                    "uniqueName": unique_name,
                    "id": "user-1",
                },
                "content": note,
                "commentType": 1,
            },
            "pullRequest": {
                "pullRequestId": pr_id,
                "status": status,
                "title": title,
                "description": description,
                "sourceRefName": source,
                "targetRefName": target,
                "repository": {
                    "id": "repo-guid",
                    "name": "demo",
                    "remoteUrl": (
                        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
                    ),
                    "project": {"id": "proj-guid", "name": "Demo"},
                    "_links": {
                        "web": {
                            "href": (
                                "https://tfs.example.com/tfs/DefaultCollection/"
                                "Demo/_git/demo/pullrequest/4"
                            )
                        }
                    },
                },
            },
        },
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/"
            }
        },
    }


def _pr_lifecycle_payload(
    *,
    event_type: str = "git.pullrequest.updated",
    status: str = "completed",
    title: str = "feat(KAN-12): add login",
    source: str = "refs/heads/feature/KAN-12",
    target: str = "refs/heads/develop",
    pr_id: int = 4,
) -> dict:
    return {
        "eventType": event_type,
        "resource": {
            "pullRequestId": pr_id,
            "status": status,
            "title": title,
            "description": "desc",
            "sourceRefName": source,
            "targetRefName": target,
            "repository": {
                "id": "repo-guid",
                "name": "demo",
                "remoteUrl": (
                    "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
                ),
                "project": {"name": "Demo"},
            },
        },
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/"
            }
        },
    }


def test_azure_issue_key_stable():
    assert azure_issue_key("DefaultCollection/Demo/demo", 4) == (
        "AZ-DEFAULTCOLLECTION-DEMO-DEMO-4"
    )
    assert is_azure_issue_key("AZ-DEFAULTCOLLECTION-DEMO-DEMO-4")
    assert not is_azure_issue_key("GL-ACME-DEMO-4")
    assert not is_azure_issue_key("KAN-1")


def test_resolve_pr_issue_key_prefers_jira_then_closes_then_az():
    keys = ["KAN", "PROJ"]
    assert (
        resolve_pr_issue_key(
            pr_title="feat(KAN-12): x",
            project_path="DefaultCollection/Demo/demo",
            pr_id=4,
            project_keys=keys,
        )
        == "KAN-12"
    )
    assert (
        resolve_pr_issue_key(
            pr_title="Add login",
            pr_description="Closes KAN-99",
            project_path="DefaultCollection/Demo/demo",
            pr_id=4,
            project_keys=keys,
        )
        == "KAN-99"
    )
    assert (
        resolve_pr_issue_key(
            pr_title="Add login",
            project_path="DefaultCollection/Demo/demo",
            pr_id=4,
            project_keys=keys,
        )
        == "AZ-DEFAULTCOLLECTION-DEMO-DEMO-4"
    )


def test_mention_plain_and_html():
    assert parse_mention_list("@yaver, DevBot") == ["yaver", "devbot"]
    assert note_mentions_bot("@yaver please look", ["yaver"])
    assert note_mentions_bot("hey @Yaver!", ["@yaver"])
    assert not note_mentions_bot("no one tagged", ["yaver"])
    html = (
        '<a href="#" data-vss-mention="version:2.0,guid">@Yaver</a> please look'
    )
    assert "yaver" in html_mention_names(html)
    assert note_mentions_bot(html, ["yaver"])
    assert (
        strip_azure_bot_mentions("@yaver explain this fn", ["yaver"])
        == "explain this fn"
    )
    stripped = strip_azure_bot_mentions(html + " explain", ["yaver"])
    assert "explain" in stripped
    assert "@yaver" not in stripped.lower()


def test_webhook_token_same_as_gitlab():
    assert validate_webhook_token("abc", "abc")
    assert validate_webhook_token("x", "")
    assert not validate_webhook_token("nope", "secret")


def test_parse_azure_git_and_pr_urls():
    parsed = parse_azure_git_url(
        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    )
    assert parsed is not None
    assert parsed["host"] == "tfs.example.com"
    assert parsed["project"] == "Demo"
    assert parsed["repository"] == "demo"
    assert parsed["collection_url"].endswith("/tfs/DefaultCollection")
    assert parse_pull_request_url(
        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/pullrequest/12"
    ) == (
        "tfs.example.com",
        "tfs/DefaultCollection/Demo/demo",
        12,
    )
    assert parse_pull_request_url("") is None
    assert parse_azure_git_url("") is None


def test_decide_accepts_pr_mention():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
        jira_project_keys=["KAN"],
    )
    assert d.accepted
    assert d.event is not None
    assert d.event.issue_key == "AZ-TFS-DEFAULTCOLLECTION-DEMO-DEMO-4"
    assert d.event.source_branch == "feature/login"
    assert d.event.target_branch == "develop"
    assert d.event.prompt == "what does login do?"
    assert d.event.host == "tfs.example.com"
    assert d.event.collection_url.endswith("/tfs/DefaultCollection")


def test_decide_uses_jira_key_from_pr_title():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(title="feat(KAN-1905): wire auth"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
        jira_project_keys=["KAN", "PROJ"],
    )
    assert d.accepted
    assert d.event is not None
    assert d.event.issue_key == "KAN-1905"
    assert not is_azure_issue_key(d.event.issue_key)


def test_decide_accepts_basic_auth_password_as_secret():
    token = base64.b64encode(b":s").decode("ascii")
    d = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"Authorization": f"Basic {token}"},
        secret="s",
        bot_mentions=["@yaver"],
        jira_project_keys=["KAN"],
    )
    assert d.accepted


def test_decide_accepts_bearer_secret():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"Authorization": "Bearer s"},
        secret="s",
        bot_mentions=["@yaver"],
        jira_project_keys=["KAN"],
    )
    assert d.accepted


def test_decide_ignores_non_comment_and_no_mention():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(note="lgtm"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert not d.accepted
    assert d.reason == "bot not mentioned"
    d2 = decide_azure_comment_webhook(
        _pr_comment_payload(note="*Yaver*\n\nalready replied"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert not d2.accepted
    assert d2.reason == "ignored bot reply"
    d3 = decide_azure_comment_webhook(
        _pr_comment_payload(note="@yaver ping", unique_name="yaver", display_name="Yaver"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
        bot_usernames=["yaver"],
    )
    assert not d3.accepted
    assert d3.reason == "ignored comment from bot user"
    d4 = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"X-Azure-Token": "bad"},
        secret="good",
        bot_mentions=["@yaver"],
    )
    assert d4.http_status == 401
    d5 = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"X-Azure-Token": "s"},
        secret="s",
        enabled=False,
        bot_mentions=["@yaver"],
    )
    assert not d5.accepted
    assert "disabled" in d5.reason
    d6 = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"X-Azure-Token": "s"},
        secret="",
        bot_mentions=["@yaver"],
    )
    assert d6.http_status == 401
    d7 = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=[],
    )
    assert not d7.accepted
    assert "AZURE_TRIGGER_USER" in d7.reason
    d8 = decide_azure_comment_webhook(
        _pr_lifecycle_payload(),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert not d8.accepted
    assert "ignored event" in d8.reason


def test_decide_empty_comment():
    payload = _pr_comment_payload(note="   ")
    d = decide_azure_comment_webhook(
        payload,
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert not d.accepted
    assert d.reason == "empty comment"


def test_decide_missing_pr_id():
    payload = _pr_comment_payload()
    payload["resource"]["pullRequest"]["pullRequestId"] = 0
    d = decide_azure_comment_webhook(
        payload,
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert not d.accepted
    assert "pull request id" in d.reason


def test_decide_missing_branches():
    payload = _pr_comment_payload(source="", target="")
    d = decide_azure_comment_webhook(
        payload,
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert not d.accepted


def test_azure_client_posts_thread(monkeypatch):
    from src.azure.client import AzureDevOpsClient

    captured = {}

    class FakeResp:
        status_code = 201
        content = b'{"id": 9}'
        text = '{"id": 9}'

        def json(self):
            return {"id": 9}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, params=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["params"] = params
            return FakeResp()

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr("src.azure.client.httpx.Client", FakeClient)
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat="azpat-test",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    out = c.post_pr_comment(
        project="Demo",
        repository="demo",
        pr_id=4,
        body="*Yaver*\n\nhi",
    )
    assert out and out["id"] == 9
    assert "/pullrequests/4/threads" in captured["url"]
    assert captured["headers"]["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(captured["headers"]["Authorization"][6:]).decode()
    assert decoded == "pat:azpat-test"
    assert not decoded.startswith(":")
    assert "oauth2" not in decoded
    assert captured["json"]["comments"][0]["content"].startswith("*Yaver*")


def test_is_azure_triggered_uses_source_metadata():
    from src.processor import JobProcessor
    from src.state.models import JiraAgentState, TaskStatus as TS

    proc = object.__new__(JobProcessor)
    sm = MagicMock()
    proc.state_manager = sm
    jira_state = JiraAgentState(
        issue_key="KAN-12",
        issue_summary="s",
        description="d",
        status=TS.EXECUTING,
        metadata={"source": "jira"},
    )
    azure_state = JiraAgentState(
        issue_key="KAN-12",
        issue_summary="s",
        description="d",
        status=TS.EXECUTING,
        metadata={"source": "azure", "workflow_type": "azure_pr"},
    )
    assert proc._is_azure_triggered("AZ-DEMO-4") is True
    assert proc._is_azure_triggered("KAN-12", azure_state) is True
    assert proc._is_azure_triggered("KAN-12", jira_state) is False
    assert proc._is_gitlab_triggered("KAN-12", azure_state) is False
    assert proc._is_git_comment_triggered("KAN-12", azure_state) is True
    assert proc._is_git_comment_triggered("KAN-12", jira_state) is False


def test_decide_pr_lifecycle_completed_and_abandoned():
    d = decide_azure_pr_webhook(
        _pr_lifecycle_payload(),
        headers={"X-Azure-Token": "s"},
        secret="s",
        jira_project_keys=["KAN"],
    )
    assert d.accepted
    assert d.event is not None
    assert d.event.is_merged
    assert d.event.should_delete_clone
    assert d.event.issue_key == "KAN-12"

    d2 = decide_azure_pr_webhook(
        _pr_lifecycle_payload(status="abandoned"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        jira_project_keys=["KAN"],
    )
    assert d2.accepted
    assert d2.event is not None
    assert not d2.event.is_merged
    assert d2.event.is_closed
    assert d2.event.should_delete_clone

    d3 = decide_azure_pr_webhook(
        _pr_lifecycle_payload(event_type="git.pullrequest.created", status="active"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        jira_project_keys=["KAN"],
    )
    assert d3.accepted
    assert d3.event is not None
    assert not d3.event.should_delete_clone


def test_decide_pr_lifecycle_merged_event_type():
    d = decide_azure_pr_webhook(
        _pr_lifecycle_payload(event_type="git.pullrequest.merged", status=""),
        headers={"X-Azure-Token": "s"},
        secret="s",
        jira_project_keys=["KAN"],
    )
    assert d.accepted
    assert d.event is not None
    assert d.event.is_merged


@pytest.mark.asyncio
async def test_complete_work_azure_source_skips_jira(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    sm.create_state("KAN-99", "feat(KAN-99): from PR", "d")
    sm.update_state(
        "KAN-99",
        status=TaskStatus.EXECUTING,
        metadata={"source": "azure", "workflow_type": "azure_pr"},
    )
    before = len(fake_jira.comments)
    await proc._complete_work(
        sm.get_state("KAN-99"),
        execution_summary="All tasks completed successfully.",
        agent_answer="Login uses JWT.",
    )
    assert sm.get_state("KAN-99").status == TaskStatus.COMPLETED
    assert len(fake_jira.comments) == before


@pytest.mark.asyncio
async def test_processor_azure_posts_reply_and_pushes(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    clone = tmp_path / "clone"
    clone.mkdir()
    git = MagicMock()
    git.remote_url = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    git.work_branch = "feature/login"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    git.ensure_feature_branch.return_value = "feature/login"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.side_effect = [
        "aaa111baseline",
        "bbb222newhead",
        "bbb222newhead",
    ]
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "[AZ] fix: login"
    git.get_last_commit_message.return_value = "fix login"
    git.build_commit_url.return_value = (
        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/commit/bbb222newhead"
    )

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "Fixed the login bug.",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
        }
    )
    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 101}

    decision = decide_azure_comment_webhook(
        _pr_comment_payload(note="@yaver please fix the login bug"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert decision.event

    def fake_init(*_a, **_k):
        proc._contexts["AZ-TFS-DEFAULTCOLLECTION-DEMO-DEMO-4"] = {
            "git": git,
            "runner": runner,
        }
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch.object(reporter, "post_progress_update") as jira_progress, patch(
        "src.azure.client.AzureDevOpsClient.post_pr_comment", fake_post
    ):
        await proc.handle_azure_pr_comment(decision.event)

    st = sm.get_state("AZ-TFS-DEFAULTCOLLECTION-DEMO-DEMO-4")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    git.push.assert_called()
    git.create_merge_request.assert_not_called()
    jira_progress.assert_not_called()
    body = posted.get("body") or ""
    assert "*Yaver*" in body
    assert "Fixed the login bug." in body
    assert "Pushed new commits" in body
    assert (st.metadata or {}).get("source") == "azure"
    assert (st.metadata or {}).get("delivery_status") == "delivered"
    assert not any("Work Completed" in (c.get("body") or "") for c in fake_jira.comments)


def test_dashboard_webhook_endpoint_dispatches(tmp_path, monkeypatch, fake_jira):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_azure_comment = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "queue_id": "q_test",
            "issue_key": "AZ-TFS-DEFAULTCOLLECTION-DEMO-DEMO-4",
            "status": "queued",
        }
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/azure",
            json=_pr_comment_payload(),
            headers={"X-Azure-Token": "tok"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["issue_key"] == "AZ-TFS-DEFAULTCOLLECTION-DEMO-DEMO-4"
    assert body["queue_id"] == "q_test"
    assert proc.enqueue_azure_comment.await_count == 1


def test_dashboard_webhook_rejects_bad_secret(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    app = create_dashboard_app(processor=proc)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/azure",
        json=_pr_comment_payload(),
        headers={"X-Azure-Token": "nope"},
    )
    assert resp.status_code == 401


def test_dashboard_webhook_disabled(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", False)
    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    client = TestClient(create_dashboard_app(processor=proc))
    resp = client.post(
        "/webhooks/azure",
        json=_pr_comment_payload(),
        headers={"X-Azure-Token": "tok"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "disabled" in resp.json()["reason"]


def test_dashboard_pr_completed_deletes_clone(tmp_path, monkeypatch, fake_jira):
    from src.dashboard.api import create_dashboard_app
    from src.dashboard.temp_storage import reset_delete_jobs
    from src.processor import JobProcessor
    from src.state.job_store import job_store

    reset_delete_jobs()
    base = tmp_path / "t"
    clone = base / "repo_az_merged01"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("src.config.settings.temp_dir_base", base)
    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.jira_projects", "KAN")
    monkeypatch.chdir(tmp_path)

    job = job_store.create_job(issue_key="KAN-12", summary="add login")
    job_store.update_job(
        job["job_id"],
        working_directory=str(clone.resolve()),
        merge_request_url=(
            "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/pullrequest/4"
        ),
        azure_project="Demo",
        azure_pr_id=4,
        merge_request_state="active",
    )

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/azure",
            json=_pr_lifecycle_payload(),
            headers={"X-Azure-Token": "tok"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "pull_request"
    assert "repo_az_merged01" in body["deleted"]
    updated = job_store.get_job(job["job_id"])
    assert updated["merge_request_state"] == "completed"


def test_tfs_never_uses_leftover_gitlab_pat(monkeypatch):
    """A lone GITLAB_PAT must not authenticate a TFS /_git/ remote."""
    from src.config import Settings
    from src.git_manager import GitCloneError, GitManager

    s = Settings()
    s.set_azure_host_pat_map({})
    s.set_gitlab_host_pat_map({})
    s.gitlab_pat = "LEFTOVER-GITLAB-PAT"
    s.gitlab_allowed_hosts = ""
    s.azure_pat = ""
    s.azure_allowed_hosts = ""
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    tfs = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    gm.remote_url = tfs
    assert gm._pat_for_remote() == ""
    assert gm._remote_uses_azure_pat() is False
    with pytest.raises(GitCloneError) as exc:
        gm._assert_remote_host_allowed(tfs)
    assert "AZURE_HOST_PATS" in str(exc.value) or "Azure PAT" in str(exc.value)

    s.set_azure_host_pat_map({"tfs.example.com": "AZURE-ENV-PAT"})
    assert gm._pat_for_remote() == "AZURE-ENV-PAT"
    assert gm._remote_uses_azure_pat() is True
    gm._assert_remote_host_allowed(tfs)


def test_azure_leftover_pat_expands_like_gitlab():
    from src.config import Settings

    s = Settings(
        azure_host_pats="",
        azure_pat="legacy-az-pat",
        azure_allowed_hosts="tfs.example.com,tfs.internal:8080",
    )
    assert s.azure_pat_for_host("tfs.example.com") == "legacy-az-pat"
    assert s.azure_pat_for_host("tfs.internal:8080") == "legacy-az-pat"
    assert s.azure_pat_for_host("other.example") == ""
    assert s.azure_has_any_pat() is True

    mapped = Settings(
        azure_host_pats='{"tfs.example.com":"map-pat"}',
        azure_pat="legacy-should-not-win",
        azure_allowed_hosts="tfs.example.com",
    )
    assert mapped.azure_pat_for_host("tfs.example.com") == "map-pat"


def test_azure_leftover_pat_authenticates_tfs_remote(monkeypatch):
    from src.config import Settings
    from src.git_manager import GitManager

    s = Settings()
    s.set_azure_host_pat_map({})
    s.set_gitlab_host_pat_map({})
    s.azure_pat = "LEFTOVER-AZURE-PAT"
    s.azure_allowed_hosts = ""
    s.gitlab_pat = "LEFTOVER-GITLAB-PAT"
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    tfs = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    gm.remote_url = tfs
    assert gm._pat_for_remote() == "LEFTOVER-AZURE-PAT"
    assert gm._remote_uses_azure_pat() is True
    gm._assert_remote_host_allowed(tfs)
    gl = "https://gitlab.example.com/acme/demo.git"
    gm.remote_url = gl
    assert gm._pat_for_remote() == "LEFTOVER-GITLAB-PAT"
    assert gm._remote_uses_azure_pat() is False


def test_settings_save_azure_webhook_enable_and_secret(monkeypatch):
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update, build_settings_view

    s = Settings()
    s.azure_webhook_enabled = False
    s.azure_webhook_secret = ""
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr("src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None)
    monkeypatch.setattr("src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None)
    view = apply_settings_update(
        SettingsUpdate(
            azure_webhook_enabled=True,
            azure_webhook_secret="hook-secret",
            azure_bot_mentions="@yaver",
        )
    )
    assert s.azure_webhook_enabled is True
    assert s.azure_webhook_secret == "hook-secret"
    assert view.azure_webhook_enabled is True
    assert view.azure_webhook_secret_configured is True
    shown = build_settings_view()
    assert "hook-secret" not in shown.model_dump_json()


def test_settings_leftover_azure_pat_and_hosts(monkeypatch):
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    s = Settings()
    s.set_azure_host_pat_map({})
    s.azure_pat = ""
    s.azure_allowed_hosts = ""
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr("src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None)
    monkeypatch.setattr("src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None)
    apply_settings_update(
        SettingsUpdate(
            azure_pat="legacy-az",
            azure_allowed_hosts="tfs.example.com",
        )
    )
    assert s.azure_pat_for_host("tfs.example.com") == "legacy-az"


def test_azure_pat_not_sent_to_gitlab_host(monkeypatch):
    from src.config import Settings
    from src.git_manager import GitManager

    s = Settings()
    s.set_azure_host_pat_map({"tfs.example.com": "AZURE-ONLY-PAT"})
    s.set_gitlab_host_pat_map({"gitlab.example.com": "GITLAB-ONLY-PAT"})
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://gitlab.example.com/acme/demo.git"
    assert gm._pat_for_remote() == "GITLAB-ONLY-PAT"
    assert gm._remote_uses_azure_pat() is False
    gm.remote_url = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    assert gm._pat_for_remote() == "AZURE-ONLY-PAT"
    assert gm._remote_uses_azure_pat() is True
    gm.remote_url = "https://evil.example.com/repo.git"
    with pytest.raises(Exception) as exc:
        gm._assert_remote_host_allowed(gm.remote_url)
    assert "refused" in str(exc.value).lower() or "credentials" in str(exc.value).lower()


def test_azure_git_env_uses_pat_user_basic(monkeypatch, tmp_path):
    from src.azure.auth import azure_basic_auth
    from src.config import Settings
    from src.git_manager import GitManager

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver-data"))
    monkeypatch.setenv("VD_DATA_DIR", str(tmp_path / "yaver-data"))
    s = Settings()
    s.set_azure_host_pat_map({"tfs.example.com": "AZURE-SECRET-PAT"})
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    assert env["VD_GIT_AUTH"] == "azure"
    assert env["VD_GIT_ASKUSER"] == "pat"
    assert env["VD_GIT_PASSWORD"] == "AZURE-SECRET-PAT"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_SSL_NO_VERIFY"] == "1"
    values = [env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_")]
    keys = [env[k] for k in env if k.startswith("GIT_CONFIG_KEY_")]
    assert any(v == f"Authorization: {azure_basic_auth('AZURE-SECRET-PAT')}" for v in values)
    assert any(k.startswith("url.https://pat:AZURE-SECRET-PAT@tfs.example.com/") for k in keys)
    assert not any("oauth2:AZURE-SECRET-PAT" in (k + v) for k, v in zip(keys, values))
    assert not any("Authorization: Bearer" in v for v in values)
    decoded = None
    for v in values:
        if v.startswith("Authorization: Basic "):
            decoded = base64.b64decode(v.split(" ", 2)[2]).decode("ascii")
    assert decoded == "pat:AZURE-SECRET-PAT"
    argv = gm._azure_git_config_args()
    assert any(a.startswith("http.extraHeader=Authorization: Basic ") for a in argv)


def test_gitlab_git_env_still_uses_oauth2(monkeypatch, tmp_path):
    from src.config import Settings
    from src.git_manager import GitManager

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver-data"))
    monkeypatch.setenv("VD_DATA_DIR", str(tmp_path / "yaver-data"))
    s = Settings()
    s.set_gitlab_host_pat_map({"gitlab.example.com": "GL-SECRET-PAT"})
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://gitlab.example.com/acme/demo.git"
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    assert env.get("VD_GIT_AUTH") == "gitlab"
    keys = [env[k] for k in env if k.startswith("GIT_CONFIG_KEY_")]
    values = [env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_")]
    assert any("oauth2:GL-SECRET-PAT" in k for k in keys)
    assert any(v.startswith("Authorization: Basic ") for v in values)
    assert not any("Authorization: Bearer" in v for v in values)


def test_settings_apply_azure_credentials(monkeypatch, tmp_path):
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update, build_settings_view

    s = Settings()
    s.set_azure_host_pat_map({})
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr("src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None)
    monkeypatch.setattr("src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None)
    body = SettingsUpdate(
        azure_credentials=[{"host": "tfs.example.com", "pat": "az-pat-1"}],
        azure_bot_mentions="@yaver",
    )
    view = apply_settings_update(body)
    assert view.azure_pat_configured is True
    assert "tfs.example.com" in view.azure_allowed_hosts
    assert view.azure_bot_mentions == "yaver"
    assert view.azure_trigger_user == "yaver"
    assert s.azure_pat_for_host("tfs.example.com") == "az-pat-1"
    shown = build_settings_view()
    assert shown.azure_credentials[0].pat_configured is True
    assert not hasattr(shown.azure_credentials[0], "pat") or getattr(
        shown.azure_credentials[0], "pat", None
    ) in (None, "")


def test_probe_azure_requires_pat(monkeypatch):
    from src.azure_connection import probe_azure_connection
    from src.config import Settings

    s = Settings()
    s.set_azure_host_pat_map({})
    monkeypatch.setattr("src.azure_connection.settings", s)
    out = probe_azure_connection("tfs.example.com")
    assert out["ok"] is False
    assert "No PAT" in out["error"]


@pytest.mark.asyncio
async def test_enqueue_azure_dedup(tmp_path, monkeypatch, fake_jira):
    from src.processor import JobProcessor
    from src.state.queue_store import WorkQueueStore

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = WorkQueueStore(queue_dir=tmp_path / "q")
    proc.dispatch_queue = AsyncMock(return_value=0)
    decision = decide_azure_comment_webhook(
        _pr_comment_payload(),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    first = await proc.enqueue_azure_comment(decision.event)
    second = await proc.enqueue_azure_comment(decision.event)
    assert first["ok"] is True
    assert second.get("duplicate") is True
    assert first["queue_id"] == second["queue_id"]


def test_gitlab_webhook_unaffected_when_azure_enabled(monkeypatch):
    from src.gitlab.webhook import decide_gitlab_note_webhook

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "az")
    d = decide_gitlab_note_webhook(
        {
            "object_kind": "note",
            "user": {"username": "alice", "name": "Alice"},
            "project": {
                "id": 1,
                "path_with_namespace": "acme/demo",
                "http_url_to_repo": "https://gitlab.example.com/acme/demo.git",
            },
            "object_attributes": {
                "id": 1,
                "note": "@berat_ai hi",
                "noteable_type": "MergeRequest",
                "discussion_id": "d1",
            },
            "merge_request": {
                "iid": 4,
                "title": "Add login",
                "description": "",
                "source_branch": "feature/login",
                "target_branch": "develop",
                "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/4",
            },
            "repository": {"url": "https://gitlab.example.com/acme/demo.git"},
        },
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
        jira_project_keys=["KAN"],
    )
    assert d.accepted
    assert d.event.issue_key.startswith("GL-")


def test_settings_keep_azure_pat_when_row_blank(monkeypatch):
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    s = Settings()
    s.set_azure_host_pat_map({"tfs.example.com": "keep-me"})
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr("src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None)
    monkeypatch.setattr("src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None)
    apply_settings_update(
        SettingsUpdate(azure_credentials=[{"host": "tfs.example.com", "pat": ""}])
    )
    assert s.azure_pat_for_host("tfs.example.com") == "keep-me"


def test_askpass_azure_prints_pat_username(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver-data"))
    content = GitManager._askpass_wrapper_content()
    assert "oauth2" in content
    assert "azure" in content.lower()
    assert "pat" in content.lower()
    path = GitManager._ensure_askpass_script()
    assert path is not None and path.is_file()
    env = os.environ.copy()
    env["VD_GIT_AUTH"] = "azure"
    env["VD_GIT_ASKUSER"] = "pat"
    env["VD_GIT_PASSWORD"] = "secret-pat"
    if path.suffix.lower() == ".cmd":
        prefix = ["cmd.exe", "/c", "call", str(path)]
    else:
        prefix = ["sh", str(path)]
    import subprocess

    user = subprocess.run(
        [*prefix, "Username for 'https://tfs':"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    password = subprocess.run(
        [*prefix, "Password:"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert user.returncode == 0, user.stderr
    assert password.returncode == 0, password.stderr
    assert user.stdout.strip() == "pat"
    assert password.stdout.strip() == "secret-pat"


def test_fail_issue_posts_azure_reply_not_jira(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    sm.create_state("AZ-DEMO-4", "pr", "comment")
    sm.update_state(
        "AZ-DEMO-4",
        status=TaskStatus.EXECUTING,
        metadata={
            "source": "azure",
            "workflow_type": "azure_pr",
            "azure_host": "tfs.example.com",
            "azure_collection_url": "https://tfs.example.com/tfs/DefaultCollection",
            "azure_project": "Demo",
            "azure_repository_id": "demo",
            "azure_pr_id": 4,
        },
    )
    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 1}

    before = len(fake_jira.comments)
    with patch("src.azure.client.AzureDevOpsClient.post_pr_comment", fake_post):
        proc._fail_issue("AZ-DEMO-4", "clone failed", suggestion="check PAT")
    assert posted.get("pr_id") == 4
    assert "clone failed" in (posted.get("body") or "")
    assert len(fake_jira.comments) == before


def test_assign_skip_for_azure_keys():
    from src.jira.client import _is_gitlab_assign_skip

    assert _is_gitlab_assign_skip("AZ-DEMO-4") is True
    assert _is_gitlab_assign_skip("KAN-1", source="azure") is True
    assert _is_gitlab_assign_skip("KAN-1", source="jira") is False


def test_event_roundtrip_dict():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(title="feat(KAN-7): x"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
        jira_project_keys=["KAN"],
    )
    from src.azure.webhook import AzurePrCommentEvent

    again = AzurePrCommentEvent.from_dict(d.event.to_dict())
    assert again.issue_key == "KAN-7"
    assert again.pr_id == 4
    assert again.source_branch == "feature/login"


def test_mention_scan_lists_extracted_and_configured():
    from src.azure.log import clip
    from src.azure.mentions import mention_scan

    scan = mention_scan("@yaver please look", ["yaver", "Yaver Bot"])
    assert scan["matched"] is True
    assert "yaver" in scan["extracted"]
    assert "yaver" in scan["configured"]
    miss = mention_scan("DOMAIN\\alice said hi", ["yaver"])
    assert miss["matched"] is False
    assert clip("one   two\nthree", 7) == "one tw…"


def test_comment_reject_reasons_unchanged_when_bot_not_mentioned():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(note="no mention here"),
        headers={"X-Azure-Token": "s"},
        secret="s",
        bot_mentions=["@yaver"],
    )
    assert d.accepted is False
    assert d.reason == "bot not mentioned"
