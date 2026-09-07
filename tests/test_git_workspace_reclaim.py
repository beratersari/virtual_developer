"""Stop + new prompt must evict leftover git and not crash on askpass lock."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.git_manager import GitManager


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts(tmp_path, monkeypatch):
    """Keep askpass writes in tmp. Do not walk the real ``.jira-agent`` (WSL 9p)."""
    data = tmp_path / "yaver-data"
    data.mkdir()
    monkeypatch.setenv("YAVER_DATA_DIR", str(data))
    monkeypatch.setenv("VD_DATA_DIR", str(data))
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: data)
    yield


def _cp(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def gm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    monkeypatch.setattr(real_settings, "gitlab_pat", "test-pat")
    monkeypatch.setattr(
        real_settings,
        "gitlab_host_pats",
        '{"gitlab.example.com":"test-pat"}',
    )
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="RCL-1")
    g.temp_dir = tmp_path / "repo"
    g.temp_dir.mkdir()
    g.remote_enabled = True
    g.remote_url = "https://gitlab.example.com/group/repo.git"
    g.remote_name = "repo"
    g.source_branch = "feature/RCL-1"
    g.target_branch = "develop"
    g.issue_key = "RCL-1"
    g.work_branch = "feature/RCL-1"
    return g


def test_ensure_askpass_permission_denied_reuses_existing(tmp_path, monkeypatch):
    """Leftover git locking vd-git-askpass.cmd must not fail workspace prep."""
    monkeypatch.chdir(tmp_path)
    path = GitManager._ensure_askpass_script()
    assert path is not None and path.exists()
    path.write_text("locked-helper\n", encoding="utf-8")

    def deny_write(self, *a, **k):
        raise PermissionError(13, "Permission denied", str(self))

    with patch.object(Path, "write_text", deny_write):
        again = GitManager._ensure_askpass_script()
    assert again == path
    assert path.read_text(encoding="utf-8") == "locked-helper\n"


def test_ensure_askpass_permission_denied_fallback_pid_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_write = Path.write_text

    def deny_shared(self, data, *a, **k):
        name = getattr(self, "name", "")
        if name in {"vd-git-askpass.sh", "vd-git-askpass.cmd", "vd-git-askpass.py"}:
            raise PermissionError(13, "Permission denied", str(self))
        return real_write(self, data, *a, **k)

    with patch.object(Path, "write_text", deny_shared):
        path = GitManager._ensure_askpass_script()
    assert path is not None
    assert path.exists()
    assert str(os.getpid()) in path.name
    assert path.read_text(encoding="utf-8")


def test_apply_pat_env_survives_askpass_oserror(gm):
    with patch.object(
        GitManager, "_ensure_askpass_script", side_effect=PermissionError(13, "denied")
    ):
        env = gm._apply_pat_to_git_env(url=gm.remote_url)
    assert env.get("VD_GIT_PASSWORD") == "test-pat"
    assert "GIT_ASKPASS" not in env
    keys = [env[k] for k in env if str(k).startswith("GIT_CONFIG_KEY_")]
    assert any(k.endswith("insteadOf") for k in keys)


def test_looks_like_git_lock_error():
    assert GitManager._looks_like_git_lock_error(
        _cp(stderr="fatal: Unable to create '.git/index.lock': File exists.")
    )
    assert GitManager._looks_like_git_lock_error(
        _cp(stderr="Another git process seems to be running in this repository")
    )
    assert not GitManager._looks_like_git_lock_error(_cp(stderr="fatal: not a git repo"))
    assert not GitManager._looks_like_git_lock_error(_cp())


def test_run_git_retries_after_index_lock(gm):
    lock_err = _cp(
        returncode=1,
        stderr="fatal: Unable to create '.git/index.lock': File exists.\n"
        "Another git process seems to be running",
    )
    ok = _cp(returncode=0, stdout="Switched\n")
    with patch.object(gm, "_run_tracked", side_effect=[lock_err, ok]) as run:
        with patch.object(gm, "reclaim_workspace") as reclaim:
            out = gm._run_git(["checkout", "-B", "feature/X", "origin/develop"])
    assert out.returncode == 0
    assert run.call_count == 2
    reclaim.assert_called_once()


def test_write_helper_file_readonly_then_ok(tmp_path):
    p = tmp_path / "helper.txt"
    p.write_text("old", encoding="utf-8")
    assert GitManager._write_helper_file(p, "old") is True
    assert GitManager._write_helper_file(p, "new") is True
    assert p.read_text(encoding="utf-8") == "new"


def test_ensure_feature_branch_reclaims_before_checkout(gm):
    with patch.object(gm, "reclaim_workspace") as reclaim:
        with patch.object(gm, "_require_target_on_remote", return_value="develop"):
            with patch.object(
                gm, "_resolve_work_branch_name", return_value="feature/RCL-1"
            ):
                with patch.object(
                    gm, "_prepare_work_branch", return_value="feature/RCL-1"
                ):
                    with patch.object(gm, "_update_submodules"):
                        assert gm.ensure_feature_branch("RCL-1") == "feature/RCL-1"
    reclaim.assert_called()


def test_ensure_askpass_mkdir_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("pathlib.Path.mkdir", side_effect=OSError("nope")):
        assert GitManager._ensure_askpass_script() is None


def test_git_manager_reclaim_no_temp_dir(gm):
    gm.temp_dir = None
    assert gm.reclaim_workspace() == 0


def test_git_manager_reclaim_swallows_errors(gm):
    with patch("src.git_manager.reclaim_workspace", side_effect=RuntimeError("boom")):
        assert gm.reclaim_workspace() == 0


def test_run_git_lock_retry_still_fails(gm):
    lock_err = _cp(
        returncode=1,
        stderr="fatal: Unable to create '.git/index.lock': File exists.",
    )
    with patch.object(gm, "_run_tracked", return_value=lock_err):
        with patch.object(gm, "reclaim_workspace"):
            with pytest.raises(RuntimeError, match="index.lock"):
                gm._run_git(["checkout", "-B", "feature/X", "origin/develop"])


def test_parse_windows_process_csv_empty_and_no_pid():
    from src.process_kill import _parse_windows_process_csv

    assert _parse_windows_process_csv("") == []
    assert _parse_windows_process_csv("Name,CommandLine\ngit.exe,foo\n") == []


def test_windows_process_rows_cim_then_wmic_fallback():
    from src.process_kill import _windows_process_rows

    cim = (
        "ProcessId,ParentProcessId,Name,CommandLine\n"
        '42,1,git.exe,"git -C C:\\\\vd\\\\t\\\\repo status"\n'
    )
    with patch("src.process_kill.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=cim, stderr=""
        )
        rows = _windows_process_rows()
    assert rows and rows[0]["pid"] == 42

    with patch("src.process_kill.subprocess.run", side_effect=OSError("no ps")):
        assert _windows_process_rows() == []


def test_kill_workspace_windows_matches_path_not_serve():
    from src.process_kill import _kill_workspace_windows

    root = Path(r"C:\vd\t\repo")
    rows = [
        {"pid": 10, "ppid": 1, "name": "opencode.exe", "cmd": "opencode serve"},
        {"pid": 11, "ppid": 10, "name": "git.exe", "cmd": r"git -C C:\vd\t\repo checkout -B x"},
        {"pid": 12, "ppid": 1, "name": "git.exe", "cmd": "git status"},
    ]
    with patch("src.process_kill._windows_process_rows", return_value=rows):
        with patch("src.process_kill._protect_pids", return_value={1}):
            with patch("src.process_kill.kill_pid") as kill:
                n = _kill_workspace_windows(root, extra_root_pids=[11], force=True)
    assert n >= 1
    killed = [c.args[0] for c in kill.call_args_list]
    assert 11 in killed
    assert 10 not in killed
    assert 12 not in killed


def test_refresh_existing_clone_reclaims_first(gm):
    with patch.object(gm, "reclaim_workspace") as reclaim:
        with patch.object(gm, "_assert_remote_host_allowed"):
            with patch.object(gm, "_with_auth_remote"):
                with patch.object(gm, "_run_git"):
                    with patch.object(gm, "_scrub_remote_credentials"):
                        with patch.object(gm, "_enable_git_longpaths"):
                            with patch.object(gm, "_update_submodules"):
                                with patch.object(gm, "_materialize_job_remote_refs"):
                                    gm._refresh_existing_clone()
    reclaim.assert_called()
