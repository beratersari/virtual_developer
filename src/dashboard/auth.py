"""Optional cookie login for the ops dashboard.

Set both ``DASHBOARD_USERNAME`` and ``DASHBOARD_PASSWORD`` in ``.env``.
Empty pair = no login (LAN default). The Jira poller is in-process and
never hits this. ``POST /yaver/webhook/gitlab`` and
``POST /yaver/webhook/azure`` keep their own tokens.

Do not send HTTP 401 or ``WWW-Authenticate: Basic``. Edge (especially on
a machine-name / LAN URL) treats that as a Windows/HTTP popup. That
popup never reaches ``POST /api/login``, so ``.env`` credentials look
rejected. The SPA probes ``GET /api/meta`` (always 200) and shows the
in-page form.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from src.config import settings
from src.dashboard.webhook_paths import ALL_WEBHOOK_PATHS

COOKIE_NAME = "yaver_dash"


def dashboard_auth_enabled() -> bool:
    return bool(
        (getattr(settings, "dashboard_username", "") or "").strip()
        and (getattr(settings, "dashboard_password", "") or "")
    )


def _want_user() -> str:
    return (getattr(settings, "dashboard_username", "") or "").strip()


def _want_password() -> str:
    return getattr(settings, "dashboard_password", "") or ""


def _equals(left: str, right: str) -> bool:
    return hmac.compare_digest(
        left.encode("utf-8"),
        right.encode("utf-8"),
    )


def parse_basic_header(authorization: str) -> tuple[str, str]:
    raw = (authorization or "").strip()
    if len(raw) < 7 or raw[:6].lower() != "basic ":
        return "", ""
    try:
        decoded = base64.b64decode(raw[6:].strip(), validate=False).decode("utf-8")
    except Exception:
        return "", ""
    user, sep, password = decoded.partition(":")
    if not sep:
        return "", ""
    return user, password


def credentials_ok(user: str, password: str) -> bool:
    if not dashboard_auth_enabled():
        return True
    return _equals(user, _want_user()) and _equals(password, _want_password())


def cookie_value(user: str, password: str) -> str:
    digest = hmac.new(
        password.encode("utf-8"),
        user.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{user}.{digest}"


def cookie_ok(value: str) -> bool:
    if not dashboard_auth_enabled():
        return True
    text = (value or "").strip()
    if "." not in text:
        return False
    user, _, digest = text.partition(".")
    expect = cookie_value(_want_user(), _want_password())
    return _equals(f"{user}.{digest}", expect)


def is_exempt_path(method: str, path: str) -> bool:
    """Paths the dashboard password must not block."""
    verb = (method or "GET").upper()
    raw = path or "/"
    if verb == "OPTIONS":
        return True
    if raw == "/api/health" or raw.startswith("/api/health/"):
        return True
    # Auth probe for the SPA login gate. A 401 here makes Edge (intranet /
    # saved Basic) show a native popup that cannot succeed — the browser
    # retries Authorization: Basic without our cookie / X-Yaver-Login.
    if verb == "GET" and raw.rstrip("/") == "/api/meta":
        return True
    if raw.rstrip("/") in ALL_WEBHOOK_PATHS:
        return True
    if verb == "POST" and raw.rstrip("/") in {"/api/logout", "/api/login"}:
        return True
    # Serve the SPA so the custom login page can render. Chrome pops the
    # native Basic dialog if HTML itself returns WWW-Authenticate: Basic.
    if verb == "GET" and not raw.startswith("/api/") and raw.rstrip("/") != "/ws":
        return True
    return False


def _explicit_basic(headers: dict[str, str]) -> bool:
    """True only when the client opted in. Chrome auto-sends cached Basic."""
    return (headers.get("x-yaver-login") or "").strip() == "1"


def request_authorized(request: Request) -> bool:
    if not dashboard_auth_enabled():
        return True
    if cookie_ok(request.cookies.get(COOKIE_NAME) or ""):
        return True
    if not _explicit_basic({k.lower(): v for k, v in request.headers.items()}):
        return False
    user, password = parse_basic_header(request.headers.get("authorization") or "")
    return credentials_ok(user, password)


def websocket_authorized(ws: WebSocket) -> bool:
    if not dashboard_auth_enabled():
        return True
    # Browser WebSocket cannot send X-Yaver-Login; cookie is the session.
    return cookie_ok(ws.cookies.get(COOKIE_NAME) or "")


class DashboardAuthMiddleware:
    """ASGI middleware: HTTP Basic or cookie for dashboard routes only."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method") or "GET"
        path = scope.get("path") or "/"
        if is_exempt_path(str(method), str(path)) or not dashboard_auth_enabled():
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }
        cookie_header = headers.get("cookie") or ""
        cookie_val = _cookie_from_header(cookie_header)
        if cookie_ok(cookie_val):
            await self.app(scope, receive, send)
            return
        if not _explicit_basic(headers):
            await _send_login_required(send)
            return
        user, password = parse_basic_header(headers.get("authorization") or "")
        if credentials_ok(user, password):
            async def send_with_cookie(message: dict) -> None:
                if message.get("type") == "http.response.start":
                    hdrs = list(message.get("headers") or [])
                    token = cookie_value(_want_user(), _want_password())
                    hdrs.append(
                        (
                            b"set-cookie",
                            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax".encode(
                                "latin-1"
                            ),
                        )
                    )
                    message = {**message, "headers": hdrs}
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return

        await _send_login_required(send)


LOGIN_REQUIRED_BODY = b'{"detail":"Dashboard login required","code":"login_required"}'


async def _send_login_required(send: Send) -> None:
    """403, never 401. Edge treats 401 as Windows/HTTP Basic on some origins."""
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(LOGIN_REQUIRED_BODY)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": LOGIN_REQUIRED_BODY})


def _cookie_from_header(header: str) -> str:
    prefix = COOKIE_NAME + "="
    for part in (header or "").split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return item[len(prefix) :]
    return ""
