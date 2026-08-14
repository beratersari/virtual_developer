"""Regression: removed dead symbols stay gone; kept compact setting stays live."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

REMOVED = (
    "_plain_str",
    "_clear_requeue_flag",
    "_repo_and_work_branch",
    "_working_tree_dirty",
    "gitlab_client_for_host",
    "_assess_incomplete_run",
    "_kill_process_tree_escalating",
    "_register_schedule_workspace",
    "GitDeliveryItem",
    "DashboardEnvelope",
    "temp_dir_format",
)


def _src_text() -> str:
    parts = []
    for path in (ROOT / "src").rglob("*.py"):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_removed_dead_symbols_stay_gone():
    blob = _src_text()
    for name in REMOVED:
        assert name not in blob, f"{name} was reintroduced"


def test_compact_continue_limit_is_gone():
    from src.config import Settings, settings

    assert "opencode_serve_max_compact_continues" not in Settings.model_fields
    assert not hasattr(settings, "opencode_serve_max_compact_continues")


def test_serve_orchestrator_has_no_dead_fields():
    tree = ast.parse((ROOT / "src" / "opencode_serve.py").read_text(encoding="utf-8"))
    fields = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            fields.add(node.target.id)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    fields.add(t.id)
    assert "max_compact_continues" not in fields
    assert "continue_prompt" not in fields


def test_settings_has_no_dead_log_or_temp_format_fields():
    from src.config import Settings

    names = set(Settings.model_fields)
    assert "log_level" not in names
    assert "log_file" not in names
    assert "temp_dir_format" not in names


def test_frontend_removed_unused_types():
    text = (ROOT / "web" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
    assert "export type TaskItem" not in text
    assert "export type TasksPayload" not in text
    assert "export type DashboardPayload" not in text


def test_post_comment_response_has_no_in_reply_to():
    from src.reporter.jira_reporter import JiraReporter

    class Client:
        def add_comment(self, issue_key: str, body: str):
            return {"id": "1"}

    reporter = JiraReporter(client=Client())
    reporter.post_comment_response("KAN-1", "hello")


@pytest.mark.asyncio
async def test_serve_run_does_not_post_continue_prompt_field():
    from tests.test_opencode_serve_e2e import FakeServeBackend, FakeServeClient
    from src.opencode_serve import ServeOrchestrator

    backend = FakeServeBackend(required_compacts=0)
    backend.auto_complete_on_idle = True
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=0.4,
        compact_poll_seconds=0.05,
    )
    result = await orch.run(prompt="do the work", title="KAN-1", agent="atlas")
    assert result.returncode == 0, result.stderr
    assert backend.message_calls == 1
    assert all("Continue the previous" not in p for p in backend.prompts)


def test_legacy_jobs_helper_accepts_remaining_kwargs():
    from src.dashboard.service import _legacy_jobs_from_sessions

    assert _legacy_jobs_from_sessions(issue_key="KAN-1", summaries={"KAN-1": "s"}) == []
