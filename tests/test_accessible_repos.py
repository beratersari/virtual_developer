"""Saved projects filled from GitLab and Azure DevOps Server tokens."""

from __future__ import annotations

import json

from src.azure.client import AzureDevOpsClient
from src.dashboard.accessible_repos import import_accessible_repositories


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        raw = json.dumps(payload).encode()
        self.content = raw
        self.text = raw.decode()

    def json(self):
        return self._payload


class _Http:
    def __init__(self, handler):
        self._handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None, params=None):
        return self._handler(url, params or {})


def test_azure_repository_list_accepts_server_2020_api(monkeypatch):
    """2022's 7.1 is rejected; Azure DevOps Server 2020 answers 6.0."""
    calls = []

    def handler(url, params):
        ver = params.get("api-version")
        calls.append(ver)
        if ver != "6.0":
            return _Resp(400, {"message": "api version not supported"})
        return _Resp(
            200,
            {
                "value": [
                    {
                        "name": "orders",
                        "remoteUrl": "https://tfs.example/tfs/DefaultCollection/App/_git/orders",
                        "defaultBranch": "refs/heads/main",
                        "project": {"name": "App"},
                    }
                ]
            },
        )

    monkeypatch.setattr(
        "src.azure.client.httpx.Client",
        lambda *args, **kwargs: _Http(handler),
    )
    client = AzureDevOpsClient(
        collection_url="https://tfs.example/tfs/DefaultCollection",
        pat="tok",
    )
    rows = client.list_git_repositories()
    assert calls[:4] == ["7.1", "7.0", "6.1", "6.0"]
    assert rows == [
        {
            "label": "App/orders",
            "url": "https://tfs.example/tfs/DefaultCollection/App/_git/orders",
            "target_branch": "main",
            "source_branch": "",
        }
    ]


def test_azure_repository_list_falls_back_to_each_project(monkeypatch):
    client = AzureDevOpsClient(
        collection_url="https://tfs.example/tfs/DefaultCollection",
        pat="tok",
    )

    def rows(self, url):
        if url.endswith("/_apis/git/repositories") and "/Demo/" not in url:
            return None
        return [
            {
                "name": "web",
                "remoteUrl": "http://tfs.example/tfs/DefaultCollection/Demo/_git/web",
                "defaultBranch": "refs/heads/develop",
                "project": {"name": "Demo"},
            }
        ]

    monkeypatch.setattr(AzureDevOpsClient, "_git_repository_rows", rows)
    monkeypatch.setattr(AzureDevOpsClient, "list_projects", lambda self: ["Demo"])
    got = client.list_git_repositories()
    assert got[0]["url"].startswith("http://tfs.example/")
    assert got[0]["target_branch"] == "develop"


def test_unreachable_azure_collection_is_reported(monkeypatch):
    from src import config as config_mod

    class _Down:
        def __init__(self, collection_url="", pat=""):
            self.pat = "tok"
            self.last_error = "GET https://tfs.example/tfs/Col/_apis/git/repositories err=getaddrinfo failed"

        def list_git_repositories(self):
            return []

    monkeypatch.setattr(
        config_mod.Settings,
        "azure_collection_pat_map",
        lambda self: {"https://tfs.example/tfs/Col": "tok"},
    )
    monkeypatch.setattr("src.azure.client.AzureDevOpsClient", _Down)
    from src.dashboard.accessible_repos import list_azure_repositories

    rows, errors = list_azure_repositories()
    assert rows == []
    assert errors and "getaddrinfo failed" in errors[0]


def test_import_keeps_saved_rows_and_adds_token_repos(monkeypatch):
    from src import config as config_mod

    monkeypatch.setattr(config_mod.settings, "project_repositories", "[]")
    monkeypatch.setattr(
        "src.dashboard.accessible_repos.list_gitlab_repositories",
        lambda: (
            [
                {
                    "label": "acme/api",
                    "url": "https://gitlab.example/acme/api.git",
                    "target_branch": "main",
                    "source_branch": "",
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        "src.dashboard.accessible_repos.list_azure_repositories",
        lambda: (
            [
                {
                    "label": "App/orders",
                    "url": "https://tfs.example/tfs/DefaultCollection/App/_git/orders",
                    "target_branch": "main",
                    "source_branch": "",
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        "src.dashboard.accessible_repos.save_runtime_settings",
        lambda updates: None,
    )
    result = import_accessible_repositories(
        [
            {
                "label": "kept",
                "url": "https://gitlab.example/acme/api.git",
                "target_branch": "develop",
                "source_branch": "",
            }
        ]
    )
    urls = [row["url"] for row in result["project_repositories"]]
    assert urls[0] == "https://gitlab.example/acme/api.git"
    assert result["project_repositories"][0]["label"] == "kept"
    assert result["project_repositories"][0]["target_branch"] == "develop"
    assert "https://tfs.example/tfs/DefaultCollection/App/_git/orders" in urls
    assert result["added"] == 1
    assert result["gitlab"] == 1
    assert result["azure"] == 1
