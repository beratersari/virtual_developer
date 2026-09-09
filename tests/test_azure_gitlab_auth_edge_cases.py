"""50 edge cases: Azure/GitLab PAT auth, Settings Test, clone/push env, MR/PR.

Azure must stay the GitLab shape with username ``pat`` instead of ``oauth2``.
IIS rejects an empty username. GitLab leftover PAT never authenticates TFS.
"""

from __future__ import annotations

import base64
import os
import subprocess
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import httpx
import pytest

from src.azure.auth import azure_basic_auth, azure_basic_auth_header, azure_basic_user
from src.azure.client import AzureDevOpsClient
from src.azure.webhook import parse_azure_git_url
from src.azure_connection import _candidate_bases, _normalize_host, probe_azure_connection
from src.config import Settings
from src.git_manager import GitManager


AZURE_URL = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
GITLAB_URL = "https://gitlab.example.com/acme/demo.git"
AZ_PAT = "AZURE-EDGE-PAT"
GL_PAT = "GL-EDGE-PAT"


def _decode_basic(header: str) -> str:
    token = header.split()[-1]
    return base64.b64decode(token).decode("utf-8")


def _gm(monkeypatch, *, remote: str, azure_map=None, gitlab_map=None, **extra):
    s = Settings()
    s.set_azure_host_pat_map(azure_map or {})
    s.set_gitlab_host_pat_map(gitlab_map or {})
    for key, value in extra.items():
        setattr(s, key, value)
    monkeypatch.setattr("src.git_manager.settings", s)
    gm = GitManager.__new__(GitManager)
    gm.remote_url = remote
    gm.remote_enabled = True
    gm.work_branch = "feature/edge"
    gm.target_branch = "develop"
    gm.source_branch = "feature/edge"
    gm.temp_dir = None
    return gm, s


def _env_pairs(env: Dict[str, str]) -> List[tuple[str, str]]:
    try:
        count = int(env.get("GIT_CONFIG_COUNT") or "0")
    except ValueError:
        count = 0
    out = []
    for i in range(count):
        out.append((env.get(f"GIT_CONFIG_KEY_{i}", ""), env.get(f"GIT_CONFIG_VALUE_{i}", "")))
    return out


class _Resp:
    def __init__(self, status: int, payload: Any = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = text

    def json(self):
        return self._payload


def _httpx_client(get_fn, post_fn=None, captured=None):
    class Fake:
        def __init__(self, *a, **k):
            if captured is not None:
                captured["headers"] = (k.get("headers") or {})
                captured["verify"] = k.get("verify")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            return get_fn(url, headers, params)

        def post(self, url, headers=None, params=None, json=None):
            if post_fn is None:
                return _Resp(500)
            return post_fn(url, headers, params, json)

    return Fake


# --- 01–10 auth encoding (GitLab shape, username pat) ---


def test_01_empty_username_becomes_pat():
    assert azure_basic_user() == "pat"
    assert azure_basic_user("") == "pat"
    assert azure_basic_user("   ") == "pat"


def test_02_custom_username_is_kept():
    assert azure_basic_user("buildsvc") == "buildsvc"
    assert _decode_basic(azure_basic_auth("tok", "buildsvc")) == "buildsvc:tok"


def test_03_basic_is_pat_colon_token_not_empty_user():
    decoded = _decode_basic(azure_basic_auth("secret-pat"))
    assert decoded == "pat:secret-pat"
    assert not decoded.startswith(":")
    assert "oauth2" not in decoded


def test_04_header_alias_matches_auth():
    assert azure_basic_auth_header("x") == azure_basic_auth("x")


def test_05_empty_token_still_has_pat_user():
    assert _decode_basic(azure_basic_auth("")) == "pat:"
    assert _decode_basic(azure_basic_auth("   ")) == "pat:"


def test_06_special_chars_stay_raw_in_basic():
    token = "ab+c/d="
    assert _decode_basic(azure_basic_auth(token)) == f"pat:{token}"


def test_07_client_headers_use_same_basic(monkeypatch):
    c = AzureDevOpsClient(host="tfs.example.com", pat="tok-1")
    assert c._headers()["Authorization"] == azure_basic_auth("tok-1")
    assert _decode_basic(c._headers()["Authorization"]) == "pat:tok-1"


def test_08_client_strips_pat_whitespace():
    c = AzureDevOpsClient(host="tfs.example.com", pat="  tok-2  ")
    assert c.pat == "tok-2"


def test_09_client_no_pat_omits_authorization():
    c = AzureDevOpsClient(host="tfs.example.com", pat="")
    assert "Authorization" not in c._headers()


def test_10_verify_false_is_product_tls(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, *a, **k):
            seen["verify"] = k.get("verify")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            r = _Resp(200, {})
            return r

    monkeypatch.setattr("src.azure.client.httpx.Client", Fake)
    AzureDevOpsClient(
        host="tfs.example.com",
        pat="t",
        collection_url="https://tfs.example.com/tfs/Col",
    ).get_pull_request("Demo", "demo", 1)
    assert seen["verify"] is False


# --- 11–22 git env: same shape, different username ---


def test_11_azure_git_env_basic_pat_not_bearer(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    values = [v for _, v in _env_pairs(env)]
    assert env["VD_GIT_AUTH"] == "azure"
    assert env["VD_GIT_ASKUSER"] == "pat"
    assert env["VD_GIT_PASSWORD"] == AZ_PAT
    assert any(v == f"Authorization: {azure_basic_auth(AZ_PAT)}" for v in values)
    assert not any("Authorization: Bearer" in v for v in values)
    assert not any("oauth2:" in k or "oauth2:" in v for k, v in _env_pairs(env))


def test_12_gitlab_git_env_oauth2_basic(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=GITLAB_URL, gitlab_map={"gitlab.example.com": GL_PAT})
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    keys = [k for k, _ in _env_pairs(env)]
    values = [v for _, v in _env_pairs(env)]
    assert env["VD_GIT_AUTH"] == "gitlab"
    assert env["VD_GIT_ASKUSER"] == "oauth2"
    assert any(f"oauth2:{GL_PAT}@" in k for k in keys)
    assert any(v.startswith("Authorization: Basic ") for v in values)
    assert _decode_basic(next(v for v in values if v.startswith("Authorization: Basic "))) == (
        f"oauth2:{GL_PAT}"
    )
    assert not any("Authorization: Bearer" in v for v in values)
    assert not any("pat:" in k for k in keys)


def test_13_azure_insteadOf_uses_pat_user(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    keys = [k for k, _ in _env_pairs(gm._apply_pat_to_git_env(gm._base_git_env()))]
    assert any(k.startswith(f"url.https://pat:{AZ_PAT}@tfs.example.com/") for k in keys)
    assert not any("https://:" in k for k in keys)


def test_14_azure_special_pat_is_quoted_in_url_not_header(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    token = "ab+c/d="
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": token})
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    keys = [k for k, _ in _env_pairs(env)]
    values = [v for _, v in _env_pairs(env)]
    assert any("pat:ab%2Bc%2Fd%3D@" in k for k in keys)
    header = next(v for v in values if v.startswith("Authorization: Basic "))
    assert _decode_basic(header) == f"pat:{token}"


def test_15_gitlab_pat_never_authenticates_tfs(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(
        monkeypatch,
        remote=AZURE_URL,
        gitlab_map={"gitlab.example.com": GL_PAT},
        gitlab_pat=GL_PAT,
    )
    assert gm._pat_for_remote() == ""
    assert gm._remote_uses_azure_pat() is False
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    assert env.get("VD_GIT_PASSWORD") != GL_PAT
    assert "VD_GIT_AUTH" not in env or env.get("VD_GIT_PASSWORD") in (None, "")


def test_16_azure_pat_never_authenticates_gitlab(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(
        monkeypatch,
        remote=GITLAB_URL,
        azure_map={"tfs.example.com": AZ_PAT},
        azure_pat=AZ_PAT,
    )
    assert gm._pat_for_remote() == ""
    assert AZ_PAT not in str(gm._apply_pat_to_git_env(gm._base_git_env()))


def test_17_leftover_azure_pat_when_map_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_pat="LEFTOVER-AZ")
    assert gm._pat_for_remote() == "LEFTOVER-AZ"
    assert gm._remote_uses_azure_pat() is True


def test_18_leftover_gitlab_pat_when_map_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=GITLAB_URL, gitlab_pat="LEFTOVER-GL")
    assert gm._pat_for_remote() == "LEFTOVER-GL"
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    assert env["VD_GIT_AUTH"] == "gitlab"


def test_19_host_with_nondefault_port(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    url = "https://tfs.example.com:8080/tfs/Col/Demo/_git/demo"
    gm, _ = _gm(monkeypatch, remote=url, azure_map={"tfs.example.com:8080": AZ_PAT})
    assert gm._pat_for_remote() == AZ_PAT
    keys = [k for k, _ in _env_pairs(gm._apply_pat_to_git_env(gm._base_git_env()))]
    assert any("tfs.example.com:8080" in k for k in keys)


def test_20_gcm_and_prompt_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    env = gm._apply_pat_to_git_env(gm._base_git_env())
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_SSL_NO_VERIFY"] == "1"
    pairs = _env_pairs(env)
    assert any(k == "credential.helper" and v == "" for k, v in pairs)
    assert any(k == "http.sslVerify" and v == "false" for k, v in pairs)


def test_21_azure_argv_extraheader_is_basic_pat(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    args = gm._azure_git_config_args()
    header = next(a for a in args if a.startswith("http.extraHeader="))
    assert header == f"http.extraHeader=Authorization: {azure_basic_auth(AZ_PAT)}"
    assert "Bearer" not in header


def test_22_gitlab_remote_has_no_azure_argv_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    gm, _ = _gm(monkeypatch, remote=GITLAB_URL, gitlab_map={"gitlab.example.com": GL_PAT})
    assert gm._azure_git_config_args() == []


# --- 23–26 askpass ---


def test_23_azure_askpass_prints_pat_then_token(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    path = GitManager._ensure_askpass_script()
    assert path is not None
    env = os.environ.copy()
    env["VD_GIT_AUTH"] = "azure"
    env["VD_GIT_ASKUSER"] = "pat"
    env["VD_GIT_PASSWORD"] = AZ_PAT
    prefix = ["cmd.exe", "/c", "call", str(path)] if path.suffix.lower() == ".cmd" else ["sh", str(path)]
    user = subprocess.run([*prefix, "Username for 'https://tfs':"], capture_output=True, text=True, env=env, timeout=15)
    pw = subprocess.run([*prefix, "Password:"], capture_output=True, text=True, env=env, timeout=15)
    assert user.returncode == 0
    assert pw.returncode == 0
    assert user.stdout.strip() == "pat"
    assert pw.stdout.strip() == AZ_PAT


def test_24_gitlab_askpass_prints_oauth2_then_token(monkeypatch, tmp_path):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "d"))
    path = GitManager._ensure_askpass_script()
    assert path is not None
    env = os.environ.copy()
    env["VD_GIT_AUTH"] = "gitlab"
    env["VD_GIT_PASSWORD"] = GL_PAT
    prefix = ["cmd.exe", "/c", "call", str(path)] if path.suffix.lower() == ".cmd" else ["sh", str(path)]
    user = subprocess.run(
        [*prefix, "Username for 'https://gitlab':"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert user.returncode == 0
    assert user.stdout.strip() == "oauth2"


def test_25_askpass_wrapper_never_invokes_product_exe():
    content = GitManager._askpass_wrapper_content()
    assert "yaver.exe" not in content.lower()
    assert "sys.executable" not in content
    assert "VD_GIT_PASSWORD" in content


def test_26_https_origin_url_uses_pat_user(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": "ab+c/d="})
    url = gm._https_url_with_settings_pat()
    assert url is not None
    assert "pat:ab%2Bc%2Fd%3D@" in url
    assert "oauth2:" not in url
    assert "://" + ":" not in url.replace("https://", "")


# --- 27–38 Settings Test / probe ---


def test_27_probe_empty_host():
    out = probe_azure_connection("")
    assert out["ok"] is False
    assert "host is required" in out["error"]


def test_28_probe_no_pat(monkeypatch):
    monkeypatch.setattr("src.azure_connection.settings", Settings())
    out = probe_azure_connection("tfs.example.com")
    assert out["ok"] is False
    assert "No PAT" in out["error"]
    assert "pat" not in (out.get("error") or "").lower() or "PAT" in out["error"]


def test_29_probe_never_echoes_token(monkeypatch):
    captured = {}

    def get(url, headers, params):
        if "connectionData" in url and "DefaultCollection" in url:
            return _Resp(200, {"authenticatedUser": {"id": "1", "uniqueName": "bot"}})
        if "projects" in url:
            return _Resp(200, {"value": []})
        return _Resp(401)

    monkeypatch.setattr(
        "src.azure_connection.httpx.Client", _httpx_client(get, captured=captured)
    )
    out = probe_azure_connection("tfs.example.com", pat="super-secret-token")
    assert out["ok"] is True
    blob = str(out)
    assert "super-secret-token" not in blob
    assert _decode_basic(captured["headers"]["Authorization"]) == "pat:super-secret-token"


def test_30_probe_origin_401_then_collection_200(monkeypatch):
    def get(url, headers, params):
        if "/tfs/DefaultCollection/_apis/connectionData" in url:
            return _Resp(200, {"authenticatedUser": {"id": "u", "displayName": "Bot"}})
        if "/tfs/DefaultCollection/_apis/projects" in url:
            return _Resp(200, {"value": [{"id": "p", "name": "Demo"}]})
        return _Resp(401)

    monkeypatch.setattr("src.azure_connection.httpx.Client", _httpx_client(get))
    out = probe_azure_connection("tfs.example.com", pat="ok")
    assert out["ok"] is True
    assert out["collection_url"].endswith("/tfs/DefaultCollection")
    assert out["project_count"] == 1


def test_31_probe_all_401(monkeypatch):
    monkeypatch.setattr(
        "src.azure_connection.httpx.Client",
        _httpx_client(lambda *a: _Resp(401)),
    )
    out = probe_azure_connection("tfs.example.com", pat="bad")
    assert out["ok"] is False
    assert out["http_status"] == 401


def test_32_probe_403_wins_over_earlier_401(monkeypatch):
    def get(url, headers, params):
        if "DefaultCollection" in url:
            return _Resp(403)
        return _Resp(401)

    monkeypatch.setattr("src.azure_connection.httpx.Client", _httpx_client(get))
    out = probe_azure_connection("tfs.example.com", pat="scoped")
    assert out["ok"] is False
    assert out["http_status"] == 403
    assert "403" in out["error"]


def test_33_probe_timeout(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise httpx.TimeoutException("slow")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.azure_connection.httpx.Client", Boom)
    out = probe_azure_connection("tfs.example.com", pat="x")
    assert out["ok"] is False
    assert "Timed out" in out["error"]


def test_34_probe_http_error(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise httpx.ConnectError("no route")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.azure_connection.httpx.Client", Boom)
    out = probe_azure_connection("tfs.example.com", pat="x")
    assert out["ok"] is False
    assert "HTTP error" in out["error"]


def test_35_candidate_bases_collection_before_origin():
    bases = _candidate_bases("tfs.example.com")
    assert bases[0].endswith("/tfs/DefaultCollection")
    assert bases[-1] == "https://tfs.example.com"
    custom = _candidate_bases("https://tfs.example.com/tfs/MyCol")
    assert custom[0] == "https://tfs.example.com/tfs/MyCol"


def test_36_candidate_bases_localhost_is_http():
    bases = _candidate_bases("127.0.0.1:8080")
    assert all(b.startswith("http://") for b in bases)
    assert any(b.endswith("/tfs/DefaultCollection") for b in bases)


def test_37_normalize_host_port_and_scheme():
    assert _normalize_host("TFS.Example.COM") == "tfs.example.com"
    assert _normalize_host("https://tfs.example.com:8080/tfs/Col") == "tfs.example.com:8080"
    assert _normalize_host("") == ""


def test_38_probe_uses_stored_host_pat(monkeypatch):
    s = Settings()
    s.set_azure_host_pat_map({"tfs.example.com": "STORED-PAT"})
    monkeypatch.setattr("src.azure_connection.settings", s)
    captured = {}

    def get(url, headers, params):
        if "DefaultCollection" in url and "connectionData" in url:
            return _Resp(200, {"authenticatedUser": {"id": "1", "uniqueName": "bot"}})
        if "projects" in url:
            return _Resp(200, {"value": []})
        return _Resp(404)

    monkeypatch.setattr(
        "src.azure_connection.httpx.Client", _httpx_client(get, captured=captured)
    )
    out = probe_azure_connection("tfs.example.com")
    assert out["ok"] is True
    assert _decode_basic(captured["headers"]["Authorization"]) == "pat:STORED-PAT"
    assert "STORED-PAT" not in str(out)


# --- 39–50 MR / PR create ---


def test_39_azure_create_mr_never_calls_glab(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    called = {}

    def fake(**kwargs):
        called.update(kwargs)
        return "https://tfs.example.com/pr/3"

    class FakeClient:
        def create_pull_request(self, **kwargs):
            return fake(**kwargs)

    monkeypatch.setattr(gm, "_azure_client_for_remote", lambda: FakeClient())
    monkeypatch.setattr(
        gm,
        "_run_glab",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("glab must not run for Azure")),
    )
    out = gm.create_merge_request("feat: x", "body")
    assert out == "https://tfs.example.com/pr/3"
    assert called["project"] == "Demo"
    assert called["repository"] == "demo"


def test_40_gitlab_non_ascii_title_uses_rest_not_glab(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=GITLAB_URL, gitlab_map={"gitlab.example.com": GL_PAT})
    monkeypatch.setattr(gm, "_get_existing_mr_url", lambda *a, **k: None)
    monkeypatch.setattr(
        gm,
        "_create_mr_via_api",
        lambda title, body, src, tgt: "https://gitlab.example.com/acme/demo/-/merge_requests/8",
    )
    monkeypatch.setattr(
        gm,
        "_run_glab",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("glab skipped for Turkish title")),
    )
    out = gm.create_merge_request("feat: giriş", "açıklama")
    assert out and out.endswith("/merge_requests/8")


def test_41_azure_refuses_protected_work_branch(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    gm.work_branch = "develop"
    gm.target_branch = "develop"
    assert gm.create_merge_request("t", "b") is None
    gm.work_branch = "release/1.0"
    gm.target_branch = "main"
    assert gm.create_merge_request("t", "b") is None


def test_42_azure_disabled_remote_returns_none(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    gm.remote_enabled = False
    assert gm.create_merge_request("t", "b") is None


def test_43_azure_missing_target_returns_none(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=AZURE_URL, azure_map={"tfs.example.com": AZ_PAT})
    gm.target_branch = ""
    gm.source_branch = ""
    assert gm.create_merge_request("t", "b", target_branch="") is None


def test_44_azure_reuses_existing_pr(monkeypatch):
    captured = {}

    def get(url, headers, params):
        captured["list"] = params
        return _Resp(
            200,
            {
                "value": [
                    {
                        "pullRequestId": 4,
                        "_links": {"web": {"href": "https://tfs.example.com/pr/4"}},
                    }
                ]
            },
        )

    def post(*a, **k):
        raise AssertionError("must not POST when an active PR exists")

    monkeypatch.setattr("src.azure.client.httpx.Client", _httpx_client(get, post))
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat=AZ_PAT,
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    url = c.create_pull_request(
        project="Demo",
        repository="demo",
        source_branch="feature/edge",
        target_branch="develop",
        title="feat",
    )
    assert url == "https://tfs.example.com/pr/4"
    assert captured["list"]["searchCriteria.sourceRefName"] == "refs/heads/feature/edge"


def test_45_azure_does_not_double_refs_prefix(monkeypatch):
    captured = {}

    def get(url, headers, params):
        return _Resp(200, {"value": []})

    def post(url, headers, params, json):
        captured["json"] = json
        return _Resp(
            201,
            {
                "pullRequestId": 9,
                "_links": {"web": {"href": "https://tfs.example.com/pr/9"}},
            },
        )

    monkeypatch.setattr("src.azure.client.httpx.Client", _httpx_client(get, post))
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat=AZ_PAT,
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    c.create_pull_request(
        project="Demo",
        repository="demo",
        source_branch="refs/heads/feature/x",
        target_branch="refs/heads/develop",
        title="t",
    )
    assert captured["json"]["sourceRefName"] == "refs/heads/feature/x"
    assert captured["json"]["targetRefName"] == "refs/heads/develop"


def test_46_azure_pr_url_fallback_from_id(monkeypatch):
    def get(url, headers, params):
        return _Resp(200, {"value": []})

    def post(url, headers, params, json):
        return _Resp(201, {"pullRequestId": 15})

    monkeypatch.setattr("src.azure.client.httpx.Client", _httpx_client(get, post))
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat=AZ_PAT,
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    url = c.create_pull_request(
        project="Demo",
        repository="demo",
        source_branch="feature/x",
        target_branch="develop",
        title="t",
    )
    assert url == (
        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/pullrequest/15"
    )


def test_47_gitlab_existing_mr_skips_create(monkeypatch):
    gm, _ = _gm(monkeypatch, remote=GITLAB_URL, gitlab_map={"gitlab.example.com": GL_PAT})
    monkeypatch.setattr(
        gm, "_get_existing_mr_url", lambda *a, **k: "https://gitlab.example.com/mr/1"
    )
    monkeypatch.setattr(
        gm,
        "_create_mr_via_api",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no new MR")),
    )
    monkeypatch.setattr(
        gm,
        "_run_glab",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no glab")),
    )
    assert gm.create_merge_request("t", "b") == "https://gitlab.example.com/mr/1"


def test_48_azure_create_without_repo_returns_none(monkeypatch):
    gm, _ = _gm(
        monkeypatch,
        remote="https://tfs.example.com/not-a-git-url",
        azure_map={"tfs.example.com": AZ_PAT},
    )
    # Force Azure path even if URL parse fails.
    monkeypatch.setattr(gm, "_looks_like_azure_remote", lambda *a, **k: True)
    assert gm._create_or_reuse_azure_pr("t", "b", "feature/x", "develop") is None


def test_49_parse_azure_urls_git_suffix_ssh_and_collection_only():
    parsed = parse_azure_git_url(
        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo.git"
    )
    assert parsed is not None
    assert parsed["repository"] == "demo"
    assert parsed["project"] == "Demo"
    ssh = parse_azure_git_url("git@tfs.example.com:tfs/DefaultCollection/Demo/_git/demo")
    assert ssh is not None
    assert ssh["host"] == "tfs.example.com"
    col_only = parse_azure_git_url(
        "https://tfs.example.com/tfs/DefaultCollection/_git/demo"
    )
    assert col_only is not None
    # Two segments before _git: prefix=tfs, project=DefaultCollection.
    # REST still lands on /tfs/DefaultCollection/_apis/git/repositories/demo.
    assert col_only["repository"] == "demo"
    assert "DefaultCollection" in (
        col_only["project"] + "/" + col_only["collection_url"]
    )
    assert parse_azure_git_url("") is None
    assert parse_azure_git_url("https://gitlab.example.com/acme/demo.git") is None


def test_50_redact_does_not_leak_azure_or_gitlab_pat(monkeypatch):
    gm, s = _gm(
        monkeypatch,
        remote=AZURE_URL,
        azure_map={"tfs.example.com": AZ_PAT},
        gitlab_map={"gitlab.example.com": GL_PAT},
    )
    monkeypatch.setattr("src.git_manager.settings", s)
    text = (
        f"fatal: https://pat:{AZ_PAT}@tfs.example.com/x "
        f"https://oauth2:{GL_PAT}@gitlab.example.com/y "
        f"Authorization: {azure_basic_auth(AZ_PAT)}"
    )
    redacted = GitManager._redact_secret_text(text)
    assert AZ_PAT not in redacted
    assert GL_PAT not in redacted
    assert "pat:***@" in redacted or "***" in redacted
