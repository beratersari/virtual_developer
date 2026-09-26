"""Critical defects from the 2026-09-25 deep review.

Each test calls production code and asserts the safe outcome.
A failure is the defect. These tests do not change product behavior.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.azure.identity import reset_identity_cache
from src.azure.webhook import decide_azure_comment_webhook
from src.config import Settings
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore
_PAT = "secret-collection-pat"
_COLLECTION = "https://tfs.example.com/tfs/DefaultCollection"


class _RecordingClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.headers = dict(kwargs.get("headers") or {})
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> "_RecordingClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, params: Any = None) -> httpx.Response:
        auth = str(self.headers.get("Authorization") or "")
        self.calls.append((url, auth))
        return httpx.Response(404, request=httpx.Request("GET", url))


class _Hold:
    calls: list[_RecordingClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.inner = _RecordingClient(*args, **kwargs)
        _Hold.calls.append(self.inner)

    def __enter__(self) -> _RecordingClient:
        return self.inner.__enter__()

    def __exit__(self, *exc: Any) -> bool:
        return self.inner.__exit__(*exc)


def _arm_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_identity_cache()
    fresh = Settings()
    fresh.azure_pat = ""
    fresh.set_azure_collection_pat_map({_COLLECTION: _PAT})
    monkeypatch.setattr("src.config.settings", fresh)
    _Hold.calls = []
    monkeypatch.setattr("src.azure.identity.httpx.Client", _Hold)


def test_comment_hook_does_not_send_collection_pat_to_remote_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured collection must not authorize connectionData on remoteUrl's host."""
    _arm_pat(monkeypatch)
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {"content": "@yaver-bot /yaver ship it"},
            "pullRequest": {
                "pullRequestId": 12,
                "title": "feat: no ticket",
                "sourceRefName": "refs/heads/feature/x",
                "targetRefName": "refs/heads/develop",
                "repository": {
                    "id": "repo-guid",
                    "name": "app",
                    "remoteUrl": "https://evil.example/tfs/DefaultCollection/Demo/_git/app",
                    "project": {"name": "Demo"},
                },
            },
        },
        "resourceContainers": {"collection": {"baseUrl": _COLLECTION}},
    }
    decide_azure_comment_webhook(payload, enabled=True, bot_mentions=["yaver-bot"])
    leaked = [
        (url, auth)
        for client in _Hold.calls
        for url, auth in client.calls
        if "evil.example" in url and _PAT in auth
    ]
    assert leaked == [], leaked


@pytest.mark.asyncio
async def test_pending_accept_is_not_replaced_by_a_second_jira_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PENDING plus a running queue row is a live accept, not a stale claim."""
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.queue_store = WorkQueueStore(tmp_path / "queue")

    async def _no_dispatch(self) -> None:
        return None

    monkeypatch.setattr(JobProcessor, "dispatch_queue", _no_dispatch)
    proc.state_manager.create_state("KAN-1", "accept window")
    assert proc.state_manager.get_state("KAN-1").status == TaskStatus.PENDING
    first = proc.queue_store.enqueue(
        source="jira",
        issue_key="KAN-1",
        summary="accept window",
        jira_event_id="ev-1",
    )
    claimed = proc.queue_store.claim_next()
    assert claimed is not None
    assert claimed["queue_id"] == first["queue_id"]
    assert claimed["status"] == "running"

    await proc.enqueue_jira_event(
        {
            "jira_event_id": "ev-2",
            "issue": {
                "key": "KAN-1",
                "fields": {"summary": "second intake", "description": ""},
            },
        }
    )
    rows = proc.queue_store.list_items(limit=20)
    running = [row for row in rows if row.get("status") == "running"]
    queued = [row for row in rows if row.get("status") == "queued"]
    assert [row["queue_id"] for row in running] == [first["queue_id"]]
    assert queued == []


def test_storage_delete_of_junction_does_not_remove_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a junction name must not delete the clone it points at."""
    import subprocess

    base = tmp_path / "clones"
    real = base / "real"
    real.mkdir(parents=True)
    (real / "keep.txt").write_text("live", encoding="utf-8")
    alias = base / "alias"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(real)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0 or not alias.exists():
        pytest.skip(f"junction not created: {created.stderr}")
    monkeypatch.setattr(
        "src.dashboard.temp_storage.resolve_temp_base", lambda: base
    )
    from src.dashboard.temp_storage import force_delete_temp_folder

    force_delete_temp_folder("alias")
    assert (real / "keep.txt").is_file()


def _isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".env").write_text("JIRA_HOST=https://jira.example.com\n", encoding="utf-8")
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)


def test_gitlab_save_keeps_pat_when_stored_host_has_a_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty Settings PAT must not drop a secret stored under a scheme-prefixed host."""
    _isolate_settings(tmp_path, monkeypatch)
    from src.config import settings
    from src.dashboard.schemas import GitlabHostCredentialUpdate, SettingsUpdate
    from src.dashboard.service import apply_settings_update

    settings.gitlab_host_pats = '{"https://gitlab.example.com":"super-secret-pat"}'
    apply_settings_update(
        SettingsUpdate(
            gitlab_credentials=[
                GitlabHostCredentialUpdate(host="https://gitlab.example.com", pat="")
            ]
        )
    )
    assert settings.gitlab_pat_for_host("gitlab.example.com") == "super-secret-pat"
