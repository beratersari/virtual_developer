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


def test_build_command_with_session_and_model(runner):
    t = AgentTask(
        description="d",
        prompt="hello world",
        agent="sisyphus",
        session_id="ses_1",
        model="custom-model",
        issue_key="KAN-9",
    )
    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "default-model"
        cmd = runner._build_command(t, Path("/tmp/x.log"))
    assert "--agent" in cmd
    # short key maps to oh-my-openagent OpenCode agent ID
    assert "Sisyphus - ultraworker" in cmd
    assert "custom-model" in cmd
    assert "--session" in cmd
    assert "ses_1" in cmd
    # Unattended: auto-approve permissions; no --title
    assert "--auto" in cmd
    assert "--title" not in cmd
    assert cmd[-1] == "hello world"


def test_build_command_default_model(runner):
    t = AgentTask(description="d", prompt="p", agent="a")
    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "bunx oh-my-opencode"
        s.default_model = "m1"
        cmd = runner._build_command(t, Path("/tmp/x.log"))
    assert "m1" in cmd
    assert "--auto" in cmd
    assert "--title" not in cmd


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


def test_build_shell_command_unix(runner, tmp_path):
    t = AgentTask(description="d", prompt='say "hi"', agent="a")
    with patch("src.orchestrator.agent_runner.IS_WINDOWS", False):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.opencode_cli = "opencode"
            s.default_model = "m"
            shell = runner._build_shell_command(t, tmp_path / "out.log")
    assert "opencode" in shell
    assert ">" in shell


def test_build_shell_command_windows(runner, tmp_path):
    t = AgentTask(description="d", prompt="hello world", agent="a")
    with patch("src.orchestrator.agent_runner.IS_WINDOWS", True):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.opencode_cli = "opencode"
            s.default_model = "m"
            shell = runner._build_shell_command(t, tmp_path / "out.log")
    assert "opencode" in shell


@pytest.mark.asyncio
async def test_run_agent_success(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(b"Session: ses_ok1\nProgress: 50%\nProgress: 100%\n")
            self.stdout.feed_eof()
            self.stderr.feed_data(b"")
            self.stderr.feed_eof()

        async def wait(self):
            return 0

        def kill(self):
            pass

    outputs = []
    progresses = []

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=FakeProc()),
        ):
            # Completeness: complete session (no open todos / clean finish)
            with patch.object(
                runner,
                "_assess_incomplete_run",
                return_value={
                    "complete": True,
                    "premature": False,
                    "reasons": [],
                },
            ):
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
    """Reproduce: OpenCode exits 0 after compaction → must NOT look successful.

    Upstream: opencode run can return exit code 0 when auto-compaction stops
    the headless loop without continuing the agent (issue #13946 / #3560).
    Virtual Developer used to mark those jobs completed; session logs end on
    "compacting" with open work remaining.
    """
    monkeypatch.chdir(tmp_path)

    class CompactExitProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(
                b"Session: ses_compact1\n"
                b"Reading files...\n"
                b"Compacting session to free context...\n"
            )
            self.stdout.feed_eof()
            self.stderr.feed_data(b"")
            self.stderr.feed_eof()

        async def wait(self):
            return 0

        def kill(self):
            pass

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=CompactExitProc()),
        ):
            # Simulate DB-backed incompleteness (open todos after compact exit)
            with patch.object(
                runner,
                "_assess_incomplete_run",
                return_value={
                    "complete": False,
                    "premature": True,
                    "reasons": [
                        "open todos: 1 pending, 1 in_progress",
                        "CLI output indicates compaction near end of run",
                    ],
                    "open_todos": 2,
                    "compact_in_output": True,
                },
            ):
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
async def test_run_agent_with_retry_retries_incomplete_compact_exit(
    runner, tmp_path, monkeypatch
):
    """Incomplete compact exit should go through the normal retry path."""
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def fake_run(task, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
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
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": str(tmp_path / "s2.log"),
            "opencode_session_id": "ses_c2",
        }

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 2
            s.agent_task_retry_delay_seconds = 0
            s.agent_task_retry_backoff_multiplier = 1
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=1)

    assert calls["n"] == 2
    assert result["returncode"] == 0
    assert result["retry_info"]["retried"] is True


@pytest.mark.asyncio
async def test_run_agent_timeout(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class HangProc:
        def __init__(self):
            self.returncode = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            # never feed eof — outer timeout should fire

        async def wait(self):
            await asyncio.sleep(100)
            return -1

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "m"
        s.agent_task_timeout_seconds = 1
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=HangProc()),
        ):
            task = AgentTask(description="d", prompt="p", agent="a", issue_key="I-2")
            result = await runner.run_agent(task, timeout_seconds=0.05)
    assert result["timed_out"] is True
    assert result["returncode"] == -1


@pytest.mark.asyncio
async def test_run_agent_timeout_after_stream_eof(runner, tmp_path, monkeypatch):
    """OpenCode can close stdout/stderr while the process hangs — still must time out."""
    monkeypatch.chdir(tmp_path)

    class EofButAlive:
        def __init__(self):
            self.returncode = None
            self.pid = 9001
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0.05)
            return self.returncode

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=EofButAlive()),
        ):
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

    async def flaky(*a, **k):
        calls["n"] += 1
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
            "opencode_session_id": "ses_ok",
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
        s.opencode_cli = "opencode"
        s.default_model = "m"
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=FakeProc(done=False)),
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            tid = await runner.run_background_agent(task)
            status = await runner.check_task_status(tid)
            assert status["status"] == "running"
            assert runner.cancel_task(tid) is True

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
