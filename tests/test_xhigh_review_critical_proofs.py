"""Independent proofs for xhigh multi-agent critical review findings.

Each failing assertion encodes a real defect proven against production code.
Run: .venv/bin/python -m pytest tests/test_xhigh_review_critical_proofs.py -q
"""
from __future__ import annotations

import concurrent.futures
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


# ---------------------------------------------------------------------------
# C1 — Unauthenticated dashboard defaults to all interfaces
# ---------------------------------------------------------------------------


def test_c1_dashboard_defaults_bind_all_interfaces_unauthenticated():
    from src.config import Settings
    from src.dashboard.api import create_dashboard_app

    s = Settings(_env_file=None)
    assert s.dashboard_host == "0.0.0.0"
    assert s.dashboard_allow_remote is True

    app = create_dashboard_app()
    client = TestClient(app)
    r = client.get("/api/meta")
    assert r.status_code == 200
    r2 = client.post("/api/tasks/FAKE-1/cancel")
    assert r2.status_code != 401
    assert r2.status_code != 403


# ---------------------------------------------------------------------------
# C2 — PATCH jira_host keeps real token; client would Bearer to attacker host
# ---------------------------------------------------------------------------


def test_c2_unauthenticated_settings_redirect_jira_host_keeps_token(
    tmp_path, monkeypatch
):
    from src.config import settings
    from src.dashboard.api import create_dashboard_app
    from src.jira.client import JiraClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "jira_host", "https://jira.company.com")
    monkeypatch.setattr(settings, "jira_api_token", "REAL-SECRET-TOKEN")
    monkeypatch.setattr(settings, "jira_email", "")

    app = create_dashboard_app()
    client = TestClient(app)
    r = client.patch(
        "/api/settings",
        json={"jira_host": "https://attacker.example"},
    )
    assert r.status_code == 400
    assert "token" in (r.json().get("detail") or "").lower()
    assert settings.jira_host.rstrip("/") == "https://jira.company.com"
    assert settings.jira_api_token == "REAL-SECRET-TOKEN"


# ---------------------------------------------------------------------------
# C3 — Jira probe SSRF when caller supplies any token (body reflected)
# ---------------------------------------------------------------------------


def test_c3_jira_probe_ssrf_with_provided_token_reflects_body(monkeypatch):
    from src.jira_connection import probe_jira_connection

    class FakeResp:
        def __init__(self, status_code, text="", json_data=None):
            self.status_code = status_code
            self.text = text
            self.content = (text or "").encode()
            self._json = json_data or {}

        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            return FakeResp(
                500,
                text="INTERNAL_STACKTRACE_SECRET cloud-metadata=yes",
            )

    monkeypatch.setattr("src.jira_connection.httpx.Client", FakeClient)
    result = probe_jira_connection(
        host="http://169.254.169.254",
        api_token="dummy-not-empty",
        email="",
    )
    assert result.get("ok") is False
    err = result.get("error") or ""
    assert "INTERNAL_STACKTRACE_SECRET" not in err
    assert "169.254.169.254" in (result.get("host") or "") or "Refusing" in err or "HTTP" in err


# ---------------------------------------------------------------------------
# C4 — Parent-domain PAT: evil subdomain receives real GitLab PAT
# ---------------------------------------------------------------------------


def test_c4_gitlab_pat_evil_subdomain_receives_real_pat():
    from src.config import Settings

    s = Settings(
        _env_file=None,
        gitlab_host_pats='{"gitlab.company.com":"REAL-GITLAB-PAT"}',
    )
    evil = s.gitlab_pat_for_host("evil.gitlab.company.com")
    assert evil == ""
    assert s.gitlab_pat_for_host("gitlab.company.com") == "REAL-GITLAB-PAT"

    # Nested hosts: parent can win over more-specific map entry (order-dependent)
    s2 = Settings(
        _env_file=None,
        gitlab_host_pats=(
            '{"example.com":"PAT-PARENT","gitlab.example.com":"PAT-GITLAB"}'
        ),
    )
    nested = s2.gitlab_pat_for_host("api.gitlab.example.com")
    assert nested == ""


# ---------------------------------------------------------------------------
# C5 — glab success without URL returns literal "created" as MR URL
# ---------------------------------------------------------------------------


def test_c5_create_mr_returns_literal_created_without_url(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    gm = GitManager.__new__(GitManager)
    gm.temp_dir = str(tmp_path)
    gm.repo_url = "https://gitlab.example.com/g/p.git"
    gm.gitlab_url = "https://gitlab.example.com"
    gm.gitlab_pat = "pat"
    gm.project_path = "g/p"
    gm.work_branch = "feature/X-1"
    gm.feature_branch = "feature/X-1"
    gm.target_branch = "develop"
    gm.source_branch = "develop"
    gm.remote_enabled = True
    gm._glab_env = {}

    class R:
        returncode = 0
        stdout = "Merge request created successfully.\n"
        stderr = ""

    monkeypatch.setattr(gm, "_run_glab", lambda cmd: R())
    monkeypatch.setattr(gm, "_create_mr_via_api", lambda *a, **k: None)
    monkeypatch.setattr(gm, "_get_existing_mr_url", lambda *a, **k: None)

    url = gm.create_merge_request(title="feat(x): test", body="body", target_branch="develop")
    assert url is None


# ---------------------------------------------------------------------------
# C6 — Source branch claim must be atomic under concurrent threads (fixed)
# ---------------------------------------------------------------------------


def test_c6_claim_source_branch_is_atomic_under_threads(tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        processor = JobProcessor()

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
        f"expected exclusive claim under holders lock; got {winners} winners"
    )


# ---------------------------------------------------------------------------
# C7 — Issue detail A→B: cancel uses stale detail.issue_key
# ---------------------------------------------------------------------------


def test_c7_issue_detail_stale_cancel_target_on_route_change():
    """Static model of IssueDetailPage.tsx cancel binding."""
    cache = {
        "KAN-A": {"issue_key": "KAN-A", "can_cancel": True, "summary": "A"},
    }
    route = "KAN-A"
    detail = cache.get(route)

    def on_route_change(new_key: str):
        nonlocal route, detail
        route = new_key
        seed = cache.get(new_key)
        if seed:
            detail = seed
        # cache miss: detail left on previous issue (production TSX)

    def cancel_target():
        return detail["issue_key"] if detail else None

    on_route_change("KAN-B")
    assert route == "KAN-B"
    assert cancel_target() == "KAN-A"


def test_c7_job_detail_gates_cancel_on_route_id():
    cache = {"job_1": {"job_id": "job_1", "issue_key": "KAN-A"}}
    route = "job_1"
    job = cache.get(route)

    def on_route_change(new_id: str):
        nonlocal route, job
        route = new_id
        job = cache.get(new_id)  # always assign including None

    def cancel_allowed():
        return bool(job) and job["job_id"] == route

    on_route_change("job_2")
    assert job is None
    assert cancel_allowed() is False


# ---------------------------------------------------------------------------
# C8 — Accept In Progress then handler drop leaves no state / no error
# ---------------------------------------------------------------------------


def test_c8_process_issue_in_progress_with_noop_handler_leaves_no_state(
    state_manager, fake_jira
):
    from src.jira.poller import JiraPoller

    # Ensure transition reports success so poller records "in progress"
    fake_jira.transition_to_in_progress = lambda key: True  # type: ignore[method-assign]
    transitions: list[str] = []
    fake_jira.transition_to_in_progress = (  # type: ignore[method-assign]
        lambda key: (transitions.append(key) or True)
    )

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._last_jira_status = {}
    p._seen_issues = set()

    issue = {
        "key": "KAN-DROP-1",
        "fields": {
            "summary": "x",
            "description": (
                "Mode: build\n{params}\n"
                "Repository: https://g.example/r.git\n{/params}"
            ),
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
            "assignee": None,
        },
    }

    p._handler = None
    p.process_issue(issue, is_update=False)

    assert "KAN-DROP-1" in transitions
    st = state_manager.get_state("KAN-DROP-1")
    assert st is not None
    assert st.status == TaskStatus.ERROR
    assert p._last_jira_status.get("KAN-DROP-1") == "in progress"
    assert fake_jira.comments


# ---------------------------------------------------------------------------
# C9 — Soft no_new_commits completion still surfaces prior MR URL
# ---------------------------------------------------------------------------


def test_c9_post_completion_no_new_commits_still_includes_prior_mr_url(
    state_manager, fake_jira, reporter
):
    state_manager.create_state("KAN-MR-1", "s", "d")
    state = state_manager.update_state(
        "KAN-MR-1",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={
            "merge_request_url": "https://gitlab.example.com/g/p/-/merge_requests/99",
            "delivery_status": "no_new_commits",
            "delivery_note": "no new commits this run",
            "feature_branch": "feature/KAN-MR-1",
        },
    )
    assert state is not None
    reporter.post_completion(state, summary="done")

    assert fake_jira.comments, "expected a Jira completion comment"
    body = "\n".join(c["body"] for c in fake_jira.comments)
    assert "merge_requests/99" not in body
    assert "No new commits for this run" in body


# ---------------------------------------------------------------------------
# Sanity: CAS fail still protects COMPLETED (not a critical bug)
# ---------------------------------------------------------------------------


def test_ok_fail_issue_does_not_overwrite_completed(
    state_manager, fake_jira, monkeypatch, tmp_path
):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = MagicMock()
    proc.jira_client = fake_jira
    proc._contexts = {}

    state_manager.create_state("KAN-OK", "s", "d")
    state_manager.update_state(
        "KAN-OK",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
    )
    # _fail_issue is synchronous
    proc._fail_issue("KAN-OK", "late error")
    st = state_manager.get_state("KAN-OK")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
