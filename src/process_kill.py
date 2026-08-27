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
    """Working directory of ``pid``, or None."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _pid_cwd_windows(int(pid))
    try:
        return Path(os.readlink(f"/proc/{int(pid)}/cwd"))
    except OSError:
        return None


def pid_cmdline(pid: int) -> str:
    """Process command line. Empty on failure."""
    if pid <= 0:
        return ""
    if os.name == "nt":
        return ""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def pid_exe(pid: int) -> str:
    """Full executable path, or empty."""
    if pid <= 0:
        return ""
    if os.name == "nt":
        return _pid_exe_windows(int(pid))
    try:
        return os.readlink(f"/proc/{int(pid)}/exe")
    except OSError:
        return ""


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        if os.name != "nt":
            return False
        try:
            Path(str(resolved).lower()).relative_to(Path(str(base).lower()))
            return True
        except ValueError:
            return False


_GENERIC_WORKSPACE_ROOTS = frozenset(
    {
        Path("/"),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/home"),
        Path("/Users"),
        Path("/mnt"),
        Path("/mnt/c"),
    }
)


def _cmdline_mentions_workspace(cmd: str, root: Path) -> bool:
    """True when ``cmd`` names this clone (not a generic ``/tmp``)."""
    if not cmd:
        return False
    try:
        resolved = root.resolve()
    except OSError:
        resolved = root
    if resolved in _GENERIC_WORKSPACE_ROOTS:
        return False
    needle = str(resolved)
    if len(needle) < 12:
        return False
    lowered = cmd.lower()
    if needle.lower() in lowered:
        return True
    alt = needle.replace("\\", "/")
    return bool(alt) and alt.lower() in lowered


def _pid_cwd_windows(pid: int) -> Optional[Path]:
    """Read another process's cwd via PEB (64-bit and 32-bit same-arch)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    process_query = 0x0400
    process_vm_read = 0x0010
    process_query_limited = 0x1000

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
    except Exception:
        return None

    handle = kernel32.OpenProcess(
        process_query | process_vm_read, False, int(pid)
    )
    if not handle:
        handle = kernel32.OpenProcess(
            process_query_limited | process_vm_read, False, int(pid)
        )
    if not handle:
        return None
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None
        )
        if status != 0 or not pbi.PebBaseAddress:
            return None
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        peb = int(pbi.PebBaseAddress)
        params_off = 0x20 if ptr_size == 8 else 0x10
        params_addr = ctypes.c_void_p()
        nread = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(peb + params_off),
            ctypes.byref(params_addr),
            ptr_size,
            ctypes.byref(nread),
        ):
            return None
        if not params_addr.value:
            return None
        curdir_off = 0x38 if ptr_size == 8 else 0x24
        us = UNICODE_STRING()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(int(params_addr.value) + curdir_off),
            ctypes.byref(us),
            ctypes.sizeof(us),
            ctypes.byref(nread),
        ):
            return None
        if not us.Buffer or us.Length == 0:
            return None
        nchars = max(1, int(us.Length) // 2)
        buf = ctypes.create_unicode_buffer(nchars + 1)
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(int(us.Buffer)),
            buf,
            int(us.Length),
            ctypes.byref(nread),
        ):
            return None
        text = (buf.value or "").rstrip("\\/\0 ")
        return Path(text) if text else None
    except Exception:
        return None
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def _pid_exe_windows(pid: int) -> str:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return ""
    process_query_limited = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return ""
    handle = kernel32.OpenProcess(process_query_limited, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(32768)
        q = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if q is None:
            return ""
        q.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        q.restype = wintypes.BOOL
        if not q(handle, 0, buf, ctypes.byref(size)):
            return ""
        return (buf.value or "").strip()
    except Exception:
        return ""
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


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
    Tracked ``extra_root_pids`` are killed even when ``workspace`` is unknown.
    """
    extra = extra_root_pids or ()
    if workspace is None:
        return _kill_tracked_pids(extra, force=force)
    try:
        root = Path(workspace).resolve()
    except OSError:
        return _kill_tracked_pids(extra, force=force)
    if not str(root):
        return _kill_tracked_pids(extra, force=force)

    if os.name == "nt":
        return _kill_workspace_windows(root, extra, force=force)
    return _kill_workspace_unix(root, extra, force=force)


def _kill_tracked_pids(extra_root_pids: Iterable[int], *, force: bool) -> int:
    """Kill known job PIDs (and their children) with no workspace path yet."""
    protected = _protect_pids()
    killed = 0
    seen: Set[int] = set()
    for ip in _iter_int_pids(extra_root_pids):
        for pid in (ip, *descendant_pids(ip)):
            if pid <= 0 or pid in seen or pid in protected:
                continue
            seen.add(pid)
            kill_pid(pid, force=force)
            killed += 1
    return killed


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


def _pid_belongs_to_workspace(pid: int, root: Path) -> bool:
    cwd = pid_cwd(pid)
    if cwd is not None and _path_is_under(cwd, root):
        return True
    if _cmdline_mentions_workspace(pid_cmdline(pid), root):
        return True
    return _cmdline_mentions_workspace(pid_exe(pid), root)


def _pgrep_workspace_pids(root: Path) -> List[int]:
    """PIDs whose argv names this clone. One ``pgrep`` — not a /proc walk."""
    try:
        needle = str(root.resolve())
    except OSError:
        needle = str(root)
    if len(needle) < 12:
        return []
    try:
        r = subprocess.run(
            ["pgrep", "-f", needle],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    found: List[int] = []
    for tok in (r.stdout or "").split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid > 0:
            found.append(pid)
    return found


def _kill_workspace_unix(
    root: Path,
    extra_root_pids: Iterable[int],
    *,
    force: bool,
) -> int:
    protected = _protect_pids()
    extra = _iter_int_pids(extra_root_pids)
    targets: List[int] = []
    seen: Set[int] = set()

    def _take(pid: int) -> None:
        if pid <= 0 or pid in seen or pid in protected:
            return
        seen.add(pid)
        targets.append(pid)

    # Tracked git/Codex PIDs die even when cwd is the parent of the clone.
    for ip in extra:
        _take(ip)
        for child in descendant_pids(ip):
            _take(child)

    walk_from = {os.getpid(), *protected, *extra}
    for rp in walk_from:
        for child in descendant_pids(rp):
            if child in seen or child in protected:
                continue
            if _pid_belongs_to_workspace(child, root):
                _take(child)

    for pid in _pgrep_workspace_pids(root):
        if pid in protected:
            continue
        cmd = pid_cmdline(pid).lower()
        if "pgrep" in cmd:
            continue
        if _pid_belongs_to_workspace(pid, root) or _cmdline_mentions_workspace(
            cmd, root
        ):
            _take(pid)

    killed = 0
    for pid in reversed(targets):
        kill_pid(pid, force=force)
        killed += 1
    return killed


def _windows_row_is_serve(row: dict) -> bool:
    name = (row.get("name") or "").lower()
    cmd = (row.get("cmd") or "").lower()
    if "serve" not in cmd:
        return False
    return "opencode" in name or "opencode" in cmd


def _windows_children_by_ppid(rows: List[dict]) -> dict:
    by_ppid: dict = {}
    for row in rows:
        ppid = row.get("ppid")
        pid = row.get("pid")
        if not isinstance(ppid, int) or not isinstance(pid, int):
            continue
        by_ppid.setdefault(ppid, []).append(pid)
    return by_ppid


def _windows_descendants(root_pids: Iterable[int], rows: List[dict]) -> List[int]:
    by_ppid = _windows_children_by_ppid(rows)
    out: List[int] = []
    seen: Set[int] = set()
    stack = [int(p) for p in root_pids if int(p) > 0]
    while stack:
        cur = stack.pop()
        for child in by_ppid.get(cur, []):
            if child in seen or child <= 0:
                continue
            seen.add(child)
            out.append(child)
            stack.append(child)
    return out


def _windows_text_names_workspace(text: str, needle: str, alt_needle: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if needle and needle in lowered:
        return True
    return bool(alt_needle) and alt_needle in lowered


def _kill_workspace_windows(
    root: Path,
    extra_root_pids: Iterable[int],
    *,
    force: bool,
) -> int:
    """Kill tracked trees and any job child (cmdline, exe path, or cwd).

    Same rules as Linux: extra/Codex/git trees, then anything whose
    command line / executable / working directory names this clone.
    Never kill the daemon or shared ``opencode serve``.
    """
    protected = _protect_pids()
    extra = _iter_int_pids(extra_root_pids)
    killed_ids: Set[int] = set()

    def _kill_one(pid: int) -> None:
        if pid <= 0 or pid in protected or pid in killed_ids:
            return
        kill_pid(pid, force=force)
        killed_ids.add(pid)

    # taskkill /T on tracked git/Codex trees (cwd may be the parent folder).
    for ip in extra:
        _kill_one(ip)

    needle = str(root).replace("/", "\\").lower()
    alt_needle = str(root).replace("\\", "/").lower()
    if not needle:
        return len(killed_ids)

    rows = _windows_process_rows()
    serve_pids = [int(r["pid"]) for r in rows if _windows_row_is_serve(r) and r.get("pid")]
    for sp in serve_pids:
        protected.add(sp)

    for row in rows:
        pid = row.get("pid")
        if not isinstance(pid, int) or pid in protected:
            continue
        if _windows_row_is_serve(row):
            continue
        cmd = row.get("cmd") or ""
        exe = row.get("exe") or ""
        if _windows_text_names_workspace(cmd, needle, alt_needle) or (
            _windows_text_names_workspace(exe, needle, alt_needle)
        ):
            _kill_one(pid)

    # OpenCode tools usually inherit the clone as cwd and omit it from argv
    # (`git status`, `npm test`). Walk serve/daemon/tracked trees and match cwd.
    walk_from = {os.getpid(), *serve_pids, *extra, *protected}
    for child in _windows_descendants(walk_from, rows):
        if child in protected or child in killed_ids:
            continue
        cwd = pid_cwd(child)
        if cwd is not None and _path_is_under(cwd, root):
            _kill_one(child)
            continue
        exe = pid_exe(child)
        if _windows_text_names_workspace(exe, needle, alt_needle):
            _kill_one(child)

    return len(killed_ids)


def _windows_process_rows() -> List[dict]:
    """``[{pid, ppid, name, cmd}, ...]`` — CIM first, WMIC fallback."""
    rows = _windows_process_rows_cim()
    if rows:
        return rows
    return _windows_process_rows_wmic()


def _windows_process_rows_cim() -> List[dict]:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath | "
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
                "ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath",
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
        exe_k = fieldmap.get("executablepath") or fieldmap.get("executable path")
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
                    "exe": str(row.get(exe_k) or "") if exe_k else "",
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
        out.append({"pid": pid, "ppid": 0, "name": "", "cmd": line, "exe": ""})
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
    extra = list(extra_root_pids or ())
    if workspace is None:
        return kill_workspace_processes(None, extra_root_pids=extra, force=force)
    try:
        root = Path(workspace).resolve()
    except OSError:
        return kill_workspace_processes(None, extra_root_pids=extra, force=force)
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
