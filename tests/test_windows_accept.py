"""A dropped Windows accept must not close the dashboard listener."""

from __future__ import annotations

import asyncio
import sys

import pytest

from src.windows_accept import (
    install_accept_exception_handler,
    keep_listening_after_accept_reset,
)


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP only")


@pytest.mark.asyncio
async def test_transient_accept_error_keeps_the_listening_socket():
    keep_listening_after_accept_reset()

    async def handler(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    loop = asyncio.get_running_loop()
    sock = next(iter(server.sockets))
    fd = sock.fileno()
    failed = loop._accept_futures[fd]
    exc = OSError(22, "The specified network name is no longer available", None, 64)
    exc.winerror = 64
    failed.set_exception(exc)
    await asyncio.sleep(0)

    assert sock.fileno() == fd
    nxt = loop._accept_futures[fd]
    assert nxt is not failed
    assert not nxt.done()

    port = sock.getsockname()[1]
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()


def test_exception_handler_ignores_the_unretrieved_accept_task():
    loop = asyncio.new_event_loop()
    try:
        install_accept_exception_handler(loop)
        exc = OSError(22, "gone", None, 64)
        exc.winerror = 64
        loop.call_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "exception": exc,
            }
        )
    finally:
        loop.close()
