"""GitLab MR comment webhook — parse, mention, client, processor, dashboard."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gitlab.keys import (
    gitlab_issue_key,
    is_gitlab_issue_key,
    jira_key_from_mr_title,
    resolve_mr_issue_key,
)
from src.gitlab.mentions import (
    note_mentions_bot,
    parse_mention_list,
    strip_bot_mentions,
)
from src.gitlab.webhook import (
    decide_gitlab_mr_webhook,
    decide_gitlab_note_webhook,
    validate_webhook_token,
)
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def _mr_payload(
    *,
    note: str = "@berat_ai what does login do?",
    username: str = "alice",
    notable: str = "MergeRequest",
    source: str = "feature/login",
    target: str = "develop",
    note_id: int = 77,
    title: str = "Add login",
    description: str = "desc",
) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": username, "name": "Alice"},
        "project": {
            "id": 1,
            "path_with_namespace": "acme/demo",
            "http_url_to_repo": "https://gitlab.example.com/acme/demo.git",
            "web_url": "https://gitlab.example.com/acme/demo",
        },
        "object_attributes": {
            "id": note_id,
            "note": note,
            "noteable_type": notable,
            "project_id": 1,
            "discussion_id": "disc-1",
        },
        "merge_request": {
            "iid": 4,
            "title": title,
            "description": description,
            "source_branch": source,
            "target_branch": target,
            "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/4",
        },
        "repository": {"url": "https://gitlab.example.com/acme/demo.git"},
    }


def _mr_lifecycle_payload(
    *,
    action: str = "merge",
    state: str = "merged",
    title: str = "feat(KAN-12): add login",
    source: str = "feature/KAN-12",
    target: str = "develop",
    iid: int = 4,
) -> dict:
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "project": {
            "id": 1,
            "path_with_namespace": "acme/demo",
            "http_url_to_repo": "https://gitlab.example.com/acme/demo.git",
            "web_url": "https://gitlab.example.com/acme/demo",
        },
        "object_attributes": {
            "iid": iid,
            "action": action,
            "state": state,
            "title": title,
            "description": "desc",
            "source_branch": source,
            "target_branch": target,
            "url": f"https://gitlab.example.com/acme/demo/-/merge_requests/{iid}",
        },
        "repository": {"url": "https://gitlab.example.com/acme/demo.git"},
    }


def test_gitlab_issue_key_stable():
    assert gitlab_issue_key("acme/demo", 4) == "GL-ACME-DEMO-4"
    assert gitlab_issue_key("Group/Sub/Repo.git", 12) == "GL-GROUP-SUB-REPO-GIT-12"
    assert is_gitlab_issue_key("GL-ACME-DEMO-4")
    assert not is_gitlab_issue_key("KAN-1")


def test_jira_key_from_mr_title_uses_project_keys():
    keys = ["KAN", "PROJ"]
    assert jira_key_from_mr_title("feat(KAN-42): add login", keys) == "KAN-42"
    assert jira_key_from_mr_title("KAN-7 fix typo", keys) == "KAN-7"
    assert jira_key_from_mr_title("fix: handle PROJ-99 edge", keys) == "PROJ-99"
    # Wrong project not configured
    assert jira_key_from_mr_title("OTHER-1 something", keys) is None
    # No false positive inside longer words
    assert jira_key_from_mr_title("XXKAN-1", keys) is None
    assert (
        resolve_mr_issue_key(
            mr_title="feat(KAN-12): x",
            project_path="acme/demo",
            mr_iid=4,
            project_keys=keys,
        )
        == "KAN-12"
    )
    # Description fallback when title has no key
    assert (
        resolve_mr_issue_key(
            mr_title="Add login",
            mr_description="Closes KAN-99",
            project_path="acme/demo",
            mr_iid=4,
            project_keys=keys,
        )
        == "KAN-99"
    )
    # Fallback GL- when neither has a key
    assert (
        resolve_mr_issue_key(
            mr_title="Add login",
            project_path="acme/demo",
            mr_iid=4,
            project_keys=keys,
        )
        == "GL-ACME-DEMO-4"
    )


def test_mention_detection_and_strip():
    assert parse_mention_list("@berat_ai, DevBot") == ["berat_ai", "devbot"]
    assert note_mentions_bot("@berat_ai please look", ["berat_ai"])
    assert note_mentions_bot("hey @Berat_AI!", ["@berat_ai"])
    assert not note_mentions_bot("no one tagged", ["berat_ai"])
    assert (
        strip_bot_mentions("@berat_ai explain this fn", ["berat_ai"])
        == "explain this fn"
    )


def test_webhook_token():
    assert validate_webhook_token("abc", "abc")
    assert validate_webhook_token("x", "")
    assert not validate_webhook_token("nope", "secret")


def test_repo_http_url_prefers_git_http_and_converts_ssh():
    from src.gitlab.webhook import _repo_http_url

    assert (
        _repo_http_url(
            {"git_http_url": "https://gitlab.example.com/acme/demo.git"},
            {},
        )
        == "https://gitlab.example.com/acme/demo.git"
    )
    assert (
        _repo_http_url(
            {"ssh_url_to_repo": "git@gitlab.example.com:acme/demo.git"},
            {},
        )
        == "https://gitlab.example.com/acme/demo.git"
    )
    assert (
        _repo_http_url(
            {},
            {"url": "git@gitlab.example.com:acme/demo.git"},
        )
        == "https://gitlab.example.com/acme/demo.git"
    )


def test_decide_accepts_mr_mention():
    d = decide_gitlab_note_webhook(
        _mr_payload(),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
        jira_project_keys=["KAN"],
    )
    assert d.accepted
    assert d.event is not None
    # Title has no Jira key → GL- fallback from project path
    assert d.event.issue_key == "GL-ACME-DEMO-4"
    assert d.event.source_branch == "feature/login"
    assert d.event.prompt == "what does login do?"


def test_decide_uses_jira_key_from_mr_title():
    d = decide_gitlab_note_webhook(
        _mr_payload(title="feat(KAN-1905): wire auth"),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
        jira_project_keys=["KAN", "PROJ"],
    )
    assert d.accepted
    assert d.event is not None
    assert d.event.issue_key == "KAN-1905"
    assert not is_gitlab_issue_key(d.event.issue_key)


def test_decide_ignores_non_mr_and_no_mention():
    d = decide_gitlab_note_webhook(
        _mr_payload(notable="Issue"),
        headers={"X-Gitlab-Event": "Note Hook"},
        bot_mentions=["@berat_ai"],
    )
    assert not d.accepted
    d2 = decide_gitlab_note_webhook(
        _mr_payload(note="lgtm"),
        headers={"X-Gitlab-Event": "Note Hook"},
        bot_mentions=["@berat_ai"],
    )
    assert not d2.accepted
    d3 = decide_gitlab_note_webhook(
        _mr_payload(note="@berat_ai ping", username="berat_ai"),
        headers={"X-Gitlab-Event": "Note Hook"},
        bot_mentions=["@berat_ai"],
        bot_usernames=["berat_ai"],
    )
    assert not d3.accepted
    d4 = decide_gitlab_note_webhook(
        _mr_payload(),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "bad"},
        secret="good",
        bot_mentions=["@berat_ai"],
    )
    assert d4.http_status == 401


def test_gitlab_client_posts_note(monkeypatch):
    from src.gitlab.client import GitlabClient

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

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("src.gitlab.client.httpx.Client", FakeClient)
    c = GitlabClient(host="gitlab.example.com", pat="glpat-test")
    out = c.post_mr_note(project=1, mr_iid=4, body="*Yaver*\n\nhi", discussion_id="d1")
    assert out and out["id"] == 9
    assert "/projects/1/merge_requests/4/notes" in captured["url"]
    assert captured["headers"]["PRIVATE-TOKEN"] == "glpat-test"
    assert captured["json"]["in_reply_to_discussion_id"] == "d1"


def test_is_gitlab_triggered_uses_source_metadata():
    from src.processor import JobProcessor
    from src.state.models import JiraAgentState, TaskStatus

    proc = object.__new__(JobProcessor)
    sm = MagicMock()
    proc.state_manager = sm
    jira_state = JiraAgentState(
        issue_key="KAN-12",
        issue_summary="s",
        description="d",
        status=TaskStatus.EXECUTING,
        metadata={"source": "jira"},
    )
    gitlab_state = JiraAgentState(
        issue_key="KAN-12",
        issue_summary="s",
        description="d",
        status=TaskStatus.EXECUTING,
        metadata={"source": "gitlab", "workflow_type": "gitlab_mr"},
    )
    assert proc._is_gitlab_triggered("GL-ACME-DEMO-4") is True
    assert proc._is_gitlab_triggered("KAN-12", gitlab_state) is True
    assert proc._is_gitlab_triggered("KAN-12", jira_state) is False
    sm.get_state.return_value = gitlab_state
    assert proc._is_gitlab_triggered("KAN-12") is True
    sm.get_state.return_value = jira_state
    assert proc._is_gitlab_triggered("KAN-12") is False


@pytest.mark.asyncio
async def test_complete_work_jira_gets_cleaned_answer_gitlab_source_skips(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """Jira jobs get a cleaned answer; GitLab-sourced KAN-12 does not comment Jira."""
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    raw = "\n".join(
        [
            "[serve] session created: ses_abc",
            "[serve] turn=initial sending message…",
            "Login uses JWT in src/auth.cpp.",
            "[serve] assessment complete=True reasons=[]",
        ]
    )

    sm.create_state("KAN-12", "feat login", "d")
    sm.update_state("KAN-12", status=TaskStatus.EXECUTING, metadata={"source": "jira"})
    await proc._complete_work(
        sm.get_state("KAN-12"),
        execution_summary="All tasks completed successfully.",
        agent_answer=raw,
    )
    bodies = [c.get("body") or "" for c in fake_jira.comments]
    assert any("Work Completed" in b for b in bodies)
    assert any("Login uses JWT" in b for b in bodies)
    assert not any("[serve]" in b for b in bodies)
    assert not any("ses_abc" in b for b in bodies)
    assert sm.get_state("KAN-12").status == TaskStatus.COMPLETED

    before = len(fake_jira.comments)
    sm.create_state("KAN-99", "feat(KAN-99): from MR", "d")
    sm.update_state(
        "KAN-99",
        status=TaskStatus.EXECUTING,
        metadata={"source": "gitlab", "workflow_type": "gitlab_mr"},
    )
    await proc._complete_work(
        sm.get_state("KAN-99"),
        execution_summary="All tasks completed successfully.",
        agent_answer=raw,
    )
    assert sm.get_state("KAN-99").status == TaskStatus.COMPLETED
    assert len(fake_jira.comments) == before


def test_gitlab_mr_reply_body_formats_codex_jsonl_as_markdown():
    from src.processor import JobProcessor

    jsonl = "\n".join(
        [
            "[codex] cwd=/tmp model=gpt",
            '{"type":"thread.started","thread_id":"tid-1"}',
            '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0,"aggregated_output":"src"}}',
            (
                '{"type":"item.completed","item":{"type":"agent_message","text":'
                '"## Login\\n\\nThe handler uses JWT.\\n\\n'
                '```python\\nreturn token\\n```"}}'
            ),
        ]
    )
    proc = object.__new__(JobProcessor)
    body = JobProcessor._gitlab_mr_reply_body(proc, jsonl, pushed=False)
    assert body.startswith("*Yaver*")
    assert "## Login" in body
    assert "The handler uses JWT." in body
    assert "```python" in body
    assert "return token" in body
    assert '{"type"' not in body
    assert "thread.started" not in body
    assert "[codex] cwd" not in body
    assert "command_execution" not in body

    plain = JobProcessor._gitlab_mr_reply_body(
        proc, "Fixed the login bug.", pushed=True, branch="feature/login"
    )
    assert "Fixed the login bug." in plain
    assert "Pushed new commits" in plain
    assert "`feature/login`" in plain

    serve = "\n".join(
        [
            "[serve] health={'healthy': True, 'version': '1.18.10'}",
            "[serve] session resumed: ses_fc27f0da3ffegKGY7nd9GFFawC",
            "[serve] turn=initial sending message…",
            "main.cpp içindeki değişken değerleri a = 4, b = 2 olarak güncellendi",
            "[serve] turn=initial done finish='stop' summary=None elapsed=126.66s",
            "[serve] assessment complete=True premature=False reasons=[]",
        ]
    )
    body = JobProcessor._gitlab_mr_reply_body(
        proc, serve, pushed=True, branch="testt", commit_sha="b1b1b8ca"
    )
    assert "a = 4, b = 2" in body
    assert "[serve]" not in body
    assert "ses_fc27f0da" not in body
    assert "Pushed new commits" in body
    assert "`testt`" in body


@pytest.mark.asyncio
async def test_processor_gitlab_posts_codex_answer_not_jsonl(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.gitlab.webhook import decide_gitlab_note_webhook
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
    git.remote_url = "https://gitlab.example.com/acme/demo.git"
    git.work_branch = "feature/login"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    git.ensure_feature_branch.return_value = "feature/login"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.return_value = "aaa111baseline"
    git.commits_ahead_of_target.return_value = 0

    jsonl = "\n".join(
        [
            "[codex] cwd=/tmp model=gpt",
            '{"type":"thread.started","thread_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}',
            '{"type":"item.completed","item":{"type":"command_execution","command":"rg login","exit_code":0}}',
            (
                '{"type":"item.completed","item":{"type":"agent_message","text":'
                '"## Login\\n\\n`AuthService` issues a JWT and stores it in the cookie."}}'
            ),
        ]
    )
    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": jsonl,
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "backend": "codex",
        }
    )

    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 202}

    decision = decide_gitlab_note_webhook(
        _mr_payload(note="@berat_ai what does login do?"),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert decision.event

    def fake_init(*_a, **_k):
        proc._contexts["GL-ACME-DEMO-4"] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch("src.gitlab.client.GitlabClient.post_mr_note", fake_post):
        await proc.handle_gitlab_mr_comment(decision.event)

    body = posted.get("body") or ""
    assert posted.get("mr_iid") == 4
    assert "*Yaver*" in body
    assert "## Login" in body
    assert "`AuthService` issues a JWT" in body
    assert '{"type"' not in body
    assert "thread.started" not in body
    assert "command_execution" not in body
    assert "[codex] cwd" not in body
    assert not any("Work Completed" in (c.get("body") or "") for c in fake_jira.comments)
    assert not any("AuthService" in (c.get("body") or "") for c in fake_jira.comments)
    git.push.assert_called()
    git.create_merge_request.assert_not_called()


@pytest.mark.asyncio
async def test_processor_gitlab_job_reuses_session_and_posts_mr(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.gitlab.webhook import decide_gitlab_note_webhook
    from src.processor import JobProcessor
    from src.state.session_bind_store import SessionBindStore
    from tests.test_opencode_sessions import _make_session_db

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    binds = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", binds)
    clone = tmp_path / "clone"
    clone.mkdir()
    db = _make_session_db(
        tmp_path / "oc.db",
        [{"id": "ses_gl1", "directory": str(clone), "title": "GL-ACME-DEMO-4: x"}],
    )
    monkeypatch.setattr("src.opencode_sessions._default_db_path", lambda: db)

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    git = MagicMock()
    git.remote_url = "https://gitlab.example.com/acme/demo.git"
    git.work_branch = "feature/login"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    git.ensure_feature_branch.return_value = "feature/login"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.return_value = "abc123deadbeef"
    git.commits_ahead_of_target.return_value = 2
    git.push.return_value = True

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "Login is wired in src/auth.cpp",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_gl1",
        }
    )

    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 99}

    decision = decide_gitlab_note_webhook(
        _mr_payload(),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert decision.event

    binds.upsert(
        repository_url=git.remote_url,
        branch="feature/login",
        target_branch="develop",
        session_id="ses_gl1",
        issue_key="GL-ACME-DEMO-4",
        working_directory=str(clone),
    )

    def fake_init(*_a, **_k):
        proc._contexts["GL-ACME-DEMO-4"] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch("src.gitlab.client.GitlabClient.post_mr_note", fake_post):
        await proc.handle_gitlab_mr_comment(decision.event)

    st = sm.get_state("GL-ACME-DEMO-4")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert runner.run_agent_with_retry.await_count == 1
    task = runner.run_agent_with_retry.await_args.args[0]
    assert task.session_id == "ses_gl1"
    assert posted.get("mr_iid") == 4
    assert "*Yaver*" in (posted.get("body") or "")
    assert not any(
        "Login is wired in src/auth.cpp" in (c.get("body") or "")
        for c in fake_jira.comments
    )
    jobs = isolate_jira_agent_artifacts["job_store"].list_jobs(issue_key="GL-ACME-DEMO-4")
    assert jobs
    assert jobs[0]["source"] == "gitlab"
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["workflow_type"] == "gitlab_mr"
    # Same SHA before/after but branch is ahead of target: still push + reuse MR.
    git.push.assert_called()
    assert "what does login do?" in task.prompt
    assert "build" in task.prompt.lower()
    assert "existing MR" in task.prompt or "existing merge request" in task.prompt.lower()
    assert "Do **not** push" in task.prompt or "Do not push" in task.prompt


@pytest.mark.asyncio
async def test_processor_gitlab_build_pushes_existing_mr(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.gitlab.webhook import decide_gitlab_note_webhook
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
    git.remote_url = "https://gitlab.example.com/acme/demo.git"
    git.work_branch = "feature/login"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    git.ensure_feature_branch.return_value = "feature/login"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.side_effect = [
        "aaa111baseline",  # snapshot
        "bbb222newhead",  # assert delivery
        "bbb222newhead",  # record after push
    ]
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "[GL-ACME-DEMO-4] fix: login"
    git.get_last_commit_message.return_value = "fix login"
    git.build_commit_url.return_value = (
        "https://gitlab.example.com/acme/demo/-/commit/bbb222newhead"
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

    decision = decide_gitlab_note_webhook(
        _mr_payload(note="@berat_ai please fix the login bug"),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert decision.event

    def fake_init(*_a, **_k):
        proc._contexts["GL-ACME-DEMO-4"] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch.object(reporter, "post_progress_update") as jira_progress, patch(
        "src.gitlab.client.GitlabClient.post_mr_note", fake_post
    ):
        await proc.handle_gitlab_mr_comment(decision.event)

    st = sm.get_state("GL-ACME-DEMO-4")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    git.push.assert_called()
    git.create_merge_request.assert_not_called()
    jira_progress.assert_not_called()
    body = posted.get("body") or ""
    assert "*Yaver*" in body
    assert "Fixed the login bug." in body
    assert "Pushed new commits" in body
    assert (st.metadata or {}).get("merge_request_url") == (
        "https://gitlab.example.com/acme/demo/-/merge_requests/4"
    )
    assert (st.metadata or {}).get("delivery_status") == "delivered"


def test_dashboard_webhook_endpoint_dispatches(tmp_path, monkeypatch, fake_jira):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    monkeypatch.setattr("src.config.settings.gitlab_bot_usernames", "berat_ai")

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "queue_id": "q_test",
            "issue_key": "GL-ACME-DEMO-4",
            "status": "queued",
        }
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/gitlab",
            json=_mr_payload(),
            headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "tok"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["issue_key"] == "GL-ACME-DEMO-4"
    assert body["queue_id"] == "q_test"
    assert proc.enqueue_gitlab_note.await_count == 1


def test_dashboard_webhook_rejects_bad_secret(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    app = create_dashboard_app(processor=proc)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/gitlab",
        json=_mr_payload(),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "nope"},
    )
    assert resp.status_code == 401


def test_decide_gitlab_mr_webhook_accepts_merge(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_projects", "KAN")
    d = decide_gitlab_mr_webhook(
        _mr_lifecycle_payload(),
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.accepted
    assert d.event is not None
    assert d.event.is_merged
    assert d.event.issue_key == "KAN-12"
    assert d.event.mr_iid == 4


def test_decide_gitlab_mr_webhook_records_close_without_merge(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_projects", "KAN")
    d = decide_gitlab_mr_webhook(
        _mr_lifecycle_payload(action="close", state="closed"),
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.accepted
    assert d.event is not None
    assert not d.event.is_merged
    assert d.event.is_closed
    assert d.event.should_delete_clone
    assert d.event.state == "closed"


def test_dashboard_mr_merge_webhook_deletes_clone(
    tmp_path, monkeypatch, fake_jira
):
    from src.dashboard.api import create_dashboard_app
    from src.dashboard.temp_storage import reset_delete_jobs
    from src.processor import JobProcessor
    from src.state.job_store import job_store

    reset_delete_jobs()
    base = tmp_path / "t"
    clone = base / "repo_merged01"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("src.config.settings.temp_dir_base", base)
    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.jira_projects", "KAN")
    monkeypatch.chdir(tmp_path)

    job = job_store.create_job(issue_key="KAN-12", summary="add login")
    job_store.update_job(
        job["job_id"],
        working_directory=str(clone.resolve()),
        merge_request_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        gitlab_project="acme/demo",
        gitlab_mr_iid=4,
        merge_request_state="opened",
    )

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/gitlab",
            json=_mr_lifecycle_payload(),
            headers={
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Token": "tok",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "merge_request"
    assert "repo_merged01" in body["deleted"]
    updated = job_store.get_job(job["job_id"])
    assert updated["merge_request_state"] == "merged"


def test_dashboard_mr_close_webhook_deletes_clone(
    tmp_path, monkeypatch, fake_jira
):
    from src.dashboard.api import create_dashboard_app
    from src.dashboard.temp_storage import reset_delete_jobs
    from src.processor import JobProcessor
    from src.state.job_store import job_store

    reset_delete_jobs()
    base = tmp_path / "t"
    clone = base / "repo_closed01"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("src.config.settings.temp_dir_base", base)
    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.jira_projects", "KAN")
    monkeypatch.chdir(tmp_path)

    job = job_store.create_job(issue_key="KAN-12", summary="add login")
    job_store.update_job(
        job["job_id"],
        working_directory=str(clone.resolve()),
        merge_request_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        gitlab_project="acme/demo",
        gitlab_mr_iid=4,
        merge_request_state="opened",
    )

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/gitlab",
            json=_mr_lifecycle_payload(action="close", state="closed"),
            headers={
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Token": "tok",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "merge_request"
    assert body["reason"] == "closed"
    assert "repo_closed01" in body["deleted"]
    updated = job_store.get_job(job["job_id"])
    assert updated["merge_request_state"] == "closed"


def test_decide_gitlab_mr_reopen_does_not_delete(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_projects", "KAN")
    d = decide_gitlab_mr_webhook(
        _mr_lifecycle_payload(action="reopen", state="opened"),
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.accepted
    assert d.event is not None
    assert not d.event.is_merged
    assert not d.event.is_closed
    assert not d.event.should_delete_clone
