"""Issue-report zip (general vs job) for the ops dashboard."""

from __future__ import annotations

import io
import json
import zipfile
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.issue_logs import IssueLogRing
from src.dashboard.issue_report import build_issue_report_zip
from src.dashboard.schemas import IssueReportRequest
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager


def _zip_names(payload: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return set(zf.namelist())


def _zip_text(payload: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return zf.read(name).decode("utf-8")


def test_general_report_includes_note_and_system_logs(tmp_path, monkeypatch):
    ring = IssueLogRing(maxlen=50, jobs_dir=tmp_path / "jobs", persist=False)
    ring.append("poller started", issue_key=None, job_id=None)
    ring.append("something happened", issue_key="KAN-1", job_id="job_abc")
    monkeypatch.setattr("src.dashboard.issue_report.issue_log_ring", ring)

    payload, filename = build_issue_report_zip(
        IssueReportRequest(kind="general", note="UI froze after poll"),
        store=JobStore(jobs_dir=tmp_path / "jobs"),
    )
    names = _zip_names(payload)
    assert filename.startswith("yaver-report-general-")
    assert filename.endswith(".zip")
    assert "NOTE.txt" in names
    assert "system/daemon.log" in names
    assert "meta.json" in names
    assert "runtime.json" in names
    assert "settings.json" in names
    assert "poll.json" in names
    assert "queue.json" in names
    assert "schedules.json" in names
    assert "sessions.json" in names
    assert "states.json" in names
    assert "job/record.json" not in names
    assert "UI froze after poll" in _zip_text(payload, "NOTE.txt")
    daemon = _zip_text(payload, "system/daemon.log")
    assert "poller started" in daemon
    assert "something happened" in daemon
    settings_blob = _zip_text(payload, "settings.json")
    assert "jira_token_configured" in settings_blob
    runtime = json.loads(_zip_text(payload, "runtime.json"))
    assert "python" in runtime
    assert "opencode_serve_health" in runtime


def test_job_report_includes_prompts_retries_and_logs(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    store = JobStore(jobs_dir=jobs_dir)
    job = store.create_job(
        issue_key="KAN-9",
        summary="Broken clone",
        description=(
            "{params}\n"
            "Repository: https://gitlab.example.com/g/r.git\n"
            "Source branch: feature/KAN-9\n"
            "Target branch: develop\n"
            "Mode: build\n"
            "{params}\n"
        ),
        workflow_type="execution",
        agent="build",
        model="ollama/demo",
        backend="opencode",
    )
    jid = job["job_id"]
    prompt = sessions / f"KAN-9_{jid}.prompt.txt"
    retry_prompt = sessions / f"KAN-9_{jid}_retry1.prompt.txt"
    session_log = sessions / f"KAN-9_{jid}.log"
    prompt.write_text("## Task\nclone the repo\n", encoding="utf-8")
    retry_prompt.write_text("finish remaining todos\n", encoding="utf-8")
    session_log.write_text("opencode session line\n", encoding="utf-8")
    store.update_job(
        jid,
        prompt_path=str(prompt),
        prompt_paths=[str(prompt), str(retry_prompt)],
        session_log_path=str(session_log),
        session_log_paths=[str(session_log)],
        retry_attempt={
            "attempt_number": 1,
            "label": "retry1",
            "reason": "error",
            "delay_seconds": 5,
            "error_message": "incomplete_session",
        },
    )

    ring = IssueLogRing(maxlen=50, jobs_dir=jobs_dir, persist=True)
    ring.append("job started", job_id=jid, issue_key="KAN-9")
    monkeypatch.setattr("src.dashboard.issue_report.issue_log_ring", ring)
    monkeypatch.setattr(
        "src.dashboard.service._artifacts_root",
        lambda: tmp_path.resolve(),
    )

    payload, filename = build_issue_report_zip(
        IssueReportRequest(kind="job", job_id=jid, note="agent looped"),
        store=store,
    )
    names = _zip_names(payload)
    assert jid.replace("/", "_") in filename
    assert "KAN-9" in filename
    assert "NOTE.txt" in names
    assert "system/daemon.log" in names
    assert "job/record.json" in names
    assert "job/parameters.json" in names
    assert "job/retry_attempts.json" in names
    assert "job/system.log" in names
    assert "job/chat.json" in names
    assert "job/chat.md" in names
    assert "job/description.txt" in names
    assert "job/issue.json" in names
    assert "job/git.txt" in names
    assert "runtime.json" in names
    assert "settings.json" in names
    assert "poll.json" in names
    params = json.loads(_zip_text(payload, "job/parameters.json"))
    assert params["issue_key"] == "KAN-9"
    assert params["model"] == "ollama/demo"
    assert params["backend"] == "opencode"
    assert params["issue_params"]["repository_url"].endswith("g/r.git")
    assert params["issue_params"]["mode"] == "build"
    assert params["retry_count"] == 1
    retries = json.loads(_zip_text(payload, "job/retry_attempts.json"))
    assert retries[0]["label"] == "retry1"
    prompt_files = [n for n in names if n.startswith("job/prompts/")]
    log_files = [n for n in names if n.startswith("job/session_logs/")]
    assert prompt_files
    assert log_files
    prompt_blob = "".join(_zip_text(payload, n) for n in prompt_files)
    assert "clone the repo" in prompt_blob
    assert "finish remaining todos" in prompt_blob
    assert "opencode session line" in "".join(_zip_text(payload, n) for n in log_files)
    assert "agent looped" in _zip_text(payload, "NOTE.txt")
    assert "job started" in _zip_text(payload, "job/system.log")
    assert "clone the repo" in _zip_text(payload, "job/description.txt") or (
        "{params}" in _zip_text(payload, "job/description.txt")
    )
    chat = json.loads(_zip_text(payload, "job/chat.json"))
    assert "messages" in chat or "error" in chat
    assert "KAN-9" in _zip_text(payload, "job/issue.json") or "issue_key" in _zip_text(
        payload, "job/issue.json"
    )
    git_txt = _zip_text(payload, "job/git.txt")
    assert "sample_project" in git_txt
    assert "gitlab.example.com/g/r.git" in git_txt
    assert "no working_directory on job" not in git_txt


def test_job_report_missing_job(tmp_path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    try:
        build_issue_report_zip(
            IssueReportRequest(kind="job", job_id="job_missing", note="x"),
            store=store,
        )
    except FileNotFoundError as e:
        assert "job_missing" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_job_kind_requires_job_id():
    from pydantic import ValidationError

    try:
        IssueReportRequest(kind="job", note="need a target")
    except ValidationError as e:
        assert "job_id" in str(e).lower()
    else:
        raise AssertionError("expected ValidationError for missing job_id")


def test_report_redacts_tokens(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_api_token", "SUPER-SECRET-TOKEN")
    ring = IssueLogRing(maxlen=20, persist=False)
    ring.append("auth SUPER-SECRET-TOKEN used", job_id=None)
    monkeypatch.setattr("src.dashboard.issue_report.issue_log_ring", ring)
    payload, _name = build_issue_report_zip(
        IssueReportRequest(kind="general", note="token leaked in logs?"),
        store=JobStore(jobs_dir=tmp_path / "jobs"),
    )
    daemon = _zip_text(payload, "system/daemon.log")
    assert "SUPER-SECRET-TOKEN" not in daemon
    assert "***" in daemon


def test_reports_http_endpoint(tmp_path, monkeypatch):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    monkeypatch.setattr("src.dashboard.api.job_store", store)
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)

    missing = client.post(
        "/api/reports",
        json={"kind": "job", "job_id": "job_nope", "note": "x"},
    )
    assert missing.status_code == 404

    bad = client.post("/api/reports", json={"kind": "general", "note": "   "})
    assert bad.status_code == 422

    ok = client.post(
        "/api/reports",
        json={"kind": "general", "note": "cannot reach gitlab"},
    )
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("application/zip")
    assert "attachment" in ok.headers.get("content-disposition", "")
    names = _zip_names(ok.content)
    assert "NOTE.txt" in names
    assert "runtime.json" in names
    assert "settings.json" in names
    assert "cannot reach gitlab" in _zip_text(ok.content, "NOTE.txt")
