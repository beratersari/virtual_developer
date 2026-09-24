"""Issue-report zip must not copy CODEX_API_KEY.

Asserts the safe outcome. Fails while environ.json keeps the raw key.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from src.dashboard.issue_logs import IssueLogRing
from src.dashboard.issue_report import build_issue_report_zip
from src.dashboard.schemas import IssueReportRequest
from src.state.job_store import JobStore

_SECRET = "jira-token-SECRETVALUE"


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


def test_issue_report_omits_codex_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", _SECRET)
    monkeypatch.setattr(
        "src.dashboard.issue_report.issue_log_ring",
        IssueLogRing(maxlen=20, persist=False),
    )
    payload, _name = build_issue_report_zip(
        IssueReportRequest(kind="general", note="operator report"),
        store=JobStore(jobs_dir=tmp_path / "jobs"),
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        text = "\n".join(
            zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()
        )
    assert _SECRET not in text
