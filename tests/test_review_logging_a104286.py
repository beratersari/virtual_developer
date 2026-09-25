"""Diagnostic zip must not keep sibling secrets that 09ac425 still misses.

Known Jira/GitLab/Azure tokens are stripped when they match settings.
This asserts the leftovers: Anthropic key and auth token, dashboard
password, and a hyphenated Authorization Basic value in a copied log.
Non-secret host, project, and serve URL text must stay readable.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from src.config import settings
from src.dashboard.issue_logs import IssueLogRing
from src.dashboard.issue_report import build_issue_report_zip
from src.dashboard.schemas import IssueReportRequest
from src.state.job_store import JobStore

_ANTHROPIC = "sk-ant-a104286-UNIQUE"
_ANTHROPIC_AUTH = "anth-auth-a104286-UNIQUE"
_CODEX = "codex-key-a104286-UNIQUE"
_DASH_PASS = "dash-pass-a104286-UNIQUE"
_BASIC = "basic-a104286-UNIQUE"
_HOST = "https://jira.a104286.example"
_PROJECTS = "KAN-A104286"
_SERVE = "http://127.0.0.1:4096"


@pytest.fixture(autouse=True)
def _fast_serve_probes(monkeypatch):
    def fake_get(base_url, path, *, params=None):
        return {
            "url": f"{base_url}{path}",
            "http_status": 200,
            "body": {"ok": True, "path": path},
        }

    monkeypatch.setattr("src.dashboard.issue_report._serve_get", fake_get)
    monkeypatch.setattr(
        "src.dashboard.issue_report._serve_provider_ids",
        lambda _url: {"models": []},
    )
    monkeypatch.setattr(
        "src.dashboard.issue_report._serve_safe_config",
        lambda _url: {"plugin": [], "autoupdate": False},
    )


def test_issue_report_log_still_has_sibling_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ANTHROPIC)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", _ANTHROPIC_AUTH)
    monkeypatch.setenv("CODEX_API_KEY", _CODEX)
    monkeypatch.setenv("DASHBOARD_PASSWORD", _DASH_PASS)
    monkeypatch.setenv("JIRA_HOST", _HOST)
    monkeypatch.setenv("JIRA_PROJECTS", _PROJECTS)
    monkeypatch.setenv("OPENCODE_SERVE_URL", _SERVE)
    monkeypatch.setattr(settings, "anthropic_auth_token", _ANTHROPIC_AUTH, raising=False)
    monkeypatch.setattr(settings, "dashboard_password", _DASH_PASS, raising=False)

    root = tmp_path / "yaver"
    monkeypatch.setenv("YAVER_DATA_DIR", str(root))
    log_dir = root / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log").write_text(
        "\n".join(
            [
                f"ANTHROPIC_API_KEY={_ANTHROPIC}",
                f"ANTHROPIC_AUTH_TOKEN={_ANTHROPIC_AUTH}",
                f"CODEX_API_KEY={_CODEX}",
                f"DASHBOARD_PASSWORD={_DASH_PASS}",
                f"Authorization: Basic {_BASIC}",
                f"using host {_HOST} projects {_PROJECTS} serve {_SERVE}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.dashboard.issue_report.issue_log_ring",
        IssueLogRing(maxlen=20, persist=False),
    )
    payload, _name = build_issue_report_zip(
        IssueReportRequest(kind="general", note="operator report"),
        store=JobStore(jobs_dir=tmp_path / "jobs"),
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        parts = [
            (name, zf.read(name).decode("utf-8", errors="replace"))
            for name in zf.namelist()
        ]
    text = "\n".join(body for _name, body in parts)
    assert _HOST in text
    assert _PROJECTS in text
    assert _SERVE in text
    leaked = [
        f"{label} in {fname}"
        for label, secret in (
            ("ANTHROPIC_API_KEY", _ANTHROPIC),
            ("ANTHROPIC_AUTH_TOKEN", _ANTHROPIC_AUTH),
            ("CODEX_API_KEY", _CODEX),
            ("DASHBOARD_PASSWORD", _DASH_PASS),
            ("Authorization", _BASIC),
        )
        for fname, body in parts
        if secret in body
    ]
    assert not leaked, " | ".join(leaked)
