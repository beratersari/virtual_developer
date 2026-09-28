"""Critical usage bugs found by reading the current source.

Each test calls production code with a small input and expects the
outcome an operator needs. They fail on the code as it is today.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from src.azure.workitems import decide_azure_workitem_comment_webhook
from src.backends.claude import claude_reply_asks_question, parse_claude_output
from src.backends.codex import parse_codex_thread_id
from src.git_manager import GitManager
from src.gitlab.mentions import note_is_execute_command
from src.opencode_sessions import assess_session_completeness
from src.processor import JobProcessor
from src.review_flow import azure_reviewer_just_assigned
from src.scheduler.service import (
    _dispatch_claimed_schedule,
    _reopen_skipped_dispatched_schedules,
)
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore
from src.state.schedule_store import ScheduleStore

_THREAD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OTHER = "11111111-2222-3333-4444-555555555555"


def _git(issue_key: str) -> GitManager:
    git = GitManager.__new__(GitManager)
    git.issue_key = issue_key
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.work_branch = "feature/shared"
    git.target_branch = "develop"
    git.remote_name = "app"
    return git


def test_same_repo_and_branches_share_one_clone_folder():
    first = _git("KAN-1")._workspace_folder_name()
    second = _git("KAN-2")._workspace_folder_name()
    assert first == second


def test_existing_mr_without_target_branch_is_not_reused():
    git = _git("KAN-1")
    mr = {"source_branch": "feature/X", "web_url": "https://gl.example/mr/9"}
    assert git._mr_matches_target(mr, "develop") is False


def test_fenced_usage_example_does_not_start_yaver():
    note = "Example:\n```\n@yaver /yaver do something\n```\n"
    assert note_is_execute_command(note, ["yaver"]) is False


def test_short_bot_name_does_not_match_another_reviewer():
    pr = {
        "pullRequestId": 9,
        "repository": {"id": "repo1", "name": "app"},
        "reviewers": [{"uniqueName": "dev", "displayName": "Dev"}],
    }
    payload = {"message": {"text": "Alice added Developer as a reviewer"}}
    assert (
        azure_reviewer_just_assigned(
            payload,
            pr,
            names=["dev"],
            collection_url="https://tfs.example/tfs/Col",
        )
        is False
    )


def test_bot_work_item_comment_is_ignored_without_identity_lookup(monkeypatch):
    monkeypatch.setattr(
        "src.azure.workitems.actor_is_pat_user", lambda *args, **kwargs: False
    )
    payload = {
        "eventType": "workitem.commented",
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection"
            }
        },
        "resource": {
            "id": 42,
            "workItemId": 42,
            "rev": 4,
            "fields": {"System.TeamProject": "Proj", "System.Title": "t"},
            "comment": {"text": "@yaver /planExecute ship the fix"},
            "revisedBy": {
                "id": "abc",
                "uniqueName": "DOMAIN\\yaver",
                "displayName": "Yaver",
            },
            "url": (
                "https://tfs.example.com/tfs/DefaultCollection"
                "/_apis/wit/workItems/42"
            ),
        },
    }
    decision = decide_azure_workitem_comment_webhook(
        payload, bot_mentions=["yaver"], enabled=True
    )
    assert decision.accepted is False
    assert decision.reason == "ignored bot author"


def test_opencode_error_finish_is_not_a_completed_session():
    for finish in ("error", "abort", "length"):
        assessment = assess_session_completeness(
            "ses_x",
            messages=[
                {
                    "role": "assistant",
                    "finish": finish,
                    "parts": [{"type": "text", "text": "stopped"}],
                }
            ],
            todos=[],
        )
        assert assessment["complete"] is False, finish
        assert assessment["premature"] is True


def test_claude_empty_final_result_uses_the_final_message():
    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Which database should I use?"},
                            {"type": "tool_use", "name": "AskUserQuestion"},
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "",
                    "session_id": _THREAD,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Committed the fix."}],
                    },
                }
            ),
        ]
    )
    parsed = parse_claude_output(raw)
    assert parsed["text"] == "Committed the fix."
    assert claude_reply_asks_question(parsed) is False


def test_codex_session_id_outside_command_output_keeps_thread_id():
    plain = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": _THREAD}),
            f"session_id: {_OTHER}",
        ]
    )
    assert parse_codex_thread_id(plain) == _THREAD
    message = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": _THREAD}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": f"session_id: {_OTHER}",
                    },
                }
            ),
        ]
    )
    assert parse_codex_thread_id(message) == _THREAD


def _processor(tmp_path):
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(tmp_path / "state")
    proc.queue_store = WorkQueueStore(tmp_path / "queue")
    return proc


def _schedule(tmp_path, issue_key: str) -> tuple[ScheduleStore, dict]:
    store = ScheduleStore(tmp_path / "schedules")
    rec = store.create(
        title="follow up",
        description="do the scheduled prompt",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=datetime.now().isoformat(timespec="seconds"),
        issue_key=issue_key,
        issue_description="Mode: build\n{params}\nRepository: https://gitlab.example.com/acme/app.git\n",
    )
    claimed = store.claim_for_dispatch(rec["schedule_id"])
    assert claimed is not None
    return store, claimed


def test_schedule_while_plan_ready_does_not_stay_dispatched(tmp_path):
    proc = _processor(tmp_path)
    proc.state_manager.create_state("KAN-2", "plan", "Mode: plan")
    proc.state_manager.update_state("KAN-2", status=TaskStatus.PLAN_READY)
    store, claimed = _schedule(tmp_path, "KAN-2")
    event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "KAN-2",
            "fields": {"summary": "plan", "description": "Mode: plan"},
        },
        "scheduled_job": True,
        "schedule_id": claimed["schedule_id"],
    }

    async def run():
        await _dispatch_claimed_schedule(
            processor=proc,
            store=store,
            schedule_id=claimed["schedule_id"],
            issue_key="KAN-2",
            claimed_rec=claimed,
            event=event,
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)

    asyncio.run(run())
    _reopen_skipped_dispatched_schedules(proc, store)
    status = (store.get(claimed["schedule_id"]) or {}).get("status")
    assert status in {"scheduled", "error"}
