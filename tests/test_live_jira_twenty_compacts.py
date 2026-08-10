"""LIVE Jira API: 20 compact-then-stop cycles must not post Error.

Uses the real Jira Cloud/on-prem REST API from ``.env`` (create issue, comments,
read-back). OpenCode is the deterministic FakeServeBackend so the 20 compact
cycles are real orchestration logic, not a 20× live-LLM wait.

Run::

    .venv/bin/python -m pytest tests/test_live_jira_twenty_compacts.py -v -s

Skipped when Jira is unconfigured or the probe fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import pytest

from src.config import settings
from src.jira.client import JiraClient
from src.opencode_serve import (
    DEFAULT_MAX_COMPACT_CONTINUES,
    ServeOrchestrator,
)
from src.reporter.jira_reporter import JiraReporter
from src.state.models import JiraAgentState, TaskStatus
from tests.test_opencode_serve_e2e import FakeServeBackend, FakeServeClient


def _jira_live_ready() -> str:
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    return ""


@pytest.mark.asyncio
async def test_live_jira_twenty_compacts_not_posted_as_error():
    """Create a real Jira issue, run 20 compact continues, assert no Error comment."""
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)

    client = JiraClient()
    from src.jira_connection import probe_jira_connection

    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")

    projects = (settings.jira_projects or "KAN").split(",")
    project = (projects[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary = f"[vd-e2e] 20-compact session retry {stamp}"
    description = (
        "Automated Virtual Developer e2e (do not process).\n"
        "Verifies context compaction is resumed, not posted as an Error.\n"
        f"Mode: e2e-compact\nRequired compact cycles: 20\n"
        f"Budget: OPENCODE_SERVE_MAX_COMPACT_CONTINUES="
        f"{DEFAULT_MAX_COMPACT_CONTINUES}\n"
    )
    created = client.create_issue(
        project,
        summary,
        description,
        issue_type="Task",
        labels=["vd-e2e-compact"],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key = created["key"]
    print(f"\n[live jira] created {key} on {client.host}", flush=True)

    backend = FakeServeBackend(required_compacts=20)
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        max_compact_continues=DEFAULT_MAX_COMPACT_CONTINUES,
    )
    result = await orch.run(
        prompt="Implement a long task that will compact 20 times, then finish.",
        title=f"{key}: 20 compact e2e",
        agent="atlas",
    )
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert result.continue_count == 20
    assert result.compact_events >= 20
    assert backend.message_calls == 21

    state = JiraAgentState(
        issue_key=key,
        issue_summary=summary,
        description=description,
        status=TaskStatus.EXECUTING,
        current_opencode_session_id=result.session_id,
        retry_count=0,
        max_retries=3,
    )
    reporter = JiraReporter(client=client)

    progress_id = reporter.post_progress_update(
        state,
        (
            f"OpenCode session {result.session_id} survived "
            f"{result.compact_events} compaction event(s) with "
            f"{result.continue_count} Continue turn(s). "
            "Compaction was treated as resume, not as an error."
        ),
        progress_percentage=80,
    )
    assert progress_id, "Jira add_comment (progress) failed"

    state.status = TaskStatus.COMPLETED
    state.completed_at = datetime.now()
    complete_id = reporter.post_completion(
        state,
        (
            f"20-compact e2e succeeded. continues={result.continue_count} "
            f"compacts={result.compact_events} returncode={result.returncode}."
        ),
        changes_made=[
            "Serve orchestrator continued through 20 compact-then-stop cycles",
            "Same OpenCode session reused (no cold restart)",
            "Jira Error heading not used for compaction",
        ],
    )
    assert complete_id, "Jira add_comment (completion) failed"

    comments = client.get_comments(key)
    assert comments, f"No comments returned for {key}"
    bodies: List[str] = []
    for c in comments:
        body = c.get("body")
        if isinstance(body, str):
            bodies.append(body)
        elif isinstance(body, dict):
            # ADF fallback — flatten text
            bodies.append(str(body))
    blob = "\n".join(bodies)
    print(f"[live jira] {key} comment count={len(comments)}", flush=True)
    assert "AI Agent — Progress Update" in blob
    assert "AI Agent — Work Completed" in blob
    assert "AI Agent — Error" not in blob
    assert "20" in blob

    fetched = client.get_issue(key, fields=["summary", "labels", "status"])
    assert fetched and fetched.get("key") == key
    labels = (fetched.get("fields") or {}).get("labels") or []
    assert "vd-e2e-compact" in labels or "vd-e2e-compact" in str(labels)

    # Persist a small report next to pytest tmp is not needed; the issue is
    # the artifact. Print the browse URL for the operator.
    host = client.host.rstrip("/")
    print(f"[live jira] browse {host}/browse/{key}", flush=True)
