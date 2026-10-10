"""Browser checks for the daily-usage UI fixes.

Chrome loads the current React source through Vite. Vite proxies to a real
FastAPI process. The assertions are what the page shows after clicks.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_ROOT = Path(__file__).resolve().parents[1]
_WEB = _ROOT / "web"
_VITE_CONFIG = _WEB / "vite.daily-e2e.config.ts"
_RETRY_TAIL = "END-OF-RETRY-ERROR"
_RETRY_ERROR = ("A" * 180) + _RETRY_TAIL

_HELPERS = r"""
(() => {
  function text() {
    return document.body ? document.body.innerText : "";
  }
  function sourceText() {
    return document.body ? (document.body.textContent || "") : "";
  }
  function setNative(input, value) {
    const proto = Object.getPrototypeOf(input);
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue(value === "" ? "0" : "");
    if (desc && desc.set) desc.set.call(input, value);
    else input.value = value;
    input.dispatchEvent(
      new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" })
    );
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  window.__yaverE2E = {
    setInput(labelText, value) {
      const label = [...document.querySelectorAll("label")].find((el) =>
        (el.textContent || "").includes(labelText)
      );
      if (!label) return { ok: false, reason: "no label" };
      const shown = (label.innerText || "").trim();
      if (!shown) return { ok: false, reason: "label not visible" };
      const input = label.querySelector("input");
      if (!input) return { ok: false, reason: "no input" };
      setNative(input, value);
      return { ok: true, value: input.value, shown };
    },
    inputByPlaceholder(placeholder) {
      const input = document.querySelector(
        "input[placeholder='" + placeholder.replace(/'/g, "") + "']"
      );
      if (!input) return { ok: false, reason: "no input" };
      return { ok: true, value: input.value };
    },
    setPlaceholder(placeholder, value) {
      const input = document.querySelector(
        "input[placeholder='" + placeholder.replace(/'/g, "") + "']"
      );
      if (!input) return { ok: false, reason: "no input" };
      setNative(input, value);
      return { ok: true, value: input.value };
    },
    saveState() {
      const btn = document.querySelector("button.go");
      if (!btn) return { ok: false, reason: "no save" };
      const label = (btn.textContent || "").replace(/\s+/g, " ").trim();
      return { ok: !btn.disabled, disabled: !!btn.disabled, text: label };
    },
    clickSave() {
      const btn = document.querySelector("button.go");
      if (!btn || btn.disabled) return { ok: false, reason: "save disabled" };
      btn.click();
      return { ok: true };
    },
    clickRow(title, label) {
      const row = [...document.querySelectorAll("li")].find((el) =>
        (el.innerText || "").includes(title)
      );
      if (!row) return { ok: false, reason: "no row" };
      const btn = [...row.querySelectorAll("button")].find(
        (el) => (el.textContent || "").trim() === label
      );
      if (!btn) return { ok: false, reason: "no button" };
      btn.click();
      return { ok: true };
    },
    clickDialog(label) {
      const dialog = document.querySelector("[role=dialog]");
      if (!dialog) return { ok: false, reason: "no dialog" };
      const btn = [...dialog.querySelectorAll("button")].find(
        (el) => (el.textContent || "").trim() === label
      );
      if (!btn) return { ok: false, reason: "no button" };
      btn.click();
      return { ok: true };
    },
    markers(present, absent) {
      const visible = text();
      const source = sourceText();
      const title = (
        (document.querySelector("#confirm-title") || {}).textContent || ""
      ).trim();
      const dialogs = document.querySelectorAll("[role=dialog]").length;
      const found = {};
      const blocked = {};
      for (const item of present) {
        found[item] = visible.includes(item) || source.includes(item);
      }
      for (const item of absent) blocked[item] = visible.includes(item);
      const ok =
        present.every((item) => found[item]) &&
        absent.every((item) => !blocked[item]);
      return {
        ok,
        href: location.href,
        title: document.title,
        dialogs,
        dialogTitle: title,
        found,
        blocked,
      };
    },
  };
})();
"""

_LOCK = threading.Lock()
_FAULTS: set[str] = set()
_PATCHES: List[Dict[str, Any]] = []

_FAULT_RULES = {
    "settings_get": ("GET", "/api/settings", "settings unavailable"),
    "queue_get": ("GET", "/api/queue", "queue unavailable"),
    "schedules_get": ("GET", "/api/schedules", "schedules unavailable"),
    "schedule_dispatch": ("POST", "/dispatch", "dispatch failed"),
    "schedule_cancel": ("POST", "/cancel", "cancel failed"),
}


def _fault_detail(method: str, path: str) -> Optional[str]:
    with _LOCK:
        active = set(_FAULTS)
    for name in active:
        rule = _FAULT_RULES.get(name)
        if rule is None:
            continue
        want_method, suffix, detail = rule
        if method != want_method:
            continue
        if name in {"schedule_dispatch", "schedule_cancel"}:
            if path.startswith("/api/schedules/") and path.endswith(suffix):
                return detail
            continue
        if path == suffix:
            return detail
    return None


async def _send_json(send: Any, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive: Any) -> bytes:
    chunks: List[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body") or b"")
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class _FaultMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            # A live tick refetches schedules and would clear a button error.
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        if path.startswith("/__e2e"):
            await self.app(scope, receive, send)
            return
        detail = _fault_detail(method, path)
        if detail is not None:
            await _send_json(send, 500, {"detail": detail})
            return
        if method == "PATCH" and path == "/api/settings":
            raw = await _read_body(receive)
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                with _LOCK:
                    _PATCHES.append(parsed)
            await self.app(scope, _replay(raw), send)
            return
        await self.app(scope, receive, send)


def _promote_e2e(app: Any) -> None:
    routes = app.router.routes
    front = [
        route
        for route in routes
        if str(getattr(route, "path", "")).startswith("/__e2e")
    ]
    rest = [route for route in routes if route not in front]
    routes[:] = front + rest


def _serve() -> None:
    data = os.environ.get("YAVER_DATA_DIR") or ""
    if not data:
        raise SystemExit("YAVER_DATA_DIR is required")
    os.environ["DASHBOARD_USERNAME"] = ""
    os.environ["DASHBOARD_PASSWORD"] = ""
    port = int(os.environ["YAVER_E2E_PORT"])

    from src.config import settings
    from src.dashboard.api import create_dashboard_app

    settings.dashboard_username = ""
    settings.dashboard_password = ""
    app = create_dashboard_app()

    def e2e_health() -> Dict[str, bool]:
        return {"ok": True}

    def e2e_faults(body: Dict[str, Any]) -> Dict[str, Any]:
        names = body.get("faults") if isinstance(body, dict) else None
        chosen = {str(item) for item in (names or []) if str(item) in _FAULT_RULES}
        with _LOCK:
            _FAULTS.clear()
            _FAULTS.update(chosen)
        return {"ok": True, "faults": sorted(chosen)}

    def e2e_reset() -> Dict[str, bool]:
        with _LOCK:
            _FAULTS.clear()
            _PATCHES.clear()
        return {"ok": True}

    def e2e_patches() -> Dict[str, Any]:
        with _LOCK:
            rows = list(_PATCHES)
        safe = []
        for row in rows:
            safe.append(
                {
                    key: row.get(key)
                    for key in (
                        "poll_interval_seconds",
                        "temp_clone_max_age_days",
                    )
                    if key in row
                }
            )
        return {"patches": safe}

    def e2e_job() -> Dict[str, str]:
        from src.state.job_store import job_store

        job = job_store.create_job(
            issue_key="E2E-RETRY",
            summary="retry error tail",
            status="error",
            workflow_type="execution",
            agent="derman-build",
        )
        if not job:
            raise RuntimeError("job was not stored")
        updated = job_store.update_job(
            job["job_id"],
            retry_attempt={
                "attempt_number": 1,
                "label": "retry1",
                "reason": "error",
                "delay_seconds": 0,
                "error_message": _RETRY_ERROR,
                "return_code": 1,
                "timestamp": "2026-01-01T00:00:00",
            },
        )
        if not updated:
            raise RuntimeError("retry was not stored")
        return {"job_id": str(job["job_id"])}

    def e2e_schedule(body: Dict[str, Any]) -> Dict[str, str]:
        from src.state.schedule_store import schedule_store

        title = str((body or {}).get("title") or "").strip()
        if not title:
            raise RuntimeError("title is required")
        rec = schedule_store.create(
            title=title,
            description="browser e2e",
            repository_url="https://gitlab.example.com/acme/api.git",
            source_branch="feature/e2e",
            target_branch="develop",
            mode="build",
            scheduled_at="2099-01-01T00:00:00",
            issue_key="E2E-SCH",
            issue_description="browser e2e schedule",
        )
        return {
            "schedule_id": str(rec["schedule_id"]),
            "title": str(rec["title"]),
        }

    app.add_api_route("/__e2e/health", e2e_health, methods=["GET"])
    app.add_api_route("/__e2e/faults", e2e_faults, methods=["POST"])
    app.add_api_route("/__e2e/reset", e2e_reset, methods=["POST"])
    app.add_api_route("/__e2e/patches", e2e_patches, methods=["GET"])
    app.add_api_route("/__e2e/job", e2e_job, methods=["POST"])
    app.add_api_route("/__e2e/schedule", e2e_schedule, methods=["POST"])
    _promote_e2e(app)
    app.add_middleware(_FaultMiddleware)

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__" and os.environ.get("YAVER_E2E_BROWSER") == "1":
    _serve()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _browser_exe() -> str:
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if chrome.is_file():
        return str(chrome)
    for candidate in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    pytest.fail("Chrome and Edge are both missing. Install one to run the browser checks.")


def _node_exe() -> str:
    found = shutil.which("node")
    if found:
        return found
    candidate = Path(r"C:\Program Files\nodejs\node.exe")
    if candidate.is_file():
        return str(candidate)
    pytest.fail("node.exe is not installed")


def _kill(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=_NO_WINDOW,
            check=False,
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def _safe_tail(path: Path, limit: int = 40) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    kept = []
    for line in lines:
        if any(
            word in line.lower()
            for word in ("token", "password", "secret", "authorization", "pat")
        ):
            kept.append("[redacted]")
        else:
            kept.append(line)
    return "\n".join(kept)


def _fingerprint(path: Path) -> Optional[tuple]:
    if not path.is_file():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


class _Cdp:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._next = 0

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        session: Optional[str] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        self._next += 1
        ident = self._next
        message: Dict[str, Any] = {"id": ident, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                raw = self.ws.recv(timeout=remaining)
            except TimeoutError:
                continue
            data = json.loads(raw)
            if data.get("id") != ident:
                continue
            if data.get("error"):
                raise RuntimeError(f"{method}: {data['error']}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        raise TimeoutError(method)


class BrowserStack:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.api_port = 0
        self.vite_port = 0
        self.origin = ""
        self._api: Optional[subprocess.Popen] = None
        self._vite: Optional[subprocess.Popen] = None
        self._chrome: Optional[subprocess.Popen] = None
        self._logs: List[Any] = []
        self._cdp: Optional[_Cdp] = None
        self._ws: Any = None
        self._session = ""
        self._opener = _opener()
        self._user_files = self._snapshot_user_files()

    def _snapshot_user_files(self) -> Dict[str, Optional[tuple]]:
        paths = [_ROOT / ".env", _ROOT / ".env.agent"]
        try:
            from src.paths import agent_data_dir

            data = agent_data_dir()
            paths.extend(
                [
                    data / "runtime_settings.json",
                    data / "saved_catalog.json",
                ]
            )
        except Exception:
            pass
        return {str(path): _fingerprint(path) for path in paths}

    def assert_user_files_unchanged(self) -> None:
        changed = []
        for name, before in self._user_files.items():
            if _fingerprint(Path(name)) != before:
                changed.append(name)
        assert not changed, f"browser e2e wrote operator files: {changed}"

    def start(self) -> None:
        try:
            self._start_api()
            self._start_vite()
            self._start_chrome()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        _kill(self._chrome)
        _kill(self._vite)
        _kill(self._api)
        for handle in self._logs:
            try:
                handle.close()
            except Exception:
                pass
        self._logs.clear()
        if _VITE_CONFIG.is_file():
            _VITE_CONFIG.unlink()

    def _start_api(self) -> None:
        self.api_port = _free_port()
        data = self.work / "data"
        data.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["YAVER_E2E_BROWSER"] = "1"
        env["YAVER_E2E_PORT"] = str(self.api_port)
        env["YAVER_DATA_DIR"] = str(data)
        env["DASHBOARD_USERNAME"] = ""
        env["DASHBOARD_PASSWORD"] = ""
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        for key in list(env):
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                env.pop(key, None)
        log_path = self.work / "api.log"
        handle = open(log_path, "w", encoding="utf-8")
        self._logs.append(handle)
        self._api = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve())],
            cwd=str(data),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
        self._wait_json(
            f"http://127.0.0.1:{self.api_port}/__e2e/health",
            self._api,
            log_path,
            timeout=60,
            key="ok",
        )

    def _start_vite(self) -> None:
        if _VITE_CONFIG.is_file():
            _VITE_CONFIG.unlink()
        self.vite_port = _free_port()
        self.origin = f"http://127.0.0.1:{self.vite_port}"
        _VITE_CONFIG.write_text(
            "\n".join(
                [
                    "import { defineConfig } from 'vite'",
                    "import react from '@vitejs/plugin-react'",
                    "import tailwindcss from '@tailwindcss/vite'",
                    "export default defineConfig({",
                    "  plugins: [react(), tailwindcss()],",
                    "  server: {",
                    f"    host: '127.0.0.1',",
                    f"    port: {self.vite_port},",
                    "    strictPort: true,",
                    "    proxy: {",
                    f"      '/api': 'http://127.0.0.1:{self.api_port}',",
                    "      '/ws': {",
                    f"        target: 'ws://127.0.0.1:{self.api_port}',",
                    "        ws: true,",
                    "      },",
                    "    },",
                    "  },",
                    "})",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        node = _node_exe()
        vite = _WEB / "node_modules" / "vite" / "bin" / "vite.js"
        if not vite.is_file():
            pytest.fail(f"Vite is not installed at {vite}")
        log_path = self.work / "vite.log"
        handle = open(log_path, "w", encoding="utf-8")
        self._logs.append(handle)
        self._vite = subprocess.Popen(
            [
                node,
                str(vite),
                "--config",
                _VITE_CONFIG.name,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.vite_port),
                "--strictPort",
            ],
            cwd=str(_WEB),
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
        self._wait_http(f"{self.origin}/", self._vite, log_path, timeout=90)
        self._wait_http(
            f"{self.origin}/src/main.tsx",
            self._vite,
            log_path,
            timeout=90,
        )
        self._wait_json(
            f"{self.origin}/api/meta",
            self._vite,
            log_path,
            timeout=30,
            key="version",
        )

    def _start_chrome(self) -> None:
        profile = self.work / "chrome"
        profile.mkdir(parents=True, exist_ok=True)
        log_path = self.work / "chrome.log"
        handle = open(log_path, "w", encoding="utf-8")
        self._logs.append(handle)
        self._chrome = subprocess.Popen(
            [
                _browser_exe(),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--no-proxy-server",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "--remote-allow-origins=*",
                "about:blank",
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
        port = self._devtools_port(profile, log_path)
        version = self._get_json(f"http://127.0.0.1:{port}/json/version")
        ws_url = str(version.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            pytest.fail("Chrome did not expose a DevTools socket")
        from websockets.sync.client import connect

        self._ws = connect(ws_url, open_timeout=15, max_size=8 * 1024 * 1024)
        self._cdp = _Cdp(self._ws)
        created = self._cdp.call("Target.createTarget", {"url": "about:blank"})
        attached = self._cdp.call(
            "Target.attachToTarget",
            {"targetId": created["targetId"], "flatten": True},
        )
        self._session = str(attached["sessionId"])
        self._cdp.call("Page.enable", session=self._session)
        self._cdp.call("Runtime.enable", session=self._session)
        self._cdp.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _HELPERS},
            session=self._session,
        )

    def _devtools_port(self, profile: Path, log_path: Path) -> int:
        marker = profile / "DevToolsActivePort"
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._chrome is not None and self._chrome.poll() is not None:
                pytest.fail(
                    "Chrome exited before DevTools opened\n" + _safe_tail(log_path)
                )
            if marker.is_file():
                line = marker.read_text(encoding="utf-8", errors="replace").splitlines()
                if line and line[0].strip().isdigit():
                    return int(line[0].strip())
            time.sleep(0.2)
        pytest.fail("Chrome did not write DevToolsActivePort\n" + _safe_tail(log_path))
        return 0

    def _wait_json(
        self,
        url: str,
        proc: Optional[subprocess.Popen],
        log_path: Path,
        timeout: float,
        key: str,
    ) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                pytest.fail(
                    f"{url} process exited {proc.returncode}\n" + _safe_tail(log_path)
                )
            try:
                with self._opener.open(url, timeout=5) as response:
                    raw = response.read()
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict) and parsed.get(key):
                    return
                last = "missing " + key
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        pytest.fail(f"{url} did not return {key} ({last})\n" + _safe_tail(log_path))

    def _wait_http(
        self,
        url: str,
        proc: Optional[subprocess.Popen],
        log_path: Path,
        timeout: float,
    ) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                pytest.fail(
                    f"{url} process exited {proc.returncode}\n" + _safe_tail(log_path)
                )
            try:
                with self._opener.open(url, timeout=5) as response:
                    if response.status == 200:
                        response.read()
                        return
                    last = str(response.status)
            except Exception as exc:
                last = str(exc)
            time.sleep(0.4)
        pytest.fail(f"{url} did not answer ({last})\n" + _safe_tail(log_path))

    def _get_json(self, url: str) -> Dict[str, Any]:
        deadline = time.time() + 15
        last = ""
        while time.time() < deadline:
            try:
                with self._opener.open(url, timeout=3) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                last = str(exc)
            time.sleep(0.2)
        pytest.fail(f"{url} did not return JSON ({last})")
        return {}

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.api_port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(req, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise AssertionError(raw.decode("utf-8", "replace")[:300]) from exc
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def reset(self) -> None:
        self.request("POST", "/__e2e/reset", {})

    def faults(self, names: List[str]) -> None:
        self.request("POST", "/__e2e/faults", {"faults": names})

    def patches(self) -> List[Dict[str, Any]]:
        payload = self.request("GET", "/__e2e/patches")
        rows = payload.get("patches")
        return rows if isinstance(rows, list) else []

    def _eval(self, expression: str, timeout: float = 20) -> Any:
        if self._cdp is None:
            raise RuntimeError("Chrome is not connected")
        result = self._cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session=self._session,
            timeout=timeout,
        )
        details = result.get("exceptionDetails")
        if details:
            text = details.get("text") or details.get("exception") or details
            raise RuntimeError(str(text))
        inner = result.get("result")
        if isinstance(inner, dict):
            return inner.get("value")
        return None

    def helper(self, name: str, *args: Any) -> Dict[str, Any]:
        payload = ", ".join(json.dumps(arg) for arg in args)
        value = self._eval(f"window.__yaverE2E.{name}({payload})")
        if not isinstance(value, dict):
            raise AssertionError(f"{name} returned {value!r}")
        return value

    def goto(self, path: str) -> None:
        if self._cdp is None:
            raise RuntimeError("Chrome is not connected")
        self._cdp.call(
            "Page.navigate",
            {"url": "data:text/html,<p>reset</p>"},
            session=self._session,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                href = self._eval("(location.href)")
            except Exception:
                href = ""
            if str(href).startswith("data:"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("did not leave the previous page")
        self._cdp.call(
            "Page.navigate",
            {"url": self.origin + path},
            session=self._session,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                state = self._eval(
                    "({ok: document.readyState === 'complete' && !!document.body})"
                )
            except Exception:
                state = None
            if isinstance(state, dict) and state.get("ok"):
                return
            time.sleep(0.2)
        raise AssertionError(f"did not load {path}")

    def _wait_helper(self, name: str, *args: Any, timeout: float = 90) -> Dict[str, Any]:
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                last = self.helper(name, *args)
            except Exception as exc:
                last = {"error": str(exc)}
                time.sleep(0.25)
                continue
            if last.get("ok"):
                return last
            time.sleep(0.25)
        raise AssertionError(f"{name} did not succeed: {last}")

    def wait_markers(
        self,
        present: List[str],
        absent: List[str],
        *,
        href: str = "",
        dialogs: Optional[int] = None,
        timeout: float = 90,
    ) -> Dict[str, Any]:
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                last = self.helper("markers", present, absent)
            except Exception as exc:
                last = {"error": str(exc)}
                time.sleep(0.25)
                continue
            href_ok = not href or str(last.get("href") or "") == href
            dialog_ok = dialogs is None or last.get("dialogs") == dialogs
            if last.get("ok") and href_ok and dialog_ok:
                return last
            time.sleep(0.25)
        raise AssertionError(
            "page did not match "
            f"present={present} absent={absent} href={href} dialogs={dialogs} "
            f"last={last}"
        )

    def set_labeled(self, label: str, value: str) -> None:
        result = self.helper("setInput", label, value)
        if not result.get("ok"):
            raise AssertionError(result)

    def click_save_when_ready(self) -> None:
        deadline = time.time() + 15
        last: Any = None
        while time.time() < deadline:
            last = self.helper("saveState")
            if last.get("ok"):
                clicked = self.helper("clickSave")
                if clicked.get("ok"):
                    return
            time.sleep(0.2)
        raise AssertionError(f"Save stayed disabled: {last}")

    def wait_patch(self, key: str, value: Any) -> List[Dict[str, Any]]:
        deadline = time.time() + 20
        last: List[Dict[str, Any]] = []
        while time.time() < deadline:
            last = self.patches()
            if any(row.get(key) == value for row in last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"no patch {key}={value!r} in {last}")

    def wait_save_settled(self) -> None:
        deadline = time.time() + 20
        last: Any = None
        while time.time() < deadline:
            last = self.helper("saveState")
            text = str(last.get("text") or "")
            if last.get("disabled") and "Saving" not in text and "Loading" not in text:
                return
            time.sleep(0.2)
        raise AssertionError(f"save did not settle: {last}")


@pytest.fixture(scope="session")
def browser(tmp_path_factory: pytest.TempPathFactory) -> Any:
    stack = BrowserStack(tmp_path_factory.mktemp("daily-browser-e2e"))
    stack.start()
    try:
        yield stack
    finally:
        try:
            stack.assert_user_files_unchanged()
        finally:
            stack.stop()


@pytest.fixture(autouse=True)
def _reset_browser(browser: BrowserStack) -> None:
    browser.reset()


def test_settings_load_failure_shows_the_api_detail(browser: BrowserStack) -> None:
    browser.faults(["settings_get"])
    browser.goto("/settings/jira")
    seen = browser.wait_markers(
        ["settings unavailable"],
        ["Could not save", "Loading settings"],
        dialogs=0,
    )
    assert seen["dialogs"] == 0
    assert seen["dialogTitle"] == ""


def test_blank_poll_interval_is_not_saved(browser: BrowserStack) -> None:
    browser.goto("/settings/jira")
    browser.wait_markers(["Poll interval (seconds)"], ["Loading settings"])
    browser.set_labeled("Poll interval (seconds)", "12")
    browser.set_labeled("Poll interval (seconds)", "")
    browser.click_save_when_ready()
    seen = browser.wait_markers(
        ["Could not save", "Poll interval needs a number."],
        [],
        dialogs=1,
    )
    assert seen["dialogTitle"] == "Could not save"
    assert browser.patches() == []


def test_clone_age_zero_is_saved(browser: BrowserStack) -> None:
    browser.goto("/settings/runtime")
    browser.wait_markers(
        ["Delete unused clones after (days)"],
        ["Loading settings"],
    )
    browser.set_labeled("Delete unused clones after (days)", "4")
    state = browser.helper("saveState")
    if not state.get("ok"):
        browser.set_labeled("Delete unused clones after (days)", "6")
        browser.click_save_when_ready()
        browser.wait_patch("temp_clone_max_age_days", 6)
        browser.wait_save_settled()
        browser.set_labeled("Delete unused clones after (days)", "4")
    browser.click_save_when_ready()
    browser.wait_patch("temp_clone_max_age_days", 4)
    browser.wait_save_settled()
    browser.set_labeled("Delete unused clones after (days)", "0")
    browser.click_save_when_ready()
    rows = browser.wait_patch("temp_clone_max_age_days", 0)
    assert rows[-1].get("temp_clone_max_age_days") == 0
    assert any(row.get("temp_clone_max_age_days") == 4 for row in rows)


def test_gitlab_settings_describe_review_commands(browser: BrowserStack) -> None:
    browser.goto("/settings/gitlab")
    browser.wait_markers(
        ["@name /review and @name /ask start a", "code review."],
        ["Code review (below)"],
    )


def test_jobs_filter_keeps_plain_text_and_uppercases_issue_keys(
    browser: BrowserStack,
) -> None:
    browser.goto("/jobs")
    browser._wait_helper("inputByPlaceholder", "Key, title, or description")
    typed = browser.helper("setPlaceholder", "Key, title, or description", "rate limit")
    assert typed.get("ok")
    browser.wait_markers(["\u00b7 rate limit"], ["RATE LIMIT"])
    browser.helper("setPlaceholder", "Key, title, or description", "kan-12")
    browser.wait_markers(["\u00b7 KAN-12"], ["\u00b7 rate limit"])
    value = browser.helper("inputByPlaceholder", "Key, title, or description")
    assert value.get("value") == "kan-12"


def test_queue_load_failure_is_not_an_empty_queue(browser: BrowserStack) -> None:
    browser.faults(["queue_get"])
    browser.goto("/jobs/queue")
    browser.wait_markers(
        ["queue unavailable"],
        ["Nothing waiting in the queue."],
    )


def test_job_overview_shows_the_full_retry_error(browser: BrowserStack) -> None:
    created = browser.request("POST", "/__e2e/job", {})
    job_id = str(created["job_id"])
    browser.goto(f"/jobs/{job_id}")
    browser.wait_markers([_RETRY_TAIL], ["Loading job"])


def test_schedule_load_failure_is_not_an_empty_list(browser: BrowserStack) -> None:
    browser.faults(["schedules_get"])
    browser.goto("/scheduled/jira")
    browser.wait_markers(
        ["schedules unavailable"],
        ["Nothing scheduled."],
    )


def test_schedule_run_now_failure_closes_the_dialog(browser: BrowserStack) -> None:
    title = f"e2e-run-{uuid.uuid4().hex[:8]}"
    browser.request("POST", "/__e2e/schedule", {"title": title})
    browser.faults(["schedule_dispatch"])
    browser.goto("/scheduled/jira")
    browser.wait_markers([title], ["Nothing scheduled."])
    opened = browser.helper("clickRow", title, "Run now")
    assert opened.get("ok"), opened
    browser.wait_markers(["Run this job now?"], [], dialogs=1)
    confirmed = browser.helper("clickDialog", "Run now")
    assert confirmed.get("ok"), confirmed
    seen = browser.wait_markers(["dispatch failed"], ["Run this job now?"], dialogs=0)
    assert seen["dialogTitle"] == ""


def test_schedule_cancel_failure_closes_the_dialog(browser: BrowserStack) -> None:
    title = f"e2e-cancel-{uuid.uuid4().hex[:8]}"
    browser.request("POST", "/__e2e/schedule", {"title": title})
    browser.faults(["schedule_cancel"])
    browser.goto("/scheduled/jira")
    browser.wait_markers([title], ["Nothing scheduled."])
    opened = browser.helper("clickRow", title, "Cancel")
    assert opened.get("ok"), opened
    browser.wait_markers(["Cancel this schedule?"], [], dialogs=1)
    confirmed = browser.helper("clickDialog", "Cancel it")
    assert confirmed.get("ok"), confirmed
    seen = browser.wait_markers(
        ["cancel failed"],
        ["Cancel this schedule?"],
        dialogs=0,
    )
    assert seen["dialogTitle"] == ""
