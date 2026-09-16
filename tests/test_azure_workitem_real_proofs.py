"""Prove Azure work-item claims with real objects (no MagicMock).

Uses on-disk state, a real JobProcessor, a real dashboard app, and a
real HTTP listener that speaks the Azure WIT comment/GET paths.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from src.azure import workitems as workitems_mod
from src.azure.tracker import azure_tracker_for
from src.azure.workitems import (
    evaluate_work_item_intake,
    normalize_work_item,
    remember_work_item,
    work_item_coords,
    work_item_is_done,
    work_item_is_intake_column,
)
from src.dashboard.api import create_dashboard_app
from src.jira.simulated_client import SimulatedJiraClient
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


# Official Azure DevOps process-template state names (docs.microsoft.com).
_PROCESS_STATES = (
    # Agile
    ("New", False),
    ("Active", False),
    ("Resolved", False),
    ("Closed", True),
    ("Removed", True),
    ("To Do", False),
    ("In Progress", False),
    ("Done", True),
    # Scrum
    ("Approved", False),
    ("Committed", False),
    # CMMI
    ("Proposed", False),
    # Basic
    ("Doing", False),
    # Already mapped Turkish Done names
    ("Kapatıldı", True),
    ("Tamamlandı", True),
    ("Bitti", True),
    ("Completed", True),
    ("Cut", True),
)


@pytest.fixture(autouse=True)
def _clear_coords():
    workitems_mod._COORDS.clear()
    yield
    workitems_mod._COORDS.clear()


def _issue(state: str, *, wid: int = 42, project: str = "Demo", collection: str = ""):
    return normalize_work_item(
        project=project,
        work_item_id=wid,
        fields={
            "System.Title": "Do the thing",
            "System.Description": "{params}\nRepository: https://example.com/r.git\n"
            "Source branch: develop\nTarget branch: main\nMode: build\n{params}",
            "System.State": state,
            "System.WorkItemType": "User Story",
            "System.AssignedTo": {
                "displayName": "Yaver",
                "uniqueName": "DOMAIN\\yaver",
                "id": "guid-1",
            },
        },
        collection_url=collection,
        host="tfs.example.com",
    )


class _AzureWitServer(ThreadingHTTPServer):
    def __init__(self, addr):
        self.comments: list[dict] = []
        self.gets: list[str] = []
        super().__init__(addr, _AzureWitHandler)


class _AzureWitHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        self.server.gets.append(path)
        if "/workitems/" in path.lower() and "/comments" not in path.lower():
            self._send(
                200,
                {
                    "id": 42,
                    "rev": 4,
                    "fields": {
                        "System.Title": "Do the thing",
                        "System.State": "Active",
                        "System.WorkItemType": "User Story",
                        "System.AssignedTo": {
                            "displayName": "Yaver",
                            "uniqueName": "DOMAIN\\yaver",
                        },
                    },
                },
            )
            return
        if "/states" in path.lower():
            self._send(
                200,
                {
                    "value": [
                        {"name": "New", "category": "Proposed"},
                        {"name": "Active", "category": "InProgress"},
                        {"name": "Closed", "category": "Completed"},
                    ]
                },
            )
            return
        self._send(404, {"message": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()
        if path.lower().endswith("/comments"):
            self.server.comments.append(data)
            self._send(200, {"id": 99, "text": data.get("text") or ""})
            return
        self._send(404, {"message": "not found"})

    def do_PATCH(self):
        self._read_json()
        self._send(200, {"id": 42, "rev": 5})


@pytest.fixture
def azure_http():
    server = _AzureWitServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}/tfs/DefaultCollection"
    try:
        yield server, base
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _real_processor(tmp_path) -> JobProcessor:
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sim = SimulatedJiraClient(base_url="http://127.0.0.1:1")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = sim
    proc.reporter = JiraReporter(client=sim)
    return proc


def test_every_process_template_state_intake():
    """Official Agile / Scrum / CMMI / Basic names vs To Do / In Progress."""
    seen_done = []
    seen_intake = []
    seen_other = []
    for name, expect_done in _PROCESS_STATES:
        issue = _issue(name)
        assert work_item_is_done(issue["fields"]) is expect_done, name
        decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
        if expect_done:
            assert decision.action == "skip", name
            assert decision.is_done is True, name
            seen_done.append(name)
        elif work_item_is_intake_column(issue["fields"]):
            assert decision.action == "accept", name
            assert decision.will_process is True, name
            seen_intake.append(name)
        else:
            assert decision.action == "skip", name
            assert decision.reason == "not todo or in progress", name
            seen_other.append(name)
    assert "Done" in seen_done and "Closed" in seen_done
    assert "To Do" in seen_intake and "In Progress" in seen_intake
    assert "New" in seen_intake and "Active" in seen_intake and "Doing" in seen_intake
    assert "Approved" in seen_intake and "Committed" in seen_intake
    assert "Proposed" in seen_intake
    assert "Resolved" in seen_other


def test_two_collections_same_id_share_one_local_key():
    """Bare TFS id is the issue key — collection A is overwritten by B."""
    a = _issue(
        "Active",
        wid=42,
        project="Alpha",
        collection="https://tfs-a.example.com/tfs/ColA",
    )
    b = _issue(
        "Active",
        wid=42,
        project="Beta",
        collection="https://tfs-b.example.com/tfs/ColB",
    )
    assert a["key"] == "WIT-ALPHA-42"
    assert b["key"] == "WIT-BETA-42"
    assert a["key"] != b["key"]
    coords_a = work_item_coords(a["key"])
    coords_b = work_item_coords(b["key"])
    assert coords_a is not None and coords_b is not None
    assert coords_a["project"] == "Alpha"
    assert coords_b["project"] == "Beta"
    assert "ColA" in (coords_a["collection_url"] or "")
    assert "ColB" in (coords_b["collection_url"] or "")


def test_reporter_comment_survives_coord_cache_clear(tmp_path, azure_http):
    server, base = azure_http
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("42", "Do the thing", "body")
    sm.update_state(
        "42",
        status=TaskStatus.EXECUTING,
        metadata={
            "source": "azure_workitem",
            "azure_host": "127.0.0.1",
            "azure_collection_url": base,
            "azure_project": "Demo",
            "azure_work_item_id": 42,
            "azure_work_item_type": "User Story",
        },
    )
    remember_work_item(
        "42",
        {
            "host": "127.0.0.1",
            "collection_url": base,
            "project": "Demo",
            "work_item_id": 42,
            "work_item_type": "User Story",
        },
    )
    workitems_mod._COORDS.clear()
    state = sm.get_state("42")
    tracker = azure_tracker_for("42", state)
    assert tracker is not None
    posted = tracker.add_comment("42", "h3. Work interrupted\n\nStopped from dashboard")
    assert posted is not None
    assert server.comments, "Azure WIT comment API was not hit after coord clear"
    assert "Work interrupted" in (server.comments[0].get("text") or "")


def test_dashboard_cancel_numeric_work_item(tmp_path, azure_http):
    server, base = azure_http
    proc = _real_processor(tmp_path)
    sm = proc.state_manager
    sm.create_state("42", "Do the thing", "body")
    sm.update_state(
        "42",
        status=TaskStatus.EXECUTING,
        current_task_id="task_live",
        metadata={
            "source": "azure_workitem",
            "azure_host": "127.0.0.1",
            "azure_collection_url": base,
            "azure_project": "Demo",
            "azure_work_item_id": 42,
            "current_job_id": "job_live",
        },
    )
    remember_work_item(
        "42",
        {
            "host": "127.0.0.1",
            "collection_url": base,
            "project": "Demo",
            "work_item_id": 42,
        },
    )
    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)
    resp = client.post("/api/tasks/42/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    live = sm.get_state("42")
    assert live is not None
    assert live.status == TaskStatus.CANCELLED
    assert server.comments, "cancel did not post a work-item comment"


def test_dashboard_cancel_pr_key_does_not_use_work_item_id(tmp_path):
    """AZ- PR jobs are not cancelled as work item 12."""
    proc = _real_processor(tmp_path)
    sm = proc.state_manager
    sm.create_state("AZ-DEMO-APP-12", "PR job", "body")
    sm.update_state(
        "AZ-DEMO-APP-12",
        status=TaskStatus.EXECUTING,
        metadata={"source": "azure", "azure_pr_id": 12},
    )
    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)
    resp = client.post("/api/tasks/AZ-DEMO-APP-12/cancel")
    assert resp.status_code == 200, resp.text
    live = sm.get_state("AZ-DEMO-APP-12")
    assert live.status == TaskStatus.CANCELLED
    assert sm.get_state("12") is None
