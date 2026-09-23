"""Keep the Windows dashboard listener alive after a dropped accept.

CPython 3.12's IOCP accept loop closes the listening socket on any
OSError. A client that vanishes during AcceptEx (sleep, VPN, a tab
closed after a long job) raises WinError 64. The poller keeps running.
Nothing can connect to the dashboard port after that.
"""

from __future__ import annotations

import sys

# Client gone during AcceptEx. Not a dead listener.
_TRANSIENT_WINERRORS = frozenset(
    {
        64,  # ERROR_NETNAME_DELETED
        995,  # ERROR_OPERATION_ABORTED
        1236,  # ERROR_CONNECTION_ABORTED
    }
)

_PATCHED = False


def _transient_accept_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, OSError)
        and getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS
    )


def keep_listening_after_accept_reset() -> None:
    """Install the Windows accept retry. No-op on other platforms."""
    global _PATCHED
    if sys.platform != "win32" or _PATCHED:
        return
    import asyncio.proactor_events as proactor_events
    from asyncio import exceptions, trsock

    loop_cls = proactor_events.BaseProactorEventLoop

    def _start_serving(
        self,
        protocol_factory,
        sock,
        sslcontext=None,
        server=None,
        backlog=100,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
    ):
        def loop(f=None):
            try:
                if f is not None:
                    conn, addr = f.result()
                    if self._debug:
                        proactor_events.logger.debug(
                            "%r got a new connection from %r: %r",
                            server,
                            addr,
                            conn,
                        )
                    protocol = protocol_factory()
                    if sslcontext is not None:
                        self._make_ssl_transport(
                            conn,
                            protocol,
                            sslcontext,
                            server_side=True,
                            extra={"peername": addr},
                            server=server,
                            ssl_handshake_timeout=ssl_handshake_timeout,
                            ssl_shutdown_timeout=ssl_shutdown_timeout,
                        )
                    else:
                        self._make_socket_transport(
                            conn,
                            protocol,
                            extra={"peername": addr},
                            server=server,
                        )
                if self.is_closed():
                    return
                f = self._proactor.accept(sock)
            except OSError as exc:
                if (
                    _transient_accept_error(exc)
                    and sock.fileno() != -1
                    and not self.is_closed()
                ):
                    proactor_events.logger.warning(
                        "Accept interrupted (%s); listening socket kept",
                        exc,
                    )
                    try:
                        f = self._proactor.accept(sock)
                    except OSError:
                        f = None
                    if f is not None:
                        self._accept_futures[sock.fileno()] = f
                        f.add_done_callback(loop)
                        return
                if sock.fileno() != -1:
                    self.call_exception_handler(
                        {
                            "message": "Accept failed on a socket",
                            "exception": exc,
                            "socket": trsock.TransportSocket(sock),
                        }
                    )
                    sock.close()
                elif self._debug:
                    proactor_events.logger.debug(
                        "Accept failed on socket %r", sock, exc_info=True
                    )
            except exceptions.CancelledError:
                sock.close()
            else:
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)

        self.call_soon(loop)

    loop_cls._start_serving = _start_serving  # type: ignore[method-assign]
    _PATCHED = True


def install_accept_exception_handler(loop) -> None:
    """Drop the unretrieved-task noise for the same transient accept errors."""
    if sys.platform != "win32":
        return
    previous = loop.get_exception_handler()

    def handler(running_loop, context) -> None:
        exc = context.get("exception")
        message = str(context.get("message") or "")
        if _transient_accept_error(exc) and (
            "Accept failed" in message or "never retrieved" in message
        ):
            return
        if previous is not None:
            previous(running_loop, context)
        else:
            running_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
