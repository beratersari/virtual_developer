"""Analytics vs merge cleanup vs age clone purge — real HTTP, real job JSON.

Creates 20+ GitLab MR job records (on-disk ``job_*.json`` + temp clones),
hits GET /api/analytics, POSTs Merge Request Hook for a subset (clones
go, job JSON stays), hits analytics again, then age-purges leftover
clones and hits analytics once more. No MagicMock of purge, analytics,
or the webhook.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
import uvicorn

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import reset_delete_jobs, reset_size_cache
from src.git_manager import purge_stale_temp_dirs
from src.processor import JobProcessor
from src.state.manager import JiraStateManager


MR_COUNT = 22
REVIEW_ON_FIRST = 4
MERGE_FIRST_N = 8
WEBHOOK_SECRET = "analytics-merge-proof"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=0.5, verify=False)
            if resp.status_code < 500:
                return
        except Exception as exc:
            last = exc
        time.sleep(0.05)
    raise RuntimeError(f"{url} not ready: {last}")


def _start_uvicorn(app, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_http(f"http://127.0.0.1:{port}/api/health")
    return server


def _mr_url(iid: int) -> str:
    return f"https://gitlab.example.com/acme/app/-/merge_requests/{iid}"


def _stamp() -> str:
    return datetime.now().replace(microsecond=0).isoformat(timespec="seconds")


def _analytics(base: str, **params: Any) -> Dict[str, Any]:
    resp = httpx.get(
        f"{base}/api/analytics",
        params={"period": "7d", "bucket": "day", **params},
        timeout=15.0,
        verify=False,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _source_jobs(body: Dict[str, Any], source_id: str) -> int:
    for row in body.get("sources") or []:
        if str(row.get("id") or "") == source_id:
            return int(row.get("jobs") or 0)
    return 0


def _post_merge(base: str, iid: int) -> Dict[str, Any]:
    resp = httpx.post(
        f"{base}/yaver/webhook/gitlab",
        headers={
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Token": WEBHOOK_SECRET,
        },
        json={
            "object_kind": "merge_request",
            "object_attributes": {
                "iid": iid,
                "action": "merge",
                "state": "merged",
                "title": f"feat: mr session {iid}",
                "description": "",
                "source_branch": f"feature/session-{iid}",
                "target_branch": "develop",
                "url": _mr_url(iid),
            },
            "project": {
                "id": 3,
                "path_with_namespace": "acme/app",
                "http_url_to_repo": "https://gitlab.example.com/acme/app.git",
                "web_url": "https://gitlab.example.com/acme/app",
            },
            "repository": {},
        },
        timeout=20.0,
        verify=False,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True, body
    return body


def test_merge_and_age_cleanup_keep_analytics_job_json(
    tmp_path: Path, isolate_jira_agent_artifacts, monkeypatch
):
    jobs = isolate_jira_agent_artifacts["job_store"]
    data = tmp_path / "yaver-data"
    plans = data / "plans"
    state_dir = data / "state"
    clones = tmp_path / "t"
    plans.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    clones.mkdir(parents=True)
    sessions = data / "sessions"
    sessions.mkdir(parents=True)
    session_log = sessions / "mr1.log"
    session_prompt = sessions / "mr1.prompt.txt"
    session_log.write_text("[serve] finish=stop\n", encoding="utf-8")
    session_prompt.write_text("@bot /yaver\n", encoding="utf-8")

    monkeypatch.setenv("YAVER_DATA_DIR", str(data))
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", jobs)
    monkeypatch.setattr("src.dashboard.service.default_job_store", jobs)
    reset_delete_jobs()
    reset_size_cache()

    now = _stamp()
    gitlab_ids: List[str] = []
    jira_ids: List[str] = []
    clone_paths: Dict[int, Path] = {}

    for iid in range(1, MR_COUNT + 1):
        clone = clones / f"app_mr_{iid}"
        clone.mkdir()
        (clone / "README.md").write_text(f"clone {iid}\n", encoding="utf-8")
        clone_paths[iid] = clone
        rec = jobs.create_job(
            issue_key=f"GL-APP-{iid}",
            summary=f"@bot /yaver on MR {iid}",
            workflow_type="gitlab-mr",
            source="gitlab",
            model="gpt-4.1",
            backend="opencode",
            agent="derman-build",
            status="completed",
            merge_request_url=_mr_url(iid),
            gitlab_project="acme/app",
            gitlab_mr_iid=iid,
            repository_url="https://gitlab.example.com/acme/app.git",
        )
        patch: Dict[str, Any] = {
            "working_directory": str(clone.resolve()),
            "started_at": now,
            "completed_at": now,
            "status": "completed",
        }
        if iid == 1:
            patch["session_log_path"] = str(session_log)
            patch["prompt_path"] = str(session_prompt)
        jobs.update_job(rec["job_id"], **patch)
        gitlab_ids.append(rec["job_id"])

    extra_review_ids: List[str] = []
    for iid in range(1, REVIEW_ON_FIRST + 1):
        rec = jobs.create_job(
            issue_key=f"GL-APP-{iid}",
            summary=f"@bot /review on MR {iid}",
            workflow_type="review",
            source="gitlab",
            model="sonnet",
            backend="opencode",
            agent="derman-reviewer",
            status="completed",
            merge_request_url=_mr_url(iid),
            gitlab_project="acme/app",
            gitlab_mr_iid=iid,
            repository_url="https://gitlab.example.com/acme/app.git",
        )
        jobs.update_job(
            rec["job_id"],
            working_directory=str(clone_paths[iid].resolve()),
            started_at=now,
            completed_at=now,
            status="completed",
        )
        extra_review_ids.append(rec["job_id"])
        gitlab_ids.append(rec["job_id"])

    for n in (1, 2):
        rec = jobs.create_job(
            issue_key=f"KAN-{n}",
            summary=f"jira plan {n}",
            workflow_type="planning",
            source="jira",
            model="gpt-4.1",
            backend="opencode",
            agent="derman-plan",
            status="completed",
        )
        jobs.update_job(
            rec["job_id"], started_at=now, completed_at=now, status="completed"
        )
        jira_ids.append(rec["job_id"])

    gitlab_job_count = MR_COUNT + REVIEW_ON_FIRST
    total_before = gitlab_job_count + len(jira_ids)
    assert len(jobs.iter_jobs()) == total_before

    sm = JiraStateManager(state_dir=state_dir)
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = jobs
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    app = create_dashboard_app(processor=proc, state_manager=sm)
    port = _free_port()
    server = _start_uvicorn(app, port)
    base = f"http://127.0.0.1:{port}"
    try:
        before = _analytics(base)
        print(
            "ANALYTICS BEFORE merge: "
            f"jobs={before['totals']['jobs']} scanned={before['scanned']} "
            f"gitlab={_source_jobs(before, 'gitlab')} "
            f"jira={_source_jobs(before, 'jira')}"
        )
        assert before["totals"]["jobs"] == total_before
        assert before["scanned"] == total_before
        assert _source_jobs(before, "gitlab") == gitlab_job_count
        assert _source_jobs(before, "jira") == len(jira_ids)

        merged_iids = list(range(1, MERGE_FIRST_N + 1))
        for iid in merged_iids:
            _post_merge(base, iid)

        leftover = {j["job_id"]: j for j in jobs.iter_jobs()}
        assert len(leftover) == total_before, (
            f"merge must keep job JSON for Analytics; expected {total_before}, "
            f"got {len(leftover)}"
        )
        for iid in merged_iids:
            url = _mr_url(iid)
            still = [
                j
                for j in leftover.values()
                if str(j.get("merge_request_url") or "") == url
            ]
            assert still, f"merged MR {iid} lost its job records"
            clone = clone_paths[iid]
            deadline = time.time() + 8.0
            while clone.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert not clone.exists(), f"merged clone still on disk: {clone}"
        assert not session_log.is_file(), "merge must still delete session logs"
        assert not session_prompt.is_file(), "merge must still delete session prompts"
        for jid in jira_ids:
            assert jid in leftover, "Jira job without MR URL was deleted on merge"

        after_merge = _analytics(base)
        print(
            "ANALYTICS AFTER merge of "
            f"{MERGE_FIRST_N} MRs: jobs={after_merge['totals']['jobs']} "
            f"scanned={after_merge['scanned']} "
            f"gitlab={_source_jobs(after_merge, 'gitlab')} "
            f"jira={_source_jobs(after_merge, 'jira')}"
        )
        assert after_merge["totals"]["jobs"] == total_before
        assert _source_jobs(after_merge, "gitlab") == gitlab_job_count
        assert _source_jobs(after_merge, "jira") == len(jira_ids)

        gitlab_only = _analytics(base, source="gitlab")
        assert gitlab_only["totals"]["jobs"] == gitlab_job_count
        jira_only = _analytics(base, source="jira")
        assert jira_only["totals"]["jobs"] == len(jira_ids)
        review_only = _analytics(base, category="review")
        assert review_only["totals"]["jobs"] == REVIEW_ON_FIRST

        # Age purge: remaining unused clones only. Job JSON must stay.
        old = time.time() - (10 * 86400.0)
        remaining_clones = [
            clone_paths[iid] for iid in range(MERGE_FIRST_N + 1, MR_COUNT + 1)
        ]
        for clone in remaining_clones:
            assert clone.is_dir(), f"unmerged clone missing before age purge: {clone}"
            os.utime(clone, (old, old))
        removed = purge_stale_temp_dirs(max_age_days=7.0, base_dir=clones)
        assert removed >= len(remaining_clones), (
            f"age purge removed {removed}, expected at least {len(remaining_clones)}"
        )
        for clone in remaining_clones:
            assert not clone.exists(), f"age purge left clone {clone}"

        after_age = _analytics(base)
        print(
            "ANALYTICS AFTER age clone purge: "
            f"jobs={after_age['totals']['jobs']} scanned={after_age['scanned']} "
            f"gitlab={_source_jobs(after_age, 'gitlab')} "
            f"jira={_source_jobs(after_age, 'jira')}"
        )
        assert after_age["totals"]["jobs"] == after_merge["totals"]["jobs"]
        assert after_age["scanned"] == after_merge["scanned"]
        assert _source_jobs(after_age, "gitlab") == _source_jobs(after_merge, "gitlab")
        assert {j["job_id"] for j in jobs.iter_jobs()} == set(leftover)
    finally:
        server.should_exit = True


def test_plan_and_issue_state_delete_alone_does_not_change_analytics(
    tmp_path: Path, isolate_jira_agent_artifacts, monkeypatch
):
    """Merge also unlinks plans/{KEY}.md and issue state. Analytics ignores both."""
    jobs = isolate_jira_agent_artifacts["job_store"]
    data = tmp_path / "yaver-data"
    plans = data / "plans"
    state_dir = data / "state"
    clones = tmp_path / "t"
    plans.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    clones.mkdir(parents=True)

    monkeypatch.setenv("YAVER_DATA_DIR", str(data))
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", jobs)
    monkeypatch.setattr("src.dashboard.service.default_job_store", jobs)
    reset_delete_jobs()
    reset_size_cache()

    from src.paths import plans_dir

    now = _stamp()
    sm = JiraStateManager(state_dir=state_dir)
    n = 5
    for iid in range(1, n + 1):
        key = f"KAN-{iid}"
        clone = clones / f"app_plan_{iid}"
        clone.mkdir()
        (clone / "README.md").write_text(f"clone {iid}\n", encoding="utf-8")
        rec = jobs.create_job(
            issue_key=key,
            summary=f"feat({key}): session {iid}",
            workflow_type="gitlab-mr",
            source="gitlab",
            model="gpt-4.1",
            backend="opencode",
            status="completed",
            merge_request_url=_mr_url(iid),
            gitlab_project="acme/app",
            gitlab_mr_iid=iid,
            repository_url="https://gitlab.example.com/acme/app.git",
        )
        jobs.update_job(
            rec["job_id"],
            working_directory=str(clone.resolve()),
            started_at=now,
            completed_at=now,
            status="completed",
        )
        (plans_dir() / f"{key}.md").write_text(f"# plan {key}\n", encoding="utf-8")
        sm.create_state(key, f"feat({key}): session {iid}")

    assert len(list(plans.glob("KAN-*.md"))) == n
    assert all(sm.get_state(f"KAN-{iid}") for iid in range(1, n + 1))

    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = jobs
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    app = create_dashboard_app(processor=proc, state_manager=sm)
    port = _free_port()
    server = _start_uvicorn(app, port)
    base = f"http://127.0.0.1:{port}"
    try:
        before = _analytics(base)
        print(
            "ANALYTICS with jobs+plans+state: "
            f"jobs={before['totals']['jobs']} scanned={before['scanned']}"
        )
        assert before["totals"]["jobs"] == n

        # Same two deletes merge does, without deleting job JSON.
        for iid in range(1, n + 1):
            key = f"KAN-{iid}"
            plan = plans_dir() / f"{key}.md"
            assert plan.is_file()
            plan.unlink()
            assert sm.delete_state(key) is True
        assert list(plans.glob("KAN-*.md")) == []
        assert all(sm.get_state(f"KAN-{iid}") is None for iid in range(1, n + 1))
        assert len(jobs.iter_jobs()) == n

        after_plan_state = _analytics(base)
        print(
            "ANALYTICS after plan+state delete only: "
            f"jobs={after_plan_state['totals']['jobs']} "
            f"scanned={after_plan_state['scanned']}"
        )
        assert after_plan_state["totals"]["jobs"] == n
        assert after_plan_state["scanned"] == n

        for iid in range(1, n + 1):
            _post_merge(base, iid)
        after_jobs = _analytics(base)
        print(
            "ANALYTICS after merge (job JSON kept): "
            f"jobs={after_jobs['totals']['jobs']} scanned={after_jobs['scanned']}"
        )
        assert after_jobs["totals"]["jobs"] == n
        assert after_jobs["scanned"] == n
        assert len(jobs.iter_jobs()) == n
    finally:
        server.should_exit = True
