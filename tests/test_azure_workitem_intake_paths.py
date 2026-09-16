"""Azure work-item webhook intake: To Do / In Progress vs other columns."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.azure.workitems import (
    decide_azure_workitem_comment_webhook,
    decide_azure_workitem_webhook,
    evaluate_work_item_intake,
    lookup_work_item_view,
    normalize_work_item,
    parse_workitem_payload,
    work_item_is_done,
    work_item_is_intake_column,
)
from src.state.models import TaskStatus
from tests.conftest import make_issue_event
from tests.test_azure_workitems import _wi_comment_payload, _wi_payload


def _issue(
    *,
    work_item_id: int = 20,
    state: str = "To Do",
    assignee: str | None = "Yaver",
    unique: str = "DOMAIN\\yaver",
    tags: str = "",
    title: str = "Work",
    description: str = "{params}\nMode: build\n{params}",
    state_category: str = "",
):
    fields = {
        "System.Title": title,
        "System.Description": description,
        "System.State": state,
        "System.AssignedTo": (
            {
                "displayName": assignee,
                "uniqueName": unique,
                "id": "guid-1",
            }
            if assignee
            else None
        ),
        "System.Tags": tags,
        "System.WorkItemType": "Bug",
    }
    if fields["System.AssignedTo"] is None:
        fields.pop("System.AssignedTo")
    return normalize_work_item(
        project="Demo",
        work_item_id=work_item_id,
        fields=fields,
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        state_category=state_category,
    )


def _state(status: TaskStatus, **meta) -> MagicMock:
    row = MagicMock()
    row.status = status
    row.metadata = dict(meta)
    return row


# ---------------------------------------------------------------------------
# Done detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,category,expect",
    [
        ("New", "new", False),
        ("To Do", "new", False),
        ("Active", "indeterminate", False),
        ("Doing", "indeterminate", False),
        ("Committed", "indeterminate", False),
        ("Approved", "", False),
        ("Resolved", "done", False),
        ("In Progress", "indeterminate", False),
        ("Done", "done", True),
        ("Closed", "done", True),
        ("Completed", "done", True),
        ("Removed", "done", True),
        ("Cut", "", True),
        ("Kapatıldı", "", True),
        ("Tamamlandı", "", True),
        ("Bitti", "", True),
    ],
)
def test_work_item_is_done_names(name, category, expect):
    issue = _issue(state=name, state_category=category)
    assert work_item_is_done(issue["fields"]) is expect


def test_work_item_is_done_empty_and_missing():
    assert work_item_is_done(None) is False
    assert work_item_is_done({}) is False
    assert work_item_is_done({"status": {}}) is False


# ---------------------------------------------------------------------------
# First-sighting intake (no local state)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        "To Do",
        "Todo",
        "to-do",
        "New",
        "Open",
        "Backlog",
        "Proposed",
        "Approved",
        "In Progress",
        "InProgress",
        "in_progress",
        "Active",
        "Doing",
        "Committed",
        "WIP",
        "Yapılacaklar",
        "Yapilacak",
        "Devam Ediyor",
    ],
)
def test_intake_accepts_todo_in_progress_and_equivalents_when_assigned(state):
    decision = evaluate_work_item_intake(
        _issue(state=state), trigger_needles=["yaver"]
    )
    assert decision.action == "accept", (state, decision.reason)
    assert decision.will_process is True
    assert decision.is_done is False
    assert decision.matched_assignee is True
    assert work_item_is_intake_column(_issue(state=state)["fields"]) is True


@pytest.mark.parametrize("state", ["Resolved", "Resolve"])
def test_intake_skips_resolved_even_when_assigned(state):
    decision = evaluate_work_item_intake(
        _issue(state=state), trigger_needles=["yaver"]
    )
    assert decision.action == "skip", (state, decision.reason)
    assert decision.will_process is False
    assert decision.is_done is False
    assert decision.reason == "not todo or in progress"
    assert work_item_is_intake_column(_issue(state=state)["fields"]) is False


@pytest.mark.parametrize("state", ["Done", "Closed", "Completed", "Removed", "Cut"])
def test_intake_skips_done_columns_even_when_assigned(state):
    decision = evaluate_work_item_intake(
        _issue(state=state), trigger_needles=["yaver"]
    )
    assert decision.action == "skip"
    assert decision.is_done is True
    assert decision.reason == "done"
    assert decision.will_process is False


def test_intake_skips_unassigned_todo_item():
    decision = evaluate_work_item_intake(
        _issue(state="To Do", assignee=None), trigger_needles=["yaver"]
    )
    assert decision.action == "skip"
    assert decision.matched_assignee is False
    assert decision.reason == "not eligible"


def test_intake_skips_wrong_assignee_on_in_progress():
    decision = evaluate_work_item_intake(
        _issue(state="In Progress", assignee="Alice", unique="DOMAIN\\alice"),
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"
    assert decision.matched_assignee is False


# ---------------------------------------------------------------------------
# Local state: in-flight, plan_ready, completed, error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.EXECUTING],
)
def test_intake_skips_in_flight_on_in_progress(status):
    decision = evaluate_work_item_intake(
        _issue(state="In Progress"),
        state=_state(status),
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"
    assert "in-flight" in decision.reason


def test_intake_skips_plan_ready_on_in_progress():
    decision = evaluate_work_item_intake(
        _issue(state="In Progress"),
        state=_state(TaskStatus.PLAN_READY),
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"
    assert "plan_ready" in decision.reason
    assert decision.plan_handoff == ""


def test_intake_does_not_requeue_completed_on_todo_or_in_progress():
    completed = _state(TaskStatus.COMPLETED, last_board_status="in progress")
    for board in ("To Do", "In Progress"):
        decision = evaluate_work_item_intake(
            _issue(state=board),
            state=completed,
            prev_status="in progress",
            trigger_needles=["yaver"],
        )
        assert decision.action == "skip", board
        assert decision.reason == "not eligible"


def test_intake_does_not_requeue_cancelled_on_in_progress():
    decision = evaluate_work_item_intake(
        _issue(state="In Progress"),
        state=_state(TaskStatus.CANCELLED, requeue_eligible=True),
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"


def test_intake_error_same_text_does_not_retry():
    decision = evaluate_work_item_intake(
        _issue(state="In Progress", title="Same"),
        state=_state(TaskStatus.ERROR, requeue_eligible=True),
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"


def test_intake_error_text_change_retries_on_in_progress():
    err = _state(
        TaskStatus.ERROR,
        requeue_eligible=True,
        last_intake_fingerprint="old-hash",
    )
    decision = evaluate_work_item_intake(
        _issue(state="In Progress", title="Fixed title", description="new body"),
        state=err,
        trigger_needles=["yaver"],
    )
    assert decision.action == "accept"
    assert decision.reason == "issue text changed"
    assert decision.is_update is True


def test_intake_error_text_change_does_not_retry_when_done():
    err = _state(
        TaskStatus.ERROR,
        requeue_eligible=True,
        last_intake_fingerprint="old-hash",
    )
    decision = evaluate_work_item_intake(
        _issue(state="Done", title="Fixed title"),
        state=err,
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"
    assert decision.reason == "done"


def test_intake_error_text_change_retries_on_active():
    err = _state(
        TaskStatus.ERROR,
        requeue_eligible=True,
        last_intake_fingerprint="old-hash",
    )
    decision = evaluate_work_item_intake(
        _issue(state="Active", title="Fixed title", description="new body"),
        state=err,
        trigger_needles=["yaver"],
    )
    assert decision.action == "accept"
    assert decision.reason == "issue text changed"


def test_intake_error_text_change_does_not_retry_on_resolved():
    err = _state(
        TaskStatus.ERROR,
        requeue_eligible=True,
        last_intake_fingerprint="old-hash",
    )
    decision = evaluate_work_item_intake(
        _issue(state="Resolved", title="Fixed title", description="new body"),
        state=err,
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"
    assert decision.reason == "not todo or in progress"


def test_intake_error_without_requeue_flag_skips():
    decision = evaluate_work_item_intake(
        _issue(state="In Progress", title="Changed"),
        state=_state(
            TaskStatus.ERROR,
            requeue_eligible=False,
            last_intake_fingerprint="old",
        ),
        trigger_needles=["yaver"],
    )
    assert decision.action == "skip"


# ---------------------------------------------------------------------------
# Trigger label AND (To Do / In Progress)
# ---------------------------------------------------------------------------


def test_intake_trigger_label_required_on_in_progress():
    bare = evaluate_work_item_intake(
        _issue(state="In Progress", tags="other"),
        trigger_needles=["yaver"],
        required_labels=["bot"],
    )
    assert bare.action == "skip"
    tagged = evaluate_work_item_intake(
        _issue(state="In Progress", tags="bot"),
        trigger_needles=["yaver"],
        required_labels=["bot"],
    )
    assert tagged.action == "accept"
    done = evaluate_work_item_intake(
        _issue(state="Done", tags="bot"),
        trigger_needles=["yaver"],
        required_labels=["bot"],
    )
    assert done.action == "skip"
    assert done.reason == "done"


# ---------------------------------------------------------------------------
# Lookup view
# ---------------------------------------------------------------------------


def test_lookup_view_in_progress_would_process():
    view = lookup_work_item_view(
        _issue(state="In Progress"), trigger_needles=["yaver"]
    )
    assert view["ok"] is True
    assert view["will_process"] is True
    assert view["is_done"] is False
    assert view["is_todo"] is False
    assert view["state"] == "In Progress"


def test_lookup_view_active_would_process():
    view = lookup_work_item_view(_issue(state="Active"), trigger_needles=["yaver"])
    assert view["ok"] is True
    assert view["will_process"] is True
    assert view["is_done"] is False
    assert view["is_todo"] is False
    assert view["state"] == "Active"


def test_lookup_view_resolved_would_not_process():
    view = lookup_work_item_view(_issue(state="Resolved"), trigger_needles=["yaver"])
    assert view["ok"] is True
    assert view["will_process"] is False
    assert view["is_done"] is False
    assert view["reason"] == "not todo or in progress"


def test_lookup_view_done_would_not_process():
    view = lookup_work_item_view(_issue(state="Done"), trigger_needles=["yaver"])
    assert view["will_process"] is False
    assert view["is_done"] is True
    assert view["action"] == "skip"
    assert view["reason"] == "done"


# ---------------------------------------------------------------------------
# Official Server 2022 service-hook envelopes
# ---------------------------------------------------------------------------


def test_parse_official_created_payload_flat_fields():
    payload = {
        "eventType": "workitem.created",
        "publisherId": "tfs",
        "resource": {
            "id": 5,
            "rev": 1,
            "fields": {
                "System.AreaPath": "Demo",
                "System.TeamProject": "Demo",
                "System.WorkItemType": "Bug",
                "System.State": "Active",
                "System.Title": "Created active",
                "System.AssignedTo": {
                    "displayName": "Yaver",
                    "uniqueName": r"DOMAIN\yaver",
                    "id": "guid-1",
                },
                "System.ChangedBy": "Alice",
            },
            "url": (
                "https://tfs.example.com/tfs/DefaultCollection/"
                "_apis/wit/workItems/5"
            ),
        },
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/"
            },
            "project": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/Demo/"
            },
        },
    }
    parsed = parse_workitem_payload(payload)
    assert parsed is not None
    assert parsed.work_item_id == 5
    assert parsed.issue["fields"]["status"]["name"] == "Active"
    assert parsed.issue["fields"]["assignee"]["displayName"] == "Yaver"
    decision = decide_azure_workitem_webhook(payload, enabled=True)
    assert decision.accepted is True


def test_parse_official_commented_history_string():
    payload = {
        "eventType": "workitem.commented",
        "publisherId": "tfs",
        "resource": {
            "id": 5,
            "rev": 4,
            "fields": {
                "System.TeamProject": "Demo",
                "System.State": "Active",
                "System.Title": "Bug",
                "System.ChangedBy": "Alice",
                "System.History": "@yaver /planExecute",
            },
            "url": (
                "https://tfs.example.com/tfs/DefaultCollection/"
                "_apis/wit/workItems/5"
            ),
        },
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/"
            }
        },
    }
    parsed = parse_workitem_payload(payload)
    assert parsed is not None
    assert parsed.work_item_id == 5
    comment = decide_azure_workitem_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver"]
    )
    assert comment.accepted is True
    assert comment.event.plan_handoff == "execute"


def test_webhook_updated_on_active_assignee_is_accepted():
    decision = decide_azure_workitem_webhook(
        _wi_payload(state="Active"),
        enabled=True,
    )
    assert decision.accepted is True
    assert "assignee" in decision.event.change_kinds


def test_webhook_updated_on_done_still_accepted_then_intake_skips():
    """Hook layer only filters fields/PAT; Done is an intake rule."""
    payload = _wi_payload(state="Done")
    hook = decide_azure_workitem_webhook(payload, enabled=True)
    assert hook.accepted is True
    decision = evaluate_work_item_intake(
        hook.event.issue, trigger_needles=["yaver"]
    )
    assert decision.action == "skip"
    assert decision.reason == "done"


def test_webhook_ignores_pat_update_on_active():
    payload = _wi_payload(state="Active")
    payload["resource"]["revisedBy"] = {
        "displayName": "Yaver Bot",
        "uniqueName": r"DOMAIN\yaver",
        "id": "pat-guid",
    }
    with patch(
        "src.azure.workitems.fetch_bot_identity",
        return_value={"id": "pat-guid", "names": ["Yaver Bot", r"DOMAIN\yaver"]},
    ):
        decision = decide_azure_workitem_webhook(payload, enabled=True)
    assert decision.accepted is False
    assert "PAT" in decision.reason


# ---------------------------------------------------------------------------
# Ingest: In Progress starts, Done does not
# ---------------------------------------------------------------------------


def _processor():
    from src.processor import JobProcessor

    proc = JobProcessor.__new__(JobProcessor)
    proc.state_manager = MagicMock()
    proc.state_manager.get_state.return_value = None
    proc.enqueue_jira_event = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "started": False,
            "issue_key": "20",
            "status": "queued",
        }
    )
    proc._record_workitem_board_status = MagicMock()
    return proc


def test_ingest_stops_when_pat_assign_fails():
    parsed = parse_workitem_payload(
        _wi_payload(state="In Progress", work_item_id=20)
    )
    assert parsed is not None
    proc = _processor()

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
            return_value=True,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
            return_value=False,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.add_comment",
            return_value={"id": "1"},
        ) as commented, patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            return await proc.ingest_azure_work_item(parsed), commented

    result, commented = asyncio.run(_run())
    assert result["ok"] is False
    assert result["reason"] == "pat assign failed"
    commented.assert_called()
    proc.enqueue_jira_event.assert_not_awaited()


def test_ingest_in_progress_assigned_enqueues_and_assigns():
    parsed = parse_workitem_payload(
        _wi_payload(state="In Progress", work_item_id=20)
    )
    assert parsed is not None
    proc = _processor()

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
            return_value=True,
        ) as moved, patch(
            "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
            return_value=True,
        ) as assigned, patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            return await proc.ingest_azure_work_item(parsed), moved, assigned

    result, moved, assigned = asyncio.run(_run())
    assert result["ok"] is True
    assert result["status"] != "skipped"
    moved.assert_called()
    assigned.assert_called()
    proc.enqueue_jira_event.assert_awaited_once()


def test_ingest_done_assigned_skips_without_enqueue():
    parsed = parse_workitem_payload(_wi_payload(state="Done", work_item_id=21))
    assert parsed is not None
    proc = _processor()

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
            return_value=True,
        ) as moved, patch(
            "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
            return_value=True,
        ) as assigned, patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            return await proc.ingest_azure_work_item(parsed), moved, assigned

    result, moved, assigned = asyncio.run(_run())
    assert result["status"] == "skipped"
    assert result["reason"] == "done"
    moved.assert_not_called()
    assigned.assert_not_called()
    proc.enqueue_jira_event.assert_not_awaited()


def test_ingest_unassigned_in_progress_skips():
    parsed = parse_workitem_payload(
        _wi_payload(state="In Progress", assignee="", work_item_id=22)
    )
    assert parsed is not None
    proc = _processor()

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            return await proc.ingest_azure_work_item(parsed)

    result = asyncio.run(_run())
    assert result["status"] == "skipped"
    proc.enqueue_jira_event.assert_not_awaited()


def test_ingest_in_flight_in_progress_skips():
    parsed = parse_workitem_payload(
        _wi_payload(state="In Progress", work_item_id=23)
    )
    assert parsed is not None
    proc = _processor()
    flying = MagicMock()
    flying.status = TaskStatus.EXECUTING
    flying.metadata = {}
    proc.state_manager.get_state.return_value = flying

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            return await proc.ingest_azure_work_item(parsed)

    result = asyncio.run(_run())
    assert result["status"] == "skipped"
    assert "in-flight" in result["reason"]
    proc.enqueue_jira_event.assert_not_awaited()


def test_ingest_plan_ready_in_progress_skips_field_update():
    parsed = parse_workitem_payload(
        _wi_payload(state="In Progress", work_item_id=24)
    )
    assert parsed is not None
    proc = _processor()
    waiting = MagicMock()
    waiting.status = TaskStatus.PLAN_READY
    waiting.metadata = {}
    proc.state_manager.get_state.return_value = waiting

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            return await proc.ingest_azure_work_item(parsed)

    result = asyncio.run(_run())
    assert result["status"] == "skipped"
    assert "plan_ready" in result["reason"]
    proc.enqueue_jira_event.assert_not_awaited()


def test_ingest_plan_execute_on_active_plan_ready_assigns():
    from src.azure.workitems import parse_workitem_payload as parse

    parsed = parse(_wi_comment_payload(state="Active"))
    assert parsed is not None
    parsed.plan_handoff = "execute"
    proc = _processor()
    ready = MagicMock()
    ready.status = TaskStatus.PLAN_READY
    ready.metadata = {}
    proc.state_manager.get_state.return_value = ready

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
            return_value=True,
        ) as moved, patch(
            "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
            return_value=True,
        ) as assigned:
            return await proc.ingest_azure_work_item(parsed), moved, assigned

    result, moved, assigned = asyncio.run(_run())
    assert result["ok"] is True
    moved.assert_called()
    assigned.assert_called()


# ---------------------------------------------------------------------------
# Processor update path: Azure To Do / In Progress vs Done vs Jira To Do
# ---------------------------------------------------------------------------


@pytest.fixture
def processor(state_manager, reporter, fake_jira):
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc.git_manager = None
    proc.agent_runner = None
    return proc


def _azure_update_event(key: str, status: str) -> dict:
    event = make_issue_event(
        key=key, event_type="jira:issue_updated", status=status
    )
    event["azure_workitem"] = True
    event["issue"]["azure"] = {
        "work_item_id": int(key),
        "project": "Demo",
        "collection_url": "https://tfs.example.com/tfs/DefaultCollection",
    }
    return event


@pytest.mark.asyncio
async def test_processor_azure_completed_in_progress_reprocesses(
    processor, state_manager
):
    state_manager.create_state("42", "Work", "x")
    state_manager.update_state(
        "42", status=TaskStatus.COMPLETED, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(
            _azure_update_event("42", "In Progress")
        )
        created.assert_awaited_once()
    assert state_manager.get_state("42").status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_processor_azure_completed_active_reprocesses(
    processor, state_manager
):
    state_manager.create_state("47", "Work", "x")
    state_manager.update_state(
        "47", status=TaskStatus.COMPLETED, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(_azure_update_event("47", "Active"))
        created.assert_awaited_once()
    assert state_manager.get_state("47").status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_processor_azure_completed_resolved_does_not_reprocess(
    processor, state_manager
):
    state_manager.create_state("49", "Work", "x")
    state_manager.update_state(
        "49", status=TaskStatus.COMPLETED, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(_azure_update_event("49", "Resolved"))
        created.assert_not_called()
    assert state_manager.get_state("49").status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_processor_azure_completed_done_does_not_reprocess(
    processor, state_manager
):
    state_manager.create_state("43", "Work", "x")
    state_manager.update_state(
        "43", status=TaskStatus.COMPLETED, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(_azure_update_event("43", "Done"))
        created.assert_not_called()
    assert state_manager.get_state("43").status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_processor_azure_error_in_progress_needs_requeue_flag(
    processor, state_manager
):
    state_manager.create_state("44", "Work", "x")
    state_manager.update_state(
        "44",
        status=TaskStatus.ERROR,
        metadata={"source": "azure_workitem"},
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(
            _azure_update_event("44", "In Progress")
        )
        created.assert_not_called()

    state_manager.update_state("44", metadata={"requeue_eligible": True})
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(
            _azure_update_event("44", "In Progress")
        )
        created.assert_awaited_once()


@pytest.mark.asyncio
async def test_processor_jira_in_progress_still_does_not_reprocess(
    processor, state_manager
):
    state_manager.create_state("PROJ-11", "Work", "x")
    state_manager.update_state("PROJ-11", status=TaskStatus.COMPLETED)
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(
            make_issue_event(
                key="PROJ-11",
                event_type="jira:issue_updated",
                status="In Progress",
            )
        )
        created.assert_not_called()


@pytest.mark.asyncio
async def test_processor_azure_pending_in_progress_kicks_created(
    processor, state_manager
):
    state_manager.create_state("45", "Work", "x")
    state_manager.update_state(
        "45", status=TaskStatus.PENDING, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(
            _azure_update_event("45", "In Progress")
        )
        created.assert_awaited_once()


@pytest.mark.asyncio
async def test_processor_azure_pending_active_kicks_created(
    processor, state_manager
):
    state_manager.create_state("48", "Work", "x")
    state_manager.update_state(
        "48", status=TaskStatus.PENDING, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(_azure_update_event("48", "Active"))
        created.assert_awaited_once()


@pytest.mark.asyncio
async def test_processor_azure_executing_active_ignored(processor, state_manager):
    state_manager.create_state("46", "Work", "x")
    state_manager.update_state(
        "46", status=TaskStatus.EXECUTING, metadata={"source": "azure_workitem"}
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(_azure_update_event("46", "Active"))
        created.assert_not_called()
    assert state_manager.get_state("46").status == TaskStatus.EXECUTING
