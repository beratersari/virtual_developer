"""The daemon owns opencode serve, and agent saves reload it when idle."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.opencode_serve_supervisor import (
    OpenCodeServeSupervisor,
    blocking_issue_keys,
    pids_from_netstat,
    pids_from_ss,
    serve_command,
    serve_env,
    serve_target,
)


class _Proc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.code = None

    def poll(self):
        return self.code


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def _supervisor(**overrides):
    clock = _Clock()
    killed: list[int] = []
    spawned: list[int] = []
    proc = _Proc(overrides.pop("pid", 7))
    listeners = {"pids": list(overrides.pop("listeners", [proc.pid]))}
    healthy = {"up": overrides.pop("healthy", True)}

    def kill(pid: int) -> None:
        killed.append(pid)
        if pid == proc.pid:
            proc.code = 0
            healthy["up"] = False
        listeners["pids"] = [item for item in listeners["pids"] if item != pid]

    def spawn():
        spawned.append(proc.pid)
        proc.code = None
        listeners["pids"] = [proc.pid]
        healthy["up"] = True
        return proc

    jobs = {"keys": list(overrides.pop("jobs", []))}
    sup = OpenCodeServeSupervisor(
        healthy=overrides.pop("healthy_fn", lambda: healthy["up"]),
        spawn=overrides.pop("spawn", spawn),
        kill=overrides.pop("kill", kill),
        listener_pids=overrides.pop("listener_pids", lambda _port: list(listeners["pids"])),
        is_serve_pid=overrides.pop("is_serve_pid", lambda pid: pid == proc.pid),
        live_jobs=overrides.pop("live_jobs", lambda: list(jobs["keys"])),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        interval=overrides.pop("interval", 2.0),
    )
    return sup, killed, spawned, healthy, jobs, listeners


def test_netstat_keeps_only_the_serve_port():
    text = "\n".join(
        [
            "TCP    127.0.0.1:4096    0.0.0.0:0    LISTENING    52528",
            "TCP    127.0.0.1:8080    0.0.0.0:0    LISTENING    30968",
            "TCP    127.0.0.1:4096    10.0.0.8:1   ESTABLISHED  111",
            "TCP    [::]:4096         [::]:0       LISTENING    52528",
        ]
    )
    assert pids_from_netstat(text, 4096) == [52528]
    assert pids_from_ss('users:(("opencode",pid=52528,fd=8))') == [52528]


def test_serve_target_probes_loopback_when_the_bind_is_all_interfaces(monkeypatch):
    monkeypatch.setattr("src.config.settings.opencode_serve_url", "http://0.0.0.0:4096")
    health, host, port = serve_target()
    assert health == "http://127.0.0.1:4096/global/health"
    assert host == "0.0.0.0"
    assert port == 4096
    command = serve_command("opencode", host, port)
    assert command[1:5] == ["serve", "--port", "4096", "--hostname"]
    assert serve_env({})["OPENCODE_DISABLE_MODELS_FETCH"] == "1"


def test_blocking_keys_include_live_slots_and_planning_state():
    class _State:
        def __init__(self, key: str, status: str) -> None:
            self.issue_key = key
            self.status = status

    class _States:
        def get_active_issues(self):
            return [
                _State("KAN-1", "executing"),
                _State("KAN-2", "planning"),
                _State("KAN-3", "completed"),
            ]

    class _Processor:
        state_manager = _States()

        def list_live_processing_keys(self):
            return ["KAN-1"]

    assert blocking_issue_keys(_Processor()) == ["KAN-1", "KAN-2"]


def test_ensure_started_leaves_a_healthy_serve():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor()
    result = sup.ensure_started()
    assert result["status"] == "ready"
    assert spawned == []
    assert killed == []
    sup.stop_owned()
    assert killed == []


def test_ensure_started_spawns_when_serve_is_down():
    sup, killed, spawned, healthy, _jobs, _listeners = _supervisor(healthy=False, listeners=[])
    result = sup.ensure_started()
    assert result["status"] == "started"
    assert spawned == [7]
    assert healthy["up"] is True
    sup.stop_owned()
    assert killed == [7]


def test_reload_restarts_the_listener_when_idle():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor()
    result = sup.request_reload()
    assert result["status"] == "reloaded"
    assert "saved agents" in result["message"]
    assert killed == [7]
    assert spawned == [7]


def test_reload_waits_while_a_job_is_running():
    sup, killed, spawned, _healthy, jobs, _listeners = _supervisor(jobs=["KAN-7"])
    result = sup.request_reload()
    assert result["status"] == "deferred"
    assert "KAN-7" in result["message"]
    assert killed == []
    assert spawned == []
    assert sup.status()["status"] == "deferred"
    jobs["keys"] = []


def test_reload_does_not_kill_a_foreign_listener():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        listeners=[99],
        is_serve_pid=lambda _pid: False,
        healthy=False,
    )
    result = sup.request_reload()
    assert result["status"] == "failed"
    assert "in use" in result["message"]
    assert killed == []
    assert spawned == []


def test_reload_does_not_kill_this_process():
    sup, killed, spawned, _healthy, _jobs, listeners = _supervisor(
        listeners=[os.getpid()],
        is_serve_pid=lambda _pid: True,
    )
    listeners["pids"] = [os.getpid()]
    result = sup.request_reload()
    assert os.getpid() not in killed
    assert spawned == []
    assert result["status"] == "failed"
    assert "did not stop" in result["message"]


def test_second_reload_waits_for_the_one_already_running():
    sup, _killed, spawned, _healthy, _jobs, _listeners = _supervisor()
    sup._reloading = True
    result = sup.request_reload()
    assert result["status"] == "deferred"
    assert spawned == []


@pytest.mark.asyncio
async def test_watch_reloads_after_the_job_finishes():
    sup, killed, spawned, _healthy, jobs, _listeners = _supervisor(interval=0.01, jobs=["KAN-1"])
    assert sup.request_reload()["status"] == "deferred"
    jobs["keys"] = []
    running = {"on": True}

    async def stop() -> None:
        await asyncio.sleep(0.08)
        running["on"] = False

    await asyncio.gather(sup.watch(lambda: running["on"]), stop())
    assert killed == [7]
    assert spawned == [7]
    assert sup.status()["status"] == "reloaded"


@pytest.mark.asyncio
async def test_watch_starts_serve_when_it_is_down():
    sup, _killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False,
        listeners=[],
        interval=0.01,
    )
    running = {"on": True}

    async def stop() -> None:
        await asyncio.sleep(0.08)
        running["on"] = False

    await asyncio.gather(sup.watch(lambda: running["on"]), stop())
    assert spawned == [7]
    assert sup.status()["status"] == "started"


@pytest.mark.asyncio
async def test_watch_backs_off_when_serve_does_not_start():
    spawned: list[str] = []

    def explode():
        spawned.append("try")
        raise FileNotFoundError("opencode")

    sup, _killed, _spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False,
        listeners=[],
        spawn=explode,
        interval=0.01,
    )
    running = {"on": True}

    async def stop() -> None:
        await asyncio.sleep(0.08)
        running["on"] = False

    await asyncio.gather(sup.watch(lambda: running["on"]), stop())
    assert spawned == ["try"]


def test_save_copies_the_agent_and_asks_serve_to_reload(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    opencode = tmp_path / "opencode"
    config = tmp_path / "config"
    claude = tmp_path / "claude"
    monkeypatch.setattr("src.opencode_agents.agents_dir", lambda: catalog)
    monkeypatch.setattr("src.opencode_agents.opencode_agents_dir", lambda: opencode)
    monkeypatch.setattr("src.opencode_agents.opencode_xdg_agents_dir", lambda: config)
    monkeypatch.setattr("src.opencode_agents.claude_agents_dir", lambda: claude)
    calls: list[str] = []

    def reload():
        calls.append("reload")
        return {
            "status": "reloaded",
            "message": "OpenCode reloaded and is using the saved agents.",
        }

    monkeypatch.setattr("src.opencode_serve_supervisor.supervisor.request_reload", reload)
    client = TestClient(create_dashboard_app())
    created = client.post("/api/opencode-agents", json={"name": "derman-docs", "text": ""})
    assert created.status_code == 200, created.text
    assert calls == ["reload"]
    saved = client.put(
        "/api/opencode-agents/derman-docs",
        json={"text": "---\nmode: primary\n---\n\nWrite the guide.\n"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["serve"]["status"] == "reloaded"
    assert "Write the guide." in (opencode / "derman-docs.md").read_text(encoding="utf-8")
    assert calls == ["reload", "reload"]
    synced = client.post("/api/opencode-agents/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["serve"]["message"].startswith("OpenCode reloaded")
    listed = client.get("/api/opencode-agents")
    assert listed.json()["serve"]["status"] == "ready"


def test_save_reports_a_deferred_reload(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.setattr("src.opencode_agents.agents_dir", lambda: catalog)
    monkeypatch.setattr("src.opencode_agents.opencode_agents_dir", lambda: tmp_path / "opencode")
    monkeypatch.setattr("src.opencode_agents.opencode_xdg_agents_dir", lambda: tmp_path / "config")
    monkeypatch.setattr("src.opencode_agents.claude_agents_dir", lambda: tmp_path / "claude")
    monkeypatch.setattr(
        "src.opencode_serve_supervisor.supervisor.request_reload",
        lambda: {
            "status": "deferred",
            "message": "Agents are saved. OpenCode will reload when KAN-1 finishes.",
        },
    )
    monkeypatch.setattr(
        "src.opencode_serve_supervisor.supervisor.status",
        lambda: {
            "status": "deferred",
            "message": "Agents are saved. OpenCode will reload when KAN-1 finishes.",
        },
    )
    client = TestClient(create_dashboard_app())
    created = client.post(
        "/api/opencode-agents",
        json={"name": "derman-docs", "text": "---\nmode: primary\n---\n\nWrite the guide.\n"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["serve"]["status"] == "deferred"
    assert "KAN-1" in created.json()["serve"]["message"]
    saved = client.put(
        "/api/opencode-agents/derman-docs",
        json={"text": "---\nmode: primary\n---\n\nWrite the guide.\n"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["serve"]["status"] == "deferred"
    assert client.get("/api/opencode-agents").json()["serve"]["status"] == "deferred"


def test_idle_serve_restarts_only_after_repeated_health_misses():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[7]
    )
    sup.ensure_started()
    sup.ensure_started()
    assert killed == []
    assert spawned == []
    result = sup.ensure_started()
    assert result["status"] == "started", result
    assert killed == [7]
    assert spawned == [7]


def test_one_health_miss_does_not_kill_a_listening_serve():
    """A single missed /global/health is not a dead process."""
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[7]
    )
    result = sup.ensure_started()
    assert killed == [], (killed, result)
    assert spawned == []
    assert result["status"] != "failed"


def test_repeated_health_misses_do_not_kill_during_a_job():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[7], jobs=["KAN-1"]
    )
    for _ in range(5):
        sup.ensure_started()
    assert killed == []
    assert spawned == []


def test_unknown_jobs_do_not_kill_a_listening_serve():
    def boom():
        raise RuntimeError("state locked")

    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[7], live_jobs=boom
    )
    for _ in range(4):
        sup.ensure_started()
    assert killed == []
    assert spawned == []


def test_healthy_check_keeps_a_deferred_agent_reload():
    sup, killed, spawned, _healthy, jobs, _listeners = _supervisor(jobs=["KAN-1"])
    assert sup.request_reload()["status"] == "deferred"
    jobs["keys"] = []
    result = sup.ensure_started()
    assert result["status"] == "ready"
    assert killed == []
    assert spawned == []
    assert sup.status()["status"] == "deferred"


def test_reload_stops_when_a_job_appears_before_the_kill():
    sup, killed, spawned, _healthy, jobs, _listeners = _supervisor()

    def is_serve(pid: int) -> bool:
        jobs["keys"] = ["KAN-1"]
        return pid == 7

    sup._is_serve_fn = is_serve
    result = sup.request_reload()
    assert result["status"] == "deferred", result
    assert "KAN-1" in result["message"]
    assert killed == []
    assert spawned == []


def _jobs_visible_only_as_the_process_is_stopped() -> list[str]:
    """The job appears only inside the kill decision, after earlier checks."""
    frame = sys._getframe()
    while frame is not None:
        if frame.f_code.co_name == "_kill_then_spawn":
            return ["KAN-1"]
        frame = frame.f_back
    return []


def test_reload_does_not_kill_when_a_job_lands_as_the_process_is_stopped():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        live_jobs=_jobs_visible_only_as_the_process_is_stopped
    )
    result = sup.request_reload()
    assert result["status"] == "deferred", result
    assert "KAN-1" in result["message"]
    assert killed == []
    assert spawned == []


def test_third_health_miss_does_not_kill_when_a_job_lands_before_the_stop():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False,
        listeners=[7],
        live_jobs=_jobs_visible_only_as_the_process_is_stopped,
    )
    sup.ensure_started()
    sup.ensure_started()
    result = sup.ensure_started()
    assert result["status"] == "running", result
    assert killed == []
    assert spawned == []
    assert sup.status()["status"] != "deferred"


def test_unhealthy_listener_is_not_ready_for_a_job():
    """A listen socket is not /global/health. Do not clear idle misses."""
    sup, killed, spawned, _healthy, jobs, _listeners = _supervisor(
        healthy=False, listeners=[7], jobs=["KAN-1"]
    )
    sup._misses = 2
    assert sup.wait_until_ready(5) is False
    assert killed == []
    assert spawned == []
    assert sup._misses == 2
    jobs["keys"] = []
    result = sup.ensure_started()
    assert result["status"] == "started", result
    assert killed == [7]


def test_job_waits_for_the_in_flight_reload():
    """A job that arrives after the old process is gone waits for that one restart."""
    sup, _killed, spawned, jobs, entered, release, active = _reload_blocked_in_spawn()
    done = threading.Event()

    def run_reload() -> None:
        sup.request_reload()
        done.set()

    worker = threading.Thread(target=run_reload)
    worker.start()
    assert entered.wait(2)
    jobs["keys"] = ["KAN-1"]

    def sleep(_seconds: float) -> None:
        release.set()
        assert done.wait(3)

    sup._sleep = sleep
    try:
        assert sup.wait_until_ready(5) is True
        assert active["max"] == 1
        assert len(spawned) == 1
    finally:
        release.set()
        done.set()
        worker.join(3)


@pytest.mark.asyncio
async def test_job_waits_out_a_serve_reload_before_its_health_check(monkeypatch):
    order: list[object] = []

    def wait(timeout: float = 45.0) -> bool:
        order.append(("wait", timeout))
        return True

    monkeypatch.setattr(
        "src.opencode_serve_supervisor.supervisor.wait_until_ready",
        wait,
    )

    class _Client:
        async def health(self, timeout=None):
            order.append(("health", timeout))
            raise RuntimeError("down")

    from src.opencode_serve import ServeOrchestrator

    result = await ServeOrchestrator(client=_Client()).run(prompt="go", title="t")
    assert order[0] == ("wait", 45.0)
    assert order[1] == ("health", 3.0)
    assert result.returncode == 1
    assert result.incomplete is False
    assert "not answering" in (result.stderr or "").lower()
    assert "down" in (result.stderr or "").lower()


@pytest.mark.asyncio
async def test_unready_serve_fails_without_calling_health(monkeypatch):
    """The job budget must not start when the gate already knows serve is down."""
    calls: list[object] = []

    def wait(timeout: float = 45.0) -> bool:
        calls.append(("wait", timeout))
        return False

    monkeypatch.setattr(
        "src.opencode_serve_supervisor.supervisor.wait_until_ready",
        wait,
    )

    class _Client:
        timeout_seconds = 7200.0

        async def health(self, timeout=None):
            calls.append(("health", timeout))
            raise AssertionError("health must not run")

    from src.opencode_serve import ServeOrchestrator

    started = time.monotonic()
    result = await ServeOrchestrator(client=_Client()).run(prompt="go", title="t")
    assert time.monotonic() - started < 2.0
    assert calls == [("wait", 45.0)]
    assert result.returncode == 1
    assert result.incomplete is False
    assert "OpenCode serve is not answering" in (result.stderr or "")


@pytest.mark.asyncio
async def test_opening_health_does_not_use_the_job_budget():
    """A peer that accepts and never answers must not wait out the client timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    sock.settimeout(0.5)
    port = sock.getsockname()[1]
    stop = threading.Event()
    held: list[socket.socket] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(0.5)
            held.append(conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    from src.opencode_serve import OpenCodeServeClient, ServeOrchestrator

    client = OpenCodeServeClient(
        f"http://127.0.0.1:{port}",
        timeout_seconds=15.0,
    )
    try:
        started = time.monotonic()
        result = await ServeOrchestrator(client=client).run(prompt="go", title="t")
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        sock.close()
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass
        thread.join(2)
        await client.aclose()
    assert elapsed < 8.0, elapsed
    assert result.returncode == 1
    assert result.incomplete is False
    assert "OpenCode serve is not answering" in (result.stderr or "")


def test_down_serve_starts_while_a_job_is_running():
    """A dead serve is started again even while a job is planning or executing."""
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[], jobs=["KAN-1"]
    )
    result = sup.ensure_started()
    assert result["status"] == "started", result
    assert spawned == [7]
    assert killed == []


@pytest.mark.asyncio
async def test_deferred_reload_still_starts_a_dead_serve_during_a_job():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[], jobs=["KAN-1"], interval=0
    )
    assert sup.request_reload()["status"] == "deferred"
    calls = {"n": 0}

    def running() -> bool:
        calls["n"] += 1
        return calls["n"] < 4

    await sup.watch(running)
    assert spawned == [7], (spawned, sup.status())
    assert killed == []
    assert sup.status()["status"] == "deferred"


@pytest.mark.asyncio
async def test_watch_starts_a_dead_serve_while_a_job_is_running():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[], jobs=["KAN-1"], interval=0
    )
    calls = {"n": 0}

    def running() -> bool:
        calls["n"] += 1
        return calls["n"] < 4

    await sup.watch(running)
    assert spawned == [7], (spawned, sup.status())
    assert killed == []


@pytest.mark.asyncio
async def test_watch_leaves_a_serve_that_recovers_during_a_job():
    """A health blip during a job must not kill the serve after the job ends."""
    sup, killed, spawned, healthy, jobs, _listeners = _supervisor(
        healthy=False, listeners=[7], jobs=["KAN-1"], interval=0
    )
    calls = {"n": 0}

    def running() -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            healthy["up"] = True
            jobs["keys"] = []
        return calls["n"] < 4

    await sup.watch(running)
    assert killed == [], (killed, spawned, sup.status())
    assert spawned == []


def _reload_blocked_in_spawn():
    """Supervisor whose spawn waits until ``release`` is set."""
    proc = _Proc(7)
    listeners = {"pids": [proc.pid]}
    healthy = {"up": True}
    killed: list[int] = []
    spawned: list[int] = []
    active = {"n": 0, "max": 0}
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def kill(pid: int) -> None:
        killed.append(pid)
        if pid == proc.pid:
            proc.code = 0
            healthy["up"] = False
        listeners["pids"] = [item for item in listeners["pids"] if item != pid]

    def spawn():
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            spawned.append(proc.pid)
            entered.set()
        if not release.wait(3):
            raise TimeoutError("spawn was not released")
        with lock:
            active["n"] -= 1
        proc.code = None
        listeners["pids"] = [proc.pid]
        healthy["up"] = True
        return proc

    sup, _killed, _spawned, _healthy, jobs, _listeners = _supervisor(
        spawn=spawn,
        kill=kill,
        listener_pids=lambda _port: list(listeners["pids"]),
        is_serve_pid=lambda pid: pid == proc.pid,
        healthy_fn=lambda: healthy["up"],
        interval=0,
    )
    return sup, killed, spawned, jobs, entered, release, active


def test_second_save_during_reload_is_loaded_when_the_first_finishes():
    sup, _killed, spawned, _jobs, entered, release, _active = _reload_blocked_in_spawn()
    first: dict[str, object] = {}

    def run_first() -> None:
        first["result"] = sup.request_reload()

    worker = threading.Thread(target=run_first)
    worker.start()
    try:
        assert entered.wait(2)
        second = sup.request_reload()
        assert second["status"] == "deferred", second
        assert len(spawned) == 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    if sup.status()["status"] == "deferred":
        follow = sup.request_reload()
        assert follow["status"] == "reloaded", follow
    assert len(spawned) >= 2, (len(spawned), sup.status(), first.get("result"))


def test_job_during_reload_does_not_start_a_second_serve():
    sup, _killed, spawned, jobs, entered, release, active = _reload_blocked_in_spawn()
    worker = threading.Thread(target=sup.request_reload)
    worker.start()
    second_done = threading.Event()
    try:
        assert entered.wait(2)
        assert len(spawned) == 1
        jobs["keys"] = ["KAN-1"]
        paused = sup.request_reload()
        assert paused["status"] == "deferred", paused
        jobs["keys"] = []
        late = threading.Thread(target=lambda: (sup.request_reload(), second_done.set()))
        late.start()
        finished = second_done.wait(2)
        assert finished and active["max"] == 1 and len(spawned) == 1, (
            f"concurrent reloads: returned={finished} "
            f"depth={active['max']} spawns={len(spawned)}"
        )
    finally:
        release.set()
        worker.join(3)
        late.join(3) if "late" in locals() else None


def test_daemon_always_starts_serve(monkeypatch):
    from src.config import Settings
    from src.daemon import JiraAgentDaemon

    assert "manage_opencode_serve" not in Settings.model_fields
    calls: dict[str, object] = {}

    class _Sup:
        def bind(self, **kwargs):
            calls["kwargs"] = kwargs

        def ensure_started(self):
            calls["started"] = True
            return {"status": "started", "message": "OpenCode serve started."}

    monkeypatch.setattr("src.opencode_serve_supervisor.supervisor", _Sup())
    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon.processor = object()
    daemon._attach_opencode_serve()
    kwargs = calls["kwargs"]
    assert isinstance(kwargs, dict)
    assert set(kwargs) == {"live_jobs"}
    assert callable(kwargs["live_jobs"])
    assert calls["started"] is True
    assert daemon._opencode_serve is not None


@pytest.mark.asyncio
async def test_daemon_stop_stops_the_serve_it_started():
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = None
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=0)
    serve = MagicMock()
    daemon._opencode_serve = serve
    with patch("sys.exit"):
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
    serve.stop_owned.assert_called_once()


def test_pending_reload_restarts_only_after_the_running_job_is_gone():
    """Queued work must not start against the serve that is about to reload."""
    sup, killed, spawned, _healthy, jobs, _listeners = _supervisor(jobs=["KAN-636"])
    assert sup.request_reload()["status"] == "deferred"
    held = sup.apply_pending_reload()
    assert held["status"] == "deferred", held
    assert killed == []
    assert spawned == []
    jobs["keys"] = []
    done = sup.apply_pending_reload()
    assert done["status"] == "reloaded", done
    assert killed == [7]
    assert spawned == [7]


def test_apply_pending_reload_does_nothing_when_agents_are_current():
    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor()
    result = sup.apply_pending_reload()
    assert result["status"] == "ready", result
    assert killed == []
    assert spawned == []


@pytest.mark.asyncio
async def test_queue_does_not_start_while_an_agent_reload_is_deferred(monkeypatch):
    from src.config import settings
    from src.opencode_serve_supervisor import supervisor
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.queue_store.enqueue(source="jira", issue_key="KAN-637", summary="queued plan")
    monkeypatch.setattr(supervisor, "reload_outstanding", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "apply_pending_reload",
        lambda timeout=90.0: {
            "status": "deferred",
            "message": "Agents are saved. OpenCode will reload when KAN-636 finishes.",
        },
    )
    started = []

    async def run(rec):
        started.append(rec.get("issue_key"))

    monkeypatch.setattr(proc, "_run_queue_item", run)
    assert await proc.dispatch_queue() == 0
    await asyncio.sleep(0)
    assert started == []
    queued = proc.queue_store.list_items(status="queued", limit=10)
    assert [row.get("issue_key") for row in queued] == ["KAN-637"]


@pytest.mark.asyncio
async def test_queue_reloads_serve_before_starting_the_next_job(monkeypatch):
    from src.config import settings
    from src.opencode_serve_supervisor import supervisor
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.queue_store.enqueue(source="jira", issue_key="KAN-638", summary="queued plan")
    state = {"outstanding": True}
    order: list[str] = []

    def outstanding() -> bool:
        return state["outstanding"]

    def apply(timeout=90.0):
        order.append("reload")
        state["outstanding"] = False
        return {
            "status": "reloaded",
            "message": "OpenCode reloaded and is using the saved agents.",
        }

    monkeypatch.setattr(supervisor, "reload_outstanding", outstanding)
    monkeypatch.setattr(supervisor, "apply_pending_reload", apply)

    async def run(rec):
        order.append(str(rec.get("issue_key")))

    monkeypatch.setattr(proc, "_run_queue_item", run)
    assert await proc.dispatch_queue() == 1
    await asyncio.sleep(0)
    assert order == ["reload", "KAN-638"]


def test_failed_reload_stays_outstanding_until_a_later_attempt_succeeds():
    """A reload that errors has not loaded the saved agents."""
    sup, _killed, spawned, healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[]
    )

    def spawn():
        spawned.append(7)
        healthy["up"] = False
        return _Proc(7)

    sup._spawn_fn = spawn
    failed = sup.request_reload()
    assert failed["status"] == "failed"
    assert sup.reload_outstanding() is True
    assert sup.status()["status"] == "failed"
    before = len(spawned)
    again = sup.apply_pending_reload()
    assert again["status"] == "failed"
    assert len(spawned) == before + 1
    assert sup.reload_outstanding() is True
    healthy["up"] = True

    def spawn_ok():
        spawned.append(7)
        healthy["up"] = True
        return _Proc(7)

    sup._spawn_fn = spawn_ok
    done = sup.apply_pending_reload()
    assert done["status"] == "reloaded", done
    assert sup.reload_outstanding() is False


@pytest.mark.asyncio
async def test_queue_does_not_start_when_agent_reload_fails(monkeypatch):
    """Deferred and reloading already hold the queue. A failed reload must too."""
    import src.opencode_serve_supervisor as serve_sup
    from src.config import settings
    from src.processor import JobProcessor

    sup, _killed, _spawned, healthy, _jobs, _listeners = _supervisor(
        healthy=False, listeners=[]
    )

    def spawn():
        healthy["up"] = False
        return _Proc(7)

    sup._spawn_fn = spawn
    monkeypatch.setattr(serve_sup, "supervisor", sup)
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    assert sup.request_reload()["status"] == "failed"
    proc = JobProcessor()
    proc.queue_store.enqueue(source="jira", issue_key="KAN-642", summary="queued plan")
    started: list[str] = []

    async def run(rec):
        started.append(str(rec.get("issue_key")))

    monkeypatch.setattr(proc, "_run_queue_item", run)
    assert await proc.dispatch_queue() == 0
    assert await proc.dispatch_queue() == 0
    await asyncio.sleep(0)
    assert started == []
    queued = proc.queue_store.list_items(status="queued", limit=10)
    assert [row.get("issue_key") for row in queued] == ["KAN-642"]
    healthy["up"] = True

    def spawn_ok():
        healthy["up"] = True
        return _Proc(7)

    sup._spawn_fn = spawn_ok
    assert await proc.dispatch_queue() == 1
    await asyncio.sleep(0)
    assert started == ["KAN-642"]


@pytest.mark.asyncio
async def test_queue_does_not_start_when_reload_state_cannot_be_read(monkeypatch):
    """An unreadable reload flag is not a clear reload."""
    from src.config import settings
    from src.opencode_serve_supervisor import supervisor
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.queue_store.enqueue(source="jira", issue_key="KAN-643", summary="queued plan")

    def outstanding() -> bool:
        raise RuntimeError("reload state locked")

    monkeypatch.setattr(supervisor, "reload_outstanding", outstanding)
    started: list[str] = []

    async def run(rec):
        started.append(str(rec.get("issue_key")))

    monkeypatch.setattr(proc, "_run_queue_item", run)
    assert await proc.dispatch_queue() == 0
    await asyncio.sleep(0)
    assert started == []
    queued = proc.queue_store.list_items(status="queued", limit=10)
    assert [row.get("issue_key") for row in queued] == ["KAN-643"]


def test_unreadable_queue_does_not_kill_a_listening_serve():
    """A failed queue read is not an empty queue."""

    class _States:
        def get_active_issues(self):
            return []

    class _Queue:
        def list_items(self, **_kwargs):
            raise RuntimeError("database is locked")

    class _Processor:
        state_manager = _States()
        queue_store = _Queue()

        def list_live_processing_keys(self):
            return []

    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False,
        listeners=[7],
        live_jobs=lambda: blocking_issue_keys(_Processor()),
    )
    for _ in range(4):
        result = sup.ensure_started()
    assert killed == [], (killed, result)
    assert spawned == []
    reloaded = sup.request_reload()
    assert killed == [], (killed, reloaded)
    assert reloaded["status"] == "deferred"


def test_unreadable_issue_state_does_not_kill_a_listening_serve():
    """A failed job-list read is not "no jobs".

    Production binds ``blocking_issue_keys``. An empty list means the
    serve is idle, and three missed health checks then replace it. A
    state-read error has to surface so that replacement does not run.
    """

    class _States:
        def get_active_issues(self):
            raise RuntimeError("sqlite locked")

    class _Processor:
        state_manager = _States()

        def list_live_processing_keys(self):
            return []

    sup, killed, spawned, _healthy, _jobs, _listeners = _supervisor(
        healthy=False,
        listeners=[7],
        live_jobs=lambda: blocking_issue_keys(_Processor()),
    )
    for _ in range(4):
        result = sup.ensure_started()
    assert killed == [], (killed, result)
    assert spawned == []


@pytest.mark.asyncio
async def test_dispatch_arriving_during_a_deferred_reload_is_not_lost(
    monkeypatch,
):
    """The kick that lands while reload is deferred has to run after it.

    Dispatch clears ``_queue_dispatch_again`` and then returns when reload
    stays deferred. The kick that arrived during that check is the signal
    that the blocking job finished. Dropping it leaves the queued row
    queued and the agent reload still outstanding.
    """
    import threading

    from src.config import settings
    from src.opencode_serve_supervisor import supervisor
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    monkeypatch.setattr(settings, "max_concurrent_jobs", 4)
    proc = JobProcessor()
    proc.queue_store.enqueue(source="jira", issue_key="KAN-640", summary="queued plan")
    entered = threading.Event()
    release = threading.Event()
    state = {"outstanding": True, "apply": 0}
    started: list[str] = []

    def outstanding() -> bool:
        return state["outstanding"]

    def apply(timeout=90.0):
        state["apply"] += 1
        if state["apply"] == 1:
            entered.set()
            assert release.wait(3)
            return {
                "status": "deferred",
                "message": "Agents are saved. OpenCode will reload when KAN-1 finishes.",
            }
        state["outstanding"] = False
        return {
            "status": "reloaded",
            "message": "OpenCode reloaded and is using the saved agents.",
        }

    monkeypatch.setattr(supervisor, "reload_outstanding", outstanding)
    monkeypatch.setattr(supervisor, "apply_pending_reload", apply)

    async def run(rec):
        started.append(str(rec.get("issue_key")))

    monkeypatch.setattr(proc, "_run_queue_item", run)
    holder = asyncio.create_task(proc.dispatch_queue())
    assert await asyncio.to_thread(entered.wait, 2)
    proc.queue_store.enqueue(source="jira", issue_key="KAN-641", summary="follow-up")
    assert await proc.dispatch_queue() == 0
    assert proc._queue_dispatch_again is True
    release.set()
    assert await holder >= 1
    await asyncio.sleep(0)
    assert state["apply"] >= 2
    assert state["outstanding"] is False
    assert started == ["KAN-640", "KAN-641"]
