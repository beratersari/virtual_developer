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
    host = "gitlab.example.com"
    gm.remote_url = f"https://{host}/group/repo.git"
    with patch("src.git_manager.subprocess.run") as run:
        run.return_value = _cp(returncode=0)
        with patch.object(gm, "_materialize_job_remote_refs"):
            with patch("src.git_manager.settings") as s:
                s.gitlab_pat = "pat"
                s.gitlab_allowed_hosts = host
                s.gitlab_allowed_hosts_list = [host]
                s.git_command_timeout_seconds = 300
                s.git_clone_timeout_seconds = 300
                gm._clone_into_temp()
    with patch("src.git_manager.subprocess.run") as run:
        run.return_value = _cp(returncode=1, stderr="fail")
        with patch("src.git_manager.settings") as s:
            s.gitlab_pat = ""
            s.gitlab_allowed_hosts = ""
            s.gitlab_allowed_hosts_list = []
            from src.git_manager import GitCloneError

            with pytest.raises(GitCloneError) as ei:
                gm._clone_into_temp()
            assert "clone" in ei.value.user_message.lower()
    gm.temp_dir = None
    with pytest.raises(RuntimeError):
        gm._clone_into_temp()


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
    # §policy.commit format: [KEY] type: description
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


def test_push_paths(gm):
    gm.remote_enabled = False
    assert gm.push() is False
    gm.remote_enabled = True
    # push() does: auth set-url, push, scrub set-url (and more on merge retry)
    with patch.object(gm, "get_current_branch", return_value="feature/x"):
        with patch.object(gm, "_run_git", return_value=_cp()):
            assert gm.push() is True
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
        # auth ok, push fail, fetch/merge/push fail, scrub
        with patch.object(gm, "_run_git", side_effect=[
            _cp(),  # auth
            RuntimeError("fail"),  # push
            _cp(),  # fetch
            _cp(),  # merge
            RuntimeError("fail2"),  # push retry
            _cp(),  # scrub
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
    with patch("src.git_manager.settings") as s:
        s.temp_cleanup_policy = "never"
        assert gm.cleanup() is True
        assert keep_dir.exists()

        s.temp_cleanup_policy = "on_success"
        assert gm.cleanup(success=False) is True
        assert keep_dir.exists()

        s.temp_cleanup_policy = "always"
        assert gm.cleanup() is True
        assert not keep_dir.exists()
    gm.temp_dir = None
    assert gm.cleanup() is True
