"""Plan → build is label-driven: plan_ready / plan_refactor / plan_execute."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.jira.poller import JiraPoller
from src.processor import JobProcessor
from src.jira.plan_labels import (
    HANDOFF_EXECUTE,
    HANDOFF_REFACTOR,
    PLAN_EXECUTE_LABEL,
    PLAN_READY_LABEL,
    PLAN_REFACTOR_LABEL,
    infer_plan_handoff,
    latest_comment_tagging_pat_user,
)
from src.orchestrator.prompt_builder import PromptBuilder
from src.state.models import TaskStatus
from src.state.session_bind_store import SessionBindStore, bind_id_for


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


@pytest.fixture
def poller(state_manager):
    p = JiraPoller(client=MagicMock(), interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    return p


def _ip_fields(*, labels, summary="s", description="d", assignee=None):
    return {
        "summary": summary,
        "description": description,
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "labels": labels,
        "assignee": assignee,
    }


def test_infer_plan_execute_requires_in_progress():
    assert (
        infer_plan_handoff(
            {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": [PLAN_EXECUTE_LABEL],
            }
        )
        is None
    )
    assert (
        infer_plan_handoff(_ip_fields(labels=[PLAN_EXECUTE_LABEL]))
        == HANDOFF_EXECUTE
    )


def test_infer_plan_refactor_rejects_when_plan_ready_present():
    assert (
        infer_plan_handoff(
            _ip_fields(labels=[PLAN_REFACTOR_LABEL, PLAN_READY_LABEL])
        )
        is None
    )
    assert (
        infer_plan_handoff(_ip_fields(labels=[PLAN_REFACTOR_LABEL]))
        == HANDOFF_REFACTOR
    )
    assert (
        infer_plan_handoff(
            {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": [PLAN_REFACTOR_LABEL],
            }
        )
        == HANDOFF_REFACTOR
    )


def test_latest_comment_picks_pat_mention_not_bot_reply():
    myself = {"name": "devbot", "displayName": "DevBot", "key": "devbot"}
    comments = [
        {"body": "[~devbot] first ask", "author": {"name": "alice"}},
        {"body": "*Yaver* Plan Ready", "author": {"name": "devbot"}},
        {"body": "hey [~devbot] please add caching", "author": {"name": "alice"}},
    ]
    body = latest_comment_tagging_pat_user(comments, myself=myself)
    assert body is not None
    assert "caching" in body


def test_latest_comment_accepts_operator_who_is_also_the_pat_user():
    """Cloud: operator API token == assignee. Their @self mention is a refactor ask."""
    myself = {
        "displayName": "Beratersari",
        "accountId": "70121:abc",
        "emailAddress": "a@b.com",
    }
    comments = [
        {
            "body": "h3. AI Agent — Plan Ready\n\nA work plan has been generated.",
            "author": {"displayName": "Beratersari", "accountId": "70121:abc"},
        },
        {
            "body": "[~accountid:70121:abc] 12+3 yapalim. kararimi degistirdim",
            "author": {"displayName": "Beratersari", "accountId": "70121:abc"},
        },
    ]
    body = latest_comment_tagging_pat_user(
        comments,
        myself=myself,
        mention_tokens=["@DevBot", "@AI"],
        extra_needles=["Beratersari"],
    )
    assert body is not None
    assert "12+3" in body


def test_plan_and_build_session_maps_are_distinct(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    repo = "https://gitlab.example.com/a/r.git"
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-1",
        target_branch="develop",
        session_id="ses_plan",
        issue_key="KAN-1",
        kind="plan",
    )
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-1",
        target_branch="develop",
        session_id="ses_build",
        issue_key="KAN-1",
        kind="build",
    )
    plan = store.get(repo, "feature/KAN-1", "develop", kind="plan")
    build = store.get(repo, "feature/KAN-1", "develop", kind="build")
    assert plan is not None and plan["session_id"] == "ses_plan"
    assert build is not None and build["session_id"] == "ses_build"
    assert bind_id_for(repo, "feature/KAN-1", "develop", kind="plan") != bind_id_for(
        repo, "feature/KAN-1", "develop", kind="build"
    )
    # Legacy empty-kind id stays stable (no kind suffix).
    legacy = bind_id_for(repo, "feature/KAN-1", "develop", issue_key="KAN-1")
    assert legacy != bind_id_for(
        repo, "feature/KAN-1", "develop", issue_key="KAN-1", kind="plan"
    )


def test_plan_execute_prompt_does_not_treat_yaver_data_dir_as_the_repo():
    text = PromptBuilder.build_plan_execute_prompt(
        r"C:\vd\yaver\plans\KAN-481.md",
        issue_key="KAN-481",
    )
    assert text.startswith("implement the plan KAN-481.md")
    assert r"C:\vd\yaver\plans\KAN-481.md" in text
    assert "not the product repository" in text.lower()
    assert "current working directory" in text.lower()
    assert "do not treat the plan file's parent directory as the project" in text.lower()


def test_poller_mode_build_does_not_start(poller, state_manager, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_assignee_names", "devbot")
    desc = (
        "{params}\nRepository: https://g.example/r.git\n"
        "Source branch: feature/x\nTarget branch: develop\n"
        "Mode: build\n{params}"
    )
    state_manager.create_state("PS-M", "plan me", desc)
    state_manager.update_state("PS-M", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("PS-M")
    issue = {
        "key": "PS-M",
        "fields": {
            "summary": "plan me",
            "description": desc,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": [PLAN_READY_LABEL],
            "assignee": {"displayName": "DevBot"},
        },
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    poller.client.get_issue = MagicMock(return_value=issue)
    keys = [i["key"] for i in poller.poll_board()]
    assert "PS-M" not in keys


def test_poller_plan_execute_in_progress_emits_once(
    poller, state_manager, monkeypatch
):
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_assignee_names", "devbot")
    state_manager.create_state("PS-E", "plan me", "d")
    state_manager.update_state("PS-E", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("PS-E")
    issue = {
        "key": "PS-E",
        "fields": _ip_fields(
            labels=[PLAN_EXECUTE_LABEL],
            assignee={"displayName": "DevBot"},
        ),
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    poller.client.get_issue = MagicMock(return_value=issue)
    r1 = poller.poll_board()
    assert "PS-E" in [i["key"] for i in r1]
    assert r1[0].get("_plan_handoff") == HANDOFF_EXECUTE
    poller._plan_start_emitted.add("PS-E")
    r2 = poller.poll_board()
    assert "PS-E" not in [i["key"] for i in r2]


@pytest.mark.asyncio
async def test_processor_plan_execute_starts_even_if_mode_plan(
    processor, state_manager, tmp_path
):
    desc = "{params}\nMode: plan\n{params}"
    state_manager.create_state("PR-E", "s", desc)
    plan = tmp_path / "PR-E.md"
    plan.write_text("# plan\n", encoding="utf-8")
    state_manager.update_state(
        "PR-E", status=TaskStatus.PLAN_READY, plan_path=str(plan)
    )
    started = {"ok": False, "flag": None}

    async def fake_exec(st, *, from_plan_execute=False):
        started["ok"] = True
        started["flag"] = from_plan_execute

    event = {
        "webhookEvent": "jira:issue_updated",
        "plan_handoff": HANDOFF_EXECUTE,
        "issue": {
            "key": "PR-E",
            "fields": _ip_fields(labels=[PLAN_EXECUTE_LABEL], description=desc),
        },
    }
    with patch.object(processor, "_start_execution_workflow", side_effect=fake_exec):
        started_flag, reason = await processor._handle_issue_updated(event)
    assert started_flag is True
    assert started["ok"] is True
    assert started["flag"] is True
    assert reason is None


@pytest.mark.asyncio
async def test_processor_mode_build_on_plan_ready_does_not_start(
    processor, state_manager
):
    desc = (
        "{params}\nRepository: https://g.example/r.git\n"
        "Source branch: feature/x\nTarget branch: develop\n"
        "Mode: build\n{params}"
    )
    state_manager.create_state("PR-B", "s", desc)
    state_manager.update_state("PR-B", status=TaskStatus.PLAN_READY)
    started = {"ok": False}

    async def fake_exec(st, **kwargs):
        started["ok"] = True

    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PR-B",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": [PLAN_READY_LABEL],
                "summary": "s",
                "description": desc,
            },
        },
    }
    with patch.object(processor, "_start_execution_workflow", side_effect=fake_exec):
        started_flag, reason = await processor._handle_issue_updated(event)
    assert started["ok"] is False
    assert started_flag is False
    assert "plan_execute" in (reason or "")


@pytest.mark.asyncio
async def test_processor_plan_refactor_uses_comment_and_plan_workflow(
    processor, state_manager
):
    state_manager.create_state("PR-R", "s", "{params}\nMode: plan\n{params}")
    state_manager.update_state("PR-R", status=TaskStatus.PLAN_READY)
    ran = {"ok": False, "comment": None}

    async def fake_plan(st, *, refactor_comment=None):
        ran["ok"] = True
        ran["comment"] = refactor_comment

    event = {
        "webhookEvent": "jira:issue_updated",
        "plan_handoff": HANDOFF_REFACTOR,
        "issue": {
            "key": "PR-R",
            "fields": _ip_fields(labels=[PLAN_REFACTOR_LABEL]),
        },
    }
    processor._latest_plan_refactor_comment = MagicMock(
        return_value="[~devbot] add retries"
    )
    with patch.object(processor, "_start_planning_workflow", side_effect=fake_plan):
        started_flag, reason = await processor._handle_issue_updated(event)
    assert started_flag is True
    assert ran["ok"] is True
    assert "retries" in (ran["comment"] or "")
    assert reason is None


def test_poller_does_not_latch_plan_refactor(poller, state_manager, monkeypatch):
    """Label-first then comment later must still emit on the next poll."""
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_assignee_names", "devbot")
    state_manager.create_state("PS-R", "plan me", "d")
    state_manager.update_state("PS-R", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("PS-R")
    issue = {
        "key": "PS-R",
        "fields": _ip_fields(
            labels=[PLAN_REFACTOR_LABEL],
            assignee={"displayName": "DevBot"},
        ),
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    poller.client.get_issue = MagicMock(return_value=issue)
    first = poller.poll_board()
    assert "PS-R" in [i["key"] for i in first]
    handled: list[str] = []
    poller._handler = lambda e: handled.append(e["issue"]["key"])
    poller.process_issue(first[0], is_update=True)
    assert "PS-R" not in poller._plan_refactor_emitted
    assert handled == ["PS-R"]
    second = poller.poll_board()
    assert "PS-R" in [i["key"] for i in second]


@pytest.mark.asyncio
async def test_plan_execute_without_plan_file_does_not_start(
    processor, state_manager
):
    state_manager.create_state("PR-NP", "s", "{params}\nMode: plan\n{params}")
    state_manager.update_state("PR-NP", status=TaskStatus.PLAN_READY, plan_path="")
    started = {"ok": False}

    async def fake_exec(st, **kwargs):
        started["ok"] = True

    event = {
        "webhookEvent": "jira:issue_updated",
        "plan_handoff": HANDOFF_EXECUTE,
        "issue": {
            "key": "PR-NP",
            "fields": _ip_fields(labels=[PLAN_EXECUTE_LABEL]),
        },
    }
    with patch.object(processor, "_start_execution_workflow", side_effect=fake_exec):
        started_flag, reason = await processor._handle_issue_updated(event)
    assert started["ok"] is False
    assert started_flag is False
    assert "without a plan" in (reason or "")


def test_materialize_plan_at_clone_root(processor, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key = "KAN-ROOT"
    plans = tmp_path / "durable"
    plans.mkdir()
    (plans / f"{key}.md").write_text("# plan\n", encoding="utf-8")
    ws = tmp_path / "clone"
    ws.mkdir()
    git = MagicMock()
    git.get_working_directory.return_value = ws
    processor._contexts[key] = {"git": git, "runner": None}
    with patch("src.processor.settings") as s:
        s.full_plans_dir = plans
        dest = processor._materialize_plan_at_clone_root(key)
    assert dest == ws / f"{key}.md"
    assert dest.read_text(encoding="utf-8") == "# plan\n"


def test_resolve_plan_for_build_prefers_own_plan(processor, tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    own = plans / "KAN-485.md"
    own.write_text("# own\n", encoding="utf-8")
    (plans / "KAN-484.md").write_text("# sibling\n", encoding="utf-8")
    monkeypatch.setattr(processor, "_durable_plan_path", lambda key: plans / f"{key}.md")
    assert processor._resolve_plan_for_build("KAN-485") == str(own)


def test_resolve_plan_for_build_uses_sibling_plan_bind(
    processor, state_manager, isolate_jira_agent_artifacts, tmp_path, monkeypatch
):
    plans = tmp_path / "plans"
    plans.mkdir()
    sibling = plans / "KAN-484.md"
    sibling.write_text("# sibling plan\n", encoding="utf-8")
    monkeypatch.setattr(processor, "_durable_plan_path", lambda key: plans / f"{key}.md")
    state_manager.create_state("KAN-485", "build it", "Mode: build")
    state_manager.update_state(
        "KAN-485",
        metadata={
            "repository_url": "https://gitlab.com/acme/demo.git",
            "source_branch": "feature/KAN-1909",
            "target_branch": "main",
        },
    )
    isolate_jira_agent_artifacts["session_bind_store"].upsert(
        repository_url="https://gitlab.com/acme/demo.git",
        branch="feature/KAN-1909",
        target_branch="main",
        session_id="ses_plan_sibling",
        issue_key="KAN-484",
        kind="plan",
    )
    found = processor._resolve_plan_for_build("KAN-485")
    assert found == str(sibling)
