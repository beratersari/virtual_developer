"""LIVE Jira API edge-case repro for the critical review findings.

Creates/updates real Cloud issues (NO bot/ai-assist labels — poller must
not start a daemon job). Compact orchestration uses FakeServeBackend so we
do not wait on a live LLM.

Run::

    .venv/bin/python -m pytest tests/test_live_review_critical_jira.py -v -s

Skipped when Jira is unconfigured or the probe fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

import pytest

from src.config import settings
from src.git_manager import GitManager
from src.issue_git_spec import parse_issue_git_spec, parse_issue_mode
from src.jira.client import JiraClient
from src.opencode_serve import ServeOrchestrator
from src.reporter.jira_reporter import JiraReporter
from src.state.models import JiraAgentState, TaskStatus
from src.state.queue_store import workspace_lock_key
from tests.test_opencode_serve_e2e import FakeServeBackend, FakeServeClient


E2E_LABEL = "vd-review-e2e"
# Never use trigger labels — the live daemon is running.


def _jira_live_ready() -> str:
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    return ""


def _comment_blob(comments: List[Dict[str, Any]]) -> str:
    bodies: List[str] = []
    for c in comments:
        body = c.get("body")
        if isinstance(body, str):
            bodies.append(body)
        elif isinstance(body, dict):
            bodies.append(str(body))
    return "\n".join(bodies)


@pytest.fixture(scope="module")
def jira_client():
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
    return client


def _project() -> str:
    return ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()


def _create(client: JiraClient, summary: str, description: str) -> str:
    created = client.create_issue(
        _project(),
        summary,
        description,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key = created["key"]
    print(f"\n[live jira] created {key}  {client.host.rstrip('/')}/browse/{key}", flush=True)
    return key


@pytest.mark.asyncio
async def test_live_jira_compact_then_stop_is_not_resumed(jira_client: JiraClient):
    """20 required compact cycles + no auto-resume → one wait, then incomplete.

    Posts the real reporter incomplete comment onto a live issue.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = _create(
        jira_client,
        f"[vd-review] compact-then-stop no-resume {stamp}",
        (
            "Automated review repro (do not process — no trigger label).\n"
            "Scenario: compact-then-stop without OpenCode auto-resume.\n"
            "Mode: e2e-review\n"
            "{params}\n"
            "Repository: https://example.invalid/vd/review.git\n"
            "Source branch: develop\n"
            "Target branch: main\n"
            "Mode: plan\n"
            "{params}\n"
        ),
    )

    backend = FakeServeBackend(required_compacts=20)
    backend.auto_complete_on_idle = False
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=0.4,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.1,
        max_compact_continues=256,
    )
    result = await orch.run(
        prompt="Long task that must survive 20 compact cycles.",
        title=f"{key}: compact-then-stop",
        agent="atlas",
    )

    finding = (
        f"returncode={result.returncode} incomplete={result.incomplete} "
        f"continues={result.continue_count} message_calls={backend.message_calls} "
        f"reasons={result.incomplete_reasons}"
    )
    print(f"[compact-no-resume] {finding}", flush=True)

    # Product today: one user turn, zero Continue, then incomplete.
    assert backend.message_calls == 1
    assert result.continue_count == 0
    assert result.returncode != 0
    assert "Finish remaining todos" not in "\n".join(backend.prompts)

    state = JiraAgentState(
        issue_key=key,
        issue_summary=f"[vd-review] compact-then-stop no-resume {stamp}",
        description="",
        status=TaskStatus.ERROR,
        current_opencode_session_id=result.session_id,
        retry_count=0,
        max_retries=3,
    )
    reporter = JiraReporter(client=jira_client)
    comment_id = reporter.post_error(
        state,
        result.stderr or finding,
        suggestion=(
            "OPENCODE_SERVE_MAX_COMPACT_CONTINUES=256 did not send another turn. "
            "Raise that setting does not resume this session."
        ),
        category="incomplete",
    )
    assert comment_id, "Jira post_error (incomplete) failed"

    # Wrapped / failed snapshots must stay incomplete (fail closed).
    from tests.test_critical_fixes_e2e import _serve_client

    wrapped_backend = FakeServeBackend(required_compacts=20)
    wrapped_backend.auto_complete_on_idle = False
    wrapped_client = _serve_client(wrapped_backend, wrap=True)
    try:
        wrapped = await ServeOrchestrator(
            client=wrapped_client,
            compact_wait_seconds=0.4,
            compact_poll_seconds=0.05,
            compact_settle_seconds=0.1,
        ).run(
            prompt="Same compact-then-stop but message list is wrapped object.",
            title=f"{key}: wrapped snapshot",
        )
    finally:
        await wrapped_client.aclose()
    print(
        f"[compact-empty-snapshot wrapped] returncode={wrapped.returncode} "
        f"incomplete={wrapped.incomplete} reasons={wrapped.incomplete_reasons}",
        flush=True,
    )
    assert wrapped.returncode != 0
    assert wrapped.incomplete or wrapped.timed_out

    raised_backend = FakeServeBackend(required_compacts=2)
    raised_backend.auto_complete_on_idle = False
    raised_client = _serve_client(raised_backend, fail_lists=True)
    try:
        raised = await ServeOrchestrator(
            client=raised_client,
            compact_wait_seconds=0.4,
            compact_poll_seconds=0.05,
            compact_settle_seconds=0.1,
        ).run(
            prompt="Compact-then-stop but list_messages returns 500.",
            title=f"{key}: list raise",
        )
    finally:
        await raised_client.aclose()
    print(
        f"[compact-empty-snapshot raise] returncode={raised.returncode} "
        f"incomplete={raised.incomplete} reasons={raised.incomplete_reasons}",
        flush=True,
    )
    assert raised.returncode != 0
    assert raised.incomplete or raised.timed_out

    note = reporter.post_error(
        state,
        (
            "Fail-closed snapshot check: wrapped list "
            f"returncode={wrapped.returncode} incomplete={wrapped.incomplete}; "
            f"HTTP 500 list returncode={raised.returncode} "
            f"incomplete={raised.incomplete}. Compact-then-stop with a good "
            f"snapshot stayed incomplete (returncode={result.returncode}, "
            f"continues={result.continue_count})."
        ),
        suggestion="These paths must not mark the Jira issue completed.",
        category="incomplete",
    )
    assert note, "Jira post_error (snapshot fail-closed) failed"

    comments = jira_client.get_comments(key)
    blob = _comment_blob(comments)
    assert "Incomplete session (context compaction)" in blob
    assert "Fail-closed snapshot check" in blob
    assert "AI Agent — Work Completed" not in blob

    fetched = jira_client.get_issue(key, fields=["summary", "labels", "status"])
    assert fetched and fetched.get("key") == key
    labels = (fetched.get("fields") or {}).get("labels") or []
    assert E2E_LABEL in labels or E2E_LABEL in str(labels)
    assert "bot" not in [str(x).lower() for x in labels]
    assert "ai-assist" not in [str(x).lower() for x in labels]


def test_live_jira_create_update_params_edge_cases(jira_client: JiraClient):
    """Create + update a live issue through params / transition / append edges."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = _create(
        jira_client,
        f"[vd-review] params/update edges {stamp}",
        (
            "Automated review repro (do not process).\n"
            "{params}\n"
            "Repository: https://gitlab.com/example/shared-source.git\n"
            "Source branch: feature/shared-review\n"
            "Target branch: develop\n"
            "Mode: plan\n"
            "{params}\n"
        ),
    )

    issue = jira_client.get_issue(key, fields=["summary", "description", "labels", "status"])
    assert issue
    fields = issue.get("fields") or {}
    desc = fields.get("description") or ""
    spec, spec_err = parse_issue_git_spec(
        summary=fields.get("summary") or "", description=desc
    )
    mode = parse_issue_mode(summary=fields.get("summary") or "", description=desc)
    assert spec is not None, spec_err
    assert spec.source_branch == "feature/shared-review"
    assert spec.target_branch == "develop"
    assert mode == "plan"

    # Edge: Mode: build alone on the same ticket (must not look like auto-start
    # to the poller because there is still no trigger label).
    updated_desc = desc.replace("Mode: plan", "Mode: build")
    assert jira_client.update_issue(key, fields={"description": updated_desc})
    again = jira_client.get_issue(key, fields=["description"])
    new_desc = (again.get("fields") or {}).get("description") or ""
    assert parse_issue_mode(description=new_desc) == "build"

    # Edge: one-sided {params} must not invent a spec.
    assert jira_client.update_issue(
        key,
        fields={
            "description": (
                "Broken template (only opening marker).\n"
                "{params}\n"
                "Repository: https://gitlab.com/example/shared-source.git\n"
                "Source branch: feature/shared-review\n"
            )
        },
    )
    broken = jira_client.get_issue(key, fields=["description"])
    broken_desc = (broken.get("fields") or {}).get("description") or ""
    broken_spec, broken_err = parse_issue_git_spec(description=broken_desc)
    print(f"[params-one-sided] spec={broken_spec} err={broken_err}", flush=True)
    assert broken_spec is None

    # Restore a valid block and append (must not wipe params).
    restored = (
        "Restored after one-sided params test.\n"
        "{params}\n"
        "Repository: https://gitlab.com/example/shared-source.git\n"
        "Source branch: feature/shared-review\n"
        "Target branch: develop\n"
        "Mode: plan\n"
        "{params}\n"
    )
    assert jira_client.update_issue(key, fields={"description": restored})
    assert jira_client.append_to_description(key, "## Plan\n\nThis is a fake plan body.")
    appended = jira_client.get_issue(key, fields=["description"])
    body = (appended.get("fields") or {}).get("description") or ""
    assert "{params}" in body
    assert "feature/shared-review" in body
    assert "fake plan body" in body

    # Labels: merge must keep e2e label; do not add bot.
    assert jira_client.add_labels(key, ["vd-review-params"])
    labeled = jira_client.get_issue(key, fields=["labels"])
    labels = (labeled.get("fields") or {}).get("labels") or []
    label_set = {str(x).lower() for x in labels}
    assert E2E_LABEL in label_set
    assert "vd-review-params" in label_set
    assert "bot" not in label_set

    # Transition edges: In Progress then back to To Do (rework signal only
    # matters with a trigger label — we keep this ticket inert).
    moved = jira_client.transition_to_in_progress(key)
    print(f"[transition] in_progress={moved}", flush=True)
    if moved:
        back = jira_client.transition_issue(key, "To Do")
        print(f"[transition] back_to_do={back}", flush=True)
        assert back

    # Two-ticket shared-source lock identity (Jira keys + same {params}).
    key_b = _create(
        jira_client,
        f"[vd-review] shared-source twin {stamp}",
        restored,
    )
    repo = "https://gitlab.com/example/shared-source.git"
    work = GitManager.resolve_work_branch_name(
        key, "feature/shared-review", "develop"
    )
    work_b = GitManager.resolve_work_branch_name(
        key_b, "feature/shared-review", "develop"
    )
    lock_a = workspace_lock_key(repo, work, "develop")
    lock_b = workspace_lock_key(repo, work_b, "develop")
    print(f"[shared-source] {key} work={work} lock={lock_a}", flush=True)
    print(f"[shared-source] {key_b} work={work_b} lock={lock_b}", flush=True)
    assert work == work_b == "feature/shared-review"
    assert lock_a == lock_b, "Same Source+Target must share the clone lock"

    # GitLab enqueue now uses keep_source=True (same folder as the clone).
    gl_work_keep = GitManager.resolve_work_branch_name(
        key, "develop", "main", keep_source=True
    )
    gl_work_queue = GitManager.resolve_work_branch_name(
        key, "develop", "main", keep_source=True
    )
    gl_lock_keep = workspace_lock_key(repo, gl_work_keep, "main")
    gl_lock_queue = workspace_lock_key(repo, gl_work_queue, "main")
    print(
        f"[gitlab-lock] keep={gl_work_keep}/{gl_lock_keep} "
        f"queue={gl_work_queue}/{gl_lock_queue}",
        flush=True,
    )
    assert gl_work_keep == gl_work_queue == "develop"
    assert gl_lock_keep == gl_lock_queue

    jira_client.add_comment(
        key,
        (
            "h3. Review edge-case notes\n\n"
            f"* Shared-source twin: {key_b} (same lock {lock_a})\n"
            f"* GitLab keep_source lock == queue lock "
            f"({gl_work_keep})\n"
            "* One-sided {{params}} parsed as no spec\n"
            "* append_to_description kept the params block\n"
        ),
    )
    comments = jira_client.get_comments(key)
    assert comments
    assert "Review edge-case notes" in _comment_blob(comments)
    print(f"[live jira] browse {jira_client.host.rstrip('/')}/browse/{key}", flush=True)
    print(f"[live jira] browse {jira_client.host.rstrip('/')}/browse/{key_b}", flush=True)
