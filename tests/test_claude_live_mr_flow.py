"""Live Claude Code job: free model, transcript parse, Yaver opens the MR.

Skipped unless ``YAVER_CLAUDE_LIVE_MR=1``. Claude commits on a feature
branch. It does not push. ``GitManager`` pushes and opens the GitLab MR.
The free model is Pollinations ``openai`` behind the local Anthropic proxy
on ``127.0.0.1:4010``. Claude's own login is not used.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.backends.claude import (
    claude_session_log_text,
    is_claude_session_id,
    parse_claude_output,
)
from src.config import settings

REPO = "https://gitlab.com/beratersari0/test_project.git"
PROXY = "http://127.0.0.1:4010"
CLAUDE = (
    Path(os.environ.get("TEMP", "/tmp"))
    / "yaver-claude-cli"
    / "node_modules"
    / ".bin"
    / "claude.cmd"
)


def _prompt(branch: str) -> str:
    return f"""
Branch: {branch}. This checkout does not contain yaver_live_marker yet.
You must add it. A reply that says no changes are required is wrong.

Use the Write tool to create include/yaver_live_marker.hpp with this exact body:

#ifndef YAVER_LIVE_MARKER_HPP
#define YAVER_LIVE_MARKER_HPP
// Live Claude Code job marker. Returns 58 so the unattended run can
// prove it edited the tree and committed. Do not remove this helper.
inline int yaver_live_marker() {{ return 58; }}
#endif

Then use the Bash tool to run:
git add include/yaver_live_marker.hpp
git commit -m "feat(live): add yaver live marker"

Do not git push. Do not open a merge request. Do not ask questions.
Stop after that commit exists.
""".strip()


@pytest.mark.asyncio
async def test_claude_free_model_commits_and_yaver_opens_mr(tmp_path, monkeypatch):
    if os.environ.get("YAVER_CLAUDE_LIVE_MR") != "1":
        pytest.skip("set YAVER_CLAUDE_LIVE_MR=1 to run the live Claude MR flow")
    if not CLAUDE.is_file():
        pytest.skip(f"Claude Code CLI is not installed at {CLAUDE}")

    import httpx

    try:
        health = httpx.get(PROXY, timeout=3, verify=False)
    except Exception as exc:
        pytest.skip(f"free-model proxy is not listening on {PROXY}: {exc}")
    if health.status_code >= 500:
        pytest.skip(f"free-model proxy unhealthy: {health.status_code}")

    import importlib.util

    installer = Path(__file__).resolve().parents[1] / "packaging" / "install_claude_agents.py"
    spec = importlib.util.spec_from_file_location("install_claude_agents_mr", installer)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "claude-home"
    mod.install_claude_agents(
        source_root=Path(__file__).resolve().parents[1],
        home=home,
    )

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", PROXY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "local-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-free")
    monkeypatch.setattr(settings, "anthropic_base_url", PROXY)
    monkeypatch.setattr(settings, "anthropic_auth_token", "local-free")
    monkeypatch.setattr(settings, "default_model", "openai")
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
    monkeypatch.setattr(settings, "git_update_submodules", False)
    monkeypatch.setattr(
        "src.backends.claude.resolve_claude_cli", lambda *_a, **_k: str(CLAUDE)
    )

    from src.git_manager import GitManager
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    issue = "CL-LIVE"
    git = GitManager(
        issue,
        remote_url=REPO,
        source_branch="develop",
        target_branch="develop",
    )
    branch = git.ensure_feature_branch(issue)
    assert branch and branch.startswith("feature/")
    work = git.get_working_directory()
    assert work is not None

    runner = AgentRunner(working_directory=work)
    task = AgentTask(
        description="Add yaver_live_marker and commit",
        prompt=_prompt(branch),
        agent="derman-build",
        issue_key=issue,
        model="openai",
        backend="claude",
    )
    result = await runner.run_agent(task, timeout_seconds=1800)
    log_path = Path(str(result.get("session_file") or ""))
    raw = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    if not raw.strip():
        raw = str(result.get("stdout") or "")
    parsed = parse_claude_output(raw)
    shown = claude_session_log_text(
        raw, str(result.get("stderr") or ""), returncode=int(result.get("returncode") or 0)
    )

    assert result.get("returncode") == 0, (result.get("stderr") or "")[-1500:]
    assert is_claude_session_id(str(result.get("opencode_session_id") or parsed.get("session_id") or ""))
    assert parsed.get("session_id")
    assert shown.strip()
    assert not shown.strip().startswith('{"type"')
    assert "tool_use_id" not in shown
    assert "yaver_live_marker" in shown.lower() or "marker" in shown.lower() or "58" in shown

    subject = git.get_last_commit_subject() or ""
    assert "yaver live marker" in subject.lower()
    assert git.commits_ahead_of_target(branch) >= 1
    assert git.push(branch) is True, git.last_push_error
    mr = git.create_merge_request(
        f"feat({issue}): add yaver live marker",
        "Live Claude Code job. The agent committed. Yaver opened this merge request.",
        target_branch="develop",
    )
    assert mr, git.last_mr_error
    assert "merge_request" in mr or "/-/merge_requests/" in mr
    print(f"MR {mr}")
