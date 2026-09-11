"""Jira poller trigger-label AND username intake.

When JIRA_TRIGGER_LABEL is empty, To Do + bot assignee is enough.
When it is set, intake needs assignee AND one of those labels.

Uses real settings, real state, a real HTTP Jira Agile server, and the
dashboard Settings API. No MagicMock of poller methods.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.jira.poller import JiraPoller
from src.jira.triggers import (
    issue_has_trigger_label,
    parse_trigger_labels,
    poller_triggers_on,
)
from src.state.manager import JiraStateManager


def test_parse_trigger_labels_splits_and_lowercases():
    assert parse_trigger_labels("Bot, AI-Assist ; bot") == ["bot", "ai-assist"]
    assert parse_trigger_labels(["Bot", " ", "ai-assist"]) == ["bot", "ai-assist"]
    assert parse_trigger_labels(None) == []
    assert parse_trigger_labels(123) == []


def test_issue_has_trigger_label_is_case_insensitive():
    assert issue_has_trigger_label(["Bot"], ["bot"]) is True
    assert issue_has_trigger_label(["other"], ["bot"]) is False
    assert issue_has_trigger_label(["ai-assist"], ["bot", "ai-assist"]) is True
    assert issue_has_trigger_label([], ["bot"]) is False
    assert issue_has_trigger_label(["bot"], []) is False


def test_poller_triggers_on_username_only_when_labels_unset():
    assert poller_triggers_on(assigned_to_bot=True, labels=[], required_labels=[]) is True
    assert poller_triggers_on(assigned_to_bot=True, labels=["x"], required_labels="") is True
    assert poller_triggers_on(assigned_to_bot=False, labels=["bot"], required_labels=[]) is False


def test_poller_triggers_on_and_when_labels_set():
    need = ["bot", "ai-assist"]
    assert (
        poller_triggers_on(
            assigned_to_bot=True, labels=["bot"], required_labels=need
        )
        is True
    )
    assert (
        poller_triggers_on(
            assigned_to_bot=True, labels=["AI-Assist"], required_labels=need
        )
        is True
    )
    assert (
        poller_triggers_on(
            assigned_to_bot=True, labels=["other"], required_labels=need
        )
        is False
    )
    assert (
        poller_triggers_on(
            assigned_to_bot=False, labels=["bot"], required_labels=need
        )
        is False
    )
    assert (
        poller_triggers_on(
            assigned_to_bot=True, labels=[], required_labels=need
        )
        is False
    )


def test_settings_leftover_trigger_labels_used_when_primary_empty(monkeypatch):
    monkeypatch.setattr(settings, "jira_trigger_label", "")
    monkeypatch.setattr(settings, "trigger_labels", "bot, ai-assist")
    assert settings.jira_trigger_label_list == ["bot", "ai-assist"]
    assert "bot" in settings.resolved_jira_trigger_label()


def test_settings_primary_label_wins_over_leftover(monkeypatch):
    monkeypatch.setattr(settings, "jira_trigger_label", "yaver-run")
    monkeypatch.setattr(settings, "trigger_labels", "bot, ai-assist")
    assert settings.jira_trigger_label_list == ["yaver-run"]


def test_poller_triggers_on_reads_live_settings(monkeypatch):
    monkeypatch.setattr(settings, "jira_trigger_label", "bot")
    monkeypatch.setattr(settings, "trigger_labels", "")
    assert poller_triggers_on(assigned_to_bot=True, labels=["bot"]) is True
    assert poller_triggers_on(assigned_to_bot=True, labels=["nope"]) is False
    monkeypatch.setattr(settings, "jira_trigger_label", "")
    assert poller_triggers_on(assigned_to_bot=True, labels=["nope"]) is True


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{host}:{port} did not start")


def _todo(key: str, *, assignee: str | None, labels: list[str]) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": key,
            "labels": labels,
            "assignee": {"displayName": assignee, "name": assignee} if assignee else None,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
        },
    }


class _JiraHandler(BaseHTTPRequestHandler):
    issues: list = []

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.endswith("/board/1/sprint"):
            self._json({"values": [{"id": 11, "name": "S1", "state": "active"}]})
            return
        if "/sprint/11/issue" in path or path.endswith("/board/1/issue"):
            self._json({"issues": list(self.issues), "total": len(self.issues)})
            return
        self._json({"error": path}, status=404)


def _poll(tmp_path, monkeypatch, issues, *, labels_cfg: str) -> list[str]:
    from src.jira.client import JiraClient

    monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
    monkeypatch.setattr(settings, "trigger_assignee_names", "devbot")
    monkeypatch.setattr(settings, "jira_trigger_label", labels_cfg)
    monkeypatch.setattr(settings, "trigger_labels", labels_cfg)
    _JiraHandler.issues = issues
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), _JiraHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    _wait_port(host, int(port))
    try:
        client = JiraClient(host=f"http://{host}:{port}", api_token="t")
        poller = JiraPoller(
            client=client,
            interval_seconds=1,
            board_id="1",
            state_manager=JiraStateManager(state_dir=tmp_path / "state"),
        )
        return [i["key"] for i in poller.poll_board()]
    finally:
        httpd.shutdown()


def test_poll_board_username_only_accepts_assignee_without_label(tmp_path, monkeypatch):
    keys = _poll(
        tmp_path,
        monkeypatch,
        [
            _todo("KAN-1", assignee="DevBot", labels=[]),
            _todo("KAN-2", assignee="Alice", labels=["bot"]),
        ],
        labels_cfg="",
    )
    assert "KAN-1" in keys
    assert "KAN-2" not in keys


def test_poll_board_label_and_username_skips_assignee_without_label(
    tmp_path, monkeypatch
):
    keys = _poll(
        tmp_path,
        monkeypatch,
        [
            _todo("KAN-A", assignee="DevBot", labels=["other"]),
            _todo("KAN-B", assignee="DevBot", labels=["bot"]),
            _todo("KAN-C", assignee="Alice", labels=["bot"]),
            _todo("KAN-D", assignee="DevBot", labels=["AI-Assist"]),
        ],
        labels_cfg="bot, ai-assist",
    )
    assert keys == ["KAN-B", "KAN-D"]


def test_poll_snapshot_records_matched_label(tmp_path, monkeypatch):
    from src.dashboard.snapshot import poll_snapshot_store

    _poll(
        tmp_path,
        monkeypatch,
        [
            _todo("KAN-B", assignee="DevBot", labels=["bot"]),
            _todo("KAN-A", assignee="DevBot", labels=["other"]),
        ],
        labels_cfg="bot",
    )
    rows = {r["key"]: r for r in poll_snapshot_store.snapshot().get("issues") or []}
    assert rows["KAN-B"]["matched_assignee"] is True
    assert rows["KAN-B"]["matched_label"] is True
    assert rows["KAN-B"]["will_process"] is True
    assert rows["KAN-A"]["matched_assignee"] is True
    assert rows["KAN-A"]["matched_label"] is False
    assert rows["KAN-A"]["will_process"] is False


def test_settings_save_label_changes_live_poller(tmp_path, monkeypatch):
    _isolate = tmp_path / "runtime_settings.json"
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: _isolate)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JIRA_TRIGGER_USER=devbot\n", encoding="utf-8")
    monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
    monkeypatch.setattr(settings, "trigger_assignee_names", "devbot")
    monkeypatch.setattr(settings, "trigger_mentions", "devbot")
    monkeypatch.setattr(settings, "jira_trigger_label", "")
    monkeypatch.setattr(settings, "trigger_labels", "")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    http = TestClient(create_dashboard_app(processor=None, state_manager=sm))
    r = http.patch("/api/settings", json={"jira_trigger_label": "bot"})
    assert r.status_code == 200, r.text
    assert settings.jira_trigger_label_list == ["bot"]
    assert poller_triggers_on(assigned_to_bot=True, labels=["bot"]) is True
    assert poller_triggers_on(assigned_to_bot=True, labels=["other"]) is False
    r2 = http.patch("/api/settings", json={"jira_trigger_label": ""})
    assert r2.status_code == 200
    assert settings.jira_trigger_label_list == []
    assert poller_triggers_on(assigned_to_bot=True, labels=["other"]) is True
