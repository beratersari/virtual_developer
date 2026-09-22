"""Dedicated thread pools so git clone cannot starve the dashboard.

Clones used to share asyncio's default executor with FastAPI sync routes
(``GET /api/jobs``, ``/api/dashboard``, WebSocket live payload). Many
``git clone`` calls then made the UI wait for a free thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Optional

from src.config import settings

_git_pool: Optional[ThreadPoolExecutor] = None
_git_pool_size = 0
_lock = threading.Lock()


def git_pool_size() -> int:
    return max(1, min(32, int(getattr(settings, "max_concurrent_jobs", 6) or 6)))


def git_executor() -> ThreadPoolExecutor:
    """Pool used only for clone / checkout / push / delete-clone."""
    global _git_pool, _git_pool_size
    want = git_pool_size()
    with _lock:
        if _git_pool is None or _git_pool_size != want:
            old = _git_pool
            _git_pool = ThreadPoolExecutor(
                max_workers=want, thread_name_prefix="yaver-git"
            )
            _git_pool_size = want
            if old is not None:
                old.shutdown(wait=False)
        return _git_pool


def resize_git_executor(limit: int) -> None:
    global _git_pool, _git_pool_size
    want = max(1, min(32, int(limit or 1)))
    with _lock:
        if _git_pool is not None and _git_pool_size == want:
            return
        old = _git_pool
        _git_pool = ThreadPoolExecutor(
            max_workers=want, thread_name_prefix="yaver-git"
        )
        _git_pool_size = want
        if old is not None:
            old.shutdown(wait=False)


async def to_git_thread(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run git/MR work off the event loop, keeping the caller's log context.

    ``loop.run_in_executor`` does not copy ``contextvars`` (Python 3.12).
    Without an explicit copy, push and merge-request errors lose ``job_id``
    and never land in the job system log — only the later main-thread
    warning does.
    """
    loop = asyncio.get_running_loop()
    pool = git_executor()
    ctx = contextvars.copy_context()
    if kwargs:
        call = partial(fn, *args, **kwargs)
        return await loop.run_in_executor(pool, ctx.run, call)
    return await loop.run_in_executor(pool, ctx.run, fn, *args)
