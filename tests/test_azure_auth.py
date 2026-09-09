"""Azure PAT auth must match Creasy: Basic pat:<PAT> for REST, git, and Test."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

from src.azure.auth import azure_basic_auth, azure_basic_user
from src.azure.client import AzureDevOpsClient
from src.azure_connection import _candidate_bases, probe_azure_connection


def test_azure_basic_auth_is_not_empty_username():
    assert azure_basic_user() == "pat"
    assert azure_basic_user("") == "pat"
    header = azure_basic_auth("secret-pat")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("ascii")
    assert decoded == "pat:secret-pat"
    assert not decoded.startswith(":")


def test_candidate_bases_try_collection_before_origin():
    bases = _candidate_bases("tfs.example.com")
    assert bases[0].endswith("/tfs/DefaultCollection")
    assert bases[-1] == "https://tfs.example.com"
    with_path = _candidate_bases("https://tfs.example.com/tfs/MyCol")
    assert with_path[0] == "https://tfs.example.com/tfs/MyCol"


def test_probe_azure_skips_host_root_401_then_succeeds(monkeypatch):
    """Settings Test used to return 401 on https://host/_apis before /tfs/Col."""

    captured = {}

    class FakeResp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self.content = b"{}" if payload is not None else b""
            self.text = ""
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            captured["auth"] = (k.get("headers") or {}).get("Authorization", "")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            if "/tfs/DefaultCollection/_apis/connectionData" in url:
                return FakeResp(
                    200,
                    {
                        "authenticatedUser": {
                            "id": "u1",
                            "uniqueName": "bot@corp",
                            "displayName": "Bot",
                        }
                    },
                )
            if "/tfs/DefaultCollection/_apis/projects" in url:
                return FakeResp(200, {"value": [{"id": "p1", "name": "Demo"}]})
            return FakeResp(401)

    monkeypatch.setattr("src.azure_connection.httpx.Client", FakeClient)
    out = probe_azure_connection("tfs.example.com", pat="valid-pat")
    assert out["ok"] is True
    assert out["user"]["username"] == "bot@corp"
    assert out["project_count"] == 1
    decoded = base64.b64decode(captured["auth"].split(" ", 1)[1]).decode("ascii")
    assert decoded == "pat:valid-pat"


def test_probe_azure_all_401_is_unauthorized(monkeypatch):
    class FakeResp:
        status_code = 401
        content = b""
        text = "denied"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr("src.azure_connection.httpx.Client", FakeClient)
    out = probe_azure_connection("tfs.example.com", pat="bad-pat")
    assert out["ok"] is False
    assert out["http_status"] == 401
    assert "401" in out["error"]


def test_azure_create_pull_request_uses_pat_basic(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 201
        content = b'{"pullRequestId": 12}'
        text = '{"pullRequestId": 12}'

        def json(self):
            return {
                "pullRequestId": 12,
                "_links": {"web": {"href": "https://tfs.example.com/pr/12"}},
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            captured["list_headers"] = headers
            empty = MagicMock()
            empty.status_code = 200
            empty.content = b'{"value":[]}'
            empty.json.return_value = {"value": []}
            return empty

        def post(self, url, headers=None, params=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("src.azure.client.httpx.Client", FakeClient)
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat="azpat-test",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    url = c.create_pull_request(
        project="Demo",
        repository="demo",
        source_branch="feature/x",
        target_branch="develop",
        title="feat: x",
        description="body",
    )
    assert url == "https://tfs.example.com/pr/12"
    decoded = base64.b64decode(captured["headers"]["Authorization"][6:]).decode()
    assert decoded == "pat:azpat-test"
    assert captured["json"]["sourceRefName"] == "refs/heads/feature/x"
    assert captured["json"]["targetRefName"] == "refs/heads/develop"


def test_git_manager_create_mr_routes_azure_to_rest(monkeypatch, tmp_path):
    from src.config import Settings
    from src.git_manager import GitManager

    s = Settings()
    s.set_azure_host_pat_map({"tfs.example.com": "AZURE-SECRET-PAT"})
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    gm.remote_enabled = True
    gm.work_branch = "feature/login"
    gm.target_branch = "develop"
    gm.source_branch = "feature/login"
    gm.get_current_branch = lambda: "feature/login"
    called = {}

    def fake_create(**kwargs):
        called.update(kwargs)
        return "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/pullrequest/9"

    class FakeClient:
        def create_pull_request(self, **kwargs):
            return fake_create(**kwargs)

    monkeypatch.setattr(gm, "_azure_client_for_remote", lambda: FakeClient())
    out = gm.create_merge_request("feat: login", "body", target_branch="develop")
    assert out and out.endswith("/pullrequest/9")
    assert called["source_branch"] == "feature/login"
    assert called["target_branch"] == "develop"
    assert called["project"] == "Demo"
    assert called["repository"] == "demo"


def test_https_url_with_settings_pat_uses_pat_user(monkeypatch):
    from src.config import Settings
    from src.git_manager import GitManager

    s = Settings()
    s.set_azure_host_pat_map({"tfs.example.com": "ab+c/d="})
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    url = gm._https_url_with_settings_pat()
    assert url is not None
    assert "pat:ab%2Bc%2Fd%3D@" in url
    assert "oauth2:" not in url
    assert not url.startswith("https://:")
