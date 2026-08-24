"""LIVE Jira + real-repository audit.

Creates real Cloud issues (NO bot / ai-assist — the board poller must not
start a daemon job). Clones the operator's real GitLab repo over HTTPS.

Run::

    .venv/bin/python -m pytest tests/test_live_audit_jira_repos.py -v -s

Skipped when Jira is unconfigured or the probe fails.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.config import settings
from src.git_manager import GitManager
from src.issue_git_spec import parse_issue_git_spec
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.jira_connection import probe_jira_connection
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import JiraAgentState, TaskStatus


E2E_LABEL = "vd-audit-e2e"
REAL_GITLAB = "https://gitlab.com/beratersari0/test_project.git"
REAL_GITHUB = "https://github.com/octocat/Hello-World.git"


def _jira_live_ready() -> str:
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    return ""


def _comment_blob(comments: List[Dict[str, Any]]) -> str:
    bodies: List[str] = []
    for c in comments:
        body = c.get("body")
        if isinstance(body, str):
            bodies.append(body)
        elif isinstance(body, dict):
            bodies.append(str(body))
    return "\n".join(bodies)


@pytest.fixture(scope="module")
def jira_client():
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)
    client = JiraClient()
    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")
    return client


def _project() -> str:
    return ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()


def _create(client: JiraClient, summary: str, description: str) -> str:
    created = client.create_issue(
        _project(),
        summary,
        description,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key = created["key"]
    print(f"\n[live audit] created {key}  {client.host.rstrip('/')}/browse/{key}", flush=True)
    return key


def test_live_create_roundtrip_real_gitlab_params(jira_client: JiraClient):
    """POST a clean {params} block with the operator's GitLab URL, GET it back."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    desc = (
        "Audit roundtrip — do not process (no trigger label).\n"
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    key = _create(jira_client, f"[vd-audit] gitlab params {stamp}", desc)
    fetched = jira_client.get_issue(key, fields=["summary", "description", "labels", "status"])
    assert fetched and fetched.get("key") == key
    fields = fetched.get("fields") or {}
    stored = fields.get("description") or ""
    assert isinstance(stored, str) and stored.strip(), "Cloud returned empty description"
    print(f"[live audit] stored description:\n{stored[:800]}", flush=True)

    spec, err = parse_issue_git_spec(fields.get("summary") or "", stored)
    assert err is None, f"parser rejected live Cloud description: {err}\n---\n{stored}"
    assert spec is not None
    assert spec.repository_url.rstrip("/") in {
        REAL_GITLAB,
        REAL_GITLAB.removesuffix(".git"),
    } or spec.repository_url.startswith("https://gitlab.com/beratersari0/test_project")
    assert spec.mode == "build"
    labels = fields.get("labels") or []
    assert E2E_LABEL in labels or E2E_LABEL in str(labels)
    assert "bot" not in [str(x).lower() for x in labels]


def test_live_cloud_autolinks_issue_key_inside_branch(jira_client: JiraClient):
    """If Cloud wiki-links ``KAN-7`` inside Source branch, parser must still work.

    Writes the *plain* text ``feature/KAN-7`` (KAN-7 exists on this site) and
    inspects what Cloud stored. Documents the actual stored form; asserts the
    parser recovers ``feature/KAN-7``.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    desc = (
        "Audit: issue-key inside branch name.\n"
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        "Source branch: feature/KAN-7\n"
        "Target branch: main\n"
        "Mode: plan\n"
        "{params}\n"
    )
    key = _create(jira_client, f"[vd-audit] branch autolink {stamp}", desc)
    fetched = jira_client.get_issue(key, fields=["description", "summary"])
    assert fetched
    stored = (fetched.get("fields") or {}).get("description") or ""
    print(f"[live audit] autolink stored:\n{stored[:800]}", flush=True)

    has_brackets = "[KAN-7]" in stored or "[KAN-7|" in stored
    spec, err = parse_issue_git_spec(
        (fetched.get("fields") or {}).get("summary") or "", stored
    )
    if has_brackets:
        # This is the production KAN-7 failure mode. Parser must recover.
        assert err is None, (
            f"Cloud stored wiki-linked branch but parser failed: {err}\n{stored}"
        )
        assert spec is not None
        assert spec.source_branch == "feature/KAN-7"
    else:
        # Cloud did not rewrite on API create. Still parse the clean form.
        assert err is None, err
        assert spec is not None
        assert spec.source_branch == "feature/KAN-7"


def test_live_comment_transition_labels_add_not_replace(jira_client: JiraClient):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = _create(
        jira_client,
        f"[vd-audit] comment/transition {stamp}",
        "Audit transitions and label merge. Safe to delete.",
    )
    assert jira_client.add_comment(key, f"[vd-audit] hello {stamp}")
    comments = jira_client.get_comments(key)
    assert stamp in _comment_blob(comments)

    moved = jira_client.transition_to_in_progress(key)
    assert moved, "transition_to_in_progress failed on Cloud KAN board"
    live = jira_client.get_issue(key, fields=["status", "labels"])
    status = ((live or {}).get("fields") or {}).get("status") or {}
    assert "progress" in (status.get("name") or "").lower(), status

    assert jira_client.add_labels(key, ["ai-plan-ready"])
    live2 = jira_client.get_issue(key, fields=["labels"])
    labels = [str(x) for x in ((live2 or {}).get("fields") or {}).get("labels") or []]
    assert E2E_LABEL in labels, labels
    assert "ai-plan-ready" in labels, labels


def test_live_poller_without_handler_fails_visible_on_jira(
    jira_client: JiraClient, tmp_path: Path
):
    """No-op handler must still move the board off To Do and post an error."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = _create(
        jira_client,
        f"[vd-audit] poller noop {stamp}",
        "Audit poller fail-visible path. Safe to delete.",
    )
    state_manager = JiraStateManager(state_dir=tmp_path / "state")
    poller = JiraPoller(
        client=jira_client,
        interval_seconds=60,
        board_id=settings.jira_board_id,
        state_manager=state_manager,
    )
    poller._handler = None
    issue = jira_client.get_issue(key)
    assert issue
    poller.process_issue(issue, is_update=False)

    live = jira_client.get_issue(key, fields=["status", "comment"])
    status = (((live or {}).get("fields") or {}).get("status") or {}).get("name") or ""
    assert "progress" in status.lower(), f"expected In Progress, got {status!r}"
    st = state_manager.get_state(key)
    assert st is not None and st.status == TaskStatus.ERROR
    comments = (((live or {}).get("fields") or {}).get("comment") or {}).get("comments") or []
    blob = _comment_blob(comments)
    assert "error" in blob.lower() or "could not" in blob.lower(), blob[:400]


def test_live_clone_real_gitlab_repo_push_without_pat(
    jira_client: JiraClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Clone the operator's real GitLab repo; push must fail with no PAT."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    desc = (
        "Audit real-repo clone + push-without-PAT.\n"
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    key = _create(jira_client, f"[vd-audit] real gitlab clone {stamp}", desc)

    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "t"))
    monkeypatch.setattr(settings, "gitlab_pat", "")
    if hasattr(settings, "gitlab_host_pats"):
        monkeypatch.setattr(settings, "gitlab_host_pats", "")

    gm = GitManager(
        issue_key=key,
        remote_url=REAL_GITLAB,
        source_branch="develop",
        target_branch="main",
    )
    assert gm.temp_dir is not None
    assert (Path(gm.temp_dir) / ".git").exists(), f"clone missing .git in {gm.temp_dir}"
    branch = gm.ensure_feature_branch(key)
    assert branch, "ensure_feature_branch returned nothing after a real clone"
    print(f"[live audit] cloned {REAL_GITLAB} → {gm.temp_dir} work={branch}", flush=True)

    pushed = gm.push(branch)
    print(
        f"[live audit] push ok={pushed} last_push_error={gm.last_push_error!r} "
        f"gitlab_pat_configured={bool((settings.gitlab_pat or '').strip())}",
        flush=True,
    )
    assert pushed is False, "push succeeded with empty GITLAB_PAT — unexpected"
    assert gm.last_push_error, "push failed but last_push_error was empty"

    state = JiraAgentState(
        issue_key=key,
        issue_summary=f"[vd-audit] real gitlab clone {stamp}",
        description=desc,
        status=TaskStatus.ERROR,
    )
    reporter = JiraReporter(client=jira_client)
    comment_id = reporter.post_error(
        state,
        (
            "Agent finished but git push failed; work was not delivered to remote.\n\n"
            f"{gm.last_push_error}"
        ),
        suggestion=(
            "GITLAB_PAT / GITLAB_HOST_PATS is empty in this environment. "
            "Set a gitlab.com PAT on Settings (or .env) then re-queue from To Do."
        ),
        category="git",
    )
    assert comment_id, "failed to post push-failure comment to live Jira"
    blob = _comment_blob(jira_client.get_comments(key))
    assert "push failed" in blob.lower() or "git push" in blob.lower(), blob[:500]

    # Do not leave the temp clone if GitManager put it under cwd/.temp somehow
    if gm.temp_dir and Path(gm.temp_dir).exists() and str(tmp_path) in str(gm.temp_dir):
        shutil.rmtree(gm.temp_dir, ignore_errors=True)


def test_live_github_public_repo_parses_and_clones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Public GitHub repo used in prior live jobs (KAN-96) must clone without PAT."""
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "t"))
    monkeypatch.setattr(settings, "gitlab_pat", "")
    gm = GitManager(
        issue_key="AUDIT-GH",
        remote_url=REAL_GITHUB,
        source_branch="master",
        target_branch="master",
    )
    assert gm.temp_dir is not None
    assert (Path(gm.temp_dir) / ".git").exists()
    branch = gm.ensure_feature_branch("AUDIT-GH")
    assert branch
    print(f"[live audit] cloned {REAL_GITHUB} work={branch}", flush=True)
    if gm.temp_dir and Path(gm.temp_dir).exists() and str(tmp_path) in str(gm.temp_dir):
        shutil.rmtree(gm.temp_dir, ignore_errors=True)
