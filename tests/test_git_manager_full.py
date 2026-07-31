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
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="GM-1")
    g.temp_dir = tmp_path / "repo"
    g.temp_dir.mkdir()
    g.remote_enabled = True
    g.remote_url = "https://gitlab.example.com/group/repo.git"
    g.remote_name = "repo"
    g.issue_key = "GM-1"
    return g


def test_init_disabled_temp():
    with patch("src.git_manager.settings") as s:
        s.use_temp_working_dir = False
        with pytest.raises(RuntimeError, match="disabled"):
            GitManager(issue_key="X-1")


def test_init_no_gitlab_url():
    with patch("src.git_manager.settings") as s:
        s.use_temp_working_dir = True
        s.project_gitlab_url = ""
        with pytest.raises(RuntimeError, match="PROJECT_GITLAB_URL"):
            GitManager(issue_key="X-1")


def test_extract_remote_name(gm):
    assert gm._extract_remote_name("https://h/g/myrepo.git") == "myrepo"
    assert gm._extract_remote_name("https://h/g/myrepo/") == "myrepo"


def test_build_clone_url(gm):
    assert "oauth2:pat@" in gm._build_clone_url("https://h/r.git", "pat")
    assert "oauth2:pat@" in gm._build_clone_url("http://h/r.git", "pat")
    assert gm._build_clone_url("https://h/r.git", "") == "https://h/r.git"


def test_create_temp_directory_collision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="C-1")
    g.remote_name = "repo"
    g.issue_key = "C-1"
    with patch("src.git_manager.settings") as s:
        s.temp_dir_base = Path(".temp")
        with patch("src.git_manager.datetime") as dt:
            dt.now.return_value.strftime.return_value = "20260101_000000"
            # pre-create path so counter kicks in
            base = tmp_path / ".temp"
            base.mkdir()
            (base / "repo_C-1_20260101_000000").mkdir()
            p = g._create_temp_directory()
            assert p.exists()


def test_clone_into_temp_success_and_fail(gm, tmp_path):
    with patch("src.git_manager.subprocess.run") as run:
        run.return_value = _cp(returncode=0)
        with patch.object(gm, "_sync_remote_branches"):
            with patch("src.git_manager.settings") as s:
                s.gitlab_pat = "pat"
                gm._clone_into_temp()
    with patch("src.git_manager.subprocess.run") as run:
        run.return_value = _cp(returncode=1, stderr="fail")
        with patch("src.git_manager.settings") as s:
            s.gitlab_pat = ""
            with pytest.raises(RuntimeError):
                gm._clone_into_temp()
    gm.temp_dir = None
    with pytest.raises(RuntimeError):
        gm._clone_into_temp()


def test_sync_remote_branches(gm):
    def run_git(args, check=True):
        if args[:2] == ["branch", "-r"]:
            return _cp("  origin/main\n  origin/HEAD -> origin/main\n  origin/feature/x\n")
        if args[0] == "rev-parse":
            return _cp(returncode=1)
        return _cp()

    with patch.object(gm, "_run_git", side_effect=run_git):
        gm._sync_remote_branches()

    with patch.object(gm, "_run_git", side_effect=RuntimeError("x")):
        gm._sync_remote_branches()

    with patch.object(gm, "_run_git", return_value=_cp(returncode=1, stderr="e")):
        # branch -r fails after fetch
        def rg(args, check=True):
            if args[:2] == ["branch", "-r"]:
                return _cp(returncode=1, stderr="e")
            return _cp()

        with patch.object(gm, "_run_git", side_effect=rg):
            gm._sync_remote_branches()


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


def test_checkout_or_create_branch(gm):
    with patch.object(gm, "_delete_local_branch", return_value=True):
        with patch.object(gm, "_run_git", return_value=_cp(stdout="refs/heads/feature/GM-1")):
            # remote exists
            name = gm._checkout_or_create_branch("feature/GM-1")
            assert name == "feature/GM-1"

        with patch.object(gm, "_run_git", return_value=_cp(stdout="")):
            with patch.object(gm, "_checkout_default_branch", return_value=True):
                name = gm._checkout_or_create_branch("feature/GM-1")
                assert name == "feature/GM-1"

        with patch.object(gm, "_run_git", return_value=_cp(stdout="")):
            with patch.object(gm, "_checkout_default_branch", return_value=False):
                with pytest.raises(RuntimeError):
                    gm._checkout_or_create_branch("feature/GM-1")


def test_checkout_default_branch(gm):
    with patch("src.git_manager.settings") as s:
        s.default_branch = "develop"
        with patch.object(gm, "_branch_exists", side_effect=lambda b, **k: b == "develop"):
            with patch.object(gm, "_run_git", return_value=_cp()):
                assert gm._checkout_default_branch() is True
        with patch.object(gm, "_branch_exists", return_value=False):
            assert gm._checkout_default_branch() is False
        s.default_branch = ""
        with patch.object(gm, "_branch_exists", side_effect=lambda b, **k: b == "main"):
            with patch.object(gm, "_run_git", return_value=_cp()):
                assert gm._checkout_default_branch() is True


def test_ensure_feature_branch(gm):
    with patch.object(gm, "_checkout_or_create_branch", return_value="feature/GM-1") as m:
        assert gm.ensure_feature_branch("GM-1") == "feature/GM-1"
        m.assert_called()


def test_format_commit_message(gm):
    msg = gm._format_commit_message("GM-1", "short", "body")
    assert "[GM-1]" in msg and "Closes: GM-1" in msg
    long_sum = "x" * 100
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


def test_push_paths(gm):
    gm.remote_enabled = False
    assert gm.push() is False
    gm.remote_enabled = True
    with patch.object(gm, "get_current_branch", return_value="feature/x"):
        with patch.object(gm, "_run_git", return_value=_cp()):
            assert gm.push() is True
        with patch.object(gm, "_run_git", side_effect=[
            RuntimeError("fail"),
            _cp(),
            _cp(),
            _cp(),
        ]):
            assert gm.push("feature/x") is True
        with patch.object(gm, "_run_git", side_effect=[
            RuntimeError("fail"),
            _cp(),
            _cp(),
            RuntimeError("fail2"),
        ]):
            assert gm.push("feature/x") is False


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
            with patch("src.git_manager.subprocess.run", return_value=_cp(stdout="https://gitlab.com/mr/2\n")):
                with patch("src.git_manager.settings") as s:
                    s.default_branch = "develop"
                    assert "http" in gm.create_merge_request("t", "body")

            with patch("src.git_manager.subprocess.run", return_value=_cp(stdout="created ok\n")):
                with patch("src.git_manager.settings") as s:
                    s.default_branch = ""
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


def test_working_dir_and_cleanup(gm):
    assert gm.get_working_directory() == gm.temp_dir
    with patch("src.git_manager.settings") as s:
        s.temp_cleanup_policy = "never"
        assert gm.cleanup() is True
        s.temp_cleanup_policy = "always"
        assert gm.cleanup() is True
    gm.temp_dir = None
    assert gm.cleanup() is True
