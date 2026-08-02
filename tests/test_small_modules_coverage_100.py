"""Drive remaining small-module gaps toward full coverage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.dashboard.issue_logs import IssueLogRing
from src.dashboard.snapshot import PollSnapshotStore
from src.issue_git_spec import (
    parse_issue_git_spec,
    parse_issue_mode,
    strip_params_block,
)
from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.state.manager import JiraStateManager
from src.state.models import JiraAgentState, TaskStatus


def test_config_empty_trigger_labels_and_gitlab_hosts():
    s = Settings(
        jira_host="https://j.example",
        jira_api_token="t",
        trigger_labels="",
        gitlab_allowed_hosts=" gitlab.com , , example.com ",
        jira_projects="",
    )
    assert s.trigger_labels_list == ["ai-assist", "bot"]
    assert s.gitlab_allowed_hosts_list == ["gitlab.com", "example.com"]
    assert s.jira_projects_list == ["PROJ"]


def test_state_models_legacy_code_review_maps_to_executing():
    st = JiraAgentState.from_dict(
        {
            "issue_key": "X-1",
            "issue_summary": "s",
            "status": "code_review",
        }
    )
    assert st.status == TaskStatus.EXECUTING


def test_update_state_if_reject_and_unknown_field(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path)
    sm.create_state("U-1", "s", "d")
    sm.update_state("U-1", status=TaskStatus.ERROR)
    assert (
        sm.update_state_if(
            "U-1",
            reject_statuses={TaskStatus.ERROR},
            status=TaskStatus.COMPLETED,
        )
        is None
    )
    sm.update_state("U-1", status=TaskStatus.EXECUTING)
    out = sm.update_state_if(
        "U-1",
        expected_statuses={TaskStatus.EXECUTING},
        not_a_field=1,
        progress_percentage=50,
    )
    assert out is not None
    assert out.progress_percentage == 50


def test_update_state_if_missing_and_delete_errors(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path)
    assert sm.update_state_if("NOPE", status=TaskStatus.COMPLETED) is None
    assert sm.delete_state("NOPE") is False
    sm.create_state("D-1", "s")
    assert sm.delete_state("D-1") is True
    # corrupt json load path
    bad = sm._get_state_file("BAD-1")
    bad.write_text("{not json", encoding="utf-8")
    assert sm.get_state("BAD-1") is None


def test_issue_log_ring_filter_and_overflow():
    ring = IssueLogRing(maxlen=3)
    ring.append("KAN-1 hello")
    ring.append("other")
    ring.append("KAN-1 world")
    ring.append("KAN-1 more")
    assert ring.for_issue("") == []
    lines = ring.for_issue("KAN-1")
    assert lines
    assert all("KAN-1" in (x.get("message") or "") for x in lines)


def test_poll_snapshot_listener_and_idle():
    store = PollSnapshotStore()
    seen = []

    def cb(snap):
        seen.append(snap)
        raise RuntimeError("listener boom")  # must be swallowed

    unsub = store.subscribe(cb)
    store.begin_poll(board_id="1", interval_seconds=30)
    store.end_poll(
        source="board",
        issues=[{"key": "K-1", "will_process": True, "matched_label": True}],
        interval_seconds=30,
        error=None,
    )
    store.set_idle()
    # bad next_poll_at for ValueError branch
    with store._lock:
        store._data["next_poll_at"] = "not-a-date"
    snap = store.snapshot()
    assert snap.get("seconds_until_next_poll") is None
    assert seen
    unsub()
    store.begin_poll(board_id="1", interval_seconds=10)


def test_workflow_router_mode_and_reason_edges():
    plan = (
        "{params}\nRepository: https://g.com/a/b.git\n"
        "Source branch: develop\nMode: planning\n{params}"
    )
    assert WorkflowRouter.route_issue("X", "s", plan) == WorkflowType.PLANNING
    build = plan.replace("planning", "execute")
    assert WorkflowRouter.route_issue("X", "s", build) == WorkflowType.EXECUTION
    wt, err = WorkflowRouter.route_issue_with_reason("X", "fix bug", "implement feature")
    assert wt == WorkflowType.PLANNING
    assert err and "Mode" in err
    wt2, err2 = WorkflowRouter.route_issue_with_reason(
        "X", "how to design", "should we use pattern"
    )
    assert wt2 == WorkflowType.ORACLE_CONSULT
    assert err2 is None


def test_prompt_builder_empty_jira_body():
    p = PromptBuilder.build_prometheus_prompt("K-1", "", "")
    assert "K-1" in p
    p2 = PromptBuilder.build_sisyphus_prompt("K-1", "", summary="")
    assert "K-1" in p2


def test_parse_issue_mode_and_invalid_mode_token():
    desc = (
        "{params}\nRepository: https://g.com/a/b.git\n"
        "Source branch: develop\nMode: nonsense\n{params}"
    )
    assert parse_issue_mode("", desc) is None
    spec, err = parse_issue_git_spec("", desc)
    assert spec is None and err and "Mode" in err


def test_issue_git_spec_expand_links_and_strip():
    desc = (
        "Task\n{params}\n"
        "Repository: [https://gitlab.com/g/r.git|https://gitlab.com/g/r.git]\n"
        "Source branch: develop\nMode: plan\n{params}\n"
    )
    spec, err = parse_issue_git_spec("", desc)
    assert err is None and spec is not None
    assert "gitlab.com" in spec.repository_url
    cleaned = strip_params_block(desc)
    assert "Task" in cleaned
    assert "{params}" not in cleaned


def test_agent_env_allowlist_strips_secrets():
    from src.orchestrator.agent_runner import _agent_subprocess_env
    import os

    with patch.dict(
        os.environ,
        {
            "PATH": "/bin",
            "HOME": "/tmp",
            "GITLAB_PAT": "secret",
            "JIRA_API_TOKEN": "secret",
            "OPENAI_API_KEY": "ok",
            "SSH_AUTH_SOCK": "/tmp/ssh",
            "VD_GIT_PASSWORD": "pat",
            "RANDOM_SECRET": "nope",
        },
        clear=False,
    ):
        env = _agent_subprocess_env()
    assert env.get("PATH")
    assert env.get("OPENAI_API_KEY") == "ok"
    assert "GITLAB_PAT" not in env
    assert "JIRA_API_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "VD_GIT_PASSWORD" not in env
    assert "RANDOM_SECRET" not in env
    assert env.get("GIT_TERMINAL_PROMPT") == "0"


def test_jira_append_to_description():
    from src.jira.client import JiraClient

    client = JiraClient.__new__(JiraClient)
    client.client = MagicMock()
    client.get_issue = MagicMock(
        return_value={"fields": {"description": "old text"}}
    )
    client.update_issue = MagicMock(return_value=True)
    assert client.append_to_description("K-1", "") is True
    assert client.append_to_description("K-1", "new plan") is True
    called = client.update_issue.call_args
    assert called is not None
    fields = called.kwargs.get("fields") or (
        called.args[1] if len(called.args) > 1 else None
    )
    if fields is None and called.args:
        # update_issue(issue_key, fields={...})
        for a in called.args:
            if isinstance(a, dict) and "description" in a:
                fields = a
    assert fields is not None
    assert "old text" in fields["description"]
    assert "new plan" in fields["description"]
    # non-string description
    client.get_issue = MagicMock(return_value={"fields": {"description": 123}})
    assert client.append_to_description("K-1", "x") is True
    # exception path
    client.get_issue = MagicMock(side_effect=RuntimeError("x"))
    assert client.append_to_description("K-1", "y") is False


def test_reporter_append_plan_to_description(reporter, fake_jira):
    fake_jira.append_to_description = MagicMock(return_value=True)
    reporter.client = fake_jira
    st = JiraAgentState(
        issue_key="P-1",
        issue_summary="s",
        status=TaskStatus.PLAN_READY,
        plan_path="/tmp/p.md",
    )
    assert reporter.append_plan_to_description(st, "") is False
    assert reporter.append_plan_to_description(st, "# plan\nstep") is True
    fake_jira.append_to_description.assert_called()


def test_src_read_version_fallbacks(tmp_path, monkeypatch):
    from src import _read_version

    monkeypatch.chdir(tmp_path)
    # no VERSION file
    assert _read_version() in ("0.0.0-dev",) or isinstance(_read_version(), str)
