"""Force-kill a process and its descendants.

Used when a job is cancelled: git clone/fetch, glab, Codex, and any
agent tool (bash/npm/git) still running in the job workspace must die
immediately (SIGKILL / taskkill /F /T), not after a polite TERM.

Never kill the daemon itself or the shared ``opencode serve`` process.

After kill, leftover ``.git/*.lock`` files (especially ``index.lock``)
must be removed so the next prompt can reuse the clone. A cancelled
``git checkout -B`` otherwise fails with "Another git process seems to
be running".
"""

from __future__ import annotations

import csv
import io
import os
import signal
import subprocess
import time
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
    """Kill tracked trees; also any process whose command line names ``root``.

    WMIC is removed on current Windows; prefer CIM. Also walk children of
    extra/serve PIDs so a ``git checkout`` that only has the clone as cwd
    (path not on argv) is still found via lock-holder reclaim afterwards.
    """
    protected = _protect_pids()
    killed = 0
    extra = _iter_int_pids(extra_root_pids)
    for ip in extra:
        if ip in protected:
            continue
        kill_pid(ip, force=force)
        killed += 1
    needle = str(root).replace("/", "\\").lower()
    if not needle:
        return killed
    alt_needle = str(root).replace("\\", "/").lower()
    rows = _windows_process_rows()
    for row in rows:
        pid = row.get("pid")
        if pid is None or pid in protected:
            continue
        cmd = (row.get("cmd") or "").lower()
        name = (row.get("name") or "").lower()
        if "opencode" in name and "serve" in cmd:
            continue
        if "opencode" in cmd and "serve" in cmd:
            continue
        # git checkout typically has the clone as cwd, not on argv. Those
        # leftover processes are killed via lock-file holders in
        # ``reclaim_workspace``. Here we only match an explicit path.
        if needle not in cmd and alt_needle not in cmd:
            continue
        kill_pid(pid, force=force)
        killed += 1
    return killed


def _windows_process_rows() -> List[dict]:
    """``[{pid, ppid, name, cmd}, ...]`` — CIM first, WMIC fallback."""
    rows = _windows_process_rows_cim()
    if rows:
        return rows
    return _windows_process_rows_wmic()


def _windows_process_rows_cim() -> List[dict]:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except Exception:
        return []
    return _parse_windows_process_csv(r.stdout or "")


def _windows_process_rows_wmic() -> List[dict]:
    try:
        r = subprocess.run(
            [
                "wmic",
                "process",
                "get",
                "ProcessId,ParentProcessId,Name,CommandLine",
                "/FORMAT:CSV",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return []
    return _parse_windows_process_csv(r.stdout or "")


def _parse_windows_process_csv(raw: str) -> List[dict]:
    if not (raw or "").strip():
        return []
    out: List[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(raw))
        fieldmap = {((k or "").strip().lower()): (k or "") for k in (reader.fieldnames or [])}
        pid_k = fieldmap.get("processid") or fieldmap.get("process id")
        ppid_k = fieldmap.get("parentprocessid") or fieldmap.get("parent process id")
        name_k = fieldmap.get("name")
        cmd_k = fieldmap.get("commandline") or fieldmap.get("command line")
        if not pid_k:
            return _parse_windows_process_csv_loose(raw)
        for row in reader:
            try:
                pid = int(str(row.get(pid_k) or "").strip() or "0")
            except ValueError:
                continue
            if pid <= 0:
                continue
            ppid = 0
            if ppid_k:
                try:
                    ppid = int(str(row.get(ppid_k) or "").strip() or "0")
                except ValueError:
                    ppid = 0
            out.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "name": str(row.get(name_k) or "") if name_k else "",
                    "cmd": str(row.get(cmd_k) or "") if cmd_k else "",
                }
            )
    except Exception:
        return _parse_windows_process_csv_loose(raw)
    return out


def _parse_windows_process_csv_loose(raw: str) -> List[dict]:
    """Last-resort: last integer token on the line is the PID (legacy WMIC)."""
    out: List[dict] = []
    for line in (raw or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        pid = None
        for part in reversed(parts):
            if part.isdigit():
                pid = int(part)
                break
        if pid is None or pid <= 0:
            continue
        out.append({"pid": pid, "ppid": 0, "name": "", "cmd": line})
    return out


def resolve_git_dir(workspace: Path) -> Optional[Path]:
    """Return the ``.git`` directory for ``workspace`` (worktree file ok)."""
    try:
        marker = Path(workspace) / ".git"
    except (OSError, TypeError):
        return None
    try:
        if marker.is_dir():
            return marker
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.lower().startswith("gitdir:"):
            continue
        raw = line.split(":", 1)[1].strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = Path(workspace) / p
        try:
            return p if p.exists() else None
        except OSError:
            return None
    return None


_GIT_LOCK_BASENAMES = (
    "index.lock",
    "HEAD.lock",
    "config.lock",
    "packed-refs.lock",
    "shallow.lock",
    "COMMIT_EDITMSG.lock",
    "gc.pid",
)


def list_git_lock_files(workspace: Path) -> List[Path]:
    """Known git lock files under ``workspace`` (not a full ``.git`` walk)."""
    git_dir = resolve_git_dir(workspace)
    if git_dir is None:
        return []
    found: List[Path] = []
    seen: Set[str] = set()

    def _add(path: Path) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            return
        try:
            if path.is_file():
                seen.add(key)
                found.append(path)
        except OSError:
            return

    for name in _GIT_LOCK_BASENAMES:
        _add(git_dir / name)
    refs = git_dir / "refs"
    try:
        if refs.is_dir():
            for p in refs.rglob("*.lock"):
                _add(p)
    except OSError:
        pass
    return found


def pids_holding_path(path: Path) -> List[int]:
    """PIDs that have ``path`` open. Best-effort; empty when unknown."""
    try:
        target = Path(path)
    except (OSError, TypeError):
        return []
    if os.name == "nt":
        return _pids_holding_path_windows(target)
    return _pids_holding_path_unix(target)


def _pids_holding_path_unix(path: Path) -> List[int]:
    """Check this process tree's fds only — never scan serve or all of /proc.

    A serve-wide fd walk (and ``fuser``) can hang uninterruptibly on WSL/9p.
    Agent git in the clone is still found by ``kill_workspace_processes``
    via cwd; leftover ``index.lock`` is then unlinked if no holder remains.
    """
    try:
        want = str(path.resolve())
    except OSError:
        want = str(path)
    found: List[int] = []
    for child in descendant_pids(os.getpid()):
        if _pid_has_open_path(child, want):
            found.append(child)
    return found


def _pid_has_open_path(pid: int, want: str) -> bool:
    fd_dir = Path(f"/proc/{int(pid)}/fd")
    try:
        fds = list(fd_dir.iterdir())
    except OSError:
        return False
    for fd in fds:
        try:
            dest = os.readlink(fd)
        except OSError:
            continue
        if dest == want or dest.startswith(want):
            return True
    return False


def _pids_holding_path_windows(path: Path) -> List[int]:
    """Restart Manager: who has this file open (index.lock / askpass)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    try:
        rstrtmgr = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    except Exception:
        return []

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [
            ("dwProcessId", wintypes.DWORD),
            ("ProcessStartTime", wintypes.FILETIME),
        ]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", wintypes.WCHAR * 256),
            ("strServiceShortName", wintypes.WCHAR * 64),
            ("ApplicationType", wintypes.DWORD),
            ("AppStatus", wintypes.ULONG),
            ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    session = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(256)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, session_key) != 0:
        return []
    try:
        resources = (ctypes.c_wchar_p * 1)(str(path))
        if rstrtmgr.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
            return []
        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reboot = wintypes.DWORD(0)
        # First call sizes the buffer
        rstrtmgr.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reboot)
        )
        n = int(needed.value or 0)
        if n <= 0:
            return []
        infos = (RM_PROCESS_INFO * n)()
        count.value = n
        if rstrtmgr.RmGetList(
            session,
            ctypes.byref(needed),
            ctypes.byref(count),
            infos,
            ctypes.byref(reboot),
        ) != 0:
            return []
        pids: List[int] = []
        for i in range(int(count.value or 0)):
            pid = int(infos[i].Process.dwProcessId)
            if pid > 0:
                pids.append(pid)
        return pids
    except Exception:
        return []
    finally:
        try:
            rstrtmgr.RmEndSession(session)
        except Exception:
            pass


def kill_file_holders(path: Path, *, force: bool = True) -> int:
    """Force-kill processes that have ``path`` open (except daemon/serve)."""
    protected = _protect_pids()
    killed = 0
    for pid in pids_holding_path(path):
        if pid in protected:
            continue
        kill_pid(pid, force=force)
        killed += 1
    return killed


def clear_stale_git_locks(workspace: Optional[Path]) -> int:
    """Remove leftover ``.git/*.lock`` after holders are gone.

    If a holder is still detected, the lock is left in place.
    """
    if workspace is None:
        return 0
    try:
        root = Path(workspace)
    except (OSError, TypeError):
        return 0
    removed = 0
    for lock in list_git_lock_files(root):
        holders = [p for p in pids_holding_path(lock) if p not in _protect_pids()]
        if holders:
            continue
        try:
            lock.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def reclaim_workspace(
    workspace: Optional[Path],
    *,
    extra_root_pids: Optional[Iterable[int]] = None,
    force: bool = True,
) -> int:
    """Kill leftover workspace processes, evict lock holders, drop stale locks.

    Safe to call on cancel and again before the next checkout on a reused clone.
    """
    if workspace is None:
        return 0
    try:
        root = Path(workspace).resolve()
    except OSError:
        return 0
    extra = list(extra_root_pids or ())
    killed = kill_workspace_processes(root, extra_root_pids=extra, force=force)
    locks = list_git_lock_files(root)
    for lock in locks:
        killed += kill_file_holders(lock, force=force)
    if killed:
        time.sleep(0.1)
        for lock in list_git_lock_files(root):
            killed += kill_file_holders(lock, force=force)
    clear_stale_git_locks(root)
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
