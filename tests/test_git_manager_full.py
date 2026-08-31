"""Full branch coverage for GitManager with mocked subprocess/git."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import subprocess

import pytest

from src.git_manager import GitManager


def _cp(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def gm(tmp_path, monkeypatch):
    """GitManager without real clone — inject temp_dir and remote flags."""
    monkeypatch.chdir(tmp_path)
    # Allowlist must include fixture host when a real .env sets GITLAB_PAT
    from src.config import settings as real_settings

    monkeypatch.setattr(
        real_settings, "gitlab_allowed_hosts", "gitlab.example.com"
    )
    monkeypatch.setattr(real_settings, "gitlab_pat", "test-pat")
    monkeypatch.setattr(
        real_settings,
        "gitlab_host_pats",
        '{"gitlab.example.com":"test-pat"}',
    )
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="GM-1")
    g.temp_dir = tmp_path / "repo"
    g.temp_dir.mkdir()
    g.remote_enabled = True
    g.remote_url = "https://gitlab.example.com/group/repo.git"
    g.remote_name = "repo"
    g.source_branch = "develop"
    g.target_branch = "develop"
    g.issue_key = "GM-1"
    return g


def test_init_no_repository_url():
    from src.git_manager import GitCloneError

    with pytest.raises(GitCloneError, match="no repository URL"):
        GitManager(issue_key="X-1", remote_url="", source_branch="develop")


def test_init_no_target_branch():
    from src.git_manager import GitTargetBranchError

    with pytest.raises(GitTargetBranchError, match="no target branch"):
        GitManager(
            issue_key="X-1",
            remote_url="https://gitlab.example.com/g/r.git",
            source_branch="",
            target_branch="",
        )


def test_extract_remote_name(gm):
    assert gm._extract_remote_name("https://h/g/myrepo.git") == "myrepo"
    assert gm._extract_remote_name("https://h/g/myrepo/") == "myrepo"


def test_build_clone_url(gm):
    # PAT must never be embedded in the URL (argv leak); clean URL only
    assert gm._build_clone_url("https://h/r.git", "pat") == "https://h/r.git"
    assert gm._build_clone_url("http://h/r.git", "pat") == "http://h/r.git"
    assert gm._build_clone_url("https://h/r.git", "") == "https://h/r.git"


def test_create_temp_directory_reuses_stable_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="C-1")
    g.remote_name = "repo"
    g.issue_key = "C-1"
    g.remote_url = "https://gitlab.example.com/g/r.git"
    g.source_branch = "feature/shared"
    g.target_branch = "develop"
    g.work_branch = "feature/shared"
    with patch("src.git_manager.settings") as s:
        s.temp_dir_base = Path(".temp")
        first = g._create_temp_directory()
        second = g._create_temp_directory()
        assert first == second
        assert first.exists()
        assert first.parent == (tmp_path / ".temp").resolve()
        # Same repo + work + target from another issue → same folder
        g.issue_key = "C-2"
        third = g._create_temp_directory()
        assert third == first
        g.target_branch = "main"
        fourth = g._create_temp_directory()
        assert fourth != first
        # Short name: no branch tokens (Windows MAX_PATH budget)
        assert "feature-shared" not in first.name
        assert "develop" not in first.name
        assert len(first.name) <= 32


def test_workspace_folder_name_is_short_and_stable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="C-1")
    g.remote_name = "test_project"
    g.issue_key = "KAN-1905"
    g.remote_url = "https://gitlab.example.com/g/test_project.git"
    g.source_branch = "feature/KAN-1905"
    g.target_branch = "feature/KAN-21"
    g.work_branch = "feature/KAN-1905"
    short = g._workspace_folder_name()
    legacy = g._legacy_workspace_folder_name()
    assert short.startswith("test_project_")
    assert len(short) <= 25  # 12 + 1 + 12
    assert "feature-KAN-1905" not in short
    assert "feature-KAN-21" in legacy
    assert short.split("_")[-1] == legacy.split("_")[-1]
    # Same identity → same digest; different target → different short name
    g.target_branch = "develop"
    other = g._workspace_folder_name()
    assert other != short
    assert other.split("_")[-1] != short.split("_")[-1]


def test_create_temp_directory_renames_legacy_long_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="C-1")
    g.remote_name = "test_project"
    g.issue_key = "KAN-21"
    g.remote_url = "https://gitlab.example.com/g/test_project.git"
    g.source_branch = "feature/KAN-1905"
    g.target_branch = "feature/KAN-21"
    g.work_branch = "feature/KAN-1905"
    with patch("src.git_manager.settings") as s:
        s.temp_dir_base = Path(".temp")
        base = (tmp_path / ".temp").resolve()
        base.mkdir()
        legacy = base / g._legacy_workspace_folder_name()
        legacy.mkdir()
        (legacy / "marker.txt").write_text("keep", encoding="utf-8")
        got = g._create_temp_directory()
        short = base / g._workspace_folder_name()
        assert got.resolve() == short.resolve()
        assert "feature-KAN" not in got.name
        assert (got / "marker.txt").read_text(encoding="utf-8") == "keep"
        assert not legacy.exists()


def test_create_temp_directory_rename_relocates_bind_and_opencode_dir(
    tmp_path, monkeypatch
):
    from src.opencode_sessions import (
        lookup_session_directory,
        session_matches_workdir,
    )
    from src.state.session_bind_store import SessionBindStore
    from tests.test_opencode_sessions import _make_session_db

    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="C-1")
    g.remote_name = "test_project"
    g.issue_key = "KAN-21"
    g.remote_url = "https://gitlab.example.com/g/test_project.git"
    g.source_branch = "feature/KAN-1905"
    g.target_branch = "feature/KAN-21"
    g.work_branch = "feature/KAN-1905"

    store = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)

    with patch("src.git_manager.settings") as s:
        s.temp_dir_base = Path(".temp")
        base = (tmp_path / ".temp").resolve()
        base.mkdir()
        legacy = base / g._legacy_workspace_folder_name()
        legacy.mkdir()
        (legacy / "marker.txt").write_text("keep", encoding="utf-8")
        db = _make_session_db(
            tmp_path / "opencode.db",
            [{"id": "ses_legacy", "directory": str(legacy), "title": "KAN-21: x"}],
        )
        monkeypatch.setattr("src.opencode_sessions._default_db_path", lambda: db)
        store.upsert(
            repository_url=g.remote_url,
            branch=g.work_branch,
            target_branch=g.target_branch,
            session_id="ses_legacy",
            issue_key="KAN-21",
            working_directory=str(legacy),
        )
        got = g._create_temp_directory()
        short = base / g._workspace_folder_name()
        assert got.resolve() == short.resolve()
        bound = store.get(g.remote_url, g.work_branch, g.target_branch)
        assert bound is not None
        assert Path(bound["working_directory"]).resolve() == short.resolve()
        d, ok = lookup_session_directory("ses_legacy", db_path=db)
        assert ok is True
        assert Path(d).resolve() == short.resolve()
        assert session_matches_workdir("ses_legacy", short, db_path=db) is True
        from src.git_manager import session_bound_workspace_paths

        assert short.resolve() in session_bound_workspace_paths()


def test_enable_git_longpaths_sets_local_config(gm, tmp_path):
    git_dir = gm.temp_dir / ".git"
    git_dir.mkdir()
    with patch.object(gm, "_run_git") as run:
        gm._enable_git_longpaths()
    run.assert_called_once_with(["config", "core.longpaths", "true"], check=False)

    # No .git → no-op
    import shutil

    shutil.rmtree(git_dir)
    with patch.object(gm, "_run_git") as run2:
        gm._enable_git_longpaths()
    run2.assert_not_called()


def test_run_git_passes_core_longpaths(gm):
    with patch("src.git_manager.subprocess.run", return_value=_cp(returncode=0)) as run:
        gm._run_git(["status"])
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["git", "-c", "core.longpaths=true"]
    assert "credential.helper=" in cmd
    assert cmd[-1] == "status"


def test_clone_into_temp_success_and_fail(gm, tmp_path):
    host = "gitlab.example.com"
    gm.remote_url = f"https://{host}/group/repo.git"
    with patch("src.git_manager.subprocess.run", return_value=_cp(returncode=0)) as run:
        with patch.object(gm, "_update_submodules") as upd:
            with patch.object(gm, "_materialize_job_remote_refs"):
                with patch("src.git_manager.settings") as s:
                    s.gitlab_pat = "pat"
                    s.gitlab_allowed_hosts = host
                    s.gitlab_allowed_hosts_list = [host]
                    s.git_command_timeout_seconds = 300
                    s.git_clone_timeout_seconds = 1800
                    s.git_update_submodules = True
                    s.gitlab_pat_for_host = lambda h: "pat"
                    s.gitlab_host_pat_map = lambda: {host: "pat"}
                    s.all_gitlab_pats = lambda: ["pat"]
                    gm._clone_into_temp()
        upd.assert_called_once()
        clone_cmds = [
            c.args[0]
            for c in run.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "git" and "clone" in c.args[0]
        ]
        assert clone_cmds, "expected a git clone subprocess call"
        cmd = clone_cmds[0]
        assert cmd[0] == "git"
        assert "-c" in cmd and "core.longpaths=true" in cmd
        i = cmd.index("clone")
        assert cmd[i : i + 2] == ["clone", "--no-single-branch"]
    with patch(
        "src.git_manager.subprocess.run",
        return_value=_cp(returncode=1, stderr="fail"),
    ):
        with patch("src.git_manager.settings") as s:
            s.gitlab_pat = ""
            s.gitlab_allowed_hosts = ""
            s.gitlab_allowed_hosts_list = []
            s.gitlab_pat_for_host = lambda h: ""
            s.gitlab_host_pat_map = lambda: {}
            s.all_gitlab_pats = lambda: []
            s.git_clone_timeout_seconds = 1800
            from src.git_manager import GitCloneError

            with pytest.raises(GitCloneError) as ei:
                gm._clone_into_temp()
            assert "clone" in ei.value.user_message.lower()
    gm.temp_dir = None
    with pytest.raises(RuntimeError):
        gm._clone_into_temp()


def test_update_submodules_skips_without_gitmodules(gm, tmp_path):
    with patch("src.git_manager.settings") as s:
        s.git_update_submodules = True
        s.git_submodule_timeout_seconds = 1800
        with patch("src.git_manager.subprocess.run") as run:
            gm._update_submodules(reason="test")
        run.assert_not_called()


def test_update_submodules_runs_init_recursive(gm, tmp_path):
    (gm.temp_dir / ".gitmodules").write_text('[submodule "x"]\n', encoding="utf-8")
    with patch("src.git_manager.settings") as s:
        s.git_update_submodules = True
        s.git_submodule_timeout_seconds = 1800
        s.gitlab_pat = "pat"
        s.gitlab_pat_for_host = lambda h: "pat"
        with patch.object(gm, "_apply_settings_pat_to_origin", return_value=True):
            with patch.object(gm, "_scrub_remote_credentials") as scrub:
                with patch(
                    "src.git_manager.subprocess.run",
                    return_value=_cp(returncode=0),
                ) as run:
                    gm._update_submodules(reason="after clone")
        run.assert_called_once()
        cmd = run.call_args[0][0]
        assert cmd == [
            "git",
            "submodule",
            "update",
            "--init",
            "--recursive",
        ]
        scrub.assert_called()


def test_update_submodules_failure_raises(gm, tmp_path):
    from src.git_manager import GitCloneError

    (gm.temp_dir / ".gitmodules").write_text('[submodule "x"]\n', encoding="utf-8")
    with patch("src.git_manager.settings") as s:
        s.git_update_submodules = True
        s.git_submodule_timeout_seconds = 1800
        s.gitlab_pat = ""
        s.gitlab_pat_for_host = lambda h: ""
        with patch.object(gm, "_apply_settings_pat_to_origin", return_value=False):
            with patch(
                "src.git_manager.subprocess.run",
                return_value=_cp(returncode=1, stderr="auth failed"),
            ):
                with pytest.raises(GitCloneError) as ei:
                    gm._update_submodules(reason="after clone")
    assert "submodule" in ei.value.user_message.lower()


def test_ensure_feature_branch_updates_submodules_after_checkout(gm):
    with patch.object(gm, "_require_target_on_remote", return_value="develop"):
        with patch.object(gm, "_resolve_work_branch_name", return_value="feature/GM-1"):
            with patch.object(
                gm, "_prepare_work_branch", return_value="feature/GM-1"
            ) as prep:
                with patch.object(gm, "_update_submodules") as upd:
                    out = gm.ensure_feature_branch("GM-1")
    assert out == "feature/GM-1"
    prep.assert_called_once()
    upd.assert_called_once()
    assert "work branch" in (upd.call_args[1].get("reason") or "")


def test_materialize_job_remote_refs_only_fetches_source_target(gm):
    """Must not list/track every origin/* branch — only source/target."""
    gm.source_branch = "feature/job"
    gm.target_branch = "develop"
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    calls: list = []

    def run_git(args, check=True, auth=False, timeout=None):
        calls.append(list(args))
        return _cp()

    with patch.object(gm, "_run_git", side_effect=run_git):
        gm._materialize_job_remote_refs()

    fetch_args = [c for c in calls if c[:2] == ["fetch", "origin"]]
    assert any(c == ["fetch", "origin", "develop"] for c in fetch_args)
    assert any(c == ["fetch", "origin", "feature/job"] for c in fetch_args)
    # No mass branch -r / --track of unrelated remotes
    assert not any(c[:2] == ["branch", "-r"] for c in calls)
    assert not any("--track" in c for c in calls)


def test_sync_remote_branches_is_alias_to_job_materialize(gm):
    gm.source_branch = "develop"
    gm.target_branch = "develop"
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    with patch.object(gm, "_materialize_job_remote_refs") as m:
        gm._sync_remote_branches()
        m.assert_called_once()


def test_materialize_job_remote_refs_noop_without_branches(gm):
    gm.source_branch = ""
    gm.target_branch = ""
    with patch.object(gm, "_run_git") as rg:
        gm._materialize_job_remote_refs()
        rg.assert_not_called()


def test_run_git_missing_dir(gm):
    gm.temp_dir = Path("/nonexistent/path/xyz")
    with pytest.raises(RuntimeError, match="locked"):
        gm._run_git(["status"])


def test_run_git_check_fail(gm):
    with patch("src.git_manager.subprocess.run", return_value=_cp(returncode=1, stderr="bad")):
        with pytest.raises(RuntimeError):
            gm._run_git(["status"], check=True)
        r = gm._run_git(["status"], check=False)
        assert r.returncode == 1


def test_has_commits_and_branch_exists(gm):
    with patch.object(gm, "_run_git", return_value=_cp(returncode=0)):
        assert gm._has_commits() is True
        assert gm._branch_exists("main") is True
        assert gm._branch_exists("main", check_remote=True) is True
    with patch.object(gm, "_run_git", return_value=_cp(returncode=1)):
        assert gm._has_commits() is False
        assert gm._branch_exists("x") is False
        assert gm._branch_exists("x", check_remote=True) is False


def test_delete_local_branch(gm):
    with patch.object(gm, "_branch_exists", return_value=False):
        assert gm._delete_local_branch("feature/x") is True

    with patch.object(gm, "_branch_exists", return_value=True):
        with patch.object(gm, "get_current_branch", return_value="feature/x"):
            with patch.object(gm, "_checkout_default_branch", return_value=False):
                with patch.object(gm, "_run_git", return_value=_cp()):
                    assert gm._delete_local_branch("feature/x") is True

    with patch.object(gm, "_branch_exists", return_value=True):
        with patch.object(gm, "get_current_branch", side_effect=RuntimeError("x")):
            assert gm._delete_local_branch("feature/x") is False


def test_checkout_or_create_branch_requires_target(gm):
    """Work branch is always created from target; missing target fails first."""
    gm.target_branch = "develop"
    with patch.object(gm, "_require_target_on_remote", return_value="develop") as req:
        with patch.object(
            gm, "_checkout_work_branch_from_target", return_value="feature/GM-1"
        ) as work:
            name = gm._checkout_or_create_branch("feature/GM-1")
            assert name == "feature/GM-1"
            req.assert_called_once()
            work.assert_called_once_with("feature/GM-1", "develop")

    with patch.object(
        gm,
        "_require_target_on_remote",
        side_effect=__import__(
            "src.git_manager", fromlist=["GitTargetBranchError"]
        ).GitTargetBranchError("missing target"),
    ):
        from src.git_manager import GitTargetBranchError

        with pytest.raises(GitTargetBranchError):
            gm._checkout_or_create_branch("feature/GM-1")


def test_require_target_on_remote_missing(gm):
    gm.target_branch = "develop"
    with patch.object(gm, "_remote_head_exists", return_value=False):
        with patch.object(gm, "_run_git", return_value=_cp()):
            from src.git_manager import GitTargetBranchError

            with pytest.raises(GitTargetBranchError) as ei:
                gm._require_target_on_remote()
            assert "develop" in ei.value.user_message.lower()
            assert "target" in ei.value.user_message.lower()


def test_require_target_on_remote_ok(gm):
    gm.target_branch = "develop"
    with patch.object(gm, "_remote_head_exists", return_value=True):
        with patch.object(gm, "_run_git", return_value=_cp()):
            assert gm._require_target_on_remote() == "develop"


def test_resolve_work_branch_name(gm):
    gm.target_branch = "develop"
    gm.source_branch = "develop"
    assert gm._resolve_work_branch_name("GM-1") == "feature/GM-1"

    gm.source_branch = "main"
    assert gm._resolve_work_branch_name("GM-1") == "feature/GM-1"

    gm.source_branch = "feature/custom"
    assert gm._resolve_work_branch_name("GM-1") == "feature/custom"


def test_checkout_work_branch_from_target(gm):
    gm.target_branch = "develop"
    with patch.object(gm, "_delete_local_branch", return_value=True):
        with patch.object(gm, "_branch_exists", return_value=True):
            with patch.object(gm, "_run_git", return_value=_cp()) as run:
                name = gm._checkout_work_branch_from_target("feature/GM-1", "develop")
                assert name == "feature/GM-1"
                assert gm.work_branch == "feature/GM-1"
                assert gm.source_branch == "feature/GM-1"
                calls = [" ".join(str(a) for a in c.args[0]) for c in run.call_args_list]
                assert any(
                    "checkout" in c and "feature/GM-1" in c and "origin/develop" in c
                    for c in calls
                )


def test_ensure_feature_branch_from_target(gm):
    """ensure_feature_branch: require target, resolve work name, prepare branch."""
    gm.source_branch = "develop"
    gm.target_branch = "develop"
    with patch.object(gm, "_require_target_on_remote", return_value="develop"):
        with patch.object(
            gm, "_prepare_work_branch", return_value="feature/GM-1"
        ) as work:
            assert gm.ensure_feature_branch("GM-1") == "feature/GM-1"
            work.assert_called_once_with("feature/GM-1", "develop")


def test_ensure_feature_branch_custom_source(gm):
    gm.source_branch = "feature/custom"
    gm.target_branch = "main"
    with patch.object(gm, "_require_target_on_remote", return_value="main"):
        with patch.object(
            gm, "_prepare_work_branch", return_value="feature/custom"
        ) as work:
            assert gm.ensure_feature_branch("GM-1") == "feature/custom"
            work.assert_called_once_with("feature/custom", "main")


def test_create_source_from_target(gm):
    gm.source_branch = "feature-base"
    gm.target_branch = "main"

    def exists(name, check_remote=False, **_k):
        if name == "main":
            return True
        return False

    with patch.object(gm, "_remote_head_exists", return_value=True):
        with patch.object(gm, "_branch_exists", side_effect=exists):
            with patch.object(gm, "_run_git", return_value=_cp()) as run:
                with patch.object(gm, "_delete_local_branch", return_value=True):
                    assert gm._create_source_from_target("feature-base", "main") is True
                    calls = [
                        " ".join(str(a) for a in c.args[0]) for c in run.call_args_list
                    ]
                    assert any("checkout" in c and "feature-base" in c for c in calls)


def test_format_commit_message(gm):
    # BUILD_PROMPT commit format: [KEY] type: description
    msg = gm._format_commit_message("GM-1", "fix: short", "body")
    assert msg.startswith("[GM-1] fix: short")
    assert "body" in msg
    assert "Closes:" not in msg
    # bare summary gets chore: prefix
    msg_plain = gm._format_commit_message("GM-1", "short")
    assert msg_plain.startswith("[GM-1] chore: short")
    long_sum = "fix: " + ("x" * 100)
    msg2 = gm._format_commit_message("GM-1", long_sum, "y" * 600)
    assert "..." in msg2


def test_configure_and_commit(gm):
    with patch.object(gm, "_run_git", return_value=_cp()) as rg:
        with patch("src.git_manager.settings") as s:
            s.git_user_name = "Bot"
            s.git_user_email = "b@e.com"
            gm._configure_git_identity()
    with patch.object(gm, "_run_git", side_effect=RuntimeError("x")):
        with patch("src.git_manager.settings") as s:
            s.git_user_name = "Bot"
            s.git_user_email = "b@e.com"
            gm._configure_git_identity()

    # no changes
    with patch.object(gm, "_configure_git_identity"):
        with patch.object(gm, "_run_git", return_value=_cp(stdout="")):
            assert gm.commit_changes("GM-1", "s") is True
    # with changes
    with patch.object(gm, "_configure_git_identity"):
        with patch.object(gm, "_run_git", side_effect=[
            _cp(stdout=" M file.py\n"),
            _cp(),
            _cp(),
        ]):
            assert gm.commit_changes("GM-1", "s", "d") is True
    with patch.object(gm, "_configure_git_identity", side_effect=RuntimeError("x")):
        assert gm.commit_changes("GM-1", "s") is False


def test_getters(gm):
    with patch.object(gm, "_run_git", return_value=_cp(stdout="feature/x\n", returncode=0)):
        assert gm.get_current_branch() == "feature/x"
    with patch.object(gm, "_run_git", return_value=_cp(returncode=1)):
        assert gm.get_current_branch() == "main"
    with patch.object(gm, "_run_git", return_value=_cp(stdout="msg\n", returncode=0)):
        assert gm.get_last_commit_message() == "msg"
        assert gm.get_last_commit_subject() == "msg"
    with patch.object(gm, "_run_git", return_value=_cp(returncode=1)):
        assert gm.get_last_commit_message() is None
        assert gm.get_last_commit_subject() is None
    with patch.object(gm, "_run_git", return_value=_cp(stdout="status out")):
        assert "status" in gm.status()


def test_summarize_git_error():
    from src.git_manager import summarize_git_error

    wrapped = (
        "Git command failed: git push -u origin -- feature/KAN-1905\n"
        "fatal: could not read Username for 'https://gitlab.com': "
        "terminal prompts disabled"
    )
    assert "could not read Username" in summarize_git_error(wrapped)
    assert "Git command failed" not in summarize_git_error(wrapped)
    assert summarize_git_error("") == ""
    assert "not allowed" in summarize_git_error(
        "remote: You are not allowed\nerror: failed to push"
    )


def test_summarize_git_error_redacts_embedded_pat():
    from src.git_manager import summarize_git_error

    pat = "glpat-TEST-SECRET-REDACT-9f3a"
    raw = (
        "git cancelled: git remote set-url origin "
        f"https://oauth2:{pat}@gitlab.example.com/g/r.git"
    )
    out = summarize_git_error(raw)
    assert pat not in out
    assert "oauth2:***@" in out


def test_git_cancelled_error_redacts_pat_in_cmd(gm):
    from src.git_manager import GitCancelledError

    pat = "glpat-TEST-SECRET-REDACT-9f3a"
    gm._cancelled = True
    gm._init_proc_state()
    with pytest.raises(GitCancelledError) as ei:
        gm._run_tracked(
            ["git", "remote", "set-url", "origin", f"https://oauth2:{pat}@h/r.git"]
        )
    assert pat not in str(ei.value)
    assert "oauth2:***@" in str(ei.value)


def test_apply_settings_pat_logs_redact_timeout(gm):
    pat = "glpat-TEST-SECRET-REDACT-9f3a"
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    gm._https_url_with_settings_pat = lambda url="": (
        f"https://oauth2:{pat}@gitlab.example.com/g/r.git"
    )

    def boom(cmd, **kwargs):
        raise TimeoutError(
            "timeout: " + " ".join(str(c) for c in cmd[:6])
        )

    gm._run_tracked = boom
    with patch("src.git_manager.logger") as log:
        assert gm._apply_settings_pat_to_origin() is False
        warned = " ".join(str(c.args[0]) for c in log.warning.call_args_list)
    assert pat not in warned
    assert "oauth2:***@" in warned or "***" in warned


def test_push_does_not_swallow_cancel_as_push_fail(gm):
    from src.git_manager import GitCancelledError

    pat = "glpat-TEST-SECRET-REDACT-9f3a"
    gm.remote_enabled = True
    gm.last_push_error = None
    gm._pat_for_remote = lambda url: pat
    gm._host_from_url = lambda url: "gitlab.example.com"
    gm.normalize_remote_url = lambda url: url
    gm.head_is_on_remote = lambda branch: False
    gm._with_auth_remote = lambda: None
    gm._scrub_remote_credentials = lambda: None

    def run_git(*a, **k):
        raise GitCancelledError(
            f"git cancelled: git remote set-url origin "
            f"https://oauth2:{pat}@gitlab.example.com/g/r.git"
        )

    gm._run_git = run_git
    with pytest.raises(GitCancelledError):
        gm.push("feature/x")
    assert gm.last_push_error is None


def test_push_merge_fail_redacts_pat_in_last_push_error(gm):
    pat = "glpat-TEST-SECRET-REDACT-9f3a"
    leak = (
        f"git cancelled: git remote set-url origin "
        f"https://oauth2:{pat}@gitlab.example.com/g/r.git"
    )
    gm.remote_enabled = True
    gm.last_push_error = None
    gm._pat_for_remote = lambda url: pat
    gm._host_from_url = lambda url: "gitlab.example.com"
    gm.normalize_remote_url = lambda url: url
    gm.head_is_on_remote = lambda branch: False
    gm._with_auth_remote = lambda: None
    gm._scrub_remote_credentials = lambda: None

    def run_git(*a, **k):
        raise RuntimeError(leak)

    gm._run_git = run_git
    assert gm.push("feature/x") is False
    err = gm.last_push_error or ""
    assert pat not in err
    assert "oauth2:***@" in err


def test_push_paths(gm):
    gm.remote_enabled = False
    assert gm.push() is False
    assert gm.last_push_error
    gm.remote_enabled = True
    # push() does: auth set-url, push, scrub set-url (and more on merge retry)
    with patch.object(gm, "get_current_branch", return_value="feature/x"):
        with patch.object(gm, "_run_git", return_value=_cp()):
            assert gm.push() is True
            assert gm.last_push_error is None
        # auth set-url ok, push fails, fetch/merge/push ok, scrub set-url
        with patch.object(gm, "_run_git", side_effect=[
            _cp(),  # auth set-url
            RuntimeError("fail"),  # push
            _cp(),  # fetch
            _cp(),  # merge
            _cp(),  # push retry
            _cp(),  # scrub
        ]):
            assert gm.push("feature/x") is True
        # push/merge fail and remote tip check fails → False
        with patch.object(gm, "head_is_on_remote", return_value=False):
            with patch.object(gm, "_run_git", side_effect=[
                _cp(),  # set-url
                RuntimeError("fail"),  # push
                _cp(),  # fetch
                _cp(),  # merge
                RuntimeError("fail2"),  # push retry
                _cp(),  # scrub
            ]):
                assert gm.push("feature/x") is False
                assert "fail2" in (gm.last_push_error or "")
        # Agent already pushed: push fails but head_is_on_remote → True
        with patch.object(gm, "head_is_on_remote", return_value=True):
            with patch.object(gm, "_run_git", side_effect=[
                _cp(),  # set-url
                RuntimeError("fail"),  # push
                _cp(),  # fetch
                _cp(),  # merge
                RuntimeError("fail2"),  # push retry
                _cp(),  # scrub
            ]):
                assert gm.push("feature/x") is True
                assert gm.last_push_error is None


def test_head_is_on_remote_matches_tip(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/x"
    gm.remote_url = "https://gitlab.example.com/g/p.git"

    def _run(args, **kwargs):
        # Ignore auth set-url / scrub / fetch noise; answer rev-parse.
        if args and args[0] == "rev-parse":
            return _cp(stdout="deadbeef\n")
        if args and args[0] == "merge-base":
            return _cp(returncode=1)
        return _cp()

    with patch.object(gm, "get_last_commit_sha", return_value="deadbeef"):
        with patch.object(gm, "_run_git", side_effect=_run):
            assert gm.head_is_on_remote("feature/x") is True

    def _run_miss(args, **kwargs):
        if args and args[0] == "rev-parse":
            return _cp(returncode=1, stdout="")
        return _cp()

    with patch.object(gm, "get_last_commit_sha", return_value="deadbeef"):
        with patch.object(gm, "_run_git", side_effect=_run_miss):
            assert gm.head_is_on_remote("feature/x") is False


def test_mr_and_comments(gm):
    gm.remote_enabled = False
    assert gm.create_merge_request("t") is None
    assert gm.add_mr_comment("http://x/1", "c") is False
    assert gm.get_mr_url() is None

    gm.remote_enabled = True
    with patch.object(gm, "get_current_branch", return_value="main"):
        assert gm.create_merge_request("t") is None

    with patch.object(gm, "get_current_branch", return_value="feature/x"):
        with patch.object(gm, "_get_existing_mr_url", return_value="http://mr/1"):
            assert gm.create_merge_request("t") == "http://mr/1"

        with patch.object(gm, "_get_existing_mr_url", return_value=None):
            with patch(
                "src.git_manager.subprocess.run",
                return_value=_cp(stdout="https://gitlab.com/mr/2\n"),
            ):
                assert "http" in gm.create_merge_request("t", "body")

            with patch(
                "src.git_manager.subprocess.run",
                return_value=_cp(stdout="created ok\n"),
            ):
                assert gm.create_merge_request("t") == "created"

            with patch(
                "src.git_manager.subprocess.run",
                return_value=_cp(returncode=1, stderr="already exists"),
            ):
                with patch.object(gm, "_get_existing_mr_url", return_value="http://mr/3"):
                    assert gm.create_merge_request("t") == "http://mr/3"

            with patch(
                "src.git_manager.subprocess.run",
                return_value=_cp(returncode=1, stderr="other error"),
            ):
                assert gm.create_merge_request("t") is None

            with patch("src.git_manager.subprocess.run", side_effect=FileNotFoundError):
                assert gm.create_merge_request("t") is None
            with patch("src.git_manager.subprocess.run", side_effect=RuntimeError("x")):
                assert gm.create_merge_request("t") is None


def test_create_mr_prefers_api_for_turkish_title(gm):
    """Non-ASCII titles must use REST JSON (UTF-8), not glab console code page."""
    gm.remote_enabled = True
    gm.work_branch = "feature/tr"
    gm.target_branch = "develop"
    title = "feat: Türkçe karakterler ğüşıöç"
    with patch.object(gm, "_get_existing_mr_url", return_value=None):
        with patch.object(
            gm, "_create_mr_via_api", return_value="https://gitlab.example.com/mr/9"
        ) as api:
            with patch.object(gm, "_run_glab") as glab:
                url = gm.create_merge_request(title, body="açıklama")
    assert url == "https://gitlab.example.com/mr/9"
    api.assert_called_once()
    assert api.call_args[0][0] == title
    glab.assert_not_called()


def test_run_git_uses_utf8_encoding(gm, tmp_path):
    """Commit subjects must be decoded as UTF-8 (Windows locale safe)."""
    gm.temp_dir = tmp_path
    (tmp_path / ".git").mkdir()
    turkish = "feat: Türkçe ğüşıöç\n"
    with patch("subprocess.run") as run:
        run.return_value = _cp(stdout=turkish, returncode=0)
        subject = gm.get_last_commit_subject()
    assert subject == "feat: Türkçe ğüşıöç"
    kwargs = run.call_args.kwargs
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"
    # git -c i18n.logOutputEncoding=utf-8 is on the command line
    cmd = run.call_args.args[0] if run.call_args.args else run.call_args[0][0]
    assert "i18n.logOutputEncoding=utf-8" in cmd


def test_get_existing_mr_url(gm):
    with patch(
        "src.git_manager.subprocess.run",
        return_value=_cp(stdout='[{"web_url":"http://mr"}]', returncode=0),
    ):
        assert gm._get_existing_mr_url("feature/x") == "http://mr"
    with patch("src.git_manager.subprocess.run", side_effect=Exception("x")):
        assert gm._get_existing_mr_url("feature/x") is None
    with patch("src.git_manager.subprocess.run", return_value=_cp(stdout="[]", returncode=0)):
        assert gm._get_existing_mr_url("feature/x") is None


def test_add_mr_comment_paths(gm):
    gm.remote_enabled = True
    with patch("src.git_manager.subprocess.run", return_value=_cp(returncode=0)):
        assert gm.add_mr_comment("http://g/mr/12", "hi") is True
    assert gm.add_mr_comment("http://g/mr/notanid", "hi") is False
    with patch("src.git_manager.subprocess.run", return_value=_cp(returncode=1, stderr="e")):
        assert gm.add_mr_comment("http://g/mr/12", "hi") is False
    with patch("src.git_manager.subprocess.run", side_effect=FileNotFoundError):
        assert gm.add_mr_comment("http://g/mr/12", "hi") is False
    with patch("src.git_manager.subprocess.run", side_effect=RuntimeError("x")):
        assert gm.add_mr_comment("http://g/mr/12", "hi") is False


def test_get_mr_url(gm):
    gm.remote_enabled = True
    with patch.object(gm, "get_current_branch", return_value="feature/x"):
        with patch(
            "src.git_manager.subprocess.run",
            return_value=_cp(stdout='[{"web_url":"http://mr"}]', returncode=0),
        ):
            assert gm.get_mr_url() == "http://mr"
        with patch("src.git_manager.subprocess.run", return_value=_cp(stdout="", returncode=0)):
            assert gm.get_mr_url() is None
        with patch("src.git_manager.subprocess.run", side_effect=Exception("x")):
            assert gm.get_mr_url() is None


def test_working_dir_and_cleanup(gm, tmp_path):
    assert gm.get_working_directory() == gm.temp_dir
    keep_dir = tmp_path / "keep"
    keep_dir.mkdir()
    gm.temp_dir = keep_dir
    assert gm.cleanup() is True
    assert keep_dir.exists()
    assert gm.cleanup(success=True) is True
    assert keep_dir.exists()
    gm.temp_dir = None
    assert gm.cleanup() is True
