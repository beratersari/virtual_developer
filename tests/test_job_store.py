"""Job history store tests."""

from pathlib import Path

from src.state.job_store import (
    JobStore,
    description_from_prompt_path,
    extract_task_description_from_prompt,
)


def test_create_list_filter_update(tmp_path: Path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    j1 = store.create_job(
        issue_key="KAN-1",
        summary="first",
        description="do the thing v1",
        workflow_type="execution",
        agent="sisyphus",
        task_id="t1",
    )
    j2 = store.create_job(
        issue_key="KAN-2",
        summary="second",
        workflow_type="planning",
        agent="prometheus",
    )
    store.update_job(j1["job_id"], status="completed", opencode_session_id="ses_abc")

    all_jobs = store.list_jobs()
    assert len(all_jobs) == 2
    assert all_jobs[0]["started_at"] >= all_jobs[1]["started_at"]

    only1 = store.list_jobs(issue_key="kan-1")
    assert len(only1) == 1
    assert only1[0]["issue_key"] == "KAN-1"
    assert only1[0]["description"] == "do the thing v1"
    assert only1[0]["opencode_session_id"] == "ses_abc"
    assert "ses_abc" in only1[0]["opencode_session_ids"]

    # Second run with different description must not rewrite the first job
    j1b = store.create_job(
        issue_key="KAN-1",
        summary="first",
        description="do the thing v2",
        workflow_type="execution",
        agent="sisyphus",
        task_id="t1b",
        status="completed",
    )
    assert store.get_job(j1["job_id"])["description"] == "do the thing v1"
    assert store.get_job(j1b["job_id"])["description"] == "do the thing v2"

    got = store.get_job(j2["job_id"])
    assert got is not None
    assert got["workflow_type"] == "planning"

    # task_id updates append history, do not wipe previous
    store.update_job(j1["job_id"], task_id="t1-retry")
    again = store.get_job(j1["job_id"])
    assert again is not None
    assert again["task_id"] == "t1-retry"
    assert again["task_ids"] == ["t1", "t1-retry"]


def test_extract_task_description_from_prompt():
    text = """# Direct Task Execution

## JIRA Issue: KAN-1

## Task
bu repoda main.cpp olustur 5+3 yap

# Sisyphus Direct Execution Prompt

## Instructions
1. Analyze
"""
    assert extract_task_description_from_prompt(text) == "bu repoda main.cpp olustur 5+3 yap"


def test_ensure_description_from_prompt_file(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    prompt = sessions / "KAN-1_20260101_120000_0.prompt.txt"
    prompt.write_text(
        "# Direct\n\n## Task\nold description v1\n\n# Instructions\n1. go\n",
        encoding="utf-8",
    )
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(
        issue_key="KAN-1",
        summary="s",
        description="",  # missing snapshot
        workflow_type="execution",
        status="completed",
    )
    store.update_job(job["job_id"], prompt_path=str(prompt))
    loaded = store.get_job(job["job_id"])
    assert loaded is not None
    assert not (loaded.get("description") or "").strip()
    fixed = store.ensure_description(loaded, persist=True)
    assert fixed["description"] == "old description v1"
    assert store.get_job(job["job_id"])["description"] == "old description v1"
    assert description_from_prompt_path(str(prompt)) == "old description v1"
