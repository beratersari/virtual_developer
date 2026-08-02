"""Per-host GitLab PAT resolution and git allowlist."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.git_manager import GitCloneError, GitManager


def test_legacy_single_pat_expands_to_hosts():
    s = Settings(
        gitlab_host_pats="",
        gitlab_pat="legacy-pat",
        gitlab_allowed_hosts="gitlab.com, gitlab.internal",
    )
    m = s.gitlab_host_pat_map()
    assert m["gitlab.com"] == "legacy-pat"
    assert m["gitlab.internal"] == "legacy-pat"
    assert s.gitlab_pat_for_host("gitlab.com") == "legacy-pat"


def test_json_map_preferred_over_legacy():
    s = Settings(
        gitlab_host_pats='{"gitlab.com":"pat-a","g.internal":"pat-b"}',
        gitlab_pat="legacy-should-not-win",
        gitlab_allowed_hosts="other",
    )
    assert s.gitlab_pat_for_host("gitlab.com") == "pat-a"
    assert s.gitlab_pat_for_host("g.internal") == "pat-b"
    assert s.gitlab_pat_for_host("other") == ""


def test_subdomain_match():
    s = Settings(gitlab_host_pats='{"gitlab.example.com":"pat-x"}')
    assert s.gitlab_pat_for_host("gitlab.example.com") == "pat-x"
    assert s.gitlab_pat_for_host("api.gitlab.example.com") == "pat-x"
    assert s.gitlab_pat_for_host("evil.com") == ""


def test_set_gitlab_host_pat_map_mirrors_legacy():
    s = Settings()
    s.set_gitlab_host_pat_map(
        {"gitlab.com": "a", "g.local": "b"}
    )
    assert "gitlab.com" in s.gitlab_host_pats
    assert set(s.gitlab_allowed_hosts_list) == {"gitlab.com", "g.local"}
    # multi-host clears single legacy PAT
    assert s.gitlab_pat == ""
    s.set_gitlab_host_pat_map({"only.com": "solo"})
    assert s.gitlab_pat == "solo"


def test_git_manager_picks_pat_by_host(monkeypatch):
    from src import git_manager as gm_mod

    monkeypatch.setattr(
        gm_mod.settings,
        "gitlab_host_pats",
        '{"gitlab.com":"pat-cloud","corp.gitlab":"pat-corp"}',
    )
    monkeypatch.setattr(gm_mod.settings, "gitlab_pat", "")
    monkeypatch.setattr(gm_mod.settings, "gitlab_allowed_hosts", "")

    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://corp.gitlab/group/repo.git"
    assert gm._pat_for_remote() == "pat-corp"
    gm.remote_url = "https://gitlab.com/org/app.git"
    assert gm._pat_for_remote() == "pat-cloud"


def test_git_manager_refuses_unknown_host_when_pats_configured(monkeypatch):
    from src import git_manager as gm_mod

    monkeypatch.setattr(
        gm_mod.settings,
        "gitlab_host_pats",
        '{"gitlab.com":"pat-cloud"}',
    )
    monkeypatch.setattr(gm_mod.settings, "gitlab_pat", "")
    monkeypatch.setattr(gm_mod.settings, "gitlab_allowed_hosts", "")

    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://evil.example/repo.git"
    with pytest.raises(GitCloneError) as ei:
        gm._assert_remote_host_allowed(gm.remote_url)
    assert "evil.example" in str(ei.value.user_message)
