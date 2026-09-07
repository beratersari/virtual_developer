"""LIVE plan → build: real OpenCode must write a plan, then follow it.

Creates a Jira ticket (no trigger label — a live daemon must not steal it),
runs ``JobProcessor`` planning, then explicit ``start_plan_execution``,
and checks OpenCode session/prompt output against the durable plan.

Opt-in (hits Jira + GitLab + a real model; slow)::

    VD_LIVE_PLAN_BUILD=1 .venv-win\\Scripts\\python.exe -m pytest \\
        tests/test_live_plan_then_build_e2e.py -v -s --tb=short
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from unittest.mock import patch

import httpx
import pytest

from src.config import settings
from src.jira.client import JiraClient
from src.jira_connection import probe_jira_connection
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


REAL_GITLAB = "https://gitlab.com/beratersari0/test_project.git"
E2E_LABEL = "vd-plan-build-e2e"
TARGET_REL = "notes/vd_plan_follow.txt"
TARGET_LINE = "PLAN_FOLLOWED=1"
PLAN_REL_RE = re.compile(r"\.sisyphus/plans/[A-Z][A-Z0-9]+-\d+\.md", re.I)
# Prefer models currently advertised by OpenCode serve. hy3-free / flash-free
# often rotate off or return "Model is unavailable" from Console.
LIVE_MODELS = (
    "opencode/north-mini-code-free",
    "opencode/ling-3.0-flash-free",
    "opencode/big-pickle",
    "opencode/deepseek-v4-flash-free",
)


def _dotenv_jira_email() -> str:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, val = raw.split("=", 1)
            if key.strip() == "JIRA_EMAIL":
                return val.strip().strip('"').strip("'")
    return (getattr(settings, "jira_email", "") or "").strip()


def _ready() -> str:
    flag = (os.environ.get("VD_LIVE_PLAN_BUILD") or "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        return "Set VD_LIVE_PLAN_BUILD=1 to run the live plan→build e2e"
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    pat = (settings.gitlab_pat or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if not pat or pat.startswith("your-"):
        return "GITLAB_PAT not configured"
    if "atlassian.net" in host.lower() and not _dotenv_jira_email():
        return "Jira Cloud needs JIRA_EMAIL in .env"
    return ""


def _gitlab_headers() -> dict:
    return {
        "PRIVATE-TOKEN": (settings.gitlab_pat or "").strip(),
        "Accept": "application/json",
    }


def collect_text_files(root: Path, *, suffixes: Iterable[str]) -> str:
    """Concatenate utf-8 text files under *root* (session logs, prompts)."""
    parts: List[str] = []
    if not root.is_dir():
        return ""
    want = {s.lower() for s in suffixes}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in want:
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def analyze_plan_follow(
    *,
    plan_text: str,
    plan_prompt: str,
    build_prompt: str,
    plan_session: str,
    build_session: str,
    workspace_file: Optional[str],
) -> Dict[str, object]:
    """Score whether the builder followed the written plan.

    Returns a dict of boolean checks plus a ``passed`` flag. Used by the live
    e2e and by a unit test with canned transcripts.
    """
    plan = plan_text or ""
    plan_low = plan.lower()
    build_sess = build_session or ""
    build_prompt_s = build_prompt or ""
    plan_prompt_s = plan_prompt or ""
    blob_build = f"{build_prompt_s}\n{build_sess}"
    checks = {
        "plan_nonempty": bool(plan.strip()),
        "plan_names_target_file": TARGET_REL.split("/")[-1].lower() in plan_low
        or TARGET_REL.lower() in plan_low,
        "plan_names_marker": TARGET_LINE.lower() in plan_low,
        "plan_prompt_asks_for_plan_file": bool(PLAN_REL_RE.search(plan_prompt_s))
        or ".sisyphus/plans/" in plan_prompt_s,
        "build_prompt_points_at_plan": bool(PLAN_REL_RE.search(build_prompt_s))
        or ".sisyphus/plans/" in build_prompt_s,
        "build_session_mentions_plan": bool(PLAN_REL_RE.search(build_sess))
        or ".sisyphus/plans/" in build_sess.lower()
        or "plan file" in build_sess.lower(),
        "build_session_mentions_target": TARGET_REL.split("/")[-1].lower()
        in build_sess.lower()
        or TARGET_REL.lower() in build_sess.lower(),
        "workspace_has_marker": (workspace_file or "").strip() == TARGET_LINE,
    }
    # Soft: builder quoted / reused a plan heading
    headings = [
        ln.lstrip("#").strip()
        for ln in plan.splitlines()
        if ln.startswith("#") and ln.lstrip("#").strip()
    ]
    checks["build_echoed_plan_heading"] = any(
        h.lower() in blob_build.lower() for h in headings[:5]
    ) if headings else True
    required = (
        "plan_nonempty",
        "plan_names_target_file",
        "plan_names_marker",
        "plan_prompt_asks_for_plan_file",
        "build_prompt_points_at_plan",
        "workspace_has_marker",
    )
    checks["passed"] = all(bool(checks[k]) for k in required)
    checks["required"] = list(required)
    return checks


def test_analyze_plan_follow_canned():
    """Analyzer: a plan that names the file + a build that writes it → pass."""
    plan = (
        f"# Add follow marker\n\n"
        f"Create `{TARGET_REL}` with the line `{TARGET_LINE}`.\n"
    )
    out = analyze_plan_follow(
        plan_text=plan,
        plan_prompt=f"Write the plan to .sisyphus/plans/KAN-9.md only.",
        build_prompt=f"Plan file (if present): .sisyphus/plans/KAN-9.md",
        plan_session="wrote .sisyphus/plans/KAN-9.md",
        build_session=(
            "Reading .sisyphus/plans/KAN-9.md then creating notes/vd_plan_follow.txt"
        ),
        workspace_file=TARGET_LINE,
    )
    assert out["passed"] is True, out

    miss = analyze_plan_follow(
        plan_text="# Vague\nDo something.\n",
        plan_prompt="no path",
        build_prompt="no plan",
        plan_session="",
        build_session="I implemented a refactor",
        workspace_file="",
    )
    assert miss["passed"] is False
    assert miss["plan_names_target_file"] is False
    assert miss["workspace_has_marker"] is False


def _jira_event(key: str, summary: str, description: str) -> dict:
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": key,
            "fields": {
                "summary": summary,
                "description": description,
                "status": {
                    "name": "To Do",
                    "statusCategory": {"key": "new"},
                },
                "labels": [E2E_LABEL],
                "assignee": None,
                "issuetype": {"name": "Task"},
            },
        },
    }


def _delete_remote_branch(branch: str) -> None:
    from urllib.parse import quote

    enc = quote(branch, safe="")
    url = (
        "https://gitlab.com/api/v4/projects/beratersari0%2Ftest_project"
        f"/repository/branches/{enc}"
    )
    with httpx.Client(timeout=30.0, verify=False) as http:
        resp = http.delete(url, headers=_gitlab_headers())
    print(f"[live] delete branch {branch} status={resp.status_code}", flush=True)


def _close_open_mrs_for_branch(branch: str) -> None:
    url = (
        "https://gitlab.com/api/v4/projects/beratersari0%2Ftest_project"
        f"/merge_requests?source_branch={branch}&state=opened"
    )
    with httpx.Client(timeout=30.0, verify=False) as http:
        mrs = http.get(url, headers=_gitlab_headers())
        if mrs.status_code != 200:
            return
        for mr in mrs.json() or []:
            iid = mr.get("iid")
            if not iid:
                continue
            http.put(
                "https://gitlab.com/api/v4/projects/beratersari0%2Ftest_project"
                f"/merge_requests/{iid}",
                headers=_gitlab_headers(),
                json={"state_event": "close"},
            )
            print(f"[live] close MR !{iid}", flush=True)


@pytest.mark.asyncio
async def test_live_plan_then_build_follows_plan(tmp_path, monkeypatch):
    skip = _ready()
    if skip:
        pytest.skip(skip)

    from tests.test_opencode_serve_live_e2e import (
        _free_port,
        _opencode_bin,
        _start_serve,
        _stop_serve,
        _wait_health,
    )

    if not _opencode_bin():
        pytest.skip("opencode binary not on PATH")

    email = _dotenv_jira_email()
    jira = JiraClient(email=email)
    probe = probe_jira_connection(
        host=jira.host, email=email, api_token=jira.api_token
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")

    serve_url = (getattr(settings, "opencode_serve_url", None) or "").strip() or (
        "http://127.0.0.1:4096"
    )
    serve_proc = None
    try:
        await _wait_health(serve_url, timeout=4.0)
        print(f"[live] reusing OpenCode serve {serve_url}", flush=True)
    except Exception:
        port = _free_port()
        serve_url = f"http://127.0.0.1:{port}"
        log = tmp_path / "opencode-serve.log"
        serve_proc = _start_serve(port, log)
        await _wait_health(serve_url, timeout=90.0)
        print(f"[live] started OpenCode serve {serve_url}", flush=True)
        monkeypatch.setattr(settings, "opencode_serve_url", serve_url)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = f"feature/vd-plan-build-{stamp}"
    summary = f"[vd-plan-build] follow the plan {stamp}"
    description = (
        "Live plan→build check. Safe to close.\n"
        f"Write `{TARGET_REL}` with exactly one line:\n"
        f"`{TARGET_LINE}`\n"
        "Do not change any other product files.\n"
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        f"Source branch: {work}\n"
        "Target branch: main\n"
        "Mode: plan\n"
        "{params}\n"
    )
    created = jira.create_issue(
        ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip(),
        summary,
        description,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {jira.last_error}")
    key = created["key"]
    print(f"\n[live] Jira {key}  {jira.host.rstrip('/')}/browse/{key}", flush=True)

    workdir = tmp_path / "run"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "t"))
    monkeypatch.setattr(settings, "sisyphus_plans_dir", Path(".sisyphus/plans"))
    monkeypatch.setattr(settings, "agent_prompts_dir", Path(__file__).resolve().parents[1] / "agent")
    monkeypatch.setattr(settings, "default_model", LIVE_MODELS[0])
    monkeypatch.setattr(settings, "agent_task_max_retries", 1)
    monkeypatch.setattr(settings, "agent_task_max_incomplete_retries", 0)
    if not (settings.gitlab_allowed_hosts or "").strip() and not (
        settings.gitlab_host_pats or ""
    ).strip():
        monkeypatch.setattr(settings, "gitlab_allowed_hosts", "gitlab.com")

    sm = JiraStateManager(state_dir=tmp_path / "state")
    jobs = JobStore(jobs_dir=tmp_path / "jobs")

    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = jobs
    proc.reporter = JiraReporter(client=jira)
    proc.jira_client = jira

    try:
        outcome = await proc.process_event(_jira_event(key, summary, description))
        print(f"[live] plan process_event={outcome}", flush=True)
        st = sm.get_state(key)
        assert st is not None, "no local state after plan"
        print(
            f"[live] after plan status={st.status.value} err={st.error_message!r}",
            flush=True,
        )
        err = st.error_message or ""
        if st.status != TaskStatus.PLAN_READY and (
            "Model is unavailable" in err
            or "finish is unfinished" in err
            or "HTTP 500" in err
        ):
            pytest.skip(
                "OpenCode Console model did not complete a plan turn "
                f"({st.status.value}: {err[:240]})"
            )
        assert st.status == TaskStatus.PLAN_READY, (
            f"expected plan_ready, got {st.status.value}: {st.error_message}"
        )

        durable = Path(workdir) / ".sisyphus" / "plans" / f"{key}.md"
        if st.plan_path:
            durable = Path(st.plan_path)
        assert durable.is_file(), f"durable plan missing: {durable}"
        plan_text = durable.read_text(encoding="utf-8", errors="replace")
        print(f"[live] plan bytes={len(plan_text)} path={durable}", flush=True)
        print(plan_text[:1500], flush=True)

        # Operator start signal (same ticket) — not Mode: build auto-promote.
        started = await proc.start_plan_execution(
            key, reason="live plan→build e2e start_plan_execution"
        )
        print(f"[live] start_plan_execution={started}", flush=True)
        st2 = sm.get_state(key)
        assert st2 is not None
        print(
            f"[live] after build status={st2.status.value} err={st2.error_message!r}",
            flush=True,
        )

        sessions = tmp_path / "_vd_runtime" / "sessions"
        # conftest isolates sessions here; also search job-linked paths
        session_blob = collect_text_files(sessions, suffixes={".log", ".txt", ".jsonl"})
        plan_prompt = ""
        build_prompt = ""
        plan_sess = ""
        build_sess = ""
        for path in sorted(sessions.rglob("*")) if sessions.is_dir() else []:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            name = path.name.lower()
            if path.suffix == ".prompt.txt" or name.endswith(".prompt.txt"):
                if "derman-plan" in text.lower() or "write the plan" in text.lower():
                    plan_prompt += text + "\n"
                else:
                    build_prompt += text + "\n"
            else:
                if "derman-plan" in text.lower() or "planning" in name:
                    plan_sess += text + "\n"
                else:
                    build_sess += text + "\n"
        if not (plan_prompt or build_prompt or plan_sess or build_sess):
            # Fallback: treat everything as both (analyzer still has plan_text)
            plan_prompt = session_blob
            build_prompt = session_blob
            plan_sess = session_blob
            build_sess = session_blob

        workspace_body: Optional[str] = None
        git = proc._git_for(key)
        if git and git.get_working_directory():
            target = Path(git.get_working_directory()) / TARGET_REL
            if target.is_file():
                workspace_body = target.read_text(encoding="utf-8", errors="replace").strip()
        if workspace_body is None and git and git.temp_dir:
            target = Path(git.temp_dir) / TARGET_REL
            if target.is_file():
                workspace_body = target.read_text(encoding="utf-8", errors="replace").strip()

        report = analyze_plan_follow(
            plan_text=plan_text,
            plan_prompt=plan_prompt,
            build_prompt=build_prompt,
            plan_session=plan_sess,
            build_session=build_sess,
            workspace_file=workspace_body,
        )
        print("[live] plan-follow report:", flush=True)
        for k, v in report.items():
            if k == "required":
                continue
            print(f"  {k}={v}", flush=True)

        assert report["plan_nonempty"], "planner wrote an empty plan"
        assert report["plan_names_target_file"], (
            f"plan does not mention {TARGET_REL}:\n{plan_text[:800]}"
        )
        assert report["plan_names_marker"], (
            f"plan does not mention {TARGET_LINE}:\n{plan_text[:800]}"
        )
        assert report["build_prompt_points_at_plan"], (
            "build prompt did not include the plan path"
        )
        assert report["workspace_has_marker"], (
            f"builder did not write {TARGET_REL} with {TARGET_LINE!r} "
            f"(got {workspace_body!r}); status={st2.status.value} "
            f"err={st2.error_message}"
        )
        assert report["passed"], report

        if not report["build_session_mentions_plan"]:
            pytest.fail(
                "OpenCode build session never mentioned the plan file — "
                "the model likely did not read it.\n"
                f"session excerpt:\n{build_sess[:1500]}"
            )
    finally:
        _close_open_mrs_for_branch(work)
        _delete_remote_branch(work)
        _stop_serve(serve_proc)
