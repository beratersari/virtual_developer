"""LIVE Jira + GitLab: deliver commits after an agent error.

Uses ``JIRA_*`` and ``GITLAB_PAT`` from ``.env``. Creates a real Jira issue
(no trigger label), clones the operator GitLab repo, commits, then runs
``_deliver_if_new_commits`` as if OpenCode returned INCOMPLETE.

Run::

    VD_LIVE_JIRA=1 VD_LIVE_GITLAB=1 .venv/bin/python -m pytest \\
        tests/test_live_jira_gitlab_deliver_after_error.py -v -s

Does not print tokens. Cleans up the pushed feature branch (and MR if opened).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from src.config import settings
from src.git_manager import GitManager
from src.jira.client import JiraClient
from src.jira_connection import probe_jira_connection
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


REAL_GITLAB = "https://gitlab.com/beratersari0/test_project.git"
E2E_LABEL = "vd-live-deliver"


def _dotenv_jira_email() -> str:
    """Runtime settings may have cleared JIRA_EMAIL; Cloud tokens need it."""
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.is_file():
        return (getattr(settings, "jira_email", "") or "").strip()
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        if key.strip() == "JIRA_EMAIL":
            return val.strip().strip('"').strip("'")
    return (getattr(settings, "jira_email", "") or "").strip()


def _ready() -> str:
    if os.environ.get("VD_LIVE_JIRA") != "1" or os.environ.get("VD_LIVE_GITLAB") != "1":
        return "Set VD_LIVE_JIRA=1 and VD_LIVE_GITLAB=1"
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    pat = (settings.gitlab_pat or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    if not pat or pat.startswith("your-"):
        return "GITLAB_PAT not configured"
    if "atlassian.net" in host.lower() and not _dotenv_jira_email():
        return "Jira Cloud needs JIRA_EMAIL in .env for Basic auth"
    return ""


pytestmark = pytest.mark.skipif(bool(_ready()), reason=_ready() or "live")


def _gitlab_headers() -> dict:
    pat = (settings.gitlab_pat or "").strip()
    return {"PRIVATE-TOKEN": pat, "Accept": "application/json"}


def _gitlab_project() -> str:
    return "beratersari0%2Ftest_project"


@pytest.fixture
def jira_client():
    skip = _ready()
    if skip:
        pytest.skip(skip)
    email = _dotenv_jira_email()
    client = JiraClient(email=email)
    probe = probe_jira_connection(
        host=client.host,
        email=email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")
    return client


@pytest.fixture
def isolated_state(tmp_path):
    return JiraStateManager(state_dir=tmp_path / "state")


def _create_jira(client: JiraClient, summary: str, description: str) -> str:
    project = ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()
    created = client.create_issue(
        project,
        summary,
        description,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key = created["key"]
    print(f"\n[live] Jira {key}  {client.host.rstrip('/')}/browse/{key}", flush=True)
    return key


def _delete_remote_branch(branch: str) -> None:
    from urllib.parse import quote

    enc = quote(branch, safe="")
    url = f"https://gitlab.com/api/v4/projects/{_gitlab_project()}/repository/branches/{enc}"
    with httpx.Client(timeout=30.0, verify=False) as http:
        resp = http.delete(url, headers=_gitlab_headers())
    print(f"[live] delete branch {branch} status={resp.status_code}", flush=True)


def _close_mr(iid: int) -> None:
    url = f"https://gitlab.com/api/v4/projects/{_gitlab_project()}/merge_requests/{iid}"
    with httpx.Client(timeout=30.0, verify=False) as http:
        resp = http.put(url, headers=_gitlab_headers(), json={"state_event": "close"})
    print(f"[live] close MR !{iid} status={resp.status_code}", flush=True)


@pytest.mark.asyncio
async def test_live_jira_gitlab_push_after_agent_error(
    jira_client: JiraClient, isolated_state, tmp_path, monkeypatch
):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = f"feature/vd-live-deliver-{stamp}"
    desc = (
        "Live deliver-after-error check. Safe to close.\n"
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        f"Source branch: {work}\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    key = _create_jira(
        jira_client,
        f"[vd-live-deliver] agent-error still pushes {stamp}",
        desc,
    )

    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "t"))
    # Lone GITLAB_PAT must be allowed for gitlab.com in this operator .env.
    if not (settings.gitlab_allowed_hosts or "").strip():
        monkeypatch.setattr(settings, "gitlab_allowed_hosts", "gitlab.com")

    gm = GitManager(
        issue_key=key,
        remote_url=REAL_GITLAB,
        source_branch=work,
        target_branch="main",
    )
    assert gm.temp_dir is not None
    assert (Path(gm.temp_dir) / ".git").is_dir()
    branch = gm.ensure_feature_branch(key)
    assert branch == work, branch

    with patch("src.processor.create_jira_client", return_value=jira_client):
        proc = JobProcessor()
    proc.state_manager = isolated_state
    proc.reporter = JiraReporter(client=jira_client)
    proc.jira_client = jira_client
    state = isolated_state.create_state(key, f"[vd-live-deliver] {stamp}", desc)
    isolated_state.update_state(key, status=TaskStatus.EXECUTING)
    proc._contexts[key] = {"git": gm, "runner": None}
    proc.git_manager = gm
    baseline = proc._snapshot_delivery_baseline(key, gm)
    assert baseline

    (Path(gm.temp_dir) / "vd_live_deliver.txt").write_text(
        f"{key} {stamp} deliver-after-error\n", encoding="utf-8"
    )
    gm._run_git(["add", "vd_live_deliver.txt"])
    gm._run_git(
        [
            "commit",
            "-m",
            f"test({key}): live deliver after agent error",
        ]
    )
    new_sha = gm.get_last_commit_sha()
    assert new_sha and new_sha != baseline

    mr_iid = None
    try:
        outcome = await proc._deliver_if_new_commits(state)
        assert outcome == "delivered", (
            f"expected delivered, got {outcome} "
            f"push_error={gm.last_push_error!r}"
        )
        assert gm.head_is_on_remote(work) is True

        from urllib.parse import quote

        with httpx.Client(timeout=30.0, verify=False) as http:
            br = http.get(
                f"https://gitlab.com/api/v4/projects/{_gitlab_project()}"
                f"/repository/branches/{quote(work, safe='')}",
                headers=_gitlab_headers(),
            )
        assert br.status_code == 200, (br.status_code, (br.text or "")[:300])
        remote_sha = ((br.json() or {}).get("commit") or {}).get("id")
        assert remote_sha == new_sha, f"remote {remote_sha} != local {new_sha}"
        print(f"[live] GitLab branch {work} sha={new_sha[:12]}", flush=True)

        refreshed = isolated_state.get_state(key)
        mr_url = (refreshed.metadata or {}).get("merge_request_url") if refreshed else None
        print(f"[live] MR url={mr_url}", flush=True)
        if mr_url and "/merge_requests/" in str(mr_url):
            try:
                mr_iid = int(str(mr_url).rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                mr_iid = None

        comment = jira_client.add_comment(
            key,
            (
                f"*Yaver* live check: agent session would have been INCOMPLETE, "
                f"but commits were still pushed to `{work}` "
                f"(`{new_sha[:12]}`).\n"
                + (f"MR: {mr_url}\n" if mr_url else "")
                + "This ticket is a live test — safe to close."
            ),
        )
        assert comment, "Jira comment failed"
        bodies = []
        for c in jira_client.get_comments(key):
            body = c.get("body")
            if isinstance(body, str):
                bodies.append(body)
        blob = "\n".join(bodies)
        assert work in blob
        assert new_sha[:12] in blob
    finally:
        if mr_iid:
            _close_mr(mr_iid)
        _delete_remote_branch(work)
        if gm.temp_dir and Path(gm.temp_dir).exists() and str(tmp_path) in str(gm.temp_dir):
            shutil.rmtree(gm.temp_dir, ignore_errors=True)
