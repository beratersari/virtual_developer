"""
Centralized logging for JIRA Virtual Developer.

Format (single consistent line):

    YYYY-MM-DD HH:MM:SS.mmm  LEVEL     module.py:line  function  message

No emojis. Optional ANSI colors when stdout is a TTY.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class LogLevel(Enum):
    """Log levels with numeric values for comparison."""

    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_WHITE = "\033[97m"

    BG_RED = "\033[41m"


class Logger:
    """
    Application logger with a fixed professional layout.

    Layout::

        2026-08-01 14:13:39.643  INFO      [client.py:44]  __init__  message

    Source file and line are always included in brackets after the level.
    """

    _min_level: LogLevel = LogLevel.DEBUG
    _use_colors: bool = True

    # level -> (label, color, optional background)
    _LEVEL_CONFIG = {
        LogLevel.DEBUG: ("DEBUG", Colors.BRIGHT_BLACK, None),
        LogLevel.INFO: ("INFO", Colors.BRIGHT_BLUE, None),
        LogLevel.WARNING: ("WARNING", Colors.BRIGHT_YELLOW, None),
        LogLevel.ERROR: ("ERROR", Colors.BRIGHT_RED, None),
        LogLevel.CRITICAL: ("CRITICAL", Colors.BRIGHT_WHITE, Colors.BG_RED),
    }

    def __init__(self) -> None:
        self._detect_color_support()

    def _detect_color_support(self) -> None:
        """Enable colors only when writing to an interactive terminal."""
        self._use_colors = bool(sys.stdout.isatty())

    def set_level(self, level: LogLevel) -> None:
        """Set minimum log level."""
        self._min_level = level

    def set_color_output(self, enabled: bool) -> None:
        """Enable or disable colored output."""
        self._use_colors = enabled

    def _get_caller_info(self, stack_offset: int = 3) -> tuple[str, int, str]:
        """Return (filename, line_number, function_name)."""
        try:
            frame = sys._getframe(stack_offset)
            filename = Path(frame.f_code.co_filename).name
            line_no = frame.f_lineno
            func_name = frame.f_code.co_name
            return filename, line_no, func_name
        except Exception:
            return "unknown", 0, "unknown"

    def _format_message(
        self,
        level: LogLevel,
        message: str,
        filename: str,
        line_no: int,
        func_name: str,
        exception: Optional[BaseException] = None,
    ) -> str:
        """Format one log record into the standard layout."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        level_name, level_color, bg_color = self._LEVEL_CONFIG[level]
        level_str = f"{level_name:<8}"
        # Always show source file and line (padded for alignment)
        location = f"[{filename}:{line_no}]"
        location_pad = f"{location:<30}"
        func_pad = f"{func_name:<20}"
        # Collapse accidental multi-line messages onto one logical line for the body
        body = " ".join(str(message).splitlines()) if message is not None else ""
        # Prefix active job id so operators can filter dashboard job logs
        try:
            from src.log_context import get_job_id

            jid = get_job_id()
            if jid and f"job_id={jid}" not in body:
                body = f"[job_id={jid}] {body}"
        except Exception:
            pass

        if self._use_colors:
            ts = f"{Colors.DIM}{timestamp}{Colors.RESET}"
            if bg_color:
                lvl = f"{bg_color}{Colors.BOLD}{level_color}{level_str}{Colors.RESET}"
            else:
                lvl = f"{level_color}{level_str}{Colors.RESET}"
            # Cyan so file:line stays readable (not dim/grey)
            loc = f"{Colors.CYAN}{location_pad}{Colors.RESET}"
            fn = f"{Colors.DIM}{func_pad}{Colors.RESET}"
            msg = f"{Colors.WHITE}{body}{Colors.RESET}"
            result = f"{ts}  {lvl}  {loc}  {fn}  {msg}"
        else:
            result = f"{timestamp}  {level_str}  {location_pad}  {func_pad}  {body}"

        if exception is not None:
            exc_lines = traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
            exc_text = "".join(exc_lines).rstrip()
            if self._use_colors:
                result += f"\n{Colors.BRIGHT_RED}{exc_text}{Colors.RESET}"
            else:
                result += f"\n{exc_text}"

        return result

    def _log(
        self,
        level: LogLevel,
        message: str,
        exception: Optional[BaseException] = None,
    ) -> None:
        if level.value < self._min_level.value:
            return

        filename, line_no, func_name = self._get_caller_info()
        formatted = self._format_message(
            level, message, filename, line_no, func_name, exception
        )

        stream = sys.stderr if level.value >= LogLevel.WARNING.value else sys.stdout
        _write_stream(stream, formatted)

        # Feed dashboard issue/job log buffer (plain text, no ANSI)
        try:
            from src.dashboard.issue_logs import issue_log_ring
            from src.log_context import get_issue_key, get_job_id

            jid = get_job_id()
            ikey = get_issue_key()
            body = " ".join(str(message).splitlines()) if message is not None else ""
            if jid and f"job_id={jid}" not in body:
                body = f"[job_id={jid}] {body}"
            plain = (
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
                f"{self._LEVEL_CONFIG[level][0]:<8}  "
                f"[{filename}:{line_no}]  {func_name}  {body}"
            )
            issue_log_ring.append(plain, job_id=jid, issue_key=ikey)
            _append_daemon_log_file(plain)
        except Exception:
            pass

    def debug(self, message: str) -> None:
        self._log(LogLevel.DEBUG, message)

    def info(self, message: str) -> None:
        self._log(LogLevel.INFO, message)

    def warning(self, message: str) -> None:
        self._log(LogLevel.WARNING, message)

    def error(self, message: str, exception: Optional[BaseException] = None) -> None:
        self._log(LogLevel.ERROR, message, exception)

    def critical(self, message: str, exception: Optional[BaseException] = None) -> None:
        self._log(LogLevel.CRITICAL, message, exception)

    def exception(self, message: str, exc: BaseException) -> None:
        """Log an error with full traceback (message, exception)."""
        self._log(LogLevel.ERROR, message, exc)


def configure_stdio() -> None:
    """Force UTF-8 stdio so frozen Windows consoles do not crash on logs."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _write_stream(stream, text: str) -> None:
    """Print one log line. Never raise — Windows cp1252 cannot encode ≈/—."""
    try:
        print(text, file=stream)
        stream.flush()
        return
    except UnicodeEncodeError:
        pass
    except Exception:
        return
    payload = (text + "\n").encode("utf-8", errors="replace")
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        try:
            buf.write(payload)
            buf.flush()
            return
        except Exception:
            pass
    try:
        enc = getattr(stream, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"), file=stream)
        stream.flush()
    except Exception:
        pass


configure_stdio()


_DAEMON_LOG_MAX_BYTES = 5 * 1024 * 1024


def daemon_log_path() -> Path:
    """Durable daemon log (plain text) under ``YAVER_DATA_DIR/logs``."""
    from src.paths import agent_subdir, ensure_agent_data_dir

    ensure_agent_data_dir()
    return agent_subdir("logs") / "daemon.log"


def _append_daemon_log_file(plain: str) -> None:
    """Best-effort append so issue reports survive a process restart."""
    path = daemon_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > _DAEMON_LOG_MAX_BYTES:
            tail = path.read_bytes()[-(_DAEMON_LOG_MAX_BYTES // 2) :]
            path.write_bytes(tail + b"\n")
        with open(path, "a", encoding="utf-8") as f:
            f.write(plain)
            if not plain.endswith("\n"):
                f.write("\n")
    except OSError:
        pass


# Global logger instance
logger = Logger()


def set_level(level: LogLevel) -> None:
    logger.set_level(level)


def set_color_output(enabled: bool) -> None:
    logger.set_color_output(enabled)
