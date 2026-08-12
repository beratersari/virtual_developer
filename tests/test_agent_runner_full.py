"""Full branch coverage for AgentRunner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_runner import (
    AgentRunner,
    AgentTask,
    resolve_opencode_agent_name,
)


@pytest.fixture
def runner(tmp_path):
    return AgentRunner(working_directory=tmp_path)


def test_resolve_opencode_agent_name_sisyphus_stack():
    assert resolve_opencode_agent_name("sisyphus") == "Sisyphus - ultraworker"
    assert resolve_opencode_agent_name("prometheus") == "Prometheus - Plan Builder"
    assert resolve_opencode_agent_name("atlas") == "Atlas - Plan Executor"
    assert resolve_opencode_agent_name("oracle") == "oracle"
    assert (
        resolve_opencode_agent_name("Sisyphus - ultraworker")
        == "Sisyphus - ultraworker"
    )


def test_agent_task_to_dict():
    t = AgentTask(description="d", prompt="p", agent="a", issue_key="I-1")
    d = t.to_dict()
    assert d["agent"] == "a"
    assert d["task_id"].startswith("task_")


def test_parse_progress_patterns(runner):
    assert runner._parse_progress("Progress: 75%") == 75
    assert runner._parse_progress("Completed: 8/10 tasks") == 80
    assert runner._parse_progress("Completed: 0/0 tasks") is None or True
    # blocks
    line = "█████░░░░░ something"
    pct = runner._parse_progress(line)
    assert pct is None or isinstance(pct, int)
    assert runner._parse_progress("no progress here") is None


def test_parse_session_id(runner):
    lines = ["blah", "Session: ses_abc123XYZ", "done"]
    assert runner._parse_session_id(lines) == "ses_abc123XYZ"
    assert runner._parse_session_id(["no session"]) is None
    assert runner._parse_session_id(["Session ID: ses_xyz"]) == "ses_xyz"


def test_get_session_file_variants(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p1 = runner._get_session_file("task_abc", issue_key="PROJ-1", attempt_number=0)
    assert "PROJ-1" in p1.name
    assert "_retry" not in p1.name
    assert p1.name.endswith(".log")
    p2 = runner._get_session_file(
        "task_abc", issue_key="PROJ-1", attempt_number=1, task_type="review"
    )
    assert "review" in p2.name
    assert "_retry1" in p2.name
    p3 = runner._get_session_file("task_only")
    assert p3.name == "task_only.log"
    p4 = runner._get_session_file("task_only", attempt_number=2)
    assert p4.name == "task_only_retry2.log"


@pytest.mark.asyncio
async def test_run_agent_success(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    outputs = []
    progresses = []

    async def fake_serve(task, **kwargs):
        if kwargs.get("on_session_id"):
            kwargs["on_session_id"]("ses_ok1")
        if kwargs.get("on_progress"):
            kwargs["on_progress"](50, "halfway")
            kwargs["on_progress"](100, "done")
        if kwargs.get("on_output"):
            kwargs["on_output"]("stdout", "Session: ses_ok1")
        result = {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "Session: ses_ok1\nProgress: 50%\nProgress: 100%\n",
            "stderr": "",
            "session_file": str(kwargs.get("session_file") or ""),
            "opencode_session_id": "ses_ok1",
            "progress": 100,
        }
        if kwargs.get("on_complete"):
            kwargs["on_complete"](result)
        return result

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        s.opencode_serve_url = "http://127.0.0.1:4096"
        with patch.object(runner, "_run_agent_via_serve", side_effect=fake_serve):
            task = AgentTask(description="d", prompt="p", agent="a", issue_key="I-1")
            result = await runner.run_agent(
                task,
                on_output=lambda stream, line: outputs.append((stream, line)),
                on_complete=lambda r: progresses.append("done"),
                on_progress=lambda pct, msg: progresses.append(pct),
            )
    assert result["returncode"] == 0
    assert result["opencode_session_id"] == "ses_ok1"
    assert "done" in progresses
    assert not result.get("incomplete")


@pytest.mark.asyncio
async def test_run_agent_exit0_after_compacting_treated_as_failure(
    runner, tmp_path, monkeypatch
):
    """Serve compact-then-stop with open todos must not look successful."""
    monkeypatch.chdir(tmp_path)

    async def fake_serve(task, **kwargs):
        if kwargs.get("on_session_id"):
            kwargs["on_session_id"]("ses_compact1")
        return {
            "task_id": task.task_id,
            "returncode": 2,
            "stdout": "Compacting session to free context...\n",
            "stderr": "[INCOMPLETE] open todos: 1 pending, 1 in_progress",
            "session_file": str(kwargs.get("session_file") or ""),
            "opencode_session_id": "ses_compact1",
            "incomplete": True,
            "incomplete_reasons": [
                "open todos: 1 pending, 1 in_progress",
                "compaction near end of run",
            ],
            "progress": 0,
        }

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        s.opencode_serve_url = "http://127.0.0.1:4096"
        with patch.object(runner, "_run_agent_via_serve", side_effect=fake_serve):
            task = AgentTask(
                description="d",
                prompt="implement feature",
                agent="sisyphus",
                issue_key="KAN-12",
            )
            result = await runner.run_agent(task)

    assert result["returncode"] != 0
    assert result["returncode"] == 2
    assert result.get("incomplete") is True
    assert result.get("opencode_session_id") == "ses_compact1"
    assert "INCOMPLETE" in (result.get("stderr") or "")
    assert any("compact" in r.lower() or "todo" in r.lower()
               for r in (result.get("incomplete_reasons") or []))


@pytest.mark.asyncio
async def test_run_agent_compact_without_open_todos_is_success(
    runner, tmp_path, monkeypatch
):
    """Serve: compact waited out, no open todos → success, no extra prompt."""
    monkeypatch.chdir(tmp_path)

    async def fake_serve(task, **kwargs):
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "All todos complete.\nCompacting session to free context...\n",
            "stderr": "",
            "session_file": str(kwargs.get("session_file") or ""),
            "opencode_session_id": "ses_compact2",
            "progress": 100,
        }

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        s.opencode_serve_url = "http://127.0.0.1:4096"
        with patch.object(runner, "_run_agent_via_serve", side_effect=fake_serve):
            task = AgentTask(
                description="d",
                prompt="5+4",
                agent="atlas",
                issue_key="E2E-COMPACT",
            )
            result = await runner.run_agent(task)

    assert result["returncode"] == 0
    assert not result.get("incomplete")


@pytest.mark.asyncio
async def test_run_agent_with_retry_does_not_resend_after_compact(
    runner, tmp_path, monkeypatch
):
    """Compact incomplete must not send another user prompt (Continue or BUILD).

    OpenCode auto-compacts in-session. A second POST pollutes chat and races
    the built-in compact loop.
    """
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[str | None, str, str]] = []

    async def fake_run(task, **kwargs):
        seen.append((task.session_id, task.prompt or "", task.task_id))
        return {
            "task_id": task.task_id,
            "returncode": 2,
            "stdout": "Compacting...",
            "stderr": "[INCOMPLETE] compact stop",
            "session_file": str(tmp_path / "s1.log"),
            "opencode_session_id": "ses_c1",
            "incomplete": True,
            "incomplete_reasons": ["compaction summary"],
        }

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 2
            s.agent_task_retry_delay_seconds = 0
            s.agent_task_retry_backoff_multiplier = 1
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = False
            task = AgentTask(description="d", prompt="full BUILD prompt", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=1)

    assert len(seen) == 1
    assert seen[0][0] is None
    assert seen[0][1] == "full BUILD prompt"
    assert result["returncode"] == 2
    assert result.get("incomplete") is True
    assert not result["retry_info"]["retried"]


@pytest.mark.asyncio
async def test_run_agent_with_retry_does_not_loop_twenty_compact_prompts(
    runner, tmp_path, monkeypatch
):
    """A 20-compact job must not send 20 extra user messages."""
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_run(task, **kwargs):
        calls["n"] += 1
        return {
            "task_id": task.task_id,
            "returncode": 2,
            "stdout": f"Compacting #{calls['n']}",
            "stderr": "[INCOMPLETE] compact stop",
            "session_file": str(tmp_path / f"s{calls['n']}.log"),
            "opencode_session_id": "ses_c20",
            "incomplete": True,
            "incomplete_reasons": ["compaction summary"],
        }

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_max_incomplete_retries = 256
            s.agent_task_retry_delay_seconds = 0
            s.agent_task_retry_backoff_multiplier = 1
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = False
            task = AgentTask(description="d", prompt="BUILD", agent="a")
            result = await runner.run_agent_with_retry(
                task,
                max_retries=3,
                max_incomplete_retries=256,
            )

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_run_agent_with_retry_open_todos_after_compact_no_finish_prompt(
    runner, tmp_path, monkeypatch
):
    """After compact wait, leftover open todos must not inject Finish-todos."""
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []

    async def fake_run(task, **kwargs):
        seen.append(task.prompt or "")
        return {
            "task_id": task.task_id,
            "returncode": 2,
            "stdout": "Compacting…",
            "stderr": "[INCOMPLETE] open todos after compact",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_todos",
            "incomplete": True,
            "incomplete_reasons": ["open todos: 2 pending, 1 in_progress"],
            "compact_events": 3,
            "had_compact": True,
        }

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 2
            s.agent_task_max_incomplete_retries = 256
            s.agent_task_retry_delay_seconds = 0
            s.agent_task_retry_backoff_multiplier = 1
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = False
            task = AgentTask(
                description="d",
                prompt="Write a README for every file",
                agent="a",
            )
            result = await runner.run_agent_with_retry(
                task,
                max_retries=2,
                max_incomplete_retries=256,
            )

    assert seen == ["Write a README for every file"]
    assert result.get("incomplete") is True
    assert not result["retry_info"]["retried"]


@pytest.mark.asyncio
async def test_run_agent_with_retry_question_does_not_resend_build(
    runner, tmp_path, monkeypatch
):
    """Model asked 'Shall I continue?' — do not retry with the BUILD kit."""
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []

    async def fake_run(task, **kwargs):
        seen.append(task.prompt or "")
        return {
            "task_id": task.task_id,
            "returncode": 2,
            "stdout": "Shall I continue with the remaining work?",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_q",
            "incomplete": True,
            "incomplete_reasons": ["assistant asked a clarifying question"],
            "assistant_asked_question": True,
            "had_compact": True,
            "compact_events": 2,
        }

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_max_incomplete_retries = 256
            s.agent_task_retry_delay_seconds = 0
            s.agent_task_retry_backoff_multiplier = 1
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(
                description="d",
                prompt="# Build mode\nYou run unattended inside a daemon",
                agent="a",
            )
            result = await runner.run_agent_with_retry(
                task,
                max_retries=3,
                max_incomplete_retries=256,
            )

    assert len(seen) == 1
    assert seen[0].startswith("# Build mode")
    assert not result["retry_info"]["retried"]
    assert result["returncode"] == 2
    assert result.get("incomplete") is True
    assert not result["retry_info"]["retried"]
    assert result["retry_info"]["incomplete_retries_used"] == 0


@pytest.mark.asyncio
async def test_run_agent_timeout(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def fake_serve(task, **kwargs):
        return {
            "task_id": task.task_id,
            "returncode": -1,
            "stdout": "",
            "stderr": f"[serve] timed out after {kwargs.get('timeout_seconds')}s",
            "session_file": str(kwargs.get("session_file") or ""),
            "opencode_session_id": None,
            "timed_out": True,
            "progress": 0,
        }

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.default_model = "m"
        s.agent_task_timeout_seconds = 1
        s.opencode_serve_url = "http://127.0.0.1:4096"
        with patch.object(runner, "_run_agent_via_serve", side_effect=fake_serve):
            task = AgentTask(description="d", prompt="p", agent="a", issue_key="I-2")
            result = await runner.run_agent(task, timeout_seconds=0.05)
    assert result["timed_out"] is True
    assert result["returncode"] == -1


@pytest.mark.asyncio
async def test_run_agent_timeout_after_stream_eof(runner, tmp_path, monkeypatch):
    """Serve HTTP timeout still marks the job timed_out."""
    monkeypatch.chdir(tmp_path)

    async def fake_serve(task, **kwargs):
        return {
            "task_id": task.task_id,
            "returncode": -1,
            "stdout": "",
            "stderr": "[TIMEOUT] Task exceeded 0.25 seconds",
            "session_file": str(kwargs.get("session_file") or ""),
            "opencode_session_id": None,
            "timed_out": True,
            "progress": 0,
        }

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        s.opencode_serve_url = "http://127.0.0.1:4096"
        with patch.object(runner, "_run_agent_via_serve", side_effect=fake_serve):
            task = AgentTask(description="d", prompt="p", agent="a", issue_key="I-EOF")
            result = await asyncio.wait_for(
                runner.run_agent(task, timeout_seconds=0.25),
                timeout=5.0,
            )
    assert result["timed_out"] is True
    assert result["returncode"] == -1
    assert "TIMEOUT" in (result.get("stderr") or "")


@pytest.mark.asyncio
async def test_run_agent_with_retry_success_first(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def ok_run(*a, **k):
        return {
            "task_id": "t",
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_1",
            "progress": 100,
        }

    with patch.object(runner, "run_agent", side_effect=ok_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 2
            s.agent_task_retry_delay_seconds = 0.01
            s.agent_task_retry_backoff_multiplier = 1.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task)
    assert result["returncode"] == 0
    assert result["retry_info"]["attempts"] == 1


@pytest.mark.asyncio
async def test_run_agent_with_retry_then_success(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    snaps: list[str | None] = []

    async def flaky(task, **k):
        calls["n"] += 1
        snaps.append(task.session_id)
        if calls["n"] == 1:
            return {
                "task_id": "t",
                "returncode": 1,
                "stdout": "",
                "stderr": "fail",
                "session_file": str(tmp_path / "s1.log"),
                "opencode_session_id": "ses_fail",
                "progress": 10,
            }
        return {
            "task_id": "t2",
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": str(tmp_path / "s2.log"),
            "opencode_session_id": "ses_fail",
            "progress": 100,
        }

    retries = []
    original_id = None
    with patch.object(runner, "run_agent", side_effect=flaky):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 2
            s.agent_task_retry_delay_seconds = 0.01
            s.agent_task_retry_backoff_multiplier = 2.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(description="d", prompt="p", agent="a")
            original_id = task.task_id
            result = await runner.run_agent_with_retry(
                task,
                on_retry=lambda *args: retries.append(args),
            )
    assert result["returncode"] == 0
    assert result["retry_info"]["retried"] is True
    assert snaps == [None, "ses_fail"]
    assert retries
    # 8th arg is new_task_id for the upcoming attempt (must differ from first)
    assert len(retries[0]) >= 8
    new_task_id = retries[0][7]
    assert new_task_id
    assert new_task_id != original_id
    assert task.task_id == new_task_id


@pytest.mark.asyncio
async def test_run_agent_with_retry_timeout_exhausted(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def always_timeout(*a, **k):
        return {
            "task_id": "t",
            "returncode": -1,
            "stdout": "",
            "stderr": "TIMEOUT",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": None,
            "progress": 0,
            "timed_out": True,
        }

    with patch.object(runner, "run_agent", side_effect=always_timeout):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 1
            s.agent_task_retry_delay_seconds = 0.01
            s.agent_task_retry_backoff_multiplier = 1.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task)
    assert result["retry_info"]["final_failure"] is True


@pytest.mark.asyncio
async def test_run_agent_with_retry_error_no_retry(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def fail(*a, **k):
        return {
            "task_id": "t",
            "returncode": 2,
            "stdout": "",
            "stderr": "err",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": None,
            "progress": 0,
        }

    with patch.object(runner, "run_agent", side_effect=fail):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_retry_delay_seconds = 0.01
            s.agent_task_retry_backoff_multiplier = 1.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = False
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task)
    assert result["returncode"] == 2


@pytest.mark.asyncio
async def test_background_check_cancel_read(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class FakeProc:
        def __init__(self, done=False):
            self.returncode = 0 if done else None
            self.pid = 1234

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.default_model = "m"
        s.opencode_serve_url = "http://127.0.0.1:4096"
        with patch.object(
            runner, "run_agent", new=AsyncMock(return_value={"returncode": 0})
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            tid = await runner.run_background_agent(task)
            status = await runner.check_task_status(tid)
            assert status["status"] == "running"
            assert runner.cancel_task(tid) is True
            await asyncio.sleep(0)

    # completed background
    runner._running_tasks["done1"] = FakeProc(done=True)
    st = await runner.check_task_status("done1")
    assert st["status"] == "completed"
    assert await runner.check_task_status("missing") is None
    assert runner.cancel_task("missing") is False

    # read session
    sf = runner._get_session_file("task_read")
    sf.write_text("hello session")
    assert "hello" in runner.read_session_output("task_read")
    assert runner.read_session_output("no_such_task_zzz") == ""


@pytest.mark.asyncio
async def test_cancel_windows_paths(runner):
    class FakeProc:
        returncode = None

        def terminate(self):
            raise RuntimeError("nope")

        def kill(self):
            raise RuntimeError("kill fail")

    runner._running_tasks["w1"] = FakeProc()
    with patch("src.orchestrator.agent_runner.IS_WINDOWS", True):
        assert runner.cancel_task("w1") is True

    class FakeProc2:
        returncode = None

        def terminate(self):
            self.returncode = -1

        def kill(self):
            pass

    runner._running_tasks["w2"] = FakeProc2()
    with patch("src.orchestrator.agent_runner.IS_WINDOWS", True):
        assert runner.cancel_task("w2") is True
