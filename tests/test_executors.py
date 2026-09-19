"""Git clone pool is not the asyncio default executor."""

import asyncio
import threading

from src.executors import git_executor, resize_git_executor, to_git_thread


def test_git_pool_is_not_default_executor():
    loop = asyncio.new_event_loop()
    try:
        default = loop._default_executor
        git = git_executor()
        assert git is not default
    finally:
        loop.close()


def test_to_git_thread_runs_on_yaver_git_pool():
    seen = []

    def _mark():
        seen.append(threading.current_thread().name)
        return 7

    resize_git_executor(2)
    assert asyncio.run(to_git_thread(_mark)) == 7
    assert seen
    assert seen[0].startswith("yaver-git")
