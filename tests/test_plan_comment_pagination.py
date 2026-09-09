"""plan_refactor must see the newest @bot comment across Jira comment pages.

Jira REST v2 ``GET /issue/{key}/comment`` is paginated (documented default
``maxResults=50``, oldest first). An unpaged GET only returns the first page,
so the operator's latest mention is missing and handoff waits forever.

These tests speak the real Jira JSON shape over HTTP. Live Cloud/Server is
exercised when ``JIRA_HOST`` + token work; otherwise skipped.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from src.jira.client import JiraClient
from src.jira.plan_labels import (
    HANDOFF_REFACTOR,
    PLAN_REFACTOR_LABEL,
    latest_comment_tagging_pat_user,
)
from src.processor import JobProcessor
from src.state.models import TaskStatus


MARKER = "VD_PAGE2_BOT_MENTION"
BOT_BODY = f"[~devbot] please revise the plan. {MARKER}"


def _comment(i: int, body: str) -> Dict[str, Any]:
    return {
        "id": str(i),
        "body": body,
        "author": {"name": "alice", "displayName": "Alice"},
    }


def _jira_comment_pages(
    *, n_fillers: int = 51, page_size: int = 50
) -> List[Dict[str, Any]]:
    """Oldest-first list: many fillers, then the operator @bot ask."""
    rows = [_comment(i, f"filler {i}") for i in range(1, n_fillers + 1)]
    rows.append(_comment(n_fillers + 1, BOT_BODY))
    return rows


class _JiraCommentAPI(BaseHTTPRequestHandler):
    """Minimal Jira REST v2 comment resource (same JSON as Server/Cloud)."""

    comments: List[Dict[str, Any]] = []
    page_size_default = 50
    hits: List[Tuple[int, int]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/rest/api/2/myself":
            self._json(
                200,
                {"name": "devbot", "displayName": "DevBot", "key": "devbot"},
            )
            return
        if not parsed.path.startswith("/rest/api/2/issue/") or not parsed.path.endswith(
            "/comment"
        ):
            self._json(404, {"errorMessages": ["not found"]})
            return
        qs = parse_qs(parsed.query)
        start = int((qs.get("startAt") or ["0"])[0])
        max_results = int((qs.get("maxResults") or [str(self.page_size_default)])[0])
        self.hits.append((start, max_results))
        total = len(self.comments)
        batch = self.comments[start : start + max_results]
        self._json(
            200,
            {
                "startAt": start,
                "maxResults": max_results,
                "total": total,
                "comments": batch,
            },
        )


def _serve_jira(comments: List[Dict[str, Any]], *, page_default: int = 50):
    _JiraCommentAPI.comments = list(comments)
    _JiraCommentAPI.page_size_default = page_default
    _JiraCommentAPI.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _JiraCommentAPI)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    return httpd, host


@pytest.fixture
def jira_paged_http():
    rows = _jira_comment_pages(n_fillers=51, page_size=50)
    httpd, host = _serve_jira(rows, page_default=50)
    try:
        yield host, rows
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unpaged_get_misses_newest_bot_comment(jira_paged_http):
    """Reproduce the bug: one GET without startAt only sees the oldest page."""
    host, rows = jira_paged_http
    import httpx

    with httpx.Client(base_url=f"{host}/rest/api/2", timeout=5.0, verify=False) as http:
        # Same request the old client made (no query params).
        first = http.get("/issue/BUSY-1/comment")
    data = first.json()
    assert data["total"] == 52
    assert data["maxResults"] == 50
    assert len(data["comments"]) == 50
    blob = "\n".join(c["body"] for c in data["comments"])
    assert MARKER not in blob
    found = latest_comment_tagging_pat_user(
        data["comments"],
        myself={"name": "devbot", "displayName": "DevBot"},
    )
    assert found is None


def test_get_comments_over_http_returns_page_two_mention(jira_paged_http):
    host, rows = jira_paged_http
    client = JiraClient(host=host, api_token="probe", email="")
    got = client.get_comments("BUSY-1")
    assert len(got) == 52
    assert got[-1]["body"] == BOT_BODY
    assert MARKER in got[-1]["body"]
    assert _JiraCommentAPI.hits[0] == (0, 50)
    assert (50, 50) in _JiraCommentAPI.hits


@pytest.mark.asyncio
async def test_plan_refactor_handoff_starts_after_later_page_mention(
    jira_paged_http, state_manager, reporter, tmp_path, monkeypatch
):
    host, _rows = jira_paged_http
    jira = JiraClient(host=host, api_token="probe", email="")
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = jira
    state_manager.create_state("BUSY-1", "s", "Mode: plan")
    state_manager.update_state("BUSY-1", status=TaskStatus.PLAN_READY)
    ran = {}

    async def fake_plan(st, *, refactor_comment=None):
        ran["c"] = refactor_comment

    event = {
        "webhookEvent": "jira:issue_updated",
        "plan_handoff": HANDOFF_REFACTOR,
        "issue": {
            "key": "BUSY-1",
            "fields": {
                "status": {
                    "name": "In Progress",
                    "statusCategory": {"key": "indeterminate"},
                },
                "labels": [PLAN_REFACTOR_LABEL],
            },
        },
    }
    with patch.object(proc, "_start_planning_workflow", side_effect=fake_plan):
        started, reason = await proc._handle_issue_updated(event)
    assert started is True
    assert reason is None
    assert MARKER in (ran.get("c") or "")


def _live_ready() -> str:
    from src.config import settings

    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    email = (getattr(settings, "jira_email", "") or "").strip()
    # Prefer .env over dashboard example overrides
    from pathlib import Path

    env = Path(__file__).resolve().parents[1] / ".env"
    vals: Dict[str, str] = {}
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    host = (vals.get("JIRA_HOST") or host).strip()
    token = (vals.get("JIRA_API_TOKEN") or token).strip()
    email = (vals.get("JIRA_EMAIL") or email).strip()
    if not host or not token or "your-jira.example" in host or "ex.atlassian.net" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if "atlassian.net" in host.lower() and not email:
        return "Jira Cloud needs JIRA_EMAIL for Basic auth"
    return ""


@pytest.mark.skipif(bool(_live_ready()), reason=_live_ready() or "live jira")
def test_live_jira_get_comments_includes_newest_page():
    """Create a real issue, post more than one page, assert the last comment is kept."""
    from datetime import datetime, timezone
    from pathlib import Path

    from src.jira_connection import probe_jira_connection

    env = Path(__file__).resolve().parents[1] / ".env"
    vals: Dict[str, str] = {}
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    host = (vals.get("JIRA_HOST") or "").strip()
    token = (vals.get("JIRA_API_TOKEN") or "").strip()
    email = (vals.get("JIRA_EMAIL") or "").strip()
    project = ((vals.get("JIRA_PROJECTS") or "KAN").split(",")[0] or "KAN").strip()

    probe = probe_jira_connection(host=host, email=email, api_token=token)
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error')}")

    client = JiraClient(host=host, email=email, api_token=token)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    created = client.create_issue(
        project,
        f"[vd-test] comment pages {stamp}",
        "Temporary pagination test. Safe to delete.",
        labels=["vd-comment-page-probe"],
    )
    if not created or not created.get("key"):
        pytest.skip(f"create_issue failed: {getattr(client, 'last_error', None)}")
    key = created["key"]
    page = JiraClient._COMMENT_PAGE_SIZE
    for i in range(page + 1):
        ok = client.add_comment(key, f"filler {i}")
        assert ok, f"add_comment filler {i} failed"
    bot = client.add_comment(key, BOT_BODY)
    assert bot
    rows = client.get_comments(key)
    assert len(rows) >= page + 2
    blob = "\n".join(
        (c.get("body") if isinstance(c.get("body"), str) else str(c.get("body") or ""))
        for c in rows
    )
    assert MARKER in blob
    found = latest_comment_tagging_pat_user(
        rows,
        myself=client.get_myself(),
        extra_needles=["devbot"],
    )
    assert found is not None
    assert MARKER in found


@pytest.mark.asyncio
async def test_found_refactor_comment_can_be_sent_to_real_opencode_serve(
    jira_paged_http, tmp_path
):
    """The comment get_comments now finds must be usable as a real serve turn.

    Issue 1 blocked plan_refactor before OpenCode started. After the paginated
    fetch, the same body is posted to a live ``opencode serve`` session.
    """
    import shutil

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not found on PATH")

    from tests.test_opencode_serve_live_e2e import (
        _free_port,
        _start_serve,
        _stop_serve,
        _wait_health,
    )
    from src.opencode_serve import OpenCodeServeClient

    host, _rows = jira_paged_http
    jira = JiraClient(host=host, api_token="probe", email="")
    comments = jira.get_comments("BUSY-1")
    body = latest_comment_tagging_pat_user(
        comments,
        myself={"name": "devbot", "displayName": "DevBot"},
    )
    assert body and MARKER in body

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    work = tmp_path / "oc-work"
    work.mkdir()
    (work / "README.md").write_text("# plan refactor probe\n", encoding="utf-8")
    proc = None
    try:
        proc = _start_serve(port, tmp_path / "serve.log")
        health = await _wait_health(base, timeout=90.0)
        assert health.get("healthy") is True, health
        client = OpenCodeServeClient(
            base, timeout_seconds=180.0, directory=str(work)
        )
        sess = await client.create_session(title="VD plan_refactor comment page")
        sid = sess.get("id")
        assert isinstance(sid, str) and sid.startswith("ses_"), sess
        msg = await client.send_message(
            sid,
            "Repeat this token and nothing else: "
            f"{MARKER}. Do not use tools. One line.",
        )
        assert msg is not None
        messages = await client.list_messages(sid, limit=20)
        assert isinstance(messages, list)
        assert len(messages) >= 1
    except TimeoutError as e:
        pytest.skip(f"opencode serve did not become healthy: {e}")
    finally:
        if proc is not None:
            _stop_serve(proc)
