"""Retry jobs must keep every prompt/log path, not only the latest."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dashboard.service import _job_prompt_paths, collect_job_text_artifacts
from src.processor import JobProcessor
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager


def _proc(tmp_path: Path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / ".jira-agent" / "jobs")
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = store
    return proc, sm, store


def test_link_job_session_paths_appends_retry_prompts(tmp_path):
    proc, sm, store = _proc(tmp_path)
    sm.create_state("KAN-P", "s", "d")
    job = store.create_job(issue_key="KAN-P", status="running")
    proc._active_jobs["KAN-P"] = job["job_id"]
    sess = tmp_path / ".jira-agent" / "sessions"
    sess.mkdir(parents=True)
    p0 = sess / "KAN-P.prompt.txt"
    p1 = sess / "KAN-P_retry1.prompt.txt"
    l0 = sess / "KAN-P.log"
    l1 = sess / "KAN-P_retry1.log"
    p0.write_text("FIRST")
    p1.write_text("RETRY")
    l0.write_text("log0")
    l1.write_text("log1")

    proc._link_job_session_paths("KAN-P", session_path=str(l0), prompt_path=str(p0))
    proc._link_job_session_paths("KAN-P", session_path=str(l1), prompt_path=str(p1))
    live = store.get_job(job["job_id"])
    assert live is not None
    assert live["prompt_paths"] == [str(p0), str(p1)]
    assert live["session_log_paths"] == [str(l0), str(l1)]
    assert live["prompt_path"] == str(p1)
    assert live["session_log_path"] == str(l1)


def test_job_prompt_paths_includes_sibling_of_retry_logs(tmp_path):
    sess = tmp_path / "sessions"
    sess.mkdir()
    p0 = sess / "KAN-P.prompt.txt"
    p1 = sess / "KAN-P_retry1.prompt.txt"
    l0 = sess / "KAN-P.log"
    l1 = sess / "KAN-P_retry1.log"
    p0.write_text("FIRST")
    p1.write_text("RETRY")
    l0.write_text("log0")
    l1.write_text("log1")
    found = _job_prompt_paths(
        {
            "prompt_path": str(p1),
            "prompt_paths": [str(p1)],
            "session_log_path": str(l1),
            "session_log_paths": [str(l0), str(l1)],
        }
    )
    assert str(p0) in found
    assert str(p1) in found


def test_collect_artifacts_returns_initial_and_retry_prompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = JobStore(jobs_dir=tmp_path / ".jira-agent" / "jobs")
    sess = tmp_path / ".jira-agent" / "sessions"
    sess.mkdir(parents=True)
    p0 = sess / "KAN-P.prompt.txt"
    p1 = sess / "KAN-P_retry1.prompt.txt"
    l0 = sess / "KAN-P.log"
    l1 = sess / "KAN-P_retry1.log"
    p0.write_text("FIRST KIT")
    p1.write_text("RETRY KIT")
    l0.write_text("log0")
    l1.write_text("log1")
    rec = store.create_job(issue_key="KAN-P", status="error")
    store.update_job(
        rec["job_id"],
        prompt_path=str(p1),
        prompt_paths=[str(p0), str(p1)],
        session_log_path=str(l1),
        session_log_paths=[str(l0), str(l1)],
    )
    with patch("src.state.job_store.job_store", store):
        arts = collect_job_text_artifacts(store.get_job(rec["job_id"]))
    bodies = [row.get("content") or "" for row in arts["prompts"]]
    names = [Path(row["path"]).name for row in arts["prompts"]]
    assert "KAN-P.prompt.txt" in names
    assert "KAN-P_retry1.prompt.txt" in names
    assert any("FIRST KIT" in b for b in bodies)
    assert any("RETRY KIT" in b for b in bodies)
