"""Jira-API e2e for the 45-min serve failure: question → nudge → leftover todos.

Production log::

    [serve] assessment … reasons=['open todos: 10 pending, 1 in_progress',
        'assistant asked a clarifying question']
    [serve] assistant asked a clarifying question — sending one unattended nudge
    [INCOMPLETE] after unattended nudge still incomplete: open todos: 4 pending,
        1 in_progress

That used to terminate a still-working long job. This file creates a Jira
issue over HTTP, points {params} at a large local origin (django-shaped
tree), and drives the real poller → processor → serve orchestrator with a
fake OpenCode that replays that conversation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from src.opencode_serve import (
    DEFAULT_UNATTENDED_NUDGE_PROMPT,
    ServeOrchestrator,
)
from src.state.models import TaskStatus
from tests.test_live_jira_session_reuse import (
    TRIGGER,
    _poll_and_process,
    _wire_processor,
)
from tests.test_opencode_serve_e2e import FakeServeBackend, FakeServeClient
from tests.test_simple_task_timing_e2e import _git

pytest_plugins = ["tests.test_live_jira_session_reuse"]


def _seed_django_shaped_tree(root: Path) -> None:
    """Many-app tree so the ticket looks like a large-repo job."""
    (root / "manage.py").write_text(
        "#!/usr/bin/env python\nimport django\n", encoding="utf-8"
    )
    (root / "requirements.txt").write_text("Django>=4.2\n", encoding="utf-8")
    (root / "README.md").write_text(
        "Large Django-shaped project for long-job serve e2e.\n",
        encoding="utf-8",
    )
    for app in (
        "accounts",
        "catalog",
        "orders",
        "billing",
        "inventory",
        "notifications",
        "analytics",
        "support",
    ):
        pkg = root / app
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            f"# {app} models\nfrom django.db import models\n\n"
            f"class {app.title()}Item(models.Model):\n    name = models.CharField(max_length=80)\n",
            encoding="utf-8",
        )
        (pkg / "views.py").write_text(
            f"# {app} views\nfrom django.http import JsonResponse\n\n"
            f"def {app}_ping(request):\n    return JsonResponse({{'app': '{app}'}})\n",
            encoding="utf-8",
        )
        (pkg / "urls.py").write_text(
            f"# {app} urls\nfrom django.urls import path\nfrom . import views\n",
            encoding="utf-8",
        )
        tests = pkg / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_models.py").write_text(
            f"def test_{app}_placeholder():\n    assert True\n",
            encoding="utf-8",
        )


def _make_large_origin(root: Path) -> Path:
    src = root / "seed"
    src.mkdir(parents=True)
    _seed_django_shaped_tree(src)
    _git(src, "init")
    _git(src, "config", "user.email", "devbot@example.com")
    _git(src, "config", "user.name", "DevBot")
    _git(src, "checkout", "-b", "develop")
    _git(src, "add", ".")
    _git(src, "commit", "-m", "chore: seed django-shaped project")
    bare = root / "origin.git"
    import subprocess

    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    return bare


class QuestionThenWorkingTodos(FakeServeBackend):
    """Replay the production 45-min conversation over fake OpenCode HTTP."""

    def __init__(self) -> None:
        super().__init__(required_compacts=0)
        self.session_id = "ses_jira_long"
        self.todos = [
            {"content": f"step-{i}", "status": "pending"} for i in range(10)
        ] + [{"content": "now", "status": "in_progress"}]
        self.auto_complete_on_idle = True
        self.idle_polls_before_auto_complete = 3
        self.assessment_logs: List[str] = []

    async def send_message(self, session_id, text, **kwargs):
        self.message_calls += 1
        self.prompts.append(text)
        self.messages.append(
            {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "user",
                    "finish": None,
                    "summary": {"diffs": []},
                },
                "parts": [{"type": "text", "text": text}],
            }
        )
        if self.message_calls == 1:
            reply = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "stop",
                    "summary": None,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": "Shall I continue with the remaining 11 steps?",
                    }
                ],
            }
            self.messages.append(reply)
            return reply
        self.todos = [
            {"content": f"step-{i}", "status": "pending"} for i in range(4)
        ] + [{"content": "now", "status": "in_progress"}]
        reply = {
            "info": {
                "id": self._next_id("msg"),
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            },
            "parts": [
                {
                    "type": "text",
                    "text": (
                        "Continuing with defaults. Implementing the remaining "
                        "Django apps now."
                    ),
                }
            ],
        }
        self.messages.append(reply)
        return reply


def _params(repo: str) -> str:
    return (
        "Long-running Virtual Developer job (automated e2e).\n"
        "Implement multi-app Django billing + inventory + notifications.\n"
        "{params}\n"
        f"Repository: {repo}\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}\n"
    )


@pytest.mark.asyncio
async def test_jira_api_long_job_question_nudge_todos_does_not_kill(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    """POST Jira issue → poller → serve: leftover todos after nudge must not ERROR."""
    board, _srv = sim_jira
    origin = _make_large_origin(tmp_path / "git")
    repo = origin.resolve().as_uri()
    file_count = sum(1 for _ in (tmp_path / "git" / "seed").rglob("*") if _.is_file())
    assert file_count >= 30, f"expected a large tree, got {file_count} files"

    proc, sm, poller, pending, _ = _wire_processor(tmp_path, monkeypatch, board, repo)
    backend = QuestionThenWorkingTodos()

    real_orch = ServeOrchestrator

    def _orch(**kwargs: Any) -> ServeOrchestrator:
        kwargs["client"] = FakeServeClient(backend)
        kwargs["compact_wait_seconds"] = 1.5
        kwargs["compact_poll_seconds"] = 0.05
        kwargs["compact_settle_seconds"] = 0.05
        return real_orch(**kwargs)

    monkeypatch.setattr("src.opencode_serve.ServeOrchestrator", _orch)

    created = board.create_issue(
        summary="[vd-e2e] long django job question+nudge+todos",
        description=_params(repo),
        labels=[TRIGGER, "vd-long-job-e2e"],
    )
    assert created and created.get("key"), getattr(board.inner, "last_error", None)
    key = created["key"]
    live0 = board.get_issue(key)
    assert live0["fields"]["status"]["name"].lower() == "to do"

    processed = await _poll_and_process(proc, poller, pending)
    assert key in processed

    st = sm.get_state(key)
    assert st is not None
    comments = board.get_comments(key)
    blob = "\n".join(str(c.get("body") or c) for c in comments)
    session_bits = "\n".join(backend.prompts)
    assert backend.message_calls == 2, backend.prompts
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert "after unattended nudge still incomplete" not in blob.lower()
    assert "after unattended nudge still incomplete" not in session_bits.lower()
    assert st.status != TaskStatus.ERROR or "unattended nudge still incomplete" not in (
        st.error_message or ""
    ).lower()
    assert st.status in {
        TaskStatus.COMPLETED,
        TaskStatus.ERROR,
        TaskStatus.EXECUTING,
        TaskStatus.PLANNING,
    }
    # Delivery may still fail (fake agent does not commit). The serve loop
    # itself must not have classified leftover todos as INCOMPLETE.
    if st.status == TaskStatus.ERROR:
        assert "open todos" not in (st.error_message or "").lower()
        assert "clarifying question" not in (st.error_message or "").lower()
