"""LIVE Jira intake + GitLab: different ticket prompts vs MR creation.

Creates real Jira issues (no bot/ai-assist labels) and runs the same
``process_event`` path the poller uses. The agent is stubbed so the prompt
controls whether a commit happens. A new MR must be opened only when the
work branch is ahead of the target.

Run::

    YAVER_DATA_DIR=/tmp/yaver-live-e2e \\
    VD_LIVE_JIRA=1 VD_LIVE_GITLAB=1 .venv/bin/python -m pytest \\
        tests/test_live_jira_job_prompts_mr_e2e.py -v -s
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
import pytest

from src.config import settings
from src.jira.client import JiraClient
from src.jira_connection import probe_jira_connection
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


REAL_GITLAB = "https://gitlab.com/beratersari0/test_project.git"
E2E_LABEL = "vd-live-prompt-mr"
TARGET = "main"


def _dotenv_jira_email() -> str:
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
    host = (
        os.environ.get("JIRA_HOST") or settings.jira_host or ""
    ).strip()
    token = (
        os.environ.get("JIRA_API_TOKEN") or settings.jira_api_token or ""
    ).strip()
    pat = (settings.gitlab_pat or os.environ.get("GITLAB_PAT") or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    if not pat or pat.startswith("your-"):
        return "GITLAB_PAT not configured"
    if "atlassian.net" in host.lower() and not (
        _dotenv_jira_email() or os.environ.get("JIRA_EMAIL")
    ):
        return "Jira Cloud needs JIRA_EMAIL in .env for Basic auth"
    return ""


pytestmark = pytest.mark.skipif(bool(_ready()), reason=_ready() or "live")


def _gitlab_headers() -> dict:
    pat = (settings.gitlab_pat or os.environ.get("GITLAB_PAT") or "").strip()
    return {"PRIVATE-TOKEN": pat, "Accept": "application/json"}


def _gitlab_project() -> str:
    return "beratersari0%2Ftest_project"


def _params(source: str, target: str, body: str) -> str:
    return (
        f"{body}\n"
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        "Mode: build\n"
        "{params}\n"
    )


@pytest.fixture
def jira_client():
    skip = _ready()
    if skip:
        pytest.skip(skip)
    email = _dotenv_jira_email() or (os.environ.get("JIRA_EMAIL") or "").strip()
    host = (os.environ.get("JIRA_HOST") or settings.jira_host or "").strip()
    token = (os.environ.get("JIRA_API_TOKEN") or settings.jira_api_token or "").strip()
    client = JiraClient(host=host, email=email, api_token=token)
    probe = probe_jira_connection(
        host=client.host, email=email, api_token=client.api_token
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")
    return client


def _create_jira(client: JiraClient, summary: str, description: str) -> str:
    project = ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()
    created = client.create_issue(
        project, summary, description, issue_type="Task", labels=[E2E_LABEL]
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key = created["key"]
    print(f"\n[live] Jira {key}  {client.host.rstrip('/')}/browse/{key}", flush=True)
    return key


def _jira_event(client: JiraClient, key: str) -> Dict[str, Any]:
    issue = client.get_issue(key)
    assert issue and issue.get("key") == key
    return {"webhookEvent": "jira:issue_created", "issue": issue}


def _list_mrs(source_branch: str) -> List[Dict[str, Any]]:
    url = (
        f"https://gitlab.com/api/v4/projects/{_gitlab_project()}/merge_requests"
        f"?source_branch={quote(source_branch, safe='')}&state=all"
    )
    with httpx.Client(timeout=30.0, verify=False) as http:
        resp = http.get(url, headers=_gitlab_headers())
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def _delete_remote_branch(branch: str) -> None:
    enc = quote(branch, safe="")
    url = (
        f"https://gitlab.com/api/v4/projects/{_gitlab_project()}"
        f"/repository/branches/{enc}"
    )
    with httpx.Client(timeout=30.0, verify=False) as http:
        resp = http.delete(url, headers=_gitlab_headers())
    print(f"[live] delete branch {branch} status={resp.status_code}", flush=True)


def _close_mrs(source_branch: str) -> None:
    for mr in _list_mrs(source_branch):
        iid = mr.get("iid")
        if not iid:
            continue
        url = (
            f"https://gitlab.com/api/v4/projects/{_gitlab_project()}"
            f"/merge_requests/{iid}"
        )
        with httpx.Client(timeout=30.0, verify=False) as http:
            resp = http.put(
                url, headers=_gitlab_headers(), json={"state_event": "close"}
            )
        print(f"[live] close MR !{iid} status={resp.status_code}", flush=True)


def _stub_prompt_agent(processor: JobProcessor, *, commit: bool):
    async def run(task, **_k):
        key = getattr(task, "issue_key", None) or ""
        git = processor._git_for(key)
        prompt = (getattr(task, "prompt", None) or "").lower()
        investigate = "investigation only" in prompt or "do not change files" in prompt
        should_commit = commit and not investigate
        if should_commit and git is not None and getattr(git, "temp_dir", None):
            path = Path(git.temp_dir) / "vd_prompt_e2e.txt"
            path.write_text(f"{key} implement from jira prompt\n", encoding="utf-8")
            git._run_git(["add", "vd_prompt_e2e.txt"])
            git._run_git(
                ["commit", "-m", f"feat({key}): implement from jira prompt"]
            )
        stdout = (
            "Investigation only. No repository changes."
            if not should_commit
            else "Implemented the requested file change."
        )
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "session_file": None,
            "opencode_session_id": "ses_live_prompt",
        }

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(side_effect=run)
    orig = processor._init_git_manager

    def init_and_swap_runner(issue_key, state=None):
        git = orig(issue_key, state)
        if git is not None:
            ctx = processor._contexts.get(issue_key) or {}
            ctx["git"] = git
            ctx["runner"] = runner
            processor._contexts[issue_key] = ctx
            processor.agent_runner = runner
        return git

    processor._init_git_manager = init_and_swap_runner  # type: ignore[method-assign]
    return runner


async def _take_jira_job(
    jira_client: JiraClient,
    tmp_path: Path,
    monkeypatch,
    *,
    stamp: str,
    source: str,
    target: str,
    summary: str,
    prompt_body: str,
    commit: bool,
) -> Dict[str, Any]:
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "t"))
    if not (settings.gitlab_allowed_hosts or "").strip():
        monkeypatch.setattr(settings, "gitlab_allowed_hosts", "gitlab.com")

    desc = _params(source, target, prompt_body)
    key = _create_jira(jira_client, f"[vd-live-prompt-mr] {summary} {stamp}", desc)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=jira_client):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = JiraReporter(client=jira_client)
    proc.jira_client = jira_client
    _stub_prompt_agent(proc, commit=commit)

    event = _jira_event(jira_client, key)
    await proc.process_event(event)
    st = sm.get_state(key)
    work = ""
    if st:
        work = str((st.metadata or {}).get("feature_branch") or source)
    git = proc._git_for(key)
    if git is not None:
        work = (getattr(git, "work_branch", None) or work or source).strip()
    mr_url = (st.metadata or {}).get("merge_request_url") if st else None
    gitlab_mrs = _list_mrs(work) if work else []
    print(
        f"[live] key={key} work={work} status={getattr(st, 'status', None)} "
        f"mr_url={mr_url} gitlab_mrs={len(gitlab_mrs)}",
        flush=True,
    )
    return {
        "key": key,
        "work": work,
        "state": st,
        "mr_url": mr_url,
        "gitlab_mrs": gitlab_mrs,
        "git": git,
        "tmp": tmp_path,
    }


def _cleanup(result: Dict[str, Any]) -> None:
    work = str(result.get("work") or "")
    if work:
        _close_mrs(work)
        _delete_remote_branch(work)
    git = result.get("git")
    tmp = result.get("tmp")
    td = getattr(git, "temp_dir", None) if git is not None else None
    if td and tmp is not None and str(tmp) in str(td):
        shutil.rmtree(td, ignore_errors=True)


@pytest.mark.asyncio
async def test_live_investigation_prompt_does_not_open_mr(
    jira_client, tmp_path, monkeypatch
):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = f"feature/vd-invest-{stamp}"
    result = None
    try:
        result = await _take_jira_job(
            jira_client,
            tmp_path,
            monkeypatch,
            stamp=stamp,
            source=work,
            target=TARGET,
            summary="investigate only",
            prompt_body=(
                "INVESTIGATION ONLY. Read the repo and write findings. "
                "Do not change files, do not commit, do not push."
            ),
            commit=False,
        )
        st = result["state"]
        assert st is not None
        assert (result["mr_url"] or "") == ""
        assert result["gitlab_mrs"] == []
        assert result["git"] is None or result["git"].commits_ahead_of_target(
            result["work"]
        ) == 0
        jira_client.add_comment(
            result["key"],
            "*Yaver* live check: investigation prompt produced no commits; "
            "no merge request was opened. Safe to close.",
        )
    finally:
        if result:
            _cleanup(result)


@pytest.mark.asyncio
async def test_live_implement_prompt_opens_mr_when_ahead(
    jira_client, tmp_path, monkeypatch
):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = f"feature/vd-impl-{stamp}"
    result = None
    try:
        result = await _take_jira_job(
            jira_client,
            tmp_path,
            monkeypatch,
            stamp=stamp,
            source=work,
            target=TARGET,
            summary="implement file",
            prompt_body="IMPLEMENT: add vd_prompt_e2e.txt and commit the change.",
            commit=True,
        )
        st = result["state"]
        assert st is not None
        assert st.status == TaskStatus.COMPLETED
        assert result["mr_url"] and "/merge_requests/" in str(result["mr_url"])
        assert result["gitlab_mrs"], "expected a GitLab MR for an ahead source"
        jira_client.add_comment(
            result["key"],
            f"*Yaver* live check: implement prompt committed ahead of `{TARGET}`; "
            f"MR {result['mr_url']}. Safe to close.",
        )
    finally:
        if result:
            _cleanup(result)


@pytest.mark.asyncio
async def test_live_source_equals_target_params_no_commits_skips_mr(
    jira_client, tmp_path, monkeypatch
):
    """Source=main Target=main (typical investigation ticket). No commits → no MR."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    result = None
    try:
        result = await _take_jira_job(
            jira_client,
            tmp_path,
            monkeypatch,
            stamp=stamp,
            source=TARGET,
            target=TARGET,
            summary="investigate on main=main",
            prompt_body=(
                "INVESTIGATION ONLY. Source and target are the same branch. "
                "Do not change files."
            ),
            commit=False,
        )
        assert (result["mr_url"] or "") == ""
        work = result["work"]
        if work and work.lower() != TARGET.lower():
            assert result["gitlab_mrs"] == []
        jira_client.add_comment(
            result["key"],
            "*Yaver* live check: Source=Target and no new commits; "
            "no merge request. Safe to close.",
        )
    finally:
        if result:
            _cleanup(result)
