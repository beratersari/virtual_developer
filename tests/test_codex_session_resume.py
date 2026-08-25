"""Codex re-queue must resume the same thread and keep the transcript."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.backends.base import is_session_or_thread_id
from src.backends.codex import DEFAULT_CODEX_RESUME_PROMPT
from src.dashboard.service import collect_job_text_artifacts
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.session_bind_store import SessionBindStore

REPO = "https://gitlab.example.com/acme/app.git"
THREAD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BUILD_PROMPT = "# BUILD KIT\nPlease implement KAN-9 from scratch."


def _proc(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    binds = SessionBindStore(binds_dir=tmp_path / "binds")
    jobs = JobStore(jobs_dir=tmp_path / ".jira-agent" / "jobs")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", binds)
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc.job_store = jobs
    return proc, sm, binds, jobs


def _git(clone: Path):
    git = MagicMock()
    git.remote_url = REPO
    git.work_branch = "feature/KAN-9"
    git.source_branch = "feature/KAN-9"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    return git


def test_is_session_or_thread_id_accepts_codex_uuid():
    assert is_session_or_thread_id(THREAD) is True
    assert is_session_or_thread_id("ses_abc123xyz") is True
    assert is_session_or_thread_id("nope") is False


def test_resume_candidates_include_bound_codex_thread(tmp_path, monkeypatch):
    proc, sm, binds, _jobs = _proc(tmp_path, monkeypatch)
    clone = tmp_path / "clone"
    clone.mkdir()
    git = _git(clone)
    sm.create_state("KAN-9", "s", "d")
    proc._contexts["KAN-9"] = {"git": git, "runner": None}
    proc._upsert_session_bind("KAN-9", THREAD)

    sids, _forgotten, _wd = proc._resume_session_candidates("KAN-9", git)
    assert THREAD in sids


def test_attach_codex_thread_resumes_without_rebuild_prompt(tmp_path, monkeypatch):
    proc, sm, binds, jobs = _proc(tmp_path, monkeypatch)
    clone = tmp_path / "clone"
    clone.mkdir()
    git = _git(clone)
    sessions = tmp_path / ".jira-agent" / "sessions"
    sessions.mkdir(parents=True)
    old_log = sessions / "KAN-9_1.log"
    old_prompt = sessions / "KAN-9_1.prompt.txt"
    old_log.write_text('{"type":"thread.started","thread_id":"%s"}\n' % THREAD)
    old_prompt.write_text(BUILD_PROMPT)

    sm.create_state("KAN-9", "s", "d")
    first = jobs.create_job(
        issue_key="KAN-9",
        summary="first",
        status="completed",
        backend="codex",
    )
    jobs.update_job(
        first["job_id"],
        opencode_session_id=THREAD,
        session_log_path=str(old_log),
        session_log_paths=[str(old_log)],
        prompt_path=str(old_prompt),
        prompt_paths=[str(old_prompt)],
    )
    proc._contexts["KAN-9"] = {"git": git, "runner": None}
    proc._upsert_session_bind("KAN-9", THREAD)
    second = jobs.create_job(
        issue_key="KAN-9",
        summary="second",
        status="executing",
        backend="codex",
    )
    proc._active_jobs["KAN-9"] = second["job_id"]

    task = AgentTask(
        description="build",
        prompt=BUILD_PROMPT,
        agent="build",
        issue_key="KAN-9",
        backend="codex",
    )
    sid = proc._attach_bound_opencode_session("KAN-9", task, git)
    assert sid == THREAD
    assert task.session_id == THREAD
    assert task.prompt == DEFAULT_CODEX_RESUME_PROMPT
    assert BUILD_PROMPT not in (task.prompt or "")

    live = jobs.get_job(second["job_id"])
    assert live is not None
    assert live.get("opencode_session_id") == THREAD
    assert str(old_log) in (live.get("session_log_paths") or [])
    assert str(old_prompt) in (live.get("prompt_paths") or [])


def test_link_job_records_codex_thread_id(tmp_path, monkeypatch):
    proc, sm, _binds, jobs = _proc(tmp_path, monkeypatch)
    sm.create_state("KAN-9", "s", "d")
    rec = jobs.create_job(issue_key="KAN-9", summary="live", status="executing")
    proc._active_jobs["KAN-9"] = rec["job_id"]
    proc._link_job_opencode_session("KAN-9", THREAD)
    live = jobs.get_job(rec["job_id"])
    assert live is not None
    assert live.get("opencode_session_id") == THREAD
    st = sm.get_state("KAN-9")
    assert st is not None
    assert st.current_opencode_session_id == THREAD


def test_collect_artifacts_includes_prior_codex_thread_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = JobStore(jobs_dir=tmp_path / ".jira-agent" / "jobs")
    sessions = tmp_path / ".jira-agent" / "sessions"
    sessions.mkdir(parents=True)
    old_log = sessions / "KAN-9_old.log"
    old_prompt = sessions / "KAN-9_old.prompt.txt"
    new_log = sessions / "KAN-9_new.log"
    new_prompt = sessions / "KAN-9_new.prompt.txt"
    old_log.write_text('{"type":"item.completed","item":{"type":"agent_message","text":"first turn"}}\n')
    old_prompt.write_text(BUILD_PROMPT)
    new_log.write_text('{"type":"item.completed","item":{"type":"agent_message","text":"second turn"}}\n')
    new_prompt.write_text(DEFAULT_CODEX_RESUME_PROMPT)
    first = store.create_job(issue_key="KAN-9", summary="first", status="completed")
    store.update_job(
        first["job_id"],
        backend="codex",
        opencode_session_id=THREAD,
        session_log_path=str(old_log),
        prompt_path=str(old_prompt),
    )
    second = store.create_job(issue_key="KAN-9", summary="second", status="executing")
    store.update_job(
        second["job_id"],
        backend="codex",
        opencode_session_id=THREAD,
        session_log_path=str(new_log),
        prompt_path=str(new_prompt),
    )
    with patch("src.state.job_store.job_store", store):
        arts = collect_job_text_artifacts(store.get_job(second["job_id"]))
    log_names = [Path(r["path"]).name for r in arts["session_logs"]]
    prompt_bodies = [r.get("content") or "" for r in arts["prompts"]]
    assert "KAN-9_old.log" in log_names
    assert "KAN-9_new.log" in log_names
    assert any(BUILD_PROMPT in body for body in prompt_bodies)
    assert any("continue the work already started" in body.lower() for body in prompt_bodies)
