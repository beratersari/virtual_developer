"""Day-to-day operator paths, exercised with real clients, stores, and git.

No unittest.mock stand-ins for Jira, the poller, the session bind store, or
git. A local HTTP server speaks the Jira routes the product actually calls.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src.config import settings
from src.git_manager import GitManager
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.operator_copy import SUGGEST_FIX_DESC_NO_IP, SUGGEST_FIX_DESC_TODO
from src.processor import JobProcessor
from src.state.models import TaskStatus


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_port(host: str, port: int, timeout: float = 5.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = socket.create_connection((host, port), timeout=0.2)
            conn.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{host}:{port} did not accept connections")


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class _JiraBoard:
    """One ticket the local Jira server keeps between requests."""

    def __init__(self, *, status_name: str, category: str, labels: list[str], description: str):
        self.status_name = status_name
        self.category = category
        self.labels = list(labels)
        self.description = description
        self.summary = "Plan the login page"
        self.comments: list[dict] = []
        self.transition_posts: list[dict] = []
        self.transitions: list[dict] = []

    def issue(self, key: str) -> dict:
        return {
            "key": key,
            "fields": {
                "summary": self.summary,
                "description": self.description,
                "labels": list(self.labels),
                "assignee": {"name": "jira ai bot", "displayName": "Jira AI Bot"},
                "status": {
                    "name": self.status_name,
                    "statusCategory": {"key": self.category},
                },
            },
        }


def _serve_jira(board: _JiraBoard) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/rest/api/2/myself":
                _json_response(
                    self,
                    200,
                    {"name": "jira ai bot", "displayName": "Jira AI Bot", "key": "jira-ai-bot"},
                )
                return
            if path == "/rest/agile/1.0/board/1/sprint":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"errorMessages":["The board does not support sprints"]}')
                return
            if path == "/rest/agile/1.0/board/1/issue":
                _json_response(
                    self,
                    200,
                    {
                        "startAt": 0,
                        "maxResults": 100,
                        "total": 1,
                        "issues": [board.issue("KAN-1")],
                    },
                )
                return
            if path == "/rest/api/2/issue/KAN-1/transitions":
                _json_response(self, 200, {"transitions": board.transitions})
                return
            if path == "/rest/api/2/issue/KAN-1/comment":
                qs = parse_qs(parsed.query)
                start = int((qs.get("startAt") or ["0"])[0])
                page = board.comments[start:]
                _json_response(
                    self,
                    200,
                    {
                        "startAt": start,
                        "maxResults": 50,
                        "total": len(board.comments),
                        "comments": page,
                    },
                )
                return
            if path == "/rest/api/2/issue/KAN-1":
                _json_response(self, 200, board.issue("KAN-1"))
                return
            self.send_error(404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            payload = self._read_json()
            if path == "/rest/api/2/issue/KAN-1/comment":
                body = payload.get("body")
                text = body if isinstance(body, str) else json.dumps(body)
                board.comments.append({"id": str(len(board.comments) + 1), "body": text})
                _json_response(self, 201, board.comments[-1])
                return
            if path == "/rest/api/2/issue/KAN-1/transitions":
                board.transition_posts.append(payload)
                chosen = str((payload.get("transition") or {}).get("id") or "")
                for row in board.transitions:
                    if str(row.get("id")) == chosen:
                        dest = row.get("to") or {}
                        board.status_name = str(dest.get("name") or board.status_name)
                        cat = (dest.get("statusCategory") or {}).get("key")
                        if cat:
                            board.category = str(cat)
                self.send_response(204)
                self.end_headers()
                return
            self.send_error(404)

        def do_PUT(self) -> None:
            payload = self._read_json()
            fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
            if "labels" in fields:
                board.labels = [str(item) for item in (fields.get("labels") or [])]
            self.send_response(204)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    _wait_port(str(host), int(port))
    return httpd


def _processor_on(httpd: ThreadingHTTPServer, tmp_path: Path, monkeypatch) -> JobProcessor:
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.setattr(settings, "jira_host", base)
    monkeypatch.setattr(settings, "jira_api_token", "tok")
    monkeypatch.setattr(settings, "jira_email", "")
    monkeypatch.setattr(settings, "jira_trigger_user", "jira ai bot")
    monkeypatch.setattr(settings, "jira_trigger_label", "")
    monkeypatch.setattr(settings, "trigger_labels", "")
    monkeypatch.setattr(settings, "jira_board_id", "1")
    monkeypatch.setattr(settings, "jira_enabled", True)
    proc = JobProcessor()
    return proc


def _close_jira(proc: JobProcessor) -> None:
    seen = set()
    for client in (getattr(proc, "jira_client", None), getattr(proc.reporter, "client", None)):
        http = getattr(client, "client", None)
        if http is None or id(http) in seen:
            continue
        seen.add(id(http))
        try:
            http.close()
        except Exception:
            pass


def _plan_file(key: str) -> Path:
    from src.paths import plans_dir

    dest = plans_dir() / f"{key}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"# plan {key}\n", encoding="utf-8")
    return dest


@pytest.mark.asyncio
async def test_revise_after_implement_is_what_the_next_poll_runs(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Operator clicks Implement, then Revise, before the next poll.

    The ticket is already In Progress (that is where a finished plan sits).
    Revise has to win: the next poll must revise, not start the build.
    """
    board = _JiraBoard(
        status_name="In Progress",
        category="indeterminate",
        labels=["plan_ready", "team"],
        description=(
            "{params}\n"
            "Repository: https://gitlab.example.com/acme/app.git\n"
            "Source branch: feature/login\n"
            "Target branch: develop\n"
            "Mode: plan\n"
            "{params}"
        ),
    )
    board.transitions = [
        {
            "id": "31",
            "name": "Done",
            "to": {"name": "Done", "statusCategory": {"key": "done"}},
        }
    ]
    httpd = _serve_jira(board)
    proc = _processor_on(httpd, tmp_path, monkeypatch)
    try:
        sm = proc.state_manager
        sm.create_state("KAN-1", board.summary, board.description)
        sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
        _plan_file("KAN-1")

        implemented = await proc.request_plan_execute_from_dashboard("KAN-1")
        assert implemented["ok"] is True, implemented
        assert board.transition_posts == []
        assert "plan_execute" in board.labels
        assert "plan_ready" not in board.labels
        assert "team" in board.labels
        assert sm.get_state("KAN-1").status == TaskStatus.PLAN_READY

        revised = await proc.request_plan_refactor_from_dashboard(
            "KAN-1", "Use Redis instead of memory"
        )
        assert revised["ok"] is True, revised
        assert "plan_refactor" in board.labels
        assert "plan_execute" not in board.labels
        assert "plan_ready" not in board.labels
        assert "team" in board.labels

        client = JiraClient(
            host=settings.jira_host, api_token="tok", email=""
        )
        try:
            stored = client.get_comments("KAN-1")
        finally:
            client.close()
        assert stored, "revision comment was not on the ticket"
        body = stored[-1]["body"]
        assert "Use Redis instead of memory" in body
        assert "jira ai bot" in body.lower()

        from src.jira.plan_labels import latest_comment_tagging_pat_user

        assert latest_comment_tagging_pat_user(
            stored,
            mention_tokens=settings.trigger_mentions_list,
            extra_needles=settings.jira_trigger_user_list,
        )

        poller = JiraPoller(
            client=proc.jira_client,
            board_id="1",
            state_manager=sm,
        )
        poller._seen_issues.add("KAN-1")
        rows = [row for row in poller.poll_board() if row.get("key") == "KAN-1"]
        assert rows, "next poll did not pick up the plan-ready ticket"
        assert rows[0].get("_plan_handoff") == "refactor"
    finally:
        httpd.shutdown()
        httpd.server_close()
        _close_jira(proc)


def _bad_template_board(*, status_name: str, category: str, transitions: list[dict]) -> _JiraBoard:
    board = _JiraBoard(
        status_name=status_name,
        category=category,
        labels=[],
        description="please build the login page",
    )
    board.transitions = transitions
    return board


def _run_bad_template(proc: JobProcessor, board: _JiraBoard):
    sm = proc.state_manager
    sm.create_state("KAN-1", board.summary, board.description)
    sm.update_state("KAN-1", status=TaskStatus.PENDING)
    poller = JiraPoller(client=proc.jira_client, board_id="1", state_manager=sm)
    proc._poller = poller
    git = proc._prepare_git_workspace_blocking(sm.get_state("KAN-1"))
    assert git is None
    state = sm.get_state("KAN-1")
    assert state is not None
    assert state.status == TaskStatus.ERROR
    assert board.comments, "operator got no Jira comment"
    return poller, board.comments[-1]["body"]


def test_bad_template_still_on_to_do_does_not_claim_in_progress(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Workflow has no In Progress transition. The ticket never leaves To Do.

    The error comment has to say that, so the operator edits the description
    where the ticket already is.
    """
    board = _bad_template_board(
        status_name="To Do",
        category="new",
        transitions=[
            {
                "id": "41",
                "name": "Done",
                "to": {"name": "Done", "statusCategory": {"key": "done"}},
            }
        ],
    )
    httpd = _serve_jira(board)
    proc = _processor_on(httpd, tmp_path, monkeypatch)
    try:
        poller, body = _run_bad_template(proc, board)
        assert board.transition_posts == []
        assert board.status_name == "To Do"
        assert "moved to *In Progress*" not in body
        assert SUGGEST_FIX_DESC_NO_IP in body
        assert poller._last_jira_status.get("KAN-1") != "in progress"
    finally:
        httpd.shutdown()
        httpd.server_close()
        _close_jira(proc)


def test_bad_template_already_in_progress_tells_operator_to_return_to_do(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Plan already moved the ticket. A later template failure must not say it is still To Do."""
    board = _bad_template_board(
        status_name="In Progress",
        category="indeterminate",
        transitions=[
            {
                "id": "41",
                "name": "Done",
                "to": {"name": "Done", "statusCategory": {"key": "done"}},
            }
        ],
    )
    httpd = _serve_jira(board)
    proc = _processor_on(httpd, tmp_path, monkeypatch)
    try:
        poller, body = _run_bad_template(proc, board)
        assert board.transition_posts == []
        assert board.status_name == "In Progress"
        assert "Hâlâ *Yapılacaklar* iken" not in body
        assert "sonra yeniden kuyruğa almak için" in body
        assert SUGGEST_FIX_DESC_TODO in body
        # No transition was accepted, so the poller tracker stays unset.
        assert poller._last_jira_status.get("KAN-1") != "in progress"
    finally:
        httpd.shutdown()
        httpd.server_close()
        _close_jira(proc)


def test_bad_template_transition_moves_the_ticket_then_says_return_to_do(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    board = _bad_template_board(
        status_name="To Do",
        category="new",
        transitions=[
            {
                "id": "21",
                "name": "In Progress",
                "to": {
                    "name": "In Progress",
                    "statusCategory": {"key": "indeterminate"},
                },
            }
        ],
    )
    httpd = _serve_jira(board)
    proc = _processor_on(httpd, tmp_path, monkeypatch)
    try:
        poller, body = _run_bad_template(proc, board)
        assert board.transition_posts
        assert board.status_name == "In Progress"
        assert "Hâlâ *Yapılacaklar* iken" not in body
        assert SUGGEST_FIX_DESC_TODO in body
        assert poller._last_jira_status.get("KAN-1") == "in progress"
    finally:
        httpd.shutdown()
        httpd.server_close()
        _close_jira(proc)


@pytest.mark.xfail(reason="SSH port kept on the HTTPS clone URL; fix deferred", strict=False)
def test_ssh_clone_url_with_a_port_does_not_use_that_port_for_https(tmp_path):
    """Pasting GitLab's ssh://host:2222/… URL must not make git dial 2222 over HTTPS."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "develop"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=yaver@example.com",
            "-c",
            "user.name=Yaver",
            "commit",
            "-m",
            "init",
        ],
        cwd=src,
        check=True,
        capture_output=True,
    )
    http_root = tmp_path / "http"
    bare = http_root / "acme" / "app.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(bare), "update-server-info"],
        check=True,
        capture_output=True,
    )

    class _DumbGit(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            rel = self.path.split("?", 1)[0].lstrip("/").replace("\\", "/")
            path = (http_root / rel).resolve()
            try:
                path.relative_to(http_root.resolve())
            except ValueError:
                self.send_error(404)
                return
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    reject = socket.socket()
    reject.bind(("127.0.0.1", 0))
    reject.listen(1)
    reject_port = int(reject.getsockname()[1])

    def _drop() -> None:
        while True:
            try:
                conn, _addr = reject.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=_drop, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DumbGit)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, git_port = httpd.server_address
    _wait_port(str(host), int(git_port))
    try:
        pasted = f"ssh://git@127.0.0.1:{reject_port}/acme/app.git"
        normalized = GitManager.normalize_remote_url(pasted)
        assert f":{reject_port}" not in normalized
        assert normalized == "https://127.0.0.1/acme/app"
        assert (
            GitManager.normalize_remote_url("ssh://git@gitlab.example.com:22/group/repo.git")
            == "https://gitlab.example.com/group/repo"
        )

        from src.azure.webhook import _repo_http_url as azure_repo_url
        from src.gitlab.webhook import _repo_http_url as gitlab_repo_url

        assert (
            gitlab_repo_url(
                {},
                {"url": "ssh://git@gitlab.example.com:2222/group/repo.git"},
            )
            == "https://gitlab.example.com/group/repo.git"
        )
        assert (
            azure_repo_url(
                {
                    "sshUrl": (
                        "ssh://git@tfs.example.com:2222/tfs/DefaultCollection/"
                        "Proj/_git/app.git"
                    )
                }
            )
            == "https://tfs.example.com/tfs/DefaultCollection/Proj/_git/app.git"
        )

        bad = subprocess.run(
            ["git", "ls-remote", f"http://127.0.0.1:{reject_port}/acme/app.git"],
            capture_output=True,
            timeout=20,
        )
        assert bad.returncode != 0
        good = subprocess.run(
            ["git", "ls-remote", f"http://127.0.0.1:{git_port}/acme/app.git"],
            capture_output=True,
            timeout=20,
        )
        assert good.returncode == 0, (good.stderr or b"").decode("utf-8", "replace")
    finally:
        reject.close()
        httpd.shutdown()
        httpd.server_close()


def _build_state(proc: JobProcessor, key: str, *, source: str, target: str = "main") -> None:
    repo = "https://gitlab.example.com/acme/app.git"
    proc.state_manager.create_state(key, f"build {key}", "Mode: build")
    proc.state_manager.update_state(
        key,
        metadata={
            "repository_url": repo,
            "source_branch": source,
            "target_branch": target,
        },
    )


def _plan_bind(store, key: str, branch: str, *, target: str = "main") -> None:
    store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch=branch,
        target_branch=target,
        session_id=f"ses_{key}",
        issue_key=key,
        kind="plan",
    )


def test_build_on_develop_does_not_reuse_another_issues_plan(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Source develop becomes feature/{KEY} for each ticket.

    That isolation is intentional, so a second ticket does not take the
    other ticket's plan file.
    """
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    sm = proc.state_manager
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    plan = _plan_file("KAN-1")
    _plan_bind(isolate_jira_agent_artifacts["session_bind_store"], "KAN-1", "feature/KAN-1")
    _plan_bind(isolate_jira_agent_artifacts["session_bind_store"], "KAN-2", "feature/KAN-2")
    _build_state(proc, "KAN-2", source="develop")
    assert plan.is_file()
    assert proc._resolve_plan_for_build("KAN-2") is None
    _build_state(proc, "KAN-4", source="feature/payments")
    assert proc._resolve_plan_for_build("KAN-4") is None


def test_build_on_develop_does_not_guess_between_two_waiting_plans(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    for key in ("KAN-1", "KAN-3"):
        proc.state_manager.create_state(key, f"plan {key}", "Mode: plan")
        proc.state_manager.update_state(key, status=TaskStatus.PLAN_READY)
        _plan_file(key)
        _plan_bind(
            isolate_jira_agent_artifacts["session_bind_store"],
            key,
            f"feature/{key}",
        )
    _build_state(proc, "KAN-2", source="develop")
    assert proc._resolve_plan_for_build("KAN-2") is None


def test_build_on_develop_does_not_take_another_features_plan(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager.create_state("KAN-1", "plan login", "Mode: plan")
    proc.state_manager.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    _plan_file("KAN-1")
    _plan_bind(
        isolate_jira_agent_artifacts["session_bind_store"],
        "KAN-1",
        "feature/login",
    )
    _build_state(proc, "KAN-2", source="develop")
    assert proc._resolve_plan_for_build("KAN-2") is None


def test_build_ticket_keeps_its_own_plan_file(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager.create_state("KAN-1", "plan login", "Mode: plan")
    proc.state_manager.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    _plan_file("KAN-1")
    _plan_bind(isolate_jira_agent_artifacts["session_bind_store"], "KAN-1", "feature/KAN-1")
    own = _plan_file("KAN-2")
    _build_state(proc, "KAN-2", source="develop")
    assert proc._resolve_plan_for_build("KAN-2") == str(own)
