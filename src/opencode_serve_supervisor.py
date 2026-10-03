"""Start and reload the shared ``opencode serve`` process.

The daemon starts serve when ``OPENCODE_SERVE_URL`` is down, as the same
user, and restarts the child it started if that child exits. A serve that
is already healthy is left running. Shutdown stops only that child.

OpenCode reads agent files when the process starts. Saving, creating, or
syncing the catalog copies the files into the OpenCode and Claude homes
and then reloads the process on the serve port. A job in ``planning`` or
``executing`` keeps the current process; the reload runs after it finishes.

A missed ``/global/health`` does not kill a process that is still
listening while a job already has a session. The opening gate is ready
only after ``/global/health`` answers. A listener that does not answer
fails that gate in a few seconds, the job leaves executing, and the quiet
process can then be replaced. Nothing listening is started again,
including while a job is running. Three missed checks with no job replace
a quiet process. The stop checks for a job once more immediately before
the kill. A job that arrives during a reload waits for that one restart.

Job cancel must not come through here. ``src/process_kill.py`` still
refuses to kill this process while it is stopping a job.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from src.logger import logger

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
# One probe is a few seconds. A busy serve can miss one. Three idle misses
# in a row mean the listener is quiet and may be replaced. A live job never
# counts toward that, so the replacement does not run as the job ends.
_HEALTH_MISS_LIMIT = 3
# The opening job gate uses this same budget. It is not the agent timeout.
HEALTH_PROBE_TIMEOUT_SECONDS = 3.0


def serve_target(url: str = "") -> tuple[str, str, int]:
    """Return ``(health_url, bind_host, port)`` for the configured serve."""
    raw = (url or "").strip()
    if not raw:
        from src.config import settings

        raw = (getattr(settings, "opencode_serve_url", None) or "").strip()
    if not raw:
        raw = "http://127.0.0.1:4096"
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 4096)
    probe = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    scheme = parsed.scheme or "http"
    return f"{scheme}://{probe}:{port}/global/health", host, port


def serve_command(binary: str, host: str, port: int) -> List[str]:
    return [
        binary,
        "serve",
        "--port",
        str(port),
        "--hostname",
        host,
        "--print-logs",
        "--log-level",
        "INFO",
    ]


def serve_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GCM_MODAL_PROMPT"] = "false"
    env["GCM_GUI_PROMPT"] = "false"
    return env


def resolve_opencode_binary() -> Optional[str]:
    """OpenCode executable: configured path, PATH, then ``~/.opencode/bin``."""
    from src.config import settings

    raw = (getattr(settings, "opencode_cli", None) or "opencode").strip() or "opencode"
    candidate = Path(raw)
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(raw)
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home()))
    for name in ("opencode.exe", "opencode"):
        path = home / ".opencode" / "bin" / name
        if path.is_file():
            return str(path)
    return None


def pids_from_netstat(text: str, port: int) -> List[int]:
    """Listening PIDs from ``netstat -ano`` for one local port."""
    suffix = f":{int(port)}"
    found: List[int] = []
    for line in (text or "").splitlines():
        if "LISTEN" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 4 or not parts[1].endswith(suffix):
            continue
        pid_text = parts[-1]
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid > 0 and pid not in found:
            found.append(pid)
    return found


def pids_from_ss(text: str) -> List[int]:
    found: List[int] = []
    for match in re.findall(r"pid=(\d+)", text or ""):
        pid = int(match)
        if pid > 0 and pid not in found:
            found.append(pid)
    return found


def _status_value(status: Any) -> str:
    value = getattr(status, "value", status)
    return str(value or "").strip().lower()


def blocking_issue_keys(processor: Any) -> List[str]:
    """Issue keys whose serve session must stay up.

    Live processing slots count, and so does local ``planning`` / ``executing``
    state. ``pending`` does not: the agent has not opened a session yet.
    """
    if processor is None:
        return []
    # Queued rows do not block a reload. An unreadable queue must not look
    # empty: list_items raises when sqlite cannot be read, and that failure
    # has to reach ``_jobs`` so a quiet listener is left running.
    store = getattr(processor, "queue_store", None)
    list_items = getattr(store, "list_items", None)
    if callable(list_items):
        list_items(status="running", limit=1)
    found: List[str] = []
    seen: set[str] = set()

    def add(key: Any) -> None:
        text = str(key or "").strip()
        if text and text not in seen:
            seen.add(text)
            found.append(text)

    live: Any = None
    try:
        live = processor.list_live_processing_keys()
    except Exception:
        live = None
    if isinstance(live, list):
        for key in live:
            add(key)
    else:
        contexts = getattr(processor, "_contexts", None)
        if isinstance(contexts, dict):
            for key in contexts:
                add(key)
    # A state-read failure must reach ``_jobs``. An empty list means idle,
    # and an idle serve is replaced after repeated health misses.
    states = processor.state_manager.get_active_issues()
    if isinstance(states, list):
        for state in states:
            if _status_value(getattr(state, "status", "")) in {"planning", "executing"}:
                add(getattr(state, "issue_key", ""))
    return found


def probe_healthy(url: str = "") -> bool:
    """True when ``/global/health`` reports healthy. Never raises."""
    import httpx
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    target = url or serve_target()[0]
    try:
        # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
        with httpx.Client(timeout=HEALTH_PROBE_TIMEOUT_SECONDS, verify=False) as client:
            response = client.get(target)
        if response.status_code < 200 or response.status_code >= 500:
            return False
        return "healthy" in (response.text or "").lower()
    except Exception:
        return False


def default_listener_pids(port: int) -> List[int]:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            return pids_from_netstat(result.stdout or "", port)
        result = subprocess.run(
            ["ss", "-ltnp", f"sport = :{int(port)}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        return pids_from_ss(result.stdout or "")
    except Exception:
        return []


def default_is_serve_pid(pid: int) -> bool:
    """True when ``pid`` looks like ``opencode serve``, not the daemon."""
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            from src.process_kill import _pid_exe_windows

            exe = _pid_exe_windows(pid).replace("/", "\\").lower()
        except Exception:
            return False
        return exe.endswith("\\opencode.exe") or exe.endswith("\\opencode")
    command = _posix_command(pid)
    return "opencode" in command and "serve" in command


def _posix_command(pid: int) -> str:
    path = Path(f"/proc/{int(pid)}/cmdline")
    try:
        raw = path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        if raw.strip():
            return raw.lower()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return (result.stdout or "").lower()
    except Exception:
        return ""


def _default_kill(pid: int) -> None:
    from src.process_kill import kill_pid

    kill_pid(int(pid), force=True)


class OpenCodeServeSupervisor:
    """One serve process for this daemon. Safe to call from HTTP threads."""

    def __init__(
        self,
        *,
        healthy: Optional[Callable[[], bool]] = None,
        spawn: Optional[Callable[[], Any]] = None,
        kill: Optional[Callable[[int], None]] = None,
        listener_pids: Optional[Callable[[int], List[int]]] = None,
        is_serve_pid: Optional[Callable[[int], bool]] = None,
        live_jobs: Optional[Callable[[], List[str]]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        interval: float = 2.0,
        reload_marker: Optional[Path] = None,
    ) -> None:
        self._healthy_fn = healthy
        self._spawn_fn = spawn
        self._kill_fn = kill
        self._listener_fn = listener_pids
        self._is_serve_fn = is_serve_pid
        self._jobs_fn = live_jobs or (lambda: [])
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._interval = interval
        self._lock = threading.Lock()
        self._proc: Any = None
        self._log: Any = None
        self._pending = False
        self._deferred_message = ""
        self._reloading = False
        self._stopping = False
        self._last: Optional[Dict[str, str]] = None
        self._fails = 0
        self._next_start = 0.0
        self._misses = 0
        self._miss_logged = False
        self._reload_marker = (
            Path(reload_marker) if reload_marker is not None else _reload_marker_path()
        )

    def bind(self, *, live_jobs: Callable[[], List[str]]) -> None:
        """Record who is in flight so a reload can wait."""
        self._jobs_fn = live_jobs

    def _marker_present(self) -> bool:
        """True when a save still needs a restart after this process is gone.

        An unreadable marker is not "no reload". The queue must not open a
        session on a serve that may still have the previous agents.
        """
        path = self._reload_marker
        try:
            return path.is_file()
        except OSError:
            return True

    def _write_marker(self) -> None:
        path = self._reload_marker
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pending", encoding="utf-8")
            except OSError as exc:
                logger.warning(f"OpenCode reload was not recorded: {exc}")

    def _clear_marker(self) -> None:
        # ``_reloading`` is still true here: this reload has not released
        # the claim yet. A second save sets ``_pending`` and must keep the file.
        with self._lock:
            if self._pending:
                return
            path = self._reload_marker
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"OpenCode reload marker was not cleared: {exc}")

    def _jobs(self) -> Optional[List[str]]:
        """Live issue keys, or ``None`` when the list cannot be read.

        ``None`` is not "no jobs". A kill must not run on an unknown list.
        """
        try:
            keys = self._jobs_fn()
        except Exception as exc:
            logger.warning(f"OpenCode serve could not list live jobs: {exc}")
            return None
        if not isinstance(keys, list):
            logger.warning("OpenCode serve could not list live jobs.")
            return None
        return [str(key) for key in keys if str(key).strip()]

    def _blocking_jobs(self) -> List[str]:
        """Keys that forbid a kill. An unreadable list counts as busy."""
        keys = self._jobs()
        if keys is None:
            return ["the current job"]
        return keys

    def _healthy(self) -> bool:
        try:
            if self._healthy_fn is not None:
                return bool(self._healthy_fn())
            return probe_healthy()
        except Exception:
            return False

    def _port(self) -> int:
        return serve_target()[2]

    def _listeners(self) -> List[int]:
        try:
            if self._listener_fn is not None:
                found = self._listener_fn(self._port())
            else:
                found = default_listener_pids(self._port())
        except Exception:
            return []
        if not isinstance(found, list):
            return []
        pids: List[int] = []
        for item in found:
            try:
                pid = int(item)
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
        return pids

    def _is_serve(self, pid: int) -> bool:
        if pid <= 0 or pid == os.getpid():
            return False
        try:
            if self._is_serve_fn is not None:
                return bool(self._is_serve_fn(pid))
            return default_is_serve_pid(pid)
        except Exception:
            return False

    def _owned_pid(self) -> Optional[int]:
        proc = self._proc
        if proc is None:
            return None
        try:
            if proc.poll() is not None:
                return None
        except Exception:
            return None
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            return pid
        return None

    def _kill(self, pid: int) -> None:
        if pid <= 0 or pid == os.getpid():
            return
        if self._kill_fn is not None:
            self._kill_fn(pid)
            return
        _default_kill(pid)

    def _opencode_listening(self) -> bool:
        """True when the serve child or an ``opencode serve`` listener is up."""
        if self._owned_pid():
            return True
        for pid in self._listeners():
            if self._is_serve(pid):
                return True
        return False

    def _foreign_listener(self) -> bool:
        owned = self._owned_pid()
        for pid in self._listeners():
            if pid == os.getpid() or pid == owned:
                continue
            if not self._is_serve(pid):
                return True
        return False

    def _kill_serve(self) -> None:
        targets: List[int] = []
        owned = self._owned_pid()
        if owned:
            targets.append(owned)
        for pid in self._listeners():
            if pid in targets or pid == os.getpid():
                continue
            if self._is_serve(pid):
                targets.append(pid)
        proc = self._proc
        self._proc = None
        for pid in targets:
            try:
                self._kill(pid)
            except Exception as exc:
                logger.warning(f"Could not stop OpenCode serve pid {pid}: {exc}")
        self._wait_dead(targets, proc)
        self._close_log()

    def _wait_dead(self, pids: List[int], proc: Any) -> None:
        deadline = self._monotonic() + 5
        while self._monotonic() < deadline:
            listening = set(self._listeners())
            owned_alive = False
            if proc is not None:
                try:
                    owned_alive = proc.poll() is None
                except Exception:
                    owned_alive = False
            if not owned_alive and not any(pid in listening for pid in pids):
                return
            self._sleep(0.1)

    def _wait_healthy(self, timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        while True:
            if self._stopping:
                return False
            if self._healthy():
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.25)

    def _wait_down(self, timeout: float = 5) -> bool:
        """True once the old serve has stopped answering."""
        deadline = self._monotonic() + timeout
        while True:
            if self._stopping:
                return False
            if not self._healthy():
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.25)

    def _remember(self, result: Dict[str, str]) -> Dict[str, str]:
        # A successful restart must not clear ``_pending``. A second save
        # during this cycle sets that flag, and the owner runs again so the
        # new process reads those files. ``_defer`` is what sets it.
        with self._lock:
            self._last = dict(result)
            if result.get("status") == "deferred":
                self._pending = True
                self._deferred_message = result.get("message") or ""
        return result

    def _defer(self, keys: List[str]) -> Dict[str, str]:
        """Save the reload for later. Do not kill the serve under a live job.

        Deferred means the agent files are already on disk and the restart
        has not run. A planning or executing job, or a restart that is
        already in progress, is the reason. The queue must not claim the
        next job while this is set. ``reloading`` is the restart itself.
        ``failed`` is a restart that ran and did not load those files.
        """
        shown = ", ".join(keys[:3])
        if len(keys) > 3:
            shown = f"{shown} (+{len(keys) - 3} more)"
        return self._remember(
            {
                "status": "deferred",
                "message": (
                    f"Agents are saved. OpenCode will reload when {shown} finishes."
                ),
            }
        )

    def _keep_failed_reload_pending(self, result: Dict[str, str]) -> None:
        """A failed agent reload has not loaded the saved files.

        The queue starts work when nothing is pending. Leaving the flag
        clear after an error runs that work on the old process.
        """
        if self._stopping or str(result.get("status") or "") != "failed":
            return
        with self._lock:
            if self._stopping:
                return
            self._pending = True
            message = str(result.get("message") or "").strip()
            if message:
                self._deferred_message = message

    def _mark_healthy(self) -> None:
        with self._lock:
            self._misses = 0
            self._miss_logged = False

    def _clear_miss_count(self) -> None:
        with self._lock:
            self._misses = 0

    def _note_miss(self) -> int:
        with self._lock:
            self._misses += 1
            return self._misses

    def _leave_running(self) -> Dict[str, str]:
        with self._lock:
            first = not self._miss_logged
            self._miss_logged = True
        if first:
            logger.info(
                "OpenCode serve missed a health check; left the process running."
            )
        return {
            "status": "running",
            "message": "OpenCode serve missed a health check and was left running.",
        }

    def _port_busy(self) -> Dict[str, str]:
        return self._remember(
            {
                "status": "failed",
                "message": (
                    f"Port {self._port()} is in use, so OpenCode serve was not restarted."
                ),
            }
        )

    def _stopping_result(self) -> Dict[str, str]:
        return self._remember({"status": "failed", "message": "Yaver is stopping."})

    def _claim_reload(self) -> bool:
        with self._lock:
            if self._stopping or self._reloading:
                return False
            self._reloading = True
            return True

    def _release_claim(self) -> None:
        with self._lock:
            self._reloading = False

    def _cycle(self, *, kind: str) -> Dict[str, str]:
        """Kill the current serve and start one replacement.

        Jobs are read again after the port check. A job that appears in
        that gap keeps the process and leaves the reload pending.
        """
        if self._stopping:
            return self._stopping_result()
        blockers = self._blocking_jobs()
        if blockers:
            return self._defer(blockers)
        if self._foreign_listener():
            return self._port_busy()
        blockers = self._blocking_jobs()
        if blockers:
            return self._defer(blockers)
        return self._kill_then_spawn(kind=kind)

    def _kill_then_spawn(self, *, kind: str) -> Dict[str, str]:
        # Last look. A job that landed after the earlier checks keeps this
        # process. A health restart must not schedule an agent-file reload.
        blockers = self._blocking_jobs()
        if blockers:
            if kind == "started":
                return self._leave_running()
            return self._defer(blockers)
        self._kill_serve()
        if self._stopping:
            return self._stopping_result()
        if not self._wait_down():
            return self._remember(
                {
                    "status": "failed",
                    "message": "OpenCode did not stop, so the saved agents are not loaded yet.",
                }
            )
        return self._spawn_and_wait(kind=kind)

    def _spawn_and_wait(self, *, kind: str) -> Dict[str, str]:
        """Start one child. Does not kill a listener first."""
        if self._stopping:
            return self._stopping_result()
        try:
            proc = self._spawn()
        except Exception as exc:
            logger.exception(f"OpenCode serve did not start: {exc}", exc)
            return self._remember(
                {
                    "status": "failed",
                    "message": "OpenCode serve did not start. Install OpenCode and try again.",
                }
            )
        if proc is None:
            return self._remember(
                {
                    "status": "failed",
                    "message": "OpenCode serve did not start. Install OpenCode and try again.",
                }
            )
        with self._lock:
            if self._stopping:
                self._proc = None
                abandon = proc
            else:
                self._proc = proc
                abandon = None
        if abandon is not None:
            try:
                if abandon.poll() is None:
                    self._kill(int(getattr(abandon, "pid", 0) or 0))
            except Exception:
                pass
            return self._stopping_result()
        if not self._wait_healthy(30):
            return self._remember(
                {
                    "status": "failed",
                    "message": "OpenCode serve did not become healthy.",
                }
            )
        self._mark_healthy()
        # This process read the agent files at start. A second save during
        # the wait sets ``_pending`` and keeps the marker for another pass.
        self._clear_marker()
        if kind == "started":
            result = {"status": "started", "message": "OpenCode serve started."}
        else:
            result = {
                "status": "reloaded",
                "message": "OpenCode reloaded and is using the saved agents.",
            }
        logger.info(f"OpenCode serve {result['status']}: {result['message']}")
        return self._remember(result)

    def _restart_quiet_serve(self) -> Dict[str, str]:
        """Replace a listener that keeps missing health, once no job is running."""
        if not self._claim_reload():
            return {
                "status": "reloading",
                "message": "OpenCode is already restarting.",
            }
        try:
            blockers = self._blocking_jobs()
            if blockers:
                return self._leave_running()
            if self._foreign_listener():
                return self._port_busy()
            blockers = self._blocking_jobs()
            if blockers:
                return self._leave_running()
            return self._kill_then_spawn(kind="started")
        finally:
            self._release_claim()

    def _spawn(self) -> Any:
        if self._spawn_fn is not None:
            return self._spawn_fn()
        return self._spawn_default()

    def _spawn_default(self) -> Any:
        binary = resolve_opencode_binary()
        if not binary:
            raise FileNotFoundError("opencode")
        _health, host, port = serve_target()
        command = serve_command(binary, host, port)
        log = self._open_log()
        kwargs: Dict[str, Any] = {
            "cwd": _project_cwd(),
            "env": serve_env(),
            "stdin": subprocess.DEVNULL,
            "stdout": log if log is not None else subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(command, **kwargs)
        except Exception:
            self._close_log()
            raise

    def _open_log(self) -> Any:
        self._close_log()
        path = _log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "w" if path.is_file() and path.stat().st_size > 2_000_000 else "a"
            handle = path.open(mode, encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(f"OpenCode serve log was not opened: {exc}")
            return None
        self._log = handle
        return handle

    def _close_log(self) -> None:
        handle = self._log
        self._log = None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:
            pass

    def ensure_started(self) -> Dict[str, str]:
        """Start serve when nothing is listening. Leave a live process up.

        A failed health probe with the process still listening is not a
        crash. A planning or executing job is never a reason to kill it.
        The opening gate fails that job when health does not answer, so
        the job leaves executing and a later idle watch can replace it.
        With no job, the process is replaced only after repeated misses.
        """
        if self._stopping:
            return self._stopping_result()
        if self._healthy():
            self._mark_healthy()
            with self._lock:
                remembered = bool(self._pending or self._reloading)
            # Same process: a deferred reload stays deferred until the queue
            # or the watch applies it. A new process has an empty flag and
            # only the marker, so a healthy serve from before the restart
            # still has to read the saved agents.
            if not remembered and self._marker_present():
                blockers = self._blocking_jobs()
                if blockers:
                    return self._defer(blockers)
                return self.request_reload()
            result = {
                "status": "ready",
                "message": "OpenCode serve is already running.",
            }
            logger.info(f"OpenCode serve ready: {result['message']}")
            return self._remember(result)
        with self._lock:
            if self._reloading:
                return {
                    "status": "reloading",
                    "message": "OpenCode is already restarting.",
                }
        if self._foreign_listener() and not self._opencode_listening():
            return self._port_busy()
        if self._opencode_listening():
            if self._blocking_jobs():
                self._clear_miss_count()
                return self._leave_running()
            if self._note_miss() < _HEALTH_MISS_LIMIT:
                return self._leave_running()
            self._clear_miss_count()
            return self._restart_quiet_serve()
        if not self._claim_reload():
            return {
                "status": "reloading",
                "message": "OpenCode is already restarting.",
            }
        try:
            return self._spawn_and_wait(kind="started")
        finally:
            self._release_claim()

    def request_reload(self) -> Dict[str, str]:
        """Restart serve so it reads the agent files. Defer while a job runs.

        A second call while a restart is in progress does not start another
        process and does not clear the in-flight flag. That call is applied
        when the first restart finishes, unless a job is running by then.

        The marker is written before the restart. Shutdown and a force-kill
        both drop the in-memory flag. The serve process often keeps running
        (the launcher started it, or this child is in its own process group).
        The next daemon reads the marker and restarts that serve.
        """
        self._write_marker()
        with self._lock:
            if self._stopping:
                return {"status": "failed", "message": "Yaver is stopping."}
            if self._reloading:
                self._pending = True
                self._deferred_message = "OpenCode is already restarting."
                return {
                    "status": "deferred",
                    "message": self._deferred_message,
                }
        blockers = self._blocking_jobs()
        if blockers:
            return self._defer(blockers)
        if not self._claim_reload():
            with self._lock:
                self._pending = True
                self._deferred_message = "OpenCode is already restarting."
            return {
                "status": "deferred",
                "message": "OpenCode is already restarting.",
            }
        try:
            result: Dict[str, str] = {
                "status": "failed",
                "message": "OpenCode serve did not start. Install OpenCode and try again.",
            }
            while True:
                with self._lock:
                    # This pass consumes the request. A save that lands
                    # during the spawn sets ``_pending`` again.
                    self._pending = False
                result = self._cycle(kind="reloaded")
                if self._stopping or result.get("status") == "deferred":
                    return result
                with self._lock:
                    again = bool(self._pending)
                if not again:
                    self._keep_failed_reload_pending(result)
                    if str(result.get("status") or "") == "reloaded":
                        self._clear_marker()
                    return result
                blockers = self._blocking_jobs()
                if blockers:
                    return self._defer(blockers)
        finally:
            self._release_claim()

    def reload_outstanding(self) -> bool:
        """True when saved agent files still need a restart."""
        with self._lock:
            if self._pending or self._reloading:
                return True
        return self._marker_present()

    def apply_pending_reload(self, timeout: float = 90.0) -> Dict[str, str]:
        """Finish a deferred agent reload once no session is open.

        The queue calls this before starting another job. A planning or
        executing job keeps the reload pending. Waiting for a restart that
        is already in progress does not mark the reload pending again.
        """
        deadline = self._monotonic() + max(0.0, float(timeout))
        while True:
            if self._stopping:
                return self._stopping_result()
            if not self.reload_outstanding():
                return self.status()
            blockers = self._blocking_jobs()
            if blockers:
                return self.status()
            with self._lock:
                reloading = self._reloading
            if reloading:
                if self._monotonic() >= deadline:
                    return {
                        "status": "reloading",
                        "message": "OpenCode is restarting.",
                    }
                self._sleep(0.25)
                continue
            result = self.request_reload()
            status = str(result.get("status") or "")
            # One attempt. A failure stays pending for the next watch or
            # queue pass. Looping here would retry the same error for the
            # whole timeout.
            if status == "failed":
                return result
            if status == "deferred" and self.reload_outstanding():
                if self._blocking_jobs() or self._monotonic() >= deadline:
                    return result
                self._sleep(0.25)
                continue
            if self.reload_outstanding() and self._monotonic() < deadline:
                continue
            return result

    def wait_until_ready(self, timeout: float = 45.0) -> bool:
        """Block until ``/global/health`` answers.

        A reload already in progress is the only reason to wait. This does
        not spawn a second process while that reload holds the port. A
        process that is only listening is not ready. The opening gate then
        fails the job, the job leaves executing, and a later idle watch can
        replace the quiet process. Do not clear the miss count here.
        """
        deadline = self._monotonic() + max(0.0, float(timeout))
        while True:
            if self._stopping:
                return False
            with self._lock:
                reloading = self._reloading
            if reloading:
                if self._monotonic() >= deadline:
                    return False
                self._sleep(0.25)
                continue
            if self._healthy():
                self._mark_healthy()
                return True
            if self._opencode_listening():
                return False
            result = self.ensure_started()
            status = str(result.get("status") or "")
            with self._lock:
                reloading = self._reloading
            if reloading or status == "reloading":
                if self._monotonic() >= deadline:
                    return False
                self._sleep(0.25)
                continue
            if status in {"ready", "started", "reloaded"} and self._healthy():
                self._mark_healthy()
                return True
            if status == "running":
                return False
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.25)

    def status(self) -> Dict[str, str]:
        with self._lock:
            if self._reloading:
                return {"status": "reloading", "message": "OpenCode is restarting."}
            pending = self._pending
            deferred_message = self._deferred_message
            last = dict(self._last) if self._last else None
        if not pending and self._marker_present():
            pending = True
        if pending and last and last.get("status") == "failed":
            return last
        if pending:
            return {
                "status": "deferred",
                "message": deferred_message
                or "Agents are saved. OpenCode will reload when the current job finishes.",
            }
        healthy = self._healthy()
        if last and last.get("status") in {"reloaded", "started"} and healthy:
            return last
        if healthy:
            self._mark_healthy()
            return {"status": "ready", "message": "OpenCode serve is running."}
        if self._opencode_listening():
            return {
                "status": "running",
                "message": "OpenCode serve missed a health check and was left running.",
            }
        if last and last.get("status") == "failed":
            return last
        return {"status": "down", "message": "OpenCode serve is not running."}

    def stop_owned(self) -> None:
        """Stop the child this supervisor started. Leave a serve it did not start."""
        with self._lock:
            self._stopping = True
            self._pending = False
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    self._kill(int(getattr(proc, "pid", 0) or 0))
            except Exception as exc:
                logger.warning(f"Could not stop OpenCode serve: {exc}")
        self._close_log()

    async def watch(self, running: Callable[[], bool]) -> None:
        """Restart a dead child, and run a reload that waited for a job."""
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            if self._stopping or not running():
                return
            with self._lock:
                if self._reloading:
                    continue
                pending = self._pending
            if not pending and self._marker_present():
                pending = True
            try:
                if pending:
                    jobs = await asyncio.to_thread(self._jobs)
                    # None means the job list could not be read. Do not kill.
                    # A live job keeps the agent reload pending, and a dead
                    # serve is still started below.
                    if not (jobs is None or jobs):
                        await asyncio.to_thread(self.apply_pending_reload)
                        continue
                healthy = await asyncio.to_thread(self._healthy)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(f"OpenCode serve watch check failed: {exc}")
                continue
            if healthy:
                self._mark_healthy()
                self._fails = 0
                self._next_start = 0.0
                continue
            now = self._monotonic()
            if now < self._next_start:
                continue
            try:
                result = await asyncio.to_thread(self.ensure_started)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(f"OpenCode serve restart failed: {exc}")
                result = {"status": "failed"}
            if result.get("status") == "failed":
                self._fails += 1
                self._next_start = now + min(30.0, 5.0 * self._fails)
            else:
                self._fails = 0
                self._next_start = 0.0


def _project_cwd() -> Optional[str]:
    from src.config import settings

    root = getattr(settings, "project_root", None)
    if root and Path(str(root)).is_dir():
        return str(root)
    return None


def _reload_marker_path() -> Path:
    """File that remembers an agent reload across a daemon restart."""
    try:
        from src.paths import agent_data_dir

        root = Path(agent_data_dir())
    except Exception:
        root = Path(tempfile.gettempdir())
    return root / "opencode-serve-reload.pending"


def _log_path() -> Path:
    try:
        from src.paths import agent_data_dir

        root = Path(agent_data_dir())
    except Exception:
        root = Path(tempfile.gettempdir())
    return root / "opencode-serve.log"


def publish_catalog() -> Dict[str, Any]:
    """Copy the agent catalog into the homes, then reload serve when idle."""
    from src.opencode_agents import sync_agents

    copied = sync_agents()
    try:
        serve = supervisor.request_reload()
    except Exception as exc:
        logger.exception(f"OpenCode reload failed: {exc}", exc)
        serve = {
            "status": "failed",
            "message": "Agent files were copied. OpenCode could not be restarted.",
        }
    if not isinstance(serve, dict):
        serve = {
            "status": "failed",
            "message": "Agent files were copied. OpenCode could not be restarted.",
        }
    return {**copied, "serve": serve}


supervisor = OpenCodeServeSupervisor()
