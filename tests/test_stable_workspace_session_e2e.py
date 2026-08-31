"""E2E: stable temp clones + OpenCode session continue.

Product change: jobs with the same Repository + work branch reuse one folder
so the OpenCode serve session can continue. Dashboard Reset drops the
bind and the next job starts cold. Missing folders are recreated at the same
stable path (then clone).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.git_manager import GitManager, purge_stale_temp_dirs
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_opencode_sessions import _make_session_db

REPO = "https://gitlab.example.com/acme/app.git"
OTHER_REPO = "https://gitlab.example.com/acme/other.git"


def _params(repo: str, source: str, *, target: str = "develop", mode: str = "build") -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        f"Mode: {mode}\n"
        "{params}\n"
    )


def _seed_git_origin(path: Path, origin: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "remote", "add", "origin", origin],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _insert_session(db: Path, *, sid: str, directory: str, title: str, updated: int = 1000) -> None:
    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT OR REPLACE INTO session (
            id, title, directory, agent, time_created, time_updated,
            cost, tokens_input, tokens_output
        ) VALUES (?, ?, ?, 'atlas', 1, ?, 0, 0, 0)
        """,
        (sid, title, directory, updated),
    )
    con.commit()
    con.close()


@contextmanager
def _git_io_patches(*, track: Optional[Dict[str, int]] = None):
    """Let directory logic run; stub network git + work-branch checkout."""
    counters = track if track is not None else {"clone": 0, "refresh": 0}

    def clone(self):
        counters["clone"] += 1
        if self.temp_dir:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            (self.temp_dir / ".cloned").write_text("1", encoding="utf-8")

    def refresh(self):
        counters["refresh"] += 1
        if self.temp_dir:
            (self.temp_dir / ".refreshed").write_text("1", encoding="utf-8")

    def ensure(self, issue_key=None):
        name = self._resolve_work_branch_name(issue_key)
        self.work_branch = name
        return name

    with (
        patch.object(GitManager, "_clone_into_temp", clone),
        patch.object(GitManager, "_refresh_existing_clone", refresh),
        patch.object(GitManager, "ensure_feature_branch", ensure),
        patch.object(GitManager, "_assert_remote_host_allowed"),
        patch("src.git_manager.set_current_temp_dir"),
    ):
        yield counters


def _live_settings(**over):
    live = MagicMock()
    live.agent_task_timeout_seconds = 30
    live.agent_task_max_retries = 0
    live.default_agent = "atlas"
    for k, v in over.items():
        setattr(live, k, v)
    return live


def _serve_settings(s):
    s.opencode_cli = "opencode"
    s.opencode_serve_url = "http://127.0.0.1:4096"
    s.default_model = "opencode/x"
    s.agent_task_timeout_seconds = 30
    s.agent_task_max_retries = 0
    s.agent_task_retry_delay_seconds = 0
    s.agent_task_retry_backoff_multiplier = 1.0
    s.agent_task_retry_on_timeout = True
    s.agent_task_retry_on_error = True


class _Harness:
    def __init__(self, tmp_path: Path, monkeypatch, fake_jira, reporter, isolate):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("src.config.settings.temp_dir_base", Path(".temp"))
        agent_src = Path(__file__).resolve().parents[1] / "agent"
        dest = tmp_path / "agent"
        dest.mkdir(exist_ok=True)
        for name in ("BUILD_PROMPT.md", "PLAN_PROMPT.md"):
            src = agent_src / name
            if src.is_file():
                shutil.copy(src, dest / name)
        self.tmp = tmp_path
        self.binds = isolate["session_bind_store"]
        self.jobs = isolate["job_store"]
        self.sm = JiraStateManager(state_dir=tmp_path / "state")
        with patch("src.processor.create_jira_client", return_value=fake_jira):
            self.proc = JobProcessor()
        self.proc.state_manager = self.sm
        self.proc.reporter = reporter
        self.proc.jira_client = fake_jira
        self.proc._mark_jira_in_progress = MagicMock(return_value=True)
        self.proc._push_and_create_mr = AsyncMock(return_value=True)
        self.proc._assert_build_delivery = MagicMock(return_value=None)
        self.proc._snapshot_delivery_baseline = MagicMock()
        self.seen: List[Dict[str, Any]] = []
        self.session_db = _make_session_db(tmp_path / "opencode.db", [])
        monkeypatch.setattr(
            "src.opencode_sessions._default_db_path", lambda: self.session_db
        )
        monkeypatch.setattr("src.config.get_settings", lambda: _live_settings())
        self.monkeypatch = monkeypatch

    def record_session(self, sid: str, directory: Path, title: str, updated: int = 1000) -> None:
        _insert_session(
            self.session_db,
            sid=sid,
            directory=str(directory.resolve()),
            title=title,
            updated=updated,
        )

    async def run_build(
        self,
        key: str,
        *,
        repo: str = REPO,
        source: str = "feature/shared",
        target: str = "develop",
        git_track: Optional[Dict[str, int]] = None,
        agent_sid: Optional[str] = None,
    ) -> Path:
        desc = _params(repo, source, target=target, mode="build")
        if self.sm.get_state(key) is None:
            self.sm.create_state(key, f"work {key}", desc)
        else:
            self.sm.update_state(key, issue_summary=f"work {key}", description=desc)

        captured: Dict[str, Path] = {}

        async def fake_run(self_runner, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
            sid = task.session_id or agent_sid or f"ses_{key.lower().replace('-', '_')}"
            wd = getattr(self_runner, "working_directory", None)
            self.seen.append(
                {
                    "issue_key": task.issue_key,
                    "session_id": task.session_id,
                    "resolved_sid": sid,
                    "attempt": kwargs.get("attempt_number"),
                    "dir": str(wd) if wd else "",
                }
            )
            log = self.tmp / f"{task.issue_key}.log"
            log.write_text(f"Session: {sid}\ndone\n", encoding="utf-8")
            return {
                "task_id": task.task_id,
                "returncode": 0,
                "stdout": f"Session: {sid}\ndone\n",
                "stderr": "",
                "session_file": str(log),
                "opencode_session_id": sid,
                "progress": 100,
            }

        real_prepare = self.proc._prepare_git_workspace

        async def wrap_prepare(state):
            git = await real_prepare(state)
            if git is not None and git.temp_dir is not None:
                captured["dir"] = Path(git.temp_dir)
            return git

        with _git_io_patches(track=git_track), patch.object(
            AgentRunner, "run_agent", fake_run
        ), patch.object(self.proc, "_prepare_git_workspace", wrap_prepare):
            with patch("src.orchestrator.agent_runner.settings") as s:
                _serve_settings(s)
                await self.proc._start_execution_workflow(self.sm.get_state(key))
        assert "dir" in captured
        return captured["dir"]

    async def run_plan(
        self,
        key: str,
        *,
        repo: str = REPO,
        source: str = "develop",
        git_track: Optional[Dict[str, int]] = None,
    ) -> Path:
        desc = _params(repo, source, mode="plan")
        if self.sm.get_state(key) is None:
            self.sm.create_state(key, f"plan {key}", desc)
        captured: Dict[str, Path] = {}

        async def fake_run(self_runner, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
            sid = task.session_id or f"ses_plan_{key.lower().replace('-', '_')}"
            self.seen.append(
                {
                    "issue_key": task.issue_key,
                    "session_id": task.session_id,
                    "resolved_sid": sid,
                }
            )
            wd = getattr(self_runner, "working_directory", None)
            if wd:
                from src.config import settings as _settings

                plans = Path(wd) / _settings.sisyphus_plans_dir
                plans.mkdir(parents=True, exist_ok=True)
                (plans / f"{task.issue_key}.md").write_text(
                    f"# Plan for {task.issue_key}\n\n1. implement the change\n",
                    encoding="utf-8",
                )
            return {
                "task_id": task.task_id,
                "returncode": 0,
                "stdout": f"Session: {sid}\n# Plan\n1. do it\n",
                "stderr": "",
                "session_file": str(self.tmp / f"{key}.plan.log"),
                "opencode_session_id": sid,
                "progress": 100,
            }

        real_prepare = self.proc._prepare_git_workspace

        async def wrap_prepare(state):
            git = await real_prepare(state)
            if git is not None and git.temp_dir is not None:
                captured["dir"] = Path(git.temp_dir)
            return git

        with _git_io_patches(track=git_track), patch.object(
            AgentRunner, "run_agent", fake_run
        ), patch.object(self.proc, "_prepare_git_workspace", wrap_prepare):
            with patch("src.orchestrator.agent_runner.settings") as s:
                _serve_settings(s)
                await self.proc._start_planning_workflow(self.sm.get_state(key))
        assert "dir" in captured
        return captured["dir"]


@pytest.fixture
def harness(tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts):
    return _Harness(
        tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
    )


# ---------------------------------------------------------------------------
# Folder identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_shared_source_two_issues_same_folder_and_session(harness):
    track = {"clone": 0, "refresh": 0}
    d1 = await harness.run_build("E2E-A", git_track=track, agent_sid="ses_shared_1")
    harness.record_session("ses_shared_1", d1, "E2E-A: first")
    assert track["clone"] == 1
    d2 = await harness.run_build("E2E-B", git_track=track, agent_sid="ses_should_not_win")
    assert d1.resolve() == d2.resolve()
    assert harness.seen[0]["session_id"] is None
    assert harness.seen[1]["session_id"] == "ses_shared_1"
    bound = harness.binds.get(REPO, "feature/shared", "develop")
    assert bound["session_id"] == "ses_shared_1"
    assert bound["issue_key"] == "E2E-B"
    assert Path(bound["working_directory"]).resolve() == d1.resolve()


@pytest.mark.asyncio
async def test_e2e_primary_source_two_issues_isolated_no_session_share(harness):
    d1 = await harness.run_build(
        "ISO-1", source="develop", agent_sid="ses_iso_1"
    )
    harness.record_session("ses_iso_1", d1, "ISO-1: first")
    d2 = await harness.run_build(
        "ISO-2", source="develop", agent_sid="ses_iso_2"
    )
    assert d1.resolve() != d2.resolve()
    assert harness.seen[0]["session_id"] is None
    assert harness.seen[1]["session_id"] is None
    assert harness.binds.get(REPO, "feature/ISO-1", "develop")["session_id"] == "ses_iso_1"
    assert harness.binds.get(REPO, "feature/ISO-2", "develop")["session_id"] == "ses_iso_2"


@pytest.mark.asyncio
async def test_e2e_same_issue_rerun_same_folder_resumes(harness):
    d1 = await harness.run_build("RERUN-1", source="develop", agent_sid="ses_rerun")
    harness.record_session("ses_rerun", d1, "RERUN-1: first")
    st = harness.sm.get_state("RERUN-1")
    assert st is not None and st.status == TaskStatus.COMPLETED
    harness.proc._reset_for_reprocess("RERUN-1")
    d2 = await harness.run_build("RERUN-1", source="develop")
    assert d1.resolve() == d2.resolve()
    assert harness.seen[1]["session_id"] == "ses_rerun"


@pytest.mark.asyncio
async def test_e2e_missing_directory_recreated_same_path_and_resumes(harness):
    track = {"clone": 0, "refresh": 0}
    d1 = await harness.run_build("MISS-1", git_track=track, agent_sid="ses_miss")
    harness.record_session("ses_miss", d1, "MISS-1: first")
    assert d1.exists()
    shutil.rmtree(d1)
    assert not d1.exists()
    d2 = await harness.run_build("MISS-2", git_track=track)
    assert d2.resolve() == d1.resolve()
    assert d2.exists()
    assert track["clone"] == 2
    assert harness.seen[1]["session_id"] == "ses_miss"


@pytest.mark.asyncio
async def test_e2e_missing_directory_and_missing_opencode_row_still_resumes(harness):
    """Bind key exists → continue even when the clone and SQLite row are gone."""
    d1 = await harness.run_build("COLD-1", agent_sid="ses_gone")
    shutil.rmtree(d1)
    d2 = await harness.run_build("COLD-2", agent_sid="ses_fresh")
    assert d2.resolve() == d1.resolve()
    assert harness.seen[1]["session_id"] == "ses_gone"
    assert harness.binds.get(REPO, "feature/shared", "develop")["session_id"] == "ses_gone"


@pytest.mark.asyncio
async def test_e2e_existing_matching_origin_refreshes_not_clones(harness):
    track = {"clone": 0, "refresh": 0}
    d1 = await harness.run_build("RF-1", git_track=track, agent_sid="ses_rf")
    harness.record_session("ses_rf", d1, "RF-1: x")
    shutil.rmtree(d1)
    _seed_git_origin(d1, REPO)
    d2 = await harness.run_build("RF-2", git_track=track)
    assert d2.resolve() == d1.resolve()
    assert track["clone"] == 1
    assert track["refresh"] == 1
    assert (d2 / ".refreshed").is_file()
    assert harness.seen[1]["session_id"] == "ses_rf"


@pytest.mark.asyncio
async def test_e2e_wrong_origin_wipes_and_reclones_same_path(harness):
    track = {"clone": 0, "refresh": 0}
    d1 = await harness.run_build("WO-1", git_track=track, agent_sid="ses_wo")
    harness.record_session("ses_wo", d1, "WO-1: x")
    shutil.rmtree(d1)
    _seed_git_origin(d1, "https://evil.example.com/not/ours.git")
    d2 = await harness.run_build("WO-2", git_track=track)
    assert d2.resolve() == d1.resolve()
    assert track["clone"] == 2
    assert track["refresh"] == 0
    assert (d2 / ".cloned").is_file()
    assert not (d2 / ".git").exists()  # fake clone does not recreate .git


@pytest.mark.asyncio
async def test_e2e_empty_dir_without_git_clones(harness):
    track = {"clone": 0, "refresh": 0}
    d1 = await harness.run_build("EMP-1", git_track=track, agent_sid="ses_emp")
    harness.record_session("ses_emp", d1, "EMP-1: x")
    shutil.rmtree(d1)
    d1.mkdir(parents=True)
    d2 = await harness.run_build("EMP-2", git_track=track)
    assert d2.resolve() == d1.resolve()
    assert track["clone"] == 2
    assert track["refresh"] == 0
    assert harness.seen[1]["session_id"] == "ses_emp"


@pytest.mark.asyncio
async def test_e2e_junk_dir_without_git_wiped_and_cloned(harness):
    track = {"clone": 0, "refresh": 0}
    d1 = await harness.run_build("JNK-1", git_track=track, agent_sid="ses_jnk")
    harness.record_session("ses_jnk", d1, "JNK-1: x")
    shutil.rmtree(d1)
    d1.mkdir(parents=True)
    (d1 / "noise.txt").write_text("not a git repo", encoding="utf-8")
    d2 = await harness.run_build("JNK-2", git_track=track)
    assert d2.resolve() == d1.resolve()
    assert track["clone"] == 2
    assert not (d2 / "noise.txt").exists()
    assert harness.seen[1]["session_id"] == "ses_jnk"


# ---------------------------------------------------------------------------
# Dashboard + cleanup / purge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_dashboard_lists_working_directory_then_reset_cold(harness):
    d1 = await harness.run_build("DASH-A", agent_sid="ses_dash")
    harness.record_session("ses_dash", d1, "DASH-A: x")
    app = create_dashboard_app(processor=harness.proc, state_manager=harness.sm)
    client = TestClient(app)
    listing = client.get("/api/opencode-sessions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    rec = body["sessions"][0]
    assert rec["session_id"] == "ses_dash"
    assert rec["branch"] == "feature/shared"
    assert Path(rec["working_directory"]).resolve() == d1.resolve()
    reset = client.delete(f"/api/opencode-sessions/{rec['bind_id']}")
    assert reset.status_code == 200
    await harness.run_build("DASH-B", agent_sid="ses_after_reset")
    assert harness.seen[-1]["session_id"] is None
    rebound = harness.binds.get(REPO, "feature/shared", "develop")
    assert rebound["session_id"] == "ses_after_reset"


@pytest.mark.asyncio
async def test_e2e_cleanup_keeps_clone_after_session_reset(
    harness, monkeypatch
):
    d1 = await harness.run_build("CLN-A", agent_sid="ses_cln")
    assert d1.exists()
    gm = GitManager.__new__(GitManager)
    gm.issue_key = "CLN-A"
    gm.temp_dir = d1
    assert gm.cleanup(success=True) is True
    assert d1.exists()
    app = create_dashboard_app(processor=harness.proc, state_manager=harness.sm)
    client = TestClient(app)
    bind_id = client.get("/api/opencode-sessions").json()["sessions"][0]["bind_id"]
    assert client.delete(f"/api/opencode-sessions/{bind_id}").status_code == 200
    gm.temp_dir = d1
    assert gm.cleanup(success=True) is True
    assert d1.exists()


@pytest.mark.asyncio
async def test_e2e_purge_protects_bound_dir_then_deletes_after_reset(harness):
    d1 = await harness.run_build("PRG-A", agent_sid="ses_prg")
    old = time.time() - 3 * 86400
    os.utime(d1, (old, old))
    removed = purge_stale_temp_dirs(max_age_days=1.0, base_dir=d1.parent)
    assert d1.exists()
    assert removed == 0
    harness.binds.delete_for(REPO, "feature/shared", "develop")
    os.utime(d1, (old, old))
    removed = purge_stale_temp_dirs(max_age_days=1.0, base_dir=d1.parent)
    assert removed >= 1
    assert not d1.exists()


# ---------------------------------------------------------------------------
# Identity edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_different_repos_same_branch_name_different_folders(harness):
    d1 = await harness.run_build("DR-1", repo=REPO, agent_sid="ses_r1")
    harness.record_session("ses_r1", d1, "DR-1: x")
    d2 = await harness.run_build("DR-2", repo=OTHER_REPO, agent_sid="ses_r2")
    assert d1.resolve() != d2.resolve()
    assert harness.seen[1]["session_id"] is None
    assert harness.binds.get(REPO, "feature/shared", "develop")["session_id"] == "ses_r1"
    assert harness.binds.get(OTHER_REPO, "feature/shared", "develop")["session_id"] == "ses_r2"


@pytest.mark.asyncio
async def test_e2e_https_and_dot_git_url_share_folder(harness):
    d1 = await harness.run_build(
        "URL-1", repo="https://gitlab.example.com/acme/app.git", agent_sid="ses_url"
    )
    harness.record_session("ses_url", d1, "URL-1: x")
    d2 = await harness.run_build(
        "URL-2", repo="https://gitlab.example.com/acme/app", agent_sid="ses_url2"
    )
    assert d1.resolve() == d2.resolve()
    assert harness.seen[1]["session_id"] == "ses_url"


@pytest.mark.asyncio
async def test_e2e_attach_relocates_when_opencode_dir_is_other_clone(harness):
    """Stale session.directory is rewritten onto the live clone; bind is kept."""
    d1 = await harness.run_build("OTH-A", agent_sid="ses_oth")
    other = harness.tmp / "foreign_clone"
    other.mkdir()
    harness.record_session("ses_oth", other, "OTH-A: x")
    d2 = await harness.run_build("OTH-B", agent_sid="ses_new_oth")
    assert d2.resolve() == d1.resolve()
    assert harness.seen[1]["session_id"] == "ses_oth"
    assert harness.binds.get(REPO, "feature/shared", "develop")["session_id"] == "ses_oth"
    from src.opencode_sessions import get_session_directory

    assert get_session_directory("ses_oth") == str(d2.resolve())


@pytest.mark.asyncio
async def test_e2e_first_job_without_session_id_does_not_bind(harness):
    async def fake_run(self_runner, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        harness.seen.append({"issue_key": task.issue_key, "session_id": task.session_id})
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "done without session token\n",
            "stderr": "",
            "session_file": str(harness.tmp / "nosid.log"),
            "opencode_session_id": None,
            "progress": 100,
        }

    key = "NOSID-1"
    harness.sm.create_state(key, "s", _params(REPO, "feature/shared"))
    with _git_io_patches(), patch.object(AgentRunner, "run_agent", fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            _serve_settings(s)
            await harness.proc._start_execution_workflow(harness.sm.get_state(key))
    assert harness.binds.get(REPO, "feature/shared", "develop") is None


@pytest.mark.asyncio
async def test_e2e_plan_then_build_same_issue_primary_source_same_folder(harness):
    track = {"clone": 0, "refresh": 0}
    d_plan = await harness.run_plan("PB-1", source="develop", git_track=track)
    st = harness.sm.get_state("PB-1")
    assert st is not None and st.status == TaskStatus.PLAN_READY
    sid = harness.seen[0]["resolved_sid"]
    harness.record_session(sid, d_plan, "PB-1: plan")
    harness.proc._reset_for_reprocess("PB-1")
    harness.sm.update_state(
        "PB-1",
        description=_params(REPO, "develop", mode="build"),
        metadata={"workflow_type": "execution"},
    )
    d_build = await harness.run_build(
        "PB-1", source="develop", git_track=track, agent_sid="ses_should_resume_plan"
    )
    assert d_plan.resolve() == d_build.resolve()
    assert harness.seen[-1]["session_id"] == sid


@pytest.mark.asyncio
async def test_e2e_shared_source_second_job_keeps_working_directory(harness):
    d1 = await harness.run_build("WD-A", agent_sid="ses_wd")
    harness.record_session("ses_wd", d1, "WD-A: x")
    first_wd = Path(harness.binds.get(REPO, "feature/shared", "develop")["working_directory"])
    d2 = await harness.run_build("WD-B")
    second = harness.binds.get(REPO, "feature/shared", "develop")
    assert Path(second["working_directory"]).resolve() == first_wd.resolve()
    assert Path(second["working_directory"]).resolve() == d2.resolve()
    assert second["issue_key"] == "WD-B"


@pytest.mark.asyncio
async def test_e2e_same_source_different_target_new_folder_and_cold_session(harness):
    """Different Target = different MR base → new clone + new OpenCode session."""
    d1 = await harness.run_build(
        "TGT-A", source="feature/shared", target="develop", agent_sid="ses_tgt_dev"
    )
    harness.record_session("ses_tgt_dev", d1, "TGT-A: x")
    d2 = await harness.run_build(
        "TGT-B", source="feature/shared", target="main", agent_sid="ses_tgt_main"
    )
    assert d1.resolve() != d2.resolve()
    assert harness.seen[1]["session_id"] is None
    assert (
        harness.binds.get(REPO, "feature/shared", "develop")["session_id"]
        == "ses_tgt_dev"
    )
    assert (
        harness.binds.get(REPO, "feature/shared", "main")["session_id"]
        == "ses_tgt_main"
    )


@pytest.mark.asyncio
async def test_e2e_prepare_only_missing_dir_clones_then_second_prepare_refreshes(
    harness,
):
    track = {"clone": 0, "refresh": 0}
    harness.sm.create_state("PREP-1", "s", _params(REPO, "feature/shared"))
    with _git_io_patches(track=track):
        g1 = await harness.proc._prepare_git_workspace(harness.sm.get_state("PREP-1"))
        assert g1 is not None
        path = Path(g1.temp_dir)
        assert track["clone"] == 1
        harness.proc._release_context("PREP-1", success=True)
        _seed_git_origin(path, REPO)
        harness.sm.create_state("PREP-2", "s", _params(REPO, "feature/shared"))
        g2 = await harness.proc._prepare_git_workspace(harness.sm.get_state("PREP-2"))
        assert g2 is not None
        assert Path(g2.temp_dir).resolve() == path.resolve()
        assert track["refresh"] == 1
        harness.proc._release_context("PREP-2", success=True)


def test_e2e_workspace_folder_name_is_stable_across_issue_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.config.settings.temp_dir_base", Path(".temp"))
    with _git_io_patches():
        a = GitManager(
            issue_key="N-1",
            remote_url=REPO,
            source_branch="feature/shared",
            target_branch="develop",
        )
        b = GitManager(
            issue_key="N-2",
            remote_url=REPO,
            source_branch="feature/shared",
            target_branch="develop",
        )
    assert a.temp_dir.resolve() == b.temp_dir.resolve()
    # Short Windows-safe name: {remote12}_{digest12} — no branch tokens
    assert len(a.temp_dir.name) <= 32
    assert a.temp_dir.name.count("_") >= 1
    assert a.issue_key not in a.temp_dir.name
    assert b.issue_key not in b.temp_dir.name
    assert "feature-shared" not in a.temp_dir.name
    assert "feature-KAN" not in a.temp_dir.name
