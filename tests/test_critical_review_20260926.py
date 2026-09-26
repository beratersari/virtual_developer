"""Critical failures found on origin/develop at b6a26b9.

Each test fails on that tree and passes once the matching path is fixed.
"""

import json
import subprocess
from unittest.mock import patch

from src.git_manager import GitCancelledError, GitManager


def test_cancel_during_submodule_set_url_scrubs_origin(tmp_path, monkeypatch):
    """set-url can finish, then cancel raises before the scrub flag is set.

    The kept clone must not keep ``oauth2:<PAT>`` in ``origin``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    clean = "https://gitlab.example.com/acme/demo.git"
    subprocess.run(
        ["git", "remote", "add", "origin", clean],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / ".gitmodules").write_text('[submodule "x"]\n', encoding="utf-8")
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="KAN-1")
    gm.temp_dir = repo
    gm.remote_url = clean
    gm._init_proc_state()
    monkeypatch.setattr(gm, "_pat_for_remote", lambda *_a, **_k: "super-secret-pat")
    real_tracked = gm._run_tracked

    def tracked(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        if "set-url" in text and "super-secret-pat" in text:
            subprocess.run(
                cmd,
                cwd=kwargs.get("cwd") or repo,
                check=True,
                capture_output=True,
            )
            raise GitCancelledError("git cancelled")
        return real_tracked(cmd, **kwargs)

    monkeypatch.setattr(gm, "_run_tracked", tracked)
    try:
        gm._update_submodules(reason="after clone")
    except GitCancelledError:
        pass
    shown = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "super-secret-pat" not in shown
    assert shown == clean

def test_gitlab_scheme_key_authenticates_client(monkeypatch):
    """A map key saved as ``https://host`` must still auth API calls.

    Settings re-save already accepts that key. ``GitlabClient`` looks up
    the bare host and, because the map is non-empty, skips leftover GITLAB_PAT.
    """
    from src.config import Settings
    from src.gitlab.client import GitlabClient

    s = Settings()
    s.gitlab_host_pats = json.dumps(
        {"https://gitlab.example.com": "glpat-secret"}
    )
    s.gitlab_pat = ""
    monkeypatch.setattr("src.gitlab.client.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    client = GitlabClient(host="gitlab.example.com")
    assert client.pat == "glpat-secret"

