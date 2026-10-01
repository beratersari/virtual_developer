"""Unit tests for state manager transitions and metadata merge."""

from datetime import datetime, timedelta

from src.state.models import TaskStatus


def test_metadata_merge_preserves_existing_keys(state_manager):
    """Regression: update_state(metadata={...}) must merge, not replace."""
    state = state_manager.create_state(
        issue_key="PROJ-100",
        issue_summary="Test",
        description="desc",
    )
    state.metadata["workflow_type"] = "direct"
    state.metadata["other"] = "keep-me"
    state_manager.set_state(state)

    state_manager.update_state(
        "PROJ-100",
        metadata={"merge_request_url": "https://gitlab.example/mr/1"},
    )

    loaded = state_manager.get_state("PROJ-100")
    assert loaded is not None
    assert loaded.metadata.get("workflow_type") == "direct"
    assert loaded.metadata.get("other") == "keep-me"
    assert loaded.metadata.get("merge_request_url") == "https://gitlab.example/mr/1"


def test_update_status_transition(state_manager):
    state_manager.create_state("PROJ-101", "s", "d")
    state_manager.update_state("PROJ-101", status=TaskStatus.PLANNING)
    loaded = state_manager.get_state("PROJ-101")
    assert loaded.status == TaskStatus.PLANNING


def test_get_active_issues_excludes_terminal(state_manager):
    state_manager.create_state("A-1", "active", "")
    state_manager.update_state("A-1", status=TaskStatus.EXECUTING)

    state_manager.create_state("A-2", "done", "")
    state_manager.update_state("A-2", status=TaskStatus.COMPLETED)

    state_manager.create_state("A-3", "err", "")
    state_manager.update_state("A-3", status=TaskStatus.ERROR)

    active = state_manager.get_active_issues()
    keys = {s.issue_key for s in active}
    assert "A-1" in keys
    assert "A-2" not in keys
    assert "A-3" not in keys


def test_create_state_defaults_pending(state_manager):
    s = state_manager.create_state("B-1", "sum", "desc")
    assert s.status == TaskStatus.PENDING
    assert state_manager.get_state("B-1").status == TaskStatus.PENDING


def test_legacy_hyphen_folded_file_still_loads(tmp_path):
    """KAN-12 stored as KAN_12.json (old sanitizer) must still be found."""
    import json

    from src.state.manager import JiraStateManager

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = state_dir / "KAN_12.json"
    legacy.write_text(
        json.dumps(
            {
                "issue_key": "KAN-12",
                "issue_summary": "legacy name",
                "description": "",
                "status": "completed",
                "metadata": {},
                "retry_history": [],
            }
        ),
        encoding="utf-8",
    )
    sm = JiraStateManager(state_dir=state_dir)
    loaded = sm.get_state("KAN-12")
    assert loaded is not None
    assert loaded.issue_key == "KAN-12"
    assert loaded.status == TaskStatus.COMPLETED
    assert not legacy.is_file()


def test_create_state_returns_disk_state_when_write_refused(state_manager):
    state_manager.create_state("B-2", "first")
    state_manager.update_state("B-2", status=TaskStatus.COMPLETED)
    returned = state_manager.create_state("B-2", "second")
    disk = state_manager.get_state("B-2")
    assert disk is not None
    assert disk.status == TaskStatus.COMPLETED
    assert returned.status == TaskStatus.COMPLETED
    assert returned.issue_summary == disk.issue_summary
