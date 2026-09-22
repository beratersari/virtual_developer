"""Dashboard Implement / Revise writes the existing Jira or Azure signal."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.jira.plan_labels import (
    PLAN_EXECUTE_LABEL,
    infer_plan_handoff,
    latest_comment_tagging_pat_user,
)
from src.processor import JobProcessor
from src.state.models import TaskStatus


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    plan = tmp_path / "plans"
    plan.mkdir()
    monkeypatch.setattr(
        proc,
        "_durable_plan_path",
        lambda key: plan / f"{key}.md",
    )
    (plan / "KAN-1.md").write_text("# plan\n", encoding="utf-8")
    (plan / "WIT-DEMO-42.md").write_text("# plan\n", encoding="utf-8")
    return proc


def _ready(state_manager, key: str, **metadata):
    state_manager.create_state(key, "Plan login", "{params}\nMode: plan\n{params}")
    state_manager.update_state(key, status=TaskStatus.PLAN_READY, metadata=metadata)


@pytest.mark.asyncio
async def test_dashboard_implement_sets_plan_execute_label(processor, state_manager):
    _ready(state_manager, "KAN-1")
    processor.start_plan_execution = AsyncMock()
    result = await processor.request_plan_execute_from_dashboard("KAN-1")
    assert result["ok"] is True
    assert "next poll" in result["message"]
    labels = [row["labels"] for row in processor.jira_client.updated if row.get("labels")]
    assert [PLAN_EXECUTE_LABEL] in labels
    processor.start_plan_execution.assert_not_awaited()
    assert (
        infer_plan_handoff(
            {
                "status": {
                    "name": "In Progress",
                    "statusCategory": {"key": "indeterminate"},
                },
                "labels": [PLAN_EXECUTE_LABEL],
            }
        )
        == "execute"
    )


@pytest.mark.asyncio
async def test_dashboard_revise_posts_mention_and_plan_refactor(
    processor, state_manager
):
    _ready(state_manager, "KAN-1")
    name = settings.jira_trigger_user_list[0]
    result = await processor.request_plan_refactor_from_dashboard(
        "KAN-1", "  Use Redis instead of memory  "
    )
    assert result["ok"] is True
    body = processor.jira_client.comments[-1]["body"]
    assert "Use Redis instead of memory" in body
    assert name.lstrip("@") in body
    assert latest_comment_tagging_pat_user(
        [{"body": body}],
        mention_tokens=[name],
        extra_needles=[name],
    )
    label_writes = [row["labels"] for row in processor.jira_client.updated]
    assert [] in label_writes
    assert ["plan_refactor"] in label_writes


@pytest.mark.asyncio
async def test_dashboard_implement_rejects_without_plan(processor, state_manager, tmp_path):
    _ready(state_manager, "KAN-2")
    result = await processor.request_plan_execute_from_dashboard("KAN-2")
    assert result["ok"] is False
    assert "plan file" in result["error"]


@pytest.mark.asyncio
async def test_dashboard_revise_rejects_empty_prompt(processor, state_manager):
    _ready(state_manager, "KAN-1")
    result = await processor.request_plan_refactor_from_dashboard("KAN-1", "   ")
    assert result["ok"] is False
    assert processor.jira_client.comments == []


@pytest.mark.asyncio
async def test_dashboard_azure_implement_uses_comment_ingest(
    processor, state_manager, monkeypatch
):
    monkeypatch.setattr(settings, "azure_trigger_user", "yaver")
    _ready(
        state_manager,
        "WIT-DEMO-42",
        source="azure_workitem",
        azure_host="tfs.example.com",
        azure_collection_url="https://tfs.example.com/tfs/DefaultCollection",
        azure_project="Demo",
        azure_work_item_id=42,
    )
    seen = {}

    async def fake_ingest(event):
        seen["event"] = event
        return {"ok": True, "started": True, "queued": False, "status": "running"}

    processor.ingest_azure_work_item = fake_ingest
    result = await processor.request_plan_execute_from_dashboard("WIT-DEMO-42")
    assert result["ok"] is True
    event = seen["event"]
    assert event.plan_handoff == "execute"
    assert event.comment_body.endswith("/planExecute")
    assert processor.jira_client.updated == []


@pytest.mark.asyncio
async def test_dashboard_azure_revise_passes_prompt(processor, state_manager, monkeypatch):
    monkeypatch.setattr(settings, "azure_trigger_user", "yaver")
    _ready(
        state_manager,
        "WIT-DEMO-42",
        source="azure_workitem",
        azure_host="tfs.example.com",
        azure_collection_url="https://tfs.example.com/tfs/DefaultCollection",
        azure_project="Demo",
        azure_work_item_id=42,
    )
    seen = {}

    async def fake_ingest(event):
        seen["event"] = event
        return {"ok": True, "started": True, "queued": False}

    processor.ingest_azure_work_item = fake_ingest
    result = await processor.request_plan_refactor_from_dashboard(
        "WIT-DEMO-42", "Tighten the API"
    )
    assert result["ok"] is True
    event = seen["event"]
    assert event.plan_handoff == "refactor"
    assert event.plan_comment == "Tighten the API"
    assert "/planRefactor Tighten the API" in event.comment_body


def test_plan_http_execute_and_start_stays_disabled(processor, state_manager):
    _ready(state_manager, "KAN-1")
    app = create_dashboard_app(
        processor=processor, state_manager=state_manager
    )
    client = TestClient(app)
    blocked = client.post("/api/tasks/KAN-1/start")
    assert blocked.status_code == 410
    ok = client.post("/api/tasks/KAN-1/plan-execute")
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    missing = client.post(
        "/api/tasks/KAN-1/plan-refactor",
        json={"prompt": "   "},
    )
    assert missing.status_code == 422
    revised = client.post(
        "/api/tasks/KAN-1/plan-refactor",
        json={"prompt": "Add retries"},
    )
    # Status is still plan_ready; the label write does not change local status.
    assert revised.status_code == 200
    assert "Add retries" in processor.jira_client.comments[-1]["body"]
