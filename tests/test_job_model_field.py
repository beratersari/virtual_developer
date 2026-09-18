"""Job records store the OpenCode model used for the run."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dashboard.service import job_dict_to_item
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_create_job_stores_model(tmp_path: Path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(
        issue_key="KAN-9",
        summary="s",
        agent="atlas",
        model="opencode/deepseek-v4-flash-free",
    )
    assert job["model"] == "opencode/deepseek-v4-flash-free"
    item = job_dict_to_item(job, store=store)
    assert item.model == "opencode/deepseek-v4-flash-free"


def test_job_dict_to_item_infers_codex_backend_from_params(tmp_path: Path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(
        issue_key="KAN-7",
        summary="s",
        description=(
            "{params}\n"
            "Repository: https://gitlab.com/a/b.git\n"
            "Source branch: develop\n"
            "Target branch: develop\n"
            "Mode: build\n"
            "Backend: codex\n"
            "{params}\n"
        ),
        model="muse-spark-1.2-contributor-free",
    )
    item = job_dict_to_item(job, store=store)
    assert item.backend == "codex"


def test_create_job_stores_backend(tmp_path: Path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(issue_key="KAN-8", backend="codex")
    assert job["backend"] == "codex"
    item = job_dict_to_item(job, store=store)
    assert item.backend == "codex"


def test_begin_workflow_run_records_default_model(
    tmp_path: Path, monkeypatch, isolate_jira_agent_artifacts
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    fake = MagicMock()
    with patch("src.processor.create_jira_client", return_value=fake):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = fake
    proc.job_store = JobStore(jobs_dir=tmp_path / "jobs")

    from src.config import settings

    monkeypatch.setattr(settings, "default_model", "provider/test-model-xyz")

    sm.create_state("KAN-11", "sum", "desc")
    st = sm.update_state("KAN-11", status=TaskStatus.PENDING)
    assert st is not None
    task = AgentTask(
        description="t",
        prompt="do it",
        agent="atlas",
        issue_key="KAN-11",
    )
    job_id = proc._begin_workflow_run(
        st,
        status=TaskStatus.EXECUTING,
        task=task,
        workflow_type="execution",
        agent="atlas",
        job_status="executing",
    )
    assert job_id
    job = proc.job_store.get_job(job_id)
    assert job is not None
    assert job.get("model") == "provider/test-model-xyz"


def test_model_for_issue_uses_review_default(monkeypatch):
    from src.config import settings
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "default_model", "plan/model")
    monkeypatch.setattr(settings, "default_review_model", "review/model")
    proc = JobProcessor.__new__(JobProcessor)
    st = MagicMock()
    st.issue_summary = ""
    st.description = ""
    st.metadata = {"workflow_type": "gitlab-review"}
    assert proc._model_for_issue(st) == "review/model"
    assert proc._model_for_issue(st, review=True) == "review/model"
    st.metadata = {"workflow_type": "gitlab_mr"}
    assert proc._model_for_issue(st) == "plan/model"
    monkeypatch.setattr(settings, "default_review_model", "")
    st.metadata = {"workflow_type": "azure-review"}
    assert proc._model_for_issue(st, review=True) == "plan/model"
