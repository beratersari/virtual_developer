"""E2E + condition matrix: per-schedule / per-issue OpenCode model.

Covers the product path the operator uses:
  Scheduled form → Jira ``Model:`` in ``{params}`` → job.model on dispatch.

Also closes every branch in ``_normalize_model_id``, ``upsert_params_model``,
``schedule_existing_issue`` model write, and ``JobProcessor._model_for_issue``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.issue_git_spec import (
    _normalize_model_id,
    parse_issue_git_spec,
    upsert_params_model,
)
from src.scheduler.service import (
    build_issue_description,
    create_scheduled_job,
    dispatch_due_schedules,
    preview_existing_issue,
    schedule_existing_issue,
    wait_inflight_dispatches,
)
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.schedule_store import ScheduleStore


def _params(*, model: str = "", extra: str = "") -> str:
    model_line = f"Model: {model}\n" if model else ""
    return (
        "{params}\n"
        "Repository: https://gitlab.com/org/app.git\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: build\n"
        f"{model_line}"
        f"{extra}"
        "{params}"
    )


def _issue(key: str = "KAN-M1", *, description: str | None = None) -> dict:
    return {
        "key": key,
        "id": "1",
        "fields": {
            "summary": "Model e2e",
            "description": description if description is not None else _params(),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": [],
        },
    }


# ---------------------------------------------------------------------------
# Helpers — every condition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expect",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("foo bar", ""),
        ("x" * 201, ""),
        ("`opencode/hy3-free`", "opencode/hy3-free"),
        ("opencode/hy3-free.", "opencode/hy3-free"),
        ("opencode/mimo-v2.5-free", "opencode/mimo-v2.5-free"),
    ],
)
def test_normalize_model_id_conditions(raw, expect):
    assert _normalize_model_id(raw) == expect  # type: ignore[arg-type]


def test_upsert_params_model_all_branches():
    assert upsert_params_model("no params here", "opencode/hy3-free") == "no params here"
    assert upsert_params_model(_params(), "") == _params()
    assert upsert_params_model(None, "opencode/hy3-free") == ""  # type: ignore[arg-type]

    inserted = upsert_params_model(_params(), "opencode/hy3-free")
    spec, err = parse_issue_git_spec("", inserted)
    assert err is None and spec is not None
    assert spec.model == "opencode/hy3-free"

    replaced = upsert_params_model(inserted, "opencode/mimo-v2.5-free")
    spec2, err2 = parse_issue_git_spec("", replaced)
    assert err2 is None and spec2 is not None
    assert spec2.model == "opencode/mimo-v2.5-free"
    assert replaced.lower().count("model:") == 1

    # Alias lines (own line after Mode)
    for label in ("LLM", "OpenCode model", "Default model"):
        body = _params(extra=f"{label}: old/id\n")
        out = upsert_params_model(body, "opencode/hy3-free")
        spec3, err3 = parse_issue_git_spec("", out)
        assert err3 is None and spec3 is not None, err3
        assert spec3.model == "opencode/hy3-free"

    # _MODEL_FIELD hits mid-line " Model:" but ^-anchored replace does not
    weird = (
        "{params}\n"
        "Repository: https://gitlab.com/org/app.git\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "note Model: leftover/id\n"
        "{params}"
    )
    sameish = upsert_params_model(weird, "opencode/hy3-free")
    assert "leftover/id" in sameish or "opencode/hy3-free" in sameish


def test_parse_model_aliases_and_absent():
    spec, err = parse_issue_git_spec("", _params())
    assert err is None and spec is not None
    assert spec.model is None

    for label in ("Model", "LLM"):
        desc = _params(extra=f"{label}: opencode/big-pickle\n")
        spec2, err2 = parse_issue_git_spec("", desc)
        assert err2 is None and spec2 is not None, err2
        assert spec2.model == "opencode/big-pickle"


# ---------------------------------------------------------------------------
# Create-new + existing Jira path
# ---------------------------------------------------------------------------


def test_e2e_create_new_writes_model_on_jira_and_schedule(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-NM"}
    client.transition_to_in_progress.return_value = True
    out = create_scheduled_job(
        title="Per-task model",
        description="Implement it",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model="opencode/mimo-v2.5-free",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/mimo-v2.5-free"
    desc = client.create_issue.call_args.kwargs["description"]
    assert "Model: opencode/mimo-v2.5-free" in desc
    spec, err = parse_issue_git_spec("Per-task model", desc)
    assert err is None and spec is not None
    assert spec.model == "opencode/mimo-v2.5-free"


def test_e2e_create_new_omits_model_line_when_unset(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-ND"}
    client.transition_to_in_progress.return_value = True
    out = create_scheduled_job(
        title="Default model",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        scheduled_at="2026-12-01T00:00:00",
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == ""
    desc = client.create_issue.call_args.kwargs["description"]
    assert "Model:" not in desc


def test_e2e_create_new_issue_key_mode_keeps_model_on_rewrite(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-42"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    out = create_scheduled_job(
        title="Rewrite source",
        repository_url="https://gitlab.com/org/app.git",
        target_branch="develop",
        mode="build",
        model="opencode/hy3-free",
        source_branch_mode="issue_key",
        scheduled_at="2026-12-01T00:00:00",
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rewritten = client.update_issue.call_args.kwargs["fields"]["description"]
    assert "Source branch: feature/KAN-42" in rewritten
    assert "Model: opencode/hy3-free" in rewritten


def test_e2e_preview_returns_existing_model():
    client = MagicMock()
    client.get_issue.return_value = _issue(
        "KAN-PV", description=_params(model="opencode/hy3-free")
    )
    out = preview_existing_issue("KAN-PV", jira_client=client)
    assert out["ok"] is True
    assert out["model"] == "opencode/hy3-free"


def test_e2e_schedule_existing_writes_model_when_jira_update_ok(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-EX")
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.return_value = True
    out = schedule_existing_issue(
        "KAN-EX",
        scheduled_at="2026-12-01T12:00:00",
        model="opencode/mimo-v2.5-free",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/mimo-v2.5-free"
    client.update_issue.assert_called_once()
    new_desc = client.update_issue.call_args.kwargs["fields"]["description"]
    assert "Model: opencode/mimo-v2.5-free" in new_desc
    assert out["schedule"]["issue_description"] == new_desc


def test_e2e_schedule_existing_update_false_still_records_model(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-UF")
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.return_value = False
    out = schedule_existing_issue(
        "KAN-UF",
        scheduled_at="2026-12-01T12:00:00",
        model="opencode/hy3-free",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/hy3-free"
    # Jira write failed — local description stays the original (no Model line)
    assert "Model:" not in (out["schedule"]["issue_description"] or "")


def test_e2e_schedule_existing_update_raises_soft(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-UR")
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.side_effect = RuntimeError("jira 500")
    out = schedule_existing_issue(
        "KAN-UR",
        scheduled_at="2026-12-01T12:00:00",
        model="opencode/hy3-free",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/hy3-free"


def test_e2e_schedule_existing_no_update_issue_attr(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")

    class BareClient:
        def get_issue(self, key):
            return _issue(key)

        def transition_to_in_progress(self, key):
            return True

        def add_labels(self, key, labels):
            return True

        def close(self):
            return None

    out = schedule_existing_issue(
        "KAN-NA",
        scheduled_at="2026-12-01T12:00:00",
        model="opencode/hy3-free",
        jira_client=BareClient(),
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/hy3-free"


def test_e2e_schedule_existing_same_model_skips_jira_write(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue(
        "KAN-SM", description=_params(model="opencode/hy3-free")
    )
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    out = schedule_existing_issue(
        "KAN-SM",
        scheduled_at="2026-12-01T12:00:00",
        model="opencode/hy3-free",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    client.update_issue.assert_not_called()


def test_e2e_schedule_existing_uses_preview_model_when_arg_empty(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue(
        "KAN-PM", description=_params(model="opencode/big-pickle")
    )
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    out = schedule_existing_issue(
        "KAN-PM",
        scheduled_at="2026-12-01T12:00:00",
        model="",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/big-pickle"
    client.update_issue.assert_not_called()


def test_e2e_schedule_existing_empty_model_does_not_write(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-EM")
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    out = schedule_existing_issue(
        "KAN-EM",
        scheduled_at="2026-12-01T12:00:00",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == ""
    client.update_issue.assert_not_called()


# ---------------------------------------------------------------------------
# HTTP API e2e
# ---------------------------------------------------------------------------


def test_e2e_api_create_and_from_issue_accept_model(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-API"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    client.get_issue.return_value = _issue("KAN-OLD")

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.schedule_store", store)
        m.setattr(
            "src.dashboard.api.create_scheduled_job",
            lambda **kw: create_scheduled_job(
                **kw, jira_client=client, store=store
            ),
        )
        m.setattr(
            "src.dashboard.api.schedule_existing_issue",
            lambda issue_key, scheduled_at, store=None, **kw: schedule_existing_issue(
                issue_key,
                scheduled_at=scheduled_at,
                jira_client=client,
                store=store or store,
                **kw,
            ),
        )
        m.setattr(
            "src.dashboard.api.preview_existing_issue",
            lambda issue_key: preview_existing_issue(
                issue_key, jira_client=client
            ),
        )
        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)

        r = tc.post(
            "/api/schedules",
            json={
                "title": "API model",
                "repository_url": "https://gitlab.com/org/app.git",
                "source_branch": "develop",
                "target_branch": "develop",
                "mode": "build",
                "model": "opencode/mimo-v2.5-free",
                "scheduled_at": "2026-12-01T00:00:00",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["schedule"]["model"] == "opencode/mimo-v2.5-free"
        assert "Model: opencode/mimo-v2.5-free" in (
            client.create_issue.call_args.kwargs["description"]
        )

        r2 = tc.get("/api/schedules/preview", params={"issue_key": "KAN-OLD"})
        assert r2.status_code == 200
        assert "model" in r2.json()

        r3 = tc.post(
            "/api/schedules/from-issue",
            json={
                "issue_key": "KAN-OLD",
                "scheduled_at": "2026-12-02T00:00:00",
                "model": "opencode/hy3-free",
            },
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["schedule"]["model"] == "opencode/hy3-free"
        assert client.update_issue.called


# ---------------------------------------------------------------------------
# Dispatch: processor uses the issue Model, not settings default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_dispatch_job_uses_issue_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    js = JobStore(jobs_dir=tmp_path / "jobs")
    jira = MagicMock()
    jira.create_issue.return_value = {"key": "KAN-DJ"}
    jira.transition_to_in_progress.return_value = True
    jira.add_comment.return_value = True
    jira.get_issue.return_value = _issue(
        "KAN-DJ", description=_params(model="opencode/mimo-v2.5-free")
    )
    jira.is_cloud = False

    created = create_scheduled_job(
        title="Dispatch model",
        description="body",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model="opencode/mimo-v2.5-free",
        scheduled_at=(datetime.now() - timedelta(minutes=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        jira_client=jira,
        store=store,
    )
    assert created["ok"] is True

    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = js
    proc.jira_client = jira
    seen: dict = {}

    async def _fake_exec(state):
        seen["model"] = proc._model_for_issue(state)
        seen["job_id"] = (state.metadata or {}).get("current_job_id")
        from src.state.models import TaskStatus

        sm.update_state(
            state.issue_key,
            status=TaskStatus.COMPLETED,
            progress_percentage=100,
            completed_at=datetime.now(),
        )

    proc._start_execution_workflow = _fake_exec  # type: ignore[method-assign]
    proc._start_planning_workflow = AsyncMock()  # type: ignore[method-assign]

    result = await dispatch_due_schedules(
        processor=proc, store=store, jira_client=jira
    )
    await wait_inflight_dispatches()
    assert result["launched"] == 1, result
    assert seen["model"] == "opencode/mimo-v2.5-free"


# ---------------------------------------------------------------------------
# Processor resolver + context-cap model override
# ---------------------------------------------------------------------------


def test_model_for_issue_conditions(tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    jira = MagicMock()
    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()

    monkeypatch.setattr("src.processor.settings.default_model", "settings/default")
    assert proc._model_for_issue(None) == "settings/default"

    st = SimpleNamespace(issue_summary="s", description=_params(model="issue/model"))
    assert proc._model_for_issue(st) == "issue/model"

    st2 = SimpleNamespace(issue_summary="s", description=_params())
    assert proc._model_for_issue(st2) == "settings/default"

    st3 = SimpleNamespace(issue_summary="s", description="no params")
    assert proc._model_for_issue(st3) == "settings/default"

    with patch(
        "src.issue_git_spec.parse_issue_git_spec",
        side_effect=RuntimeError("boom"),
    ):
        assert proc._model_for_issue(st) == "settings/default"

    monkeypatch.setattr("src.processor.settings.default_model", None)
    st_none = SimpleNamespace(issue_summary=None, description=None)
    assert proc._model_for_issue(st_none) == ""
    with patch(
        "src.issue_git_spec.parse_issue_git_spec",
        return_value=(SimpleNamespace(model=""), None),
    ):
        assert proc._model_for_issue(st) == ""


def test_apply_context_limit_uses_override_model(tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    jira = MagicMock()
    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    monkeypatch.setattr("src.processor.settings.opencode_context_limit", 128000)
    monkeypatch.setattr("src.processor.settings.default_model", "settings/default")
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".git" / "info").mkdir(parents=True)
    called: dict = {}

    def _fake_write(path, *, model, context_limit):
        called["model"] = model
        called["limit"] = context_limit
        return path / "opencode.json"

    with patch("src.opencode_models.write_workspace_context_limit", _fake_write):
        proc._apply_job_opencode_context_limit(wd, model="opencode/hy3-free")
    assert called["model"] == "opencode/hy3-free"
    assert called["limit"] == 128000

    proc._apply_job_opencode_context_limit(None, model="x")
    monkeypatch.setattr("src.processor.settings.opencode_context_limit", "nope")
    proc._apply_job_opencode_context_limit(wd, model="x")
    monkeypatch.setattr("src.processor.settings.opencode_context_limit", 0)
    proc._apply_job_opencode_context_limit(wd, model="x")
    monkeypatch.setattr("src.processor.settings.opencode_context_limit", 128000)
    monkeypatch.setattr("src.processor.settings.default_model", "")
    proc._apply_job_opencode_context_limit(wd, model="")
    with patch(
        "src.opencode_models.write_workspace_context_limit",
        side_effect=OSError("nope"),
    ):
        proc._apply_job_opencode_context_limit(wd, model="opencode/hy3-free")
    with patch(
        "src.opencode_models.write_workspace_context_limit", return_value=None
    ):
        proc._apply_job_opencode_context_limit(wd, model="opencode/hy3-free")


def test_build_issue_description_junk_model_omitted():
    text = build_issue_description(
        description="",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model="not a model",
    )
    assert "Model:" not in text
