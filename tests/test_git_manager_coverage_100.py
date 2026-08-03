"""Coverage-focused tests for src/git_manager.py (missing branches)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.git_manager import (
    GitCloneError,
    GitManager,
    GitSourceBranchError,
    GitTargetBranchError,
)


def _cp(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def gm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="COV-1")
    g.temp_dir = tmp_path / "repo"
    g.temp_dir.mkdir()
    g.remote_enabled = True
    g.remote_url = "https://gitlab.example.com/group/repo.git"
    g.remote_name = "repo"
    g.source_branch = "develop"
    g.target_branch = "develop"
    g.issue_key = "COV-1"
    g.work_branch = "feature/COV-1"
    return g


# --- Exception classes ---


def test_git_source_branch_error_attrs():
    e = GitSourceBranchError("user msg", technical="tech")
    assert e.user_message == "user msg"
    assert e.technical == "tech"
    assert "user msg" in str(e)


# --- Clone timeout / host allowlist ---


def test_clone_timeout_raises_git_clone_error(gm):
    with patch(
        "src.git_manager.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        with patch("src.git_manager.settings") as s:
            s.gitlab_pat = ""
            s.gitlab_allowed_hosts_list = []
            s.git_clone_timeout_seconds = 30
            with pytest.raises(GitCloneError) as ei:
                gm._clone_into_temp()
    assert "timed out" in ei.value.user_message.lower()
    assert "30" in ei.value.user_message


def test_assert_remote_host_allowed_branches(gm, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gitlab_pat", "secret-pat")
    # urlparse("https://") → no hostname
    with patch.object(
        type(settings),
        "gitlab_allowed_hosts_list",
        property(lambda self: ["gitlab.example.com"]),
    ):
        with pytest.raises(GitCloneError, match="no host"):
            gm._assert_remote_host_allowed("https://")

    with patch.object(type(settings), "gitlab_allowed_hosts_list", property(lambda self: [])):
        with pytest.raises(GitCloneError, match="GITLAB_ALLOWED_HOSTS"):
            gm._assert_remote_host_allowed("https://gitlab.example.com/g/r.git")

    with patch.object(
        type(settings),
        "gitlab_allowed_hosts_list",
        property(lambda self: ["allowed.example.com"]),
    ):
        with pytest.raises(GitCloneError, match="refused to send credentials"):
            gm._assert_remote_host_allowed("https://evil.example.com/g/r.git")


def test_host_from_url_edges():
    assert GitManager._host_from_url("") == ""
    assert GitManager._host_from_url("gitlab.example.com/group/repo") == "gitlab.example.com"
    assert GitManager._host_from_url("https://GitLab.Example.COM/a/b") == "gitlab.example.com"


# --- Setup / temp dir ---


def test_setup_defaults_source_to_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_clone_into_temp"):
        with patch.object(GitManager, "_create_temp_directory", return_value=tmp_path / "t"):
            (tmp_path / "t").mkdir(exist_ok=True)
            with patch("src.git_manager.set_current_temp_dir"):
                g = GitManager(
                    issue_key="S-1",
                    remote_url="https://gitlab.example.com/g/r.git",
                    source_branch="",
                    target_branch="main",
                )
    assert g.source_branch == "main"
    assert g.remote_enabled is True


def test_create_temp_directory_unsafe_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(GitManager, "_setup_temp_working_dir"):
        g = GitManager(issue_key="U-1")
    g.remote_name = "repo"
    g.issue_key = "U-1"
    with patch("src.git_manager.settings") as s:
        s.temp_dir_base = Path(".temp")
        with patch.object(Path, "relative_to", side_effect=ValueError("escape")):
            with pytest.raises(RuntimeError, match="Unsafe temp path"):
                g._create_temp_directory()


# --- Auth / scrub / askpass ---


def test_scrub_remote_no_url(gm):
    gm.remote_url = None
    gm._scrub_remote_credentials()  # no-op


def test_scrub_remote_exception(gm):
    with patch.object(gm, "_run_git", side_effect=RuntimeError("boom")):
        gm._scrub_remote_credentials()  # logs warning


def test_with_auth_remote_no_url(gm):
    gm.remote_url = None
    gm._with_auth_remote()


def test_git_auth_env_no_pat(gm, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gitlab_pat", "")
    env = gm._git_auth_env()
    assert "VD_GIT_PASSWORD" not in env or env.get("VD_GIT_PASSWORD") != "x"


def test_ensure_askpass_script_unix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = GitManager._ensure_askpass_script()
    assert path.exists()
    # rewrite when content differs
    path.write_text("stale", encoding="utf-8")
    path2 = GitManager._ensure_askpass_script()
    assert "VD_GIT_PASSWORD" in path2.read_text(encoding="utf-8")


def test_ensure_askpass_script_windows(tmp_path, monkeypatch):
    """Force the Windows (.cmd) branch without requiring WindowsPath."""
    import pathlib

    monkeypatch.chdir(tmp_path)
    with patch("src.git_manager.os.name", "nt"):
        with patch.object(pathlib, "WindowsPath", pathlib.PosixPath):
            path = GitManager._ensure_askpass_script()
            assert path.name == "vd-git-askpass.cmd"
            text = path.read_text(encoding="utf-8")
            assert "VD_GIT_PASSWORD" in text
            assert "oauth2" in text


def test_ensure_askpass_chmod_oserror(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_chmod = Path.chmod

    def boom(self, mode):
        raise OSError("nope")

    with patch.object(Path, "chmod", boom):
        path = GitManager._ensure_askpass_script()
        assert path.exists()


def test_redact_secret_empty():
    assert GitManager._redact_secret_text("") == ""
    assert GitManager._redact_secret_text(None) is None


# --- Remote head / resolve work branch ---


def test_remote_head_exists(gm):
    gm.target_branch = "develop"
    with patch.object(gm, "_with_auth_remote"):
        with patch.object(gm, "_scrub_remote_credentials"):
            with patch.object(
                gm,
                "_run_git",
                return_value=_cp("abc\trefs/heads/develop\n"),
            ):
                assert gm._remote_head_exists("develop") is True
            with patch.object(gm, "_run_git", return_value=_cp("other\n")):
                assert gm._remote_head_exists("develop") is False
            assert gm._remote_head_exists("") is False


def test_is_primary_base_release():
    assert GitManager._is_primary_base("release/1.0") is True
    assert GitManager._is_primary_base("") is False
    assert GitManager._is_primary_base("feature/x") is False


def test_resolve_work_branch_feature_key_match(gm):
    gm.source_branch = "feature/COV-1"
    gm.target_branch = "develop"
    assert gm._resolve_work_branch_name("COV-1") == "feature/COV-1"


def test_resolve_work_branch_fix_prefix(gm):
    gm.source_branch = "fix/hotfix-login"
    gm.target_branch = "main"
    assert gm._resolve_work_branch_name("COV-1") == "fix/hotfix-login"


def test_resolve_work_branch_source_differs_from_issue_key(gm):
    """Source branch name may differ from Jira key — still use params source."""
    gm.source_branch = "feature/legacy-name"
    gm.target_branch = "develop"
    assert gm._resolve_work_branch_name("KAN-11") == "feature/legacy-name"


def test_resolve_work_branch_custom_source_used(gm):
    """Non-primary Source is used even when not feature/ or fix/ prefix."""
    gm.source_branch = "staging-work"
    gm.target_branch = "main"
    assert gm._resolve_work_branch_name("COV-1") == "staging-work"


def test_resolve_work_branch_primary_base_uses_feature_key(gm):
    gm.source_branch = "develop"
    gm.target_branch = "develop"
    assert gm._resolve_work_branch_name("COV-1") == "feature/COV-1"


def test_require_target_empty(gm):
    gm.target_branch = ""
    with pytest.raises(GitTargetBranchError) as ei:
        gm._require_target_on_remote()
    assert "target branch" in ei.value.user_message.lower()


# --- Work branch checkout edges ---


def test_checkout_work_branch_empty_name(gm):
    with pytest.raises(GitSourceBranchError, match="empty"):
        gm._checkout_work_branch_from_target("", "develop")


def test_checkout_work_branch_equals_target(gm):
    with pytest.raises(GitSourceBranchError, match="refused"):
        gm._checkout_work_branch_from_target("develop", "develop")


def test_checkout_work_branch_missing_origin(gm):
    with patch.object(gm, "_delete_local_branch", return_value=True):
        with patch.object(gm, "_branch_exists", return_value=False):
            with pytest.raises(GitTargetBranchError, match="missing"):
                gm._checkout_work_branch_from_target("feature/X", "develop")


def test_checkout_work_branch_local_fallback(gm):
    def exists(name, check_remote=False):
        if check_remote:
            return False
        return name == "develop"

    with patch.object(gm, "_delete_local_branch", return_value=True):
        with patch.object(gm, "_branch_exists", side_effect=exists):
            with patch.object(gm, "_run_git", return_value=_cp()) as run:
                name = gm._checkout_work_branch_from_target("feature/X", "develop")
                assert name == "feature/X"
                calls = [" ".join(map(str, c.args[0])) for c in run.call_args_list]
                assert any("checkout" in c and "feature/X" in c for c in calls)


def test_checkout_source_branch(gm):
    with patch.object(gm, "_require_target_on_remote", return_value="develop"):
        with patch.object(gm, "_resolve_work_branch_name", return_value="feature/COV-1"):
            with patch.object(
                gm, "_prepare_work_branch", return_value="feature/COV-1"
            ) as co:
                gm._checkout_source_branch()
                co.assert_called_once_with("feature/COV-1", "develop")


def test_prepare_work_branch_uses_remote_when_exists(gm):
    """If source exists on remote, checkout it — do not recreate from target."""
    with patch.object(gm, "_remote_head_exists", return_value=True):
        with patch.object(
            gm, "_checkout_existing_remote_branch", return_value="feature/legacy"
        ) as existing:
            with patch.object(gm, "_checkout_work_branch_from_target") as create:
                out = gm._prepare_work_branch("feature/legacy", "develop")
                assert out == "feature/legacy"
                existing.assert_called_once_with("feature/legacy")
                create.assert_not_called()


def test_prepare_work_branch_creates_from_target_when_missing(gm):
    """If source is not on remote, create it from target."""
    with patch.object(gm, "_remote_head_exists", return_value=False):
        with patch.object(gm, "_branch_exists", return_value=False):
            with patch.object(
                gm, "_checkout_work_branch_from_target", return_value="feature/legacy"
            ) as create:
                with patch.object(gm, "_checkout_existing_remote_branch") as existing:
                    out = gm._prepare_work_branch("feature/legacy", "develop")
                    assert out == "feature/legacy"
                    create.assert_called_once_with("feature/legacy", "develop")
                    existing.assert_not_called()


def test_ensure_feature_branch_prepares_not_always_from_target(gm):
    gm.source_branch = "feature/legacy"
    gm.target_branch = "develop"
    with patch.object(gm, "_require_target_on_remote", return_value="develop"):
        with patch.object(
            gm, "_resolve_work_branch_name", return_value="feature/legacy"
        ):
            with patch.object(
                gm, "_prepare_work_branch", return_value="feature/legacy"
            ) as prep:
                assert gm.ensure_feature_branch("KAN-11") == "feature/legacy"
                prep.assert_called_once_with("feature/legacy", "develop")


def test_create_source_from_target_false_paths(gm):
    assert gm._create_source_from_target("", "main") is False
    assert gm._create_source_from_target("main", "main") is False
    with patch.object(gm, "_remote_head_exists", return_value=False):
        with patch.object(gm, "_branch_exists", return_value=False):
            assert gm._create_source_from_target("feature/x", "main") is False
    with patch.object(gm, "_remote_head_exists", return_value=True):
        with patch.object(
            gm,
            "_checkout_work_branch_from_target",
            side_effect=GitSourceBranchError("x"),
        ):
            assert gm._create_source_from_target("feature/x", "main") is False


def test_checkout_default_branch(gm):
    with patch.object(gm, "_require_target_on_remote", return_value="develop"):
        with patch.object(gm, "_run_git", return_value=_cp()):
            with patch.object(gm, "_branch_exists", return_value=True):
                assert gm._checkout_default_branch() is True
            with patch.object(gm, "_branch_exists", return_value=False):
                assert gm._checkout_default_branch() is False
    with patch.object(
        gm, "_require_target_on_remote", side_effect=GitTargetBranchError("nope")
    ):
        assert gm._checkout_default_branch() is False


# --- commits_ahead / ensure_on_work_branch / push work_branch ---


def test_commits_ahead_of_target(gm):
    gm.work_branch = "feature/COV-1"
    gm.target_branch = "develop"
    with patch.object(gm, "_run_git", return_value=_cp(stdout="3\n")):
        assert gm.commits_ahead_of_target() == 3
    with patch.object(
        gm,
        "_run_git",
        side_effect=[_cp(returncode=1), _cp(stdout="2\n")],
    ):
        assert gm.commits_ahead_of_target("feature/COV-1") == 2
    with patch.object(gm, "_run_git", return_value=_cp(returncode=1)):
        assert gm.commits_ahead_of_target() == 0
    with patch.object(gm, "_run_git", return_value=_cp(stdout="not-int\n")):
        assert gm.commits_ahead_of_target() == 0
    gm.work_branch = None
    gm.target_branch = ""
    with patch.object(gm, "get_current_branch", return_value=""):
        assert gm.commits_ahead_of_target() == 0


def test_ensure_on_work_branch(gm):
    gm.work_branch = ""
    assert gm.ensure_on_work_branch() is False
    gm.work_branch = "feature/COV-1"
    with patch.object(gm, "get_current_branch", return_value="feature/COV-1"):
        assert gm.ensure_on_work_branch() is True
    with patch.object(gm, "get_current_branch", side_effect=["main", "feature/COV-1"]):
        with patch.object(gm, "_run_git", return_value=_cp()):
            assert gm.ensure_on_work_branch() is True
    with patch.object(gm, "get_current_branch", return_value="main"):
        with patch.object(gm, "_run_git", side_effect=RuntimeError("fail")):
            assert gm.ensure_on_work_branch() is False
    with patch.object(gm, "get_current_branch", side_effect=["main", "other"]):
        with patch.object(gm, "_run_git", return_value=_cp()):
            assert gm.ensure_on_work_branch() is False


def test_push_prefers_work_branch(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/from-work"
    with patch.object(gm, "get_current_branch", return_value="wrong-branch") as gcb:
        with patch.object(gm, "_with_auth_remote"):
            with patch.object(gm, "_scrub_remote_credentials"):
                with patch.object(gm, "_run_git", return_value=_cp()) as run:
                    assert gm.push() is True
                    push_args = run.call_args_list[0].args[0]
                    assert "feature/from-work" in push_args
                    gcb.assert_not_called()


# --- Format commit / sync branches ---


def test_format_commit_strips_leading_key(gm):
    msg = gm._format_commit_message("COV-1", "[COV-1] fix: already prefixed")
    assert msg.startswith("[COV-1] fix: already prefixed")
    assert msg.count("[COV-1]") == 1


def test_materialize_job_remote_refs_fetches_target_only(gm):
    gm.source_branch = ""
    gm.target_branch = "main"
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    calls: list = []

    def run_git(args, check=True, auth=False, timeout=None):
        calls.append(list(args))
        return _cp()

    with patch.object(gm, "_run_git", side_effect=run_git):
        gm._materialize_job_remote_refs()

    assert ["fetch", "origin", "main"] in calls
    assert not any("--track" in c for c in calls)
    assert not any(c[:2] == ["branch", "-r"] for c in calls)


# --- GitLab host / glab env / API MR ---


def test_gitlab_host_and_project_edges(gm):
    gm.remote_url = None
    assert gm._gitlab_host_and_project()[0] == "gitlab.com"
    gm.remote_url = 12345  # type: ignore[assignment]
    host, path = gm._gitlab_host_and_project()
    assert host
    # scheme non-http gets https:// prefix; still returns host/path tuple
    gm.remote_url = "gitlab.example.com/group/repo.git"
    host, path = gm._gitlab_host_and_project()
    assert host == "gitlab.example.com"
    assert "repo" in path


def test_glab_env_refuses_unallowed_host(gm, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gitlab_pat", "tok")
    with patch.object(
        gm,
        "_assert_remote_host_allowed",
        side_effect=GitCloneError("nope"),
    ):
        env = gm._glab_env()
        assert "GITLAB_TOKEN" not in env
        assert env.get("GITLAB_HOST")


def test_create_mr_via_api_paths(gm, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gitlab_pat", "")
    assert gm._create_mr_via_api("t", "b", "feature/x", "develop") is None

    monkeypatch.setattr(settings, "gitlab_pat", "tok")
    with patch.object(gm, "_assert_remote_host_allowed", side_effect=GitCloneError("x")):
        assert gm._create_mr_via_api("t", "b", "feature/x", "develop") is None

    with patch.object(gm, "_assert_remote_host_allowed"):
        with patch.object(gm, "_gitlab_host_and_project", return_value=("h", "")):
            assert gm._create_mr_via_api("t", "b", "feature/x", "develop") is None

        with patch.object(
            gm, "_gitlab_host_and_project", return_value=("gitlab.example.com", "g/r")
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"web_url": "https://gitlab.example.com/mr/1"}
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            with patch("httpx.Client", return_value=mock_client):
                url = gm._create_mr_via_api("t", "b", "feature/x", "develop")
                assert url == "https://gitlab.example.com/mr/1"

            mock_resp409 = MagicMock()
            mock_resp409.status_code = 409
            mock_resp409.text = "already exists"
            mock_client.post.return_value = mock_resp409
            with patch("httpx.Client", return_value=mock_client):
                with patch.object(
                    gm, "_get_existing_mr_url", return_value="https://mr/exist"
                ):
                    assert gm._create_mr_via_api("t", "b", "feature/x", "develop") == (
                        "https://mr/exist"
                    )

            mock_resp500 = MagicMock()
            mock_resp500.status_code = 500
            mock_resp500.text = "server error"
            mock_client.post.return_value = mock_resp500
            with patch("httpx.Client", return_value=mock_client):
                assert gm._create_mr_via_api("t", "b", "feature/x", "develop") is None

            with patch("httpx.Client", side_effect=RuntimeError("net")):
                assert gm._create_mr_via_api("t", "b", "feature/x", "develop") is None


def test_get_existing_mr_url_api_fallback(gm, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gitlab_pat", "tok")
    with patch.object(gm, "_run_glab", side_effect=Exception("no glab")):
        with patch.object(gm, "_gitlab_host_and_project", return_value=("h", "")):
            assert gm._get_existing_mr_url("feature/x") is None
        with patch.object(gm, "_gitlab_host_and_project", return_value=("h", "g/r")):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = [{"web_url": "https://mr/api"}]
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp
            with patch("httpx.Client", return_value=mock_client):
                assert gm._get_existing_mr_url("feature/x") == "https://mr/api"
            with patch("httpx.Client", side_effect=RuntimeError("x")):
                assert gm._get_existing_mr_url("feature/x") is None


# --- create_merge_request protected / API fallbacks ---


def test_create_mr_protected_branches(gm):
    gm.remote_enabled = True
    gm.work_branch = "main"
    gm.target_branch = "develop"
    assert gm.create_merge_request("title") is None

    gm.work_branch = "develop"
    assert gm.create_merge_request("title") is None

    gm.work_branch = "release/2.0"
    assert gm.create_merge_request("title") is None

    gm.work_branch = "feature/ok"
    gm.target_branch = ""
    with patch.object(gm, "get_current_branch", return_value="feature/ok"):
        with patch.object(gm, "_get_existing_mr_url", return_value=None):
            # source also empty after strip — no target
            gm.source_branch = ""
            assert gm.create_merge_request("title") is None


def test_create_mr_glab_already_exists(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/ok"
    gm.target_branch = "develop"
    with patch.object(gm, "_get_existing_mr_url", side_effect=[None, "https://mr/ex"]):
        with patch.object(
            gm,
            "_run_glab",
            return_value=_cp(returncode=1, stderr="already exists 409"),
        ):
            assert gm.create_merge_request("t") == "https://mr/ex"


def test_create_mr_target_missing_then_api(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/ok"
    gm.target_branch = "develop"
    with patch.object(gm, "_get_existing_mr_url", return_value=None):
        with patch.object(
            gm,
            "_run_glab",
            return_value=_cp(returncode=1, stderr="target_branch does not exist"),
        ):
            with patch.object(
                gm, "_create_mr_via_api", return_value="https://mr/api"
            ) as api:
                # final API pass may also be called
                assert gm.create_merge_request("t") == "https://mr/api"
                assert api.called


def test_create_mr_glab_auth_fail_api_ok(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/ok"
    gm.target_branch = "develop"
    with patch.object(gm, "_get_existing_mr_url", return_value=None):
        with patch.object(
            gm,
            "_run_glab",
            return_value=_cp(returncode=1, stderr="unauthorized"),
        ):
            with patch.object(
                gm, "_create_mr_via_api", return_value="https://mr/from-api"
            ):
                assert gm.create_merge_request("t") == "https://mr/from-api"


def test_create_mr_file_not_found_uses_api(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/ok"
    gm.target_branch = "develop"
    with patch.object(gm, "_get_existing_mr_url", return_value=None):
        with patch.object(gm, "_run_glab", side_effect=FileNotFoundError):
            with patch.object(
                gm, "_create_mr_via_api", return_value="https://mr/fnf"
            ):
                assert gm.create_merge_request("t") == "https://mr/fnf"
            with patch.object(gm, "_create_mr_via_api", return_value=None):
                assert gm.create_merge_request("t") is None


def test_create_mr_generic_exception_api_fallback(gm):
    gm.remote_enabled = True
    gm.work_branch = "feature/ok"
    gm.target_branch = "develop"
    with patch.object(gm, "_get_existing_mr_url", return_value=None):
        with patch.object(gm, "_run_glab", side_effect=RuntimeError("boom")):
            with patch.object(
                gm, "_create_mr_via_api", return_value="https://mr/exc"
            ):
                assert gm.create_merge_request("t") == "https://mr/exc"
            with patch.object(gm, "_create_mr_via_api", return_value=None):
                assert gm.create_merge_request("t") is None


def test_get_mr_url_exception(gm):
    gm.remote_enabled = True
    with patch.object(gm, "get_current_branch", side_effect=RuntimeError("x")):
        assert gm.get_mr_url() is None


def test_cleanup_rmtree_fails(gm, tmp_path):
    d = tmp_path / "killme"
    d.mkdir()
    gm.temp_dir = d
    with patch("src.git_manager.settings") as s:
        s.temp_cleanup_policy = "always"
        with patch("src.git_manager.shutil.rmtree", side_effect=OSError("busy")):
            assert gm.cleanup() is False


def test_delete_local_branch_switch_paths(gm):
    with patch.object(gm, "_branch_exists", return_value=True):
        with patch.object(gm, "get_current_branch", return_value="feature/x"):
            with patch.object(gm, "_checkout_default_branch", return_value=True):
                with patch.object(gm, "_run_git", return_value=_cp()):
                    assert gm._delete_local_branch("feature/x") is True


def test_branch_exists_remote_only(gm):
    with patch.object(
        gm,
        "_run_git",
        side_effect=[_cp(returncode=1), _cp(returncode=0)],
    ):
        assert gm._branch_exists("remote-only", check_remote=True) is True
