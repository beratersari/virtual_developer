"""Force-kill a process and its descendants.

Used when a job is cancelled: git clone/fetch, glab, Codex, and any
agent tool (bash/npm/git) still running in the job workspace must die
immediately (SIGKILL / taskkill /F /T), not after a polite TERM.

Never kill the daemon itself or the shared ``opencode serve`` process.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set


def kill_process_tree(process: Any, *, force: bool = True) -> None:
    """Kill ``process`` and, when possible, its whole process group / tree."""
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if pid is not None:
        kill_pid(int(pid), force=force)
        return
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def kill_pid(pid: int, *, force: bool = True) -> None:
    """Force-kill one pid and its children (process group on Unix)."""
    if pid <= 0:
        return
    if os.name == "nt":
        _kill_pid_windows(pid, force=force)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    # Children first so they cannot outlive a parent that already died.
    for child in _direct_child_pids(pid):
        if child != pid:
            try:
                os.kill(child, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    try:
        os.killpg(pid, sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.killpg(os.getpgid(pid), sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _direct_child_pids(pid: int) -> List[int]:
    """Read ``/proc/<pid>/task/*/children`` only — never scan all of /proc.

    A full ``/proc`` walk can hang uninterruptibly on WSL/9p. This is a
    handful of files for one process tree.
    """
    task_dir = Path(f"/proc/{int(pid)}/task")
    if not task_dir.is_dir():
        return []
    found: List[int] = []
    try:
        tasks = list(task_dir.iterdir())
    except OSError:
        return []
    for task in tasks:
        try:
            raw = (task / "children").read_text(encoding="ascii", errors="ignore")
        except OSError:
            continue
        for part in raw.split():
            try:
                found.append(int(part))
            except ValueError:
                continue
    return found


def descendant_pids(pid: int) -> List[int]:
    """All descendants of ``pid`` (not including ``pid``). Narrow /proc reads."""
    if pid <= 0:
        return []
    seen: Set[int] = set()
    stack = [int(pid)]
    out: List[int] = []
    while stack:
        cur = stack.pop()
        for child in _direct_child_pids(cur):
            if child in seen or child <= 0:
                continue
            seen.add(child)
            out.append(child)
            stack.append(child)
    return out


def pid_cwd(pid: int) -> Optional[Path]:
    """Working directory of ``pid``, or None. Single ``/proc`` read."""
    if pid <= 0 or os.name == "nt":
        return None
    try:
        return Path(os.readlink(f"/proc/{int(pid)}/cwd"))
    except OSError:
        return None


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def opencode_serve_pids() -> List[int]:
    """PIDs of the shared ``opencode serve`` (must not be killed on job cancel)."""
    pids: List[int] = []
    try:
        if os.name == "nt":
            r = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq opencode.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in (r.stdout or "").splitlines():
                parts = [p.strip().strip('"') for p in line.split(",")]
                if len(parts) >= 2 and parts[1].isdigit():
                    pids.append(int(parts[1]))
        else:
            r = subprocess.run(
                ["pgrep", "-f", "opencode serve"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for tok in (r.stdout or "").split():
                try:
                    pids.append(int(tok))
                except ValueError:
                    continue
    except Exception:
        return []
    return pids


def kill_workspace_processes(
    workspace: Optional[Path],
    *,
    extra_root_pids: Optional[Iterable[int]] = None,
    force: bool = True,
) -> int:
    """Force-kill leftover job children whose cwd is ``workspace``.

    Walks descendants of this process (git/codex we spawned) and of
    ``opencode serve`` (agent tools). Does **not** kill the daemon or serve.
    """
    if workspace is None:
        return 0
    try:
        root = Path(workspace).resolve()
    except OSError:
        return 0
    if not str(root):
        return 0

    if os.name == "nt":
        return _kill_workspace_windows(root, extra_root_pids or (), force=force)
    return _kill_workspace_unix(root, extra_root_pids or (), force=force)


def _protect_pids() -> Set[int]:
    """PIDs that must survive job cancel (daemon + shared serve)."""
    protected = {os.getpid(), os.getppid()}
    for p in opencode_serve_pids():
        protected.add(p)
    return protected


def _iter_int_pids(raw: Iterable[int]) -> List[int]:
    out: List[int] = []
    for p in raw:
        try:
            ip = int(p)
        except (TypeError, ValueError):
            continue
        if ip > 0:
            out.append(ip)
    return out


def _kill_workspace_unix(
    root: Path,
    extra_root_pids: Iterable[int],
    *,
    force: bool,
) -> int:
    protected = _protect_pids()
    walk_from = {os.getpid(), *protected, *_iter_int_pids(extra_root_pids)}
    targets: List[int] = []
    seen: Set[int] = set()
    for rp in walk_from:
        for child in descendant_pids(rp):
            if child in seen or child in protected:
                continue
            seen.add(child)
            cwd = pid_cwd(child)
            if cwd is not None and _path_is_under(cwd, root):
                targets.append(child)
    killed = 0
    for pid in reversed(targets):
        kill_pid(pid, force=force)
        killed += 1
    return killed


def _kill_workspace_windows(
    root: Path,
    extra_root_pids: Iterable[int],
    *,
    force: bool,
) -> int:
    """Kill tracked trees; also any process whose command line names ``root``."""
    protected = _protect_pids()
    killed = 0
    for ip in _iter_int_pids(extra_root_pids):
        if ip in protected:
            continue
        kill_pid(ip, force=force)
        killed += 1
    needle = str(root).replace("/", "\\").lower()
    if not needle:
        return killed
    try:
        r = subprocess.run(
            [
                "wmic",
                "process",
                "get",
                "ProcessId,CommandLine",
                "/FORMAT:CSV",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return killed
    for line in (r.stdout or "").splitlines():
        low = line.lower()
        if needle not in low:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        pid = None
        for part in reversed(parts):
            if part.isdigit():
                pid = int(part)
                break
        if pid is None or pid in protected:
            continue
        if "opencode" in low and "serve" in low:
            continue
        kill_pid(pid, force=force)
        killed += 1
    return killed


def _kill_pid_windows(pid: int, *, force: bool) -> None:
    flags = ["/F", "/T", "/PID", str(pid)] if force else ["/T", "/PID", str(pid)]
    try:
        subprocess.run(
            ["taskkill", *flags],
            capture_output=True,
            timeout=15,
            check=False,
        )
        return
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
