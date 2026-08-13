"""Critical concurrent-job isolation proofs (architect review).

Focus: shared Source branch claim under ``asyncio.to_thread`` git init.
Correct product behaviour: at most one concurrent holder per (repo, source).
"""

from __future__ import annotations

import concurrent.futures
import inspect
import time
from unittest.mock import MagicMock, patch

import pytest

from src.git_manager import GitManager
from src.processor import JobProcessor
from src.state.session_bind_store import bind_id_for


@pytest.fixture
def processor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        return JobProcessor()


def test_prepare_git_workspace_offloads_claim_to_worker_thread(processor):
    """Claim runs inside _init_git_manager, which is off the event loop."""
    prep = inspect.getsource(JobProcessor._prepare_git_workspace)
    init = inspect.getsource(JobProcessor._init_git_manager)
    claim = inspect.getsource(JobProcessor._claim_source_branch)
    assert "to_thread" in prep
    assert "_claim_source_branch" in init
    # Thread-safe claim (worker threads cannot use asyncio.Lock)
    assert "threading.Lock" in inspect.getsource(JobProcessor.__init__) or hasattr(
        processor, "_source_branch_holders_lock"
    )
    assert "_source_branch_holders_lock" in claim


def test_claim_source_branch_must_be_atomic_under_threads(processor):
    """At most one winner when check→set is interleaved across threads.

    Claim runs under ``asyncio.to_thread`` git init. A RaceDict widens the
    window between get and set; the holders lock must still allow only one winner.
    """

    class RaceDict(dict):
        def get(self, *a, **k):
            v = super().get(*a, **k)
            time.sleep(0.03)
            return v

    repo = "https://gitlab.example.com/g/r.git"
    branch = "feature/shared-work"
    processor._source_branch_holders = RaceDict()

    def claim(i: int) -> bool:
        return processor._claim_source_branch(f"ISSUE-{i}", repo, branch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))

    winners = sum(1 for ok in results if ok)
    assert winners == 1, (
        f"expected exclusive claim; got {winners} winners "
        f"(holders={dict(processor._source_branch_holders)}) — "
        f"concurrent jobs can share one clone + session bind"
    )


def test_shared_custom_source_collides_workspace_and_session_bind():
    """Same Source (non-primary) → same work branch → same folder + bind id."""
    repo = "https://gitlab.example.com/g/r.git"
    src = "feature/shared-work"
    tgt = "develop"
    work_a = GitManager.resolve_work_branch_name("A-1", src, tgt)
    work_b = GitManager.resolve_work_branch_name("B-2", src, tgt)
    assert work_a == work_b == src
    assert bind_id_for(repo, work_a, tgt) == bind_id_for(repo, work_b, tgt)


def test_primary_source_isolates_per_issue_work_branch():
    """Source=develop isolates as feature/{KEY} so concurrent jobs do not share."""
    repo = "https://gitlab.example.com/g/r.git"
    work_a = GitManager.resolve_work_branch_name("A-1", "develop", "develop")
    work_b = GitManager.resolve_work_branch_name("B-2", "develop", "develop")
    assert work_a == "feature/A-1"
    assert work_b == "feature/B-2"
    assert bind_id_for(repo, work_a, "develop") != bind_id_for(
        repo, work_b, "develop"
    )


def test_git_for_and_runner_for_no_foreign_fallback(processor, tmp_path):
    from src.orchestrator.agent_runner import AgentRunner

    ra = AgentRunner(working_directory=tmp_path / "a")
    rb = AgentRunner(working_directory=tmp_path / "b")
    ga = MagicMock(issue_key="A-1")
    gb = MagicMock(issue_key="B-2")
    processor._contexts["A-1"] = {"git": ga, "runner": ra}
    processor._contexts["B-2"] = {"git": gb, "runner": rb}
    processor.git_manager = gb
    processor.agent_runner = rb
    assert processor._git_for("A-1") is ga
    assert processor._runner_for("A-1") is ra
    assert processor._git_for("MISSING") is None
    assert processor._runner_for("MISSING") is None


@pytest.mark.asyncio
async def test_same_issue_second_event_skipped_while_live(processor, state_manager):
    processor.state_manager = state_manager
    state_manager.create_state("SAME-1", "s", "d")
    processor._contexts["SAME-1"] = {"git": MagicMock(), "runner": MagicMock()}
    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SAME-1",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "summary": "s",
                "description": "d",
                "labels": ["bot"],
            },
        },
    }
    out = await processor.process_event(event)
    assert out.get("work_started") is False
    assert "live" in (out.get("skipped") or "").lower() or out.get("ok") is True
