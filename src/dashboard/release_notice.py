"""Published-release check for the dashboard banner.

The daemon asks the office release site on a timer. A newer package becomes
a notice on the live dashboard feed. The dashboard does not download that
package and does not replace files. The notice only tells the operator to
run ``update.bat`` or ``update.sh``.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from src.config import DEFAULT_RELEASE_HOST, settings
from src.logger import logger

# Same cadence idea as the board poller: often enough to notice a publish,
# rare enough that a quiet release server is not hit on every dashboard tick.
RELEASE_CHECK_INTERVAL_SECONDS = 15 * 60
_DEFAULT_RELEASE_PORT = 8090
_PLATFORMS = frozenset(
    {
        "windows",
        "ubuntu-18.04",
        "ubuntu-20.04",
        "ubuntu-22.04",
        "ubuntu-24.04",
    }
)
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_LOCK = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def version_key(value: str) -> tuple:
    """Numeric ``MAJOR.MINOR.PATCH`` from the text before ``+`` or ``-``."""
    head = (value or "").strip().split("+", 1)[0].split("-", 1)[0]
    numbers: List[int] = []
    for piece in head.split("."):
        if not piece.isdigit():
            break
        numbers.append(int(piece))
    return tuple(numbers)


def version_is_newer(remote: str, local: str) -> bool:
    """True when the published version is a later release than this install."""
    left = version_key(remote)
    right = version_key(local)
    if not left or not right:
        return False
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    return left > right


def detect_update_platform(
    *,
    system: Optional[str] = None,
    os_release_text: Optional[str] = None,
) -> str:
    """Platform id the update script would request, or ``""`` when none exists."""
    override = (os.environ.get("YAVER_UPDATE_PLATFORM") or "").strip()
    if override:
        return override if override in _PLATFORMS else ""
    name = system if system is not None else os.name
    if name == "nt":
        return "windows"
    text = os_release_text
    if text is None:
        try:
            with open("/etc/os-release", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return ""
    info: Dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        info[key.strip()] = raw.strip().strip('"').strip("'")
    if info.get("ID") != "ubuntu":
        return ""
    parts = (info.get("VERSION_ID") or "").split(".")
    if len(parts) < 2:
        return ""
    platform = f"ubuntu-{parts[0]}.{parts[1]}"
    return platform if platform in _PLATFORMS else ""


def _safe_host(value: str) -> str:
    host = (value or "").strip()
    if not host or not _HOST_RE.fullmatch(host) or ".." in host:
        return ""
    return host


def _release_endpoint(platform: str, current: str = "") -> str:
    raw = str(getattr(settings, "release_host", "") or "").strip()
    if not raw:
        raw = DEFAULT_RELEASE_HOST
    host = _safe_host(raw)
    if not host:
        return ""
    try:
        port = int(getattr(settings, "release_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    if port == 0:
        port = _DEFAULT_RELEASE_PORT
    if port < 1 or port > 65535 or not platform:
        return ""
    params = {"platform": platform}
    installed = (current or "").strip()
    if installed and _VERSION_RE.fullmatch(installed):
        params["current"] = installed
    query = urlencode(params)
    return f"http://{host}:{port}/api/latest?{query}"


def _update_steps(platform: str) -> List[str]:
    if platform == "windows":
        return [
            "Close Yaver.",
            "In the Yaver folder, run update.bat.",
            "The script reads RELEASE_HOST and RELEASE_PORT from .env. A blank host uses 15.210.7.55 and a blank port uses 8090.",
            "It checks the package, stops yaver.exe in that folder, and replaces the program files. Your .env stays. The script you ran stays.",
            "When the script prints a line that begins with Updated to, start yaver.exe.",
            "If this folder has no update.bat yet, download the zip once, copy update.bat into the folder, and run it there.",
            "The script updates an installed copy. A git checkout is left unchanged.",
        ]
    return [
        "Close Yaver.",
        "In the Yaver folder, run chmod 755 update.sh if needed, then ./update.sh.",
        "The script reads RELEASE_HOST and RELEASE_PORT from .env. A blank host uses 15.210.7.55 and a blank port uses 8090.",
        "It checks the package, stops yaver in that folder, and replaces the program files. Your .env stays. The script you ran stays.",
        "When the script prints a line that begins with Updated to, start ./yaver.",
        "If this folder has no update.sh yet, download the zip once, copy update.sh into the folder, and run it there.",
        "The script updates an installed copy. A git checkout is left unchanged.",
    ]


def _notice(
    *,
    available: bool,
    current: str,
    latest: str,
    platform: str,
    checked_at: str,
) -> Dict[str, Any]:
    message = ""
    steps: List[str] = []
    if available:
        message = f"Yaver {latest} is available. This install is {current}."
        steps = _update_steps(platform)
    return {
        "available": available,
        "current": current,
        "latest": latest if available else "",
        "platform": platform,
        "message": message,
        "steps": steps,
        "checked_at": checked_at,
    }


def _public(notice: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(notice)
    out["steps"] = list(notice.get("steps") or [])
    return out


def peek_release_notice() -> Dict[str, Any]:
    """Last check, or a quiet notice when this process has not checked yet."""
    with _LOCK:
        if _cache is not None:
            return _public(_cache)
    return _notice(
        available=False,
        current="",
        latest="",
        platform="",
        checked_at="",
    )


def clear_release_notice_cache() -> None:
    global _cache
    with _LOCK:
        _cache = None


def _current_version(current: Optional[str]) -> str:
    if current is not None:
        return current.strip()
    from src.dashboard.service import read_app_version

    return (read_app_version() or "").strip()


def refresh_release_notice(
    *,
    current: Optional[str] = None,
    platform: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
    opener: Optional[Callable[..., httpx.Client]] = None,
) -> Dict[str, Any]:
    """Ask the release site once and remember the result until the next check."""
    global _cache
    installed = _current_version(current)
    plat = platform if platform is not None else detect_update_platform()
    checked = datetime.now().isoformat(timespec="seconds")
    notice = _notice(
        available=False,
        current=installed,
        latest="",
        platform=plat,
        checked_at=checked,
    )
    url = _release_endpoint(plat, installed)
    if not url or not plat:
        with _LOCK:
            _cache = notice
        return _public(notice)
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    timeout = httpx.Timeout(5.0, connect=3.0)
    # INTENTIONAL: verify=False. http only, and redirects are not followed.
    # The version check does not send Analytics. The release admin loads
    # those counts with a separate GET when an address is selected.
    client_factory = opener or httpx.Client
    try:
        with client_factory(
            timeout=timeout,
            verify=False,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.get(url)
    except Exception as exc:
        logger.info(f"Release check could not reach the release server: {exc}")
        with _LOCK:
            _cache = notice
        return _public(notice)
    if response.status_code != 200:
        logger.info(
            f"Release check for {plat} returned HTTP {response.status_code}"
        )
        with _LOCK:
            _cache = notice
        return _public(notice)
    try:
        body = response.json()
    except Exception:
        body = None
    latest = ""
    if isinstance(body, dict):
        latest = str(body.get("version") or "").strip()
    if not _VERSION_RE.fullmatch(latest) or not version_is_newer(latest, installed):
        with _LOCK:
            _cache = notice
        return _public(notice)
    notice = _notice(
        available=True,
        current=installed,
        latest=latest,
        platform=plat,
        checked_at=checked,
    )
    logger.info(f"Release check: {latest} is published; this install is {installed}")
    with _LOCK:
        _cache = notice
    return _public(notice)
