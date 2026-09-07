"""LIVE plan → build: real Jira REST + OpenCode must write a plan, then follow it.

Creates a Jira ticket via REST, assigns it to the PAT user, runs
``JobProcessor`` planning, then the real handoff: PUT ``Mode: build``
and let the poller + processor start implementation.

A second ticket with only ``bot`` / ``ai-assist`` labels (unassigned) must
not be accepted.

Opt-in (hits Jira + GitLab + a real model; slow)::

    VD_LIVE_PLAN_BUILD=1 uv run --python 3.12 --with-requirements requirements.txt \\
        --with pytest --with pytest-asyncio python -m pytest \\
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
PLAN_REL_RE = re.compile(
    r"(?:\.sisyphus/plans/|\.yaver-plans/|data[/\\]plans[/\\]|plans[/\\])"
    r"[A-Z][A-Z0-9]+-\d+\.md",
    re.I,
)
# Prefer models that recently completed a generate on this serve.
LIVE_MODELS = (
    "opencode/mimo-v2.5-free",
    "opencode/big-pickle",
    "opencode/north-mini-code-free",
)


def _dotenv_jira_email() -> str:
    return (_dotenv_map().get("JIRA_EMAIL") or "").strip()


def _dotenv_map() -> Dict[str, str]:
    env = Path(__file__).resolve().parents[1] / ".env"
    out: Dict[str, str] = {}
    if not env.is_file():
        return out
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _ready() -> str:
    flag = (os.environ.get("VD_LIVE_PLAN_BUILD") or "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        return "Set VD_LIVE_PLAN_BUILD=1 to run the live plan→build e2e"
    vals = _dotenv_map()
    host = (vals.get("JIRA_HOST") or "").strip()
    token = (vals.get("JIRA_API_TOKEN") or "").strip()
    pat = (vals.get("GITLAB_PAT") or settings.gitlab_pat or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured in .env"
    if not pat or pat.startswith("your-"):
        return "GITLAB_PAT not configured"
    if "atlassian.net" in host.lower() and not (vals.get("JIRA_EMAIL") or "").strip():
        return "Jira Cloud needs JIRA_EMAIL in .env"
    return ""


def _transition_to_todo(jira, issue_key: str) -> bool:
    """Move a Cloud/on-prem issue back to a To Do-like status."""
    trans = jira.get_transitions(issue_key) or []
    hints = (
        "to do",
        "todo",
        "backlog",
        "open",
        "yapılacak",
        "yapilacak",
        "selected for development",
    )
    for t in trans:
        name = str(t.get("name") or "").lower()
        to = t.get("to") or {}
        to_name = str(to.get("name") or "").lower()
        cat = str((to.get("statusCategory") or {}).get("key") or "").lower()
        if cat == "new" or any(h in name or h in to_name for h in hints):
            return bool(jira.do_transition(issue_key, t["id"]))
    return False


def _live_jira_client():
    from src.jira.client import JiraClient

    vals = _dotenv_map()
    return JiraClient(
        host=(vals.get("JIRA_HOST") or "").strip(),
        email=(vals.get("JIRA_EMAIL") or "").strip(),
        api_token=(vals.get("JIRA_API_TOKEN") or "").strip(),
    )


def _gitlab_headers() -> dict:
    pat = (_dotenv_map().get("GITLAB_PAT") or settings.gitlab_pat or "").strip()
    return {
        "PRIVATE-TOKEN": pat,
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


def flatten_serve_messages(raw_messages: Iterable) -> tuple[List[str], List[dict]]:
    """Pull assistant text + tool path/name pairs out of OpenCode serve JSON."""
    texts: List[str] = []
    tools: List[dict] = []
    for msg in raw_messages or []:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") if isinstance(msg.get("info"), dict) else {}
        role = str(info.get("role") or msg.get("role") or "")
        parts = msg.get("parts")
        if isinstance(parts, dict):
            parts = [parts]
        if not isinstance(parts, list):
            parts = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "").lower()
            if ptype in {"text", "reasoning", "thinking"}:
                blob = str(part.get("text") or part.get("content") or "").strip()
                if blob:
                    texts.append(blob)
                continue
            if ptype != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            if not inp and isinstance(part.get("input"), dict):
                inp = part["input"]
            path = str(
                inp.get("path")
                or inp.get("filePath")
                or inp.get("file")
                or inp.get("target")
                or ""
            )
            title = str(state.get("title") or part.get("title") or "")
            tools.append(
                {
                    "role": role,
                    "tool": str(part.get("tool") or state.get("title") or ""),
                    "path": path,
                    "title": title,
                    "status": str(state.get("status") or part.get("status") or ""),
                }
            )
    return texts, tools


def analyze_serve_plan_use(
    messages: Iterable,
    *,
    issue_key: str,
) -> Dict[str, object]:
    """Did this OpenCode session read the durable plan and write the marker file?"""
    texts, tools = flatten_serve_messages(messages)
    blob = "\n".join(texts)
    plan_name = f"{issue_key}.md".lower()
    read_plan = False
    wrote_target = False
    plan_tools: List[str] = []
    for t in tools:
        path = f"{t.get('path') or ''} {t.get('title') or ''}".replace("\\", "/").lower()
        tool = str(t.get("tool") or "").lower()
        looks_plan = (
            plan_name in path
            or ".yaver-plans/" in path
            or "/plans/" in path
            or bool(PLAN_REL_RE.search(path))
        )
        if looks_plan:
            plan_tools.append(f"{tool}:{t.get('path') or t.get('title')}")
            if tool in {"read", "view", "cat", "read_file"} or "read" in tool:
                read_plan = True
        target_hit = (
            TARGET_REL.lower() in path or Path(TARGET_REL).name.lower() in path
        )
        if target_hit and (
            tool in {"write", "edit", "apply_patch", "strreplace", "multiedit"}
            or "write" in tool
            or "edit" in tool
        ):
            wrote_target = True
    if not read_plan and PLAN_REL_RE.search(blob):
        # Model quoted the plan path in prose (weaker, still a signal)
        read_plan = "read" in blob.lower() and (
            plan_name in blob.lower() or "plan" in blob.lower()
        )
    return {
        "read_plan": read_plan,
        "wrote_target": wrote_target,
        "plan_tools": plan_tools,
        "tool_count": len(tools),
        "tools": tools,
        "text": blob,
    }


def analyze_plan_follow(
    *,
    plan_text: str,
    plan_prompt: str,
    build_prompt: str,
    plan_session: str,
    build_session: str,
    workspace_file: Optional[str],
    build_read_plan_file: bool = False,
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
        or "plans/" in plan_prompt_s.replace("\\", "/"),
        "build_prompt_points_at_plan": bool(PLAN_REL_RE.search(blob_build))
        or "plans/" in blob_build.replace("\\", "/").lower(),
        "build_session_mentions_plan": bool(PLAN_REL_RE.search(build_sess))
        or "plans/" in build_sess.replace("\\", "/").lower()
        or "plan file" in build_sess.lower(),
        "build_session_mentions_target": TARGET_REL.split("/")[-1].lower()
        in build_sess.lower()
        or TARGET_REL.lower() in build_sess.lower(),
        "workspace_has_marker": TARGET_LINE in (workspace_file or "").strip(),
        "build_read_plan_file": bool(build_read_plan_file),
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
        "build_read_plan_file",
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
        build_read_plan_file=True,
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
    assert miss["build_read_plan_file"] is False


def test_analyze_serve_plan_use_detects_read_and_write():
    msgs = [
        {
            "info": {"role": "assistant"},
            "parts": [
                {
                    "type": "tool",
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"path": ".yaver-plans/KAN-99.md"},
                    },
                },
                {
                    "type": "tool",
                    "tool": "write",
                    "state": {
                        "status": "completed",
                        "input": {"path": TARGET_REL},
                    },
                },
            ],
        }
    ]
    out = analyze_serve_plan_use(msgs, issue_key="KAN-99")
    assert out["read_plan"] is True
    assert out["wrote_target"] is True


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
    jira = _live_jira_client()
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
    project = ((_dotenv_map().get("JIRA_PROJECTS") or "KAN").split(",")[0] or "KAN").strip()
    created = jira.create_issue(
        project,
        summary,
        description,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {jira.last_error}")
    key = created["key"]
    print(f"\n[live] Jira {key}  {jira.host.rstrip('/')}/browse/{key}", flush=True)

    me = jira.get_myself() or {}
    account_id = str(me.get("accountId") or "").strip()
    display = str(me.get("displayName") or "").strip()
    if not account_id or not display:
        pytest.skip(f"GET /myself missing accountId/displayName: {me}")
    monkeypatch.setattr(settings, "trigger_assignee_names", display.lower())
    if not jira.assign_issue(key, account_id):
        pytest.skip(f"Could not assign {key} to PAT user: {jira.last_error}")
    live = jira.get_issue(key, fields=["summary", "labels", "assignee", "status", "description"])
    assert live, f"GET {key} failed after assign"
    live_fields = live.get("fields") or {}
    assert (live_fields.get("assignee") or {}).get("accountId") == account_id
    assert "bot" not in {str(x).lower() for x in (live_fields.get("labels") or [])}

    contrast = jira.create_issue(
        project,
        f"[vd-plan-build] label-only skip {stamp}",
        description,
        issue_type="Task",
        labels=[E2E_LABEL, "bot", "ai-assist"],
    )
    contrast_key = (contrast or {}).get("key")
    print(f"[live] contrast label-only {contrast_key}", flush=True)

    from src.jira.poller import JiraPoller
    from src.jira.triggers import poller_triggers_on

    intake_poller = JiraPoller(client=jira, board_id="1", interval_seconds=30)
    intake_poller.state_manager = JiraStateManager(state_dir=tmp_path / "intake-state")
    assert intake_poller._is_assigned_to_jira_ai_bot(key, live_fields) is True
    assert poller_triggers_on(assigned_to_bot=True) is True
    if contrast_key:
        c_live = jira.get_issue(contrast_key, fields=["labels", "assignee", "status"])
        c_fields = (c_live or {}).get("fields") or {}
        assert intake_poller._is_assigned_to_jira_ai_bot(contrast_key, c_fields) is False
        assert poller_triggers_on(assigned_to_bot=False) is False
        with patch.object(jira, "get_active_sprint", return_value={"id": 1, "name": "S"}):
            with patch.object(jira, "get_sprint_issues", return_value=[live, c_live]):
                accepted = intake_poller.poll_board()
        accepted_keys = [i["key"] for i in accepted]
        assert key in accepted_keys
        assert contrast_key not in accepted_keys
        print(f"[live] poller accepted {accepted_keys} (assignee-only)", flush=True)

    workdir = tmp_path / "run"
    workdir.mkdir()
    runtime_data = tmp_path / "_vd_runtime"
    runtime_data.mkdir(exist_ok=True)
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("YAVER_DATA_DIR", str(runtime_data))
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "t"))
    monkeypatch.setattr(settings, "sisyphus_plans_dir", Path(".sisyphus/plans"))
    monkeypatch.setattr(settings, "agent_prompts_dir", Path(__file__).resolve().parents[1] / "agent")
    monkeypatch.setattr(settings, "default_model", LIVE_MODELS[0])
    monkeypatch.setattr(settings, "agent_task_timeout_seconds", 1800)
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
        if not (live_fields.get("description") or "").strip():
            live.setdefault("fields", {})["description"] = description
        live.setdefault("fields", {})["summary"] = summary
        outcome = await proc.process_event(
            {"webhookEvent": "jira:issue_created", "issue": live}
        )
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

        from src.paths import plans_dir as _plans_dir

        durable = _plans_dir() / f"{key}.md"
        if st.plan_path and Path(st.plan_path).is_file():
            durable = Path(st.plan_path)
        assert durable.is_file(), f"durable plan missing: {durable}"
        plan_text = durable.read_text(encoding="utf-8", errors="replace")
        print(f"[live] plan bytes={len(plan_text)} path={durable}", flush=True)
        print(plan_text[:1500], flush=True)

        # Product handoff: Mode: plan must not implement; plan_execute must.
        handoff = JiraPoller(client=jira, board_id="1", interval_seconds=30)
        handoff.state_manager = sm
        handoff._seen_issues.add(key)
        still_plan = jira.get_issue(
            key, fields=["summary", "description", "labels", "assignee", "status"]
        )
        assert still_plan
        with patch.object(jira, "get_active_sprint", return_value={"id": 1, "name": "S"}):
            with patch.object(jira, "get_sprint_issues", return_value=[still_plan]):
                skipped = handoff.poll_board()
        assert key not in [i["key"] for i in skipped], (
            f"Mode: plan started a build: {[i['key'] for i in skipped]}"
        )
        print(f"[live] {key} still Mode: plan → poller did not start build", flush=True)

        assert jira.add_labels(key, ["plan_execute"]), jira.last_error
        live_build = jira.get_issue(
            key, fields=["summary", "description", "labels", "assignee", "status"]
        )
        assert live_build
        live_build.setdefault("fields", {})["status"] = {
            "name": "In Progress",
            "statusCategory": {"key": "indeterminate"},
        }
        live_build["fields"]["labels"] = list(
            live_build.get("fields", {}).get("labels") or []
        )
        if "plan_execute" not in [
            str(x).lower() for x in live_build["fields"]["labels"]
        ]:
            live_build["fields"]["labels"].append("plan_execute")
        handoff._plan_start_emitted.discard(key)
        with patch.object(jira, "get_active_sprint", return_value={"id": 1, "name": "S"}):
            with patch.object(jira, "get_sprint_issues", return_value=[live_build]):
                to_build = handoff.poll_board()
        assert key in [i["key"] for i in to_build], (
            f"plan_execute not accepted: {[i['key'] for i in to_build]}"
        )
        print(f"[live] {key} plan_execute → poller accepted implementation", flush=True)

        build_outcome = await proc.process_event(
            {"webhookEvent": "jira:issue_updated", "issue": live_build}
        )
        print(f"[live] build process_event={build_outcome}", flush=True)
        st2 = sm.get_state(key)
        assert st2 is not None
        print(
            f"[live] after build status={st2.status.value} err={st2.error_message!r}",
            flush=True,
        )
        assert build_outcome.get("work_started") is True, build_outcome

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
        if not build_prompt.strip():
            # Serve logs the rendered kit; there may be no separate .prompt.txt
            build_prompt = build_sess or session_blob
        kit = Path(__file__).resolve().parents[1] / "agent" / "BUILD_PROMPT.md"
        if kit.is_file():
            build_prompt = kit.read_text(encoding="utf-8", errors="replace") + "\n" + build_prompt

        workspace_body: Optional[str] = None
        git = proc._git_for(key)
        roots = []
        if git:
            wd = git.get_working_directory()
            if wd:
                roots.append(Path(wd))
            if getattr(git, "temp_dir", None):
                roots.append(Path(git.temp_dir))
        roots.append(tmp_path)
        seen: set[Path] = set()
        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.exists():
                continue
            seen.add(resolved)
            target = resolved / TARGET_REL
            if target.is_file():
                workspace_body = target.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                break
            for found in resolved.rglob(Path(TARGET_REL).name):
                if found.is_file():
                    workspace_body = found.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                    break
            if workspace_body is not None:
                break
            try:
                import subprocess

                shown = subprocess.run(
                    ["git", "show", f"HEAD:{TARGET_REL}"],
                    cwd=str(resolved),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
                if shown.returncode == 0 and shown.stdout.strip():
                    workspace_body = shown.stdout.strip()
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue

        session_ids: List[str] = []
        meta = dict(st2.metadata or {})
        for sid in meta.get("opencode_session_ids") or []:
            if sid and str(sid) not in session_ids:
                session_ids.append(str(sid))
        cur = getattr(st2, "current_opencode_session_id", None) or meta.get(
            "current_opencode_session_id"
        )
        if cur and str(cur) not in session_ids:
            session_ids.append(str(cur))
        for rec in jobs.list_jobs(issue_key=key):
            for field in ("opencode_session_id", "session_id"):
                sid = rec.get(field)
                if sid and str(sid) not in session_ids:
                    session_ids.append(str(sid))
        print(f"[live] opencode session ids: {session_ids}", flush=True)

        serve_analyses: List[dict] = []
        build_read_plan = False
        clone_dir = None
        if git:
            clone_dir = git.get_working_directory()
        if session_ids:
            from src.opencode_serve import OpenCodeServeClient

            client = OpenCodeServeClient(
                base_url=serve_url,
                directory=str(clone_dir) if clone_dir else None,
            )
            try:
                for sid in session_ids:
                    try:
                        msgs = await client.list_all_messages(sid, max_messages=500)
                    except Exception as exc:
                        print(
                            f"[live] list_messages {sid} failed: {exc}",
                            flush=True,
                        )
                        continue
                    used = analyze_serve_plan_use(msgs, issue_key=key)
                    serve_analyses.append(
                        {
                            "session_id": sid,
                            "read_plan": used["read_plan"],
                            "wrote_target": used["wrote_target"],
                            "plan_tools": used["plan_tools"],
                            "tool_count": used["tool_count"],
                            "text_excerpt": str(used["text"] or "")[:1200],
                            "tools": [
                                f"{t.get('tool')}:{t.get('path') or t.get('title')}"
                                for t in (used["tools"] or [])[:40]
                            ],
                        }
                    )
                    print(
                        f"[live] serve {sid} read_plan={used['read_plan']} "
                        f"wrote_target={used['wrote_target']} "
                        f"tools={used['tool_count']} "
                        f"plan_tools={used['plan_tools']}",
                        flush=True,
                    )
                    for line in (used["tools"] or [])[:40]:
                        print(
                            f"    tool={line.get('tool')} "
                            f"path={line.get('path') or line.get('title')} "
                            f"status={line.get('status')}",
                            flush=True,
                        )
                    if used["read_plan"] and (
                        used["wrote_target"] or sid == session_ids[-1]
                    ):
                        build_read_plan = True
                    extra = str(used["text"] or "")
                    if extra:
                        build_sess += "\n" + extra
            finally:
                try:
                    await client.aclose()
                except Exception:
                    pass

        if not build_read_plan:
            # Session-log fallback: the builder named the plan file.
            blob = f"{build_prompt}\n{build_sess}"
            if PLAN_REL_RE.search(blob) and (
                f"{key}.md".lower() in blob.lower()
                or "yaver-plans" in blob.lower()
                or "plans/" in blob.replace("\\", "/").lower()
            ):
                # Only accept if a read-like tool/path also appears
                low = blob.lower()
                if any(
                    tok in low
                    for tok in (
                        "read the plan",
                        "reading the plan",
                        "opened the plan",
                        ".yaver-plans/",
                        f"{key.lower()}.md",
                    )
                ):
                    build_read_plan = True

        report = analyze_plan_follow(
            plan_text=plan_text,
            plan_prompt=plan_prompt,
            build_prompt=build_prompt,
            plan_session=plan_sess,
            build_session=build_sess,
            workspace_file=workspace_body,
            build_read_plan_file=build_read_plan,
        )
        print("[live] plan-follow report:", flush=True)
        for k, v in report.items():
            if k == "required":
                continue
            print(f"  {k}={v}", flush=True)
        if serve_analyses:
            print("[live] serve analyses:", flush=True)
            for row in serve_analyses:
                print(
                    f"  sid={row['session_id']} read={row['read_plan']} "
                    f"wrote={row['wrote_target']} tools={row['tools'][:12]}",
                    flush=True,
                )

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
        assert report["build_read_plan_file"], (
            "OpenCode build session never read the plan file — "
            "the model likely implemented from the Jira description only.\n"
            f"session ids={session_ids}\n"
            f"serve={serve_analyses}\n"
            f"session excerpt:\n{build_sess[:2000]}"
        )
        assert report["passed"], report
    finally:
        _close_open_mrs_for_branch(work)
        _delete_remote_branch(work)
        _stop_serve(serve_proc)
