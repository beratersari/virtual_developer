"""Operator-facing Azure DevOps logs. Never interpolate a PAT or Authorization."""

from __future__ import annotations

from typing import Any, Optional

from src.logger import LogLevel, logger

PREFIX = "[azure]"


def clip(text: Any, limit: int = 240) -> str:
    """Single-line preview for comments / error bodies."""
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def yn(value: Any) -> str:
    return "yes" if value else "no"


def azure_info(msg: str) -> None:
    # _log (not info) so file:line is the real caller, not this helper.
    logger._log(LogLevel.INFO, f"{PREFIX} {msg}")


def azure_warning(msg: str) -> None:
    logger._log(LogLevel.WARNING, f"{PREFIX} {msg}")


def azure_error(msg: str) -> None:
    logger._log(LogLevel.ERROR, f"{PREFIX} {msg}")


def azure_exception(msg: str, exc: BaseException) -> None:
    logger._log(LogLevel.ERROR, f"{PREFIX} {msg}", exception=exc)


def http_detail(
    *,
    method: str,
    url: str,
    status: Optional[int] = None,
    api_version: str = "",
    body: Any = "",
) -> str:
    """Safe HTTP summary (no headers)."""
    parts = [method.upper(), url]
    if api_version:
        parts.append(f"api={api_version}")
    if status is not None:
        parts.append(f"status={status}")
    preview = clip(body, 200)
    if preview:
        parts.append(f"body={preview!r}")
    return " ".join(parts)
