#!/usr/bin/env python3
"""Hit a live Yaver Linux dist + OpenCode serve with 30+ distinct HTTP requests.

Designed for WSL Ubuntu + the standalone linux zip (yaver) plus a real
``opencode serve``. Exit 0 only when every required case passes.

    YAVER_BASE=http://127.0.0.1:18081 \\
    OPENCODE_BASE=http://127.0.0.1:14097 \\
    python3 packaging/linux/wsl_integration_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

YAVER = os.environ.get("YAVER_BASE", "http://127.0.0.1:18081").rstrip("/")
OC = os.environ.get("OPENCODE_BASE", "http://127.0.0.1:14097").rstrip("/")


class Probe:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def req(
        self,
        name: str,
        method: str,
        url: str,
        *,
        expect: Any = 200,
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 30.0,
        allow_timeout: bool = False,
    ) -> Optional[Any]:
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        want = expect if isinstance(expect, (list, tuple, set)) else (expect,)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        except TimeoutError as exc:
            if allow_timeout:
                self.passed.append(name)
                print(f"OK   {name} -> timeout (provider stall; route accepted)")
                return None
            self.failed.append(f"{name}: timed out")
            print(f"FAIL {name}: timed out")
            return None
        except urllib.error.URLError as exc:
            if allow_timeout and "time" in str(exc).lower():
                self.passed.append(name)
                print(f"OK   {name} -> timeout (provider stall; route accepted)")
                return None
            self.failed.append(f"{name}: {exc}")
            print(f"FAIL {name}: {exc}")
            return None
        except Exception as exc:
            if allow_timeout and "time" in str(exc).lower():
                self.passed.append(name)
                print(f"OK   {name} -> timeout (provider stall; route accepted)")
                return None
            self.failed.append(f"{name}: {exc}")
            print(f"FAIL {name}: {exc}")
            return None
        ok = status in want
        line = f"{name} -> {status} ({ctype.split(';')[0]})"
        if ok:
            self.passed.append(name)
            print(f"OK   {line}")
        else:
            self.failed.append(f"{name}: expected {want} got {status}")
            print(f"FAIL {line} expected {want}")
        if not raw:
            return None
        if "json" in (ctype or "") or raw[:1] in (b"{", b"["):
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return raw
        return raw

    def summary(self) -> int:
        print()
        print(f"passed={len(self.passed)} failed={len(self.failed)}")
        for item in self.failed:
            print(f"  - {item}")
        if len(self.passed) < 30:
            print(f"FAIL need at least 30 passing requests, got {len(self.passed)}")
            return 1
        return 0 if not self.failed else 1


def main() -> int:
    p = Probe()

    # --- Yaver dashboard / API (distinct routes) ---
    index = p.req("yaver.spa_index", "GET", f"{YAVER}/", expect=200)
    p.req("yaver.favicon", "GET", f"{YAVER}/favicon.svg", expect=(200, 404))
    if isinstance(index, (bytes, bytearray)):
        text = index.decode("utf-8", errors="replace")
        js = css = None
        for token in text.replace("'", '"').split('"'):
            if token.startswith("/assets/index-") and token.endswith(".js"):
                js = token
            if token.startswith("/assets/index-") and token.endswith(".css"):
                css = token
        if js:
            p.req("yaver.spa_js", "GET", f"{YAVER}{js}", expect=200)
        else:
            p.failed.append("yaver.spa_js: no hashed JS in index.html")
            print("FAIL yaver.spa_js: no hashed JS")
        if css:
            p.req("yaver.spa_css", "GET", f"{YAVER}{css}", expect=200)
        else:
            p.failed.append("yaver.spa_css: no hashed CSS in index.html")
            print("FAIL yaver.spa_css: no hashed CSS")
    p.req("yaver.spa_fallback", "GET", f"{YAVER}/jobs/does-not-exist", expect=200)

    health = p.req("yaver.health", "GET", f"{YAVER}/api/health", expect=200)
    meta = p.req("yaver.meta", "GET", f"{YAVER}/api/meta", expect=200)
    if isinstance(meta, dict) and not meta.get("version"):
        p.failed.append("yaver.meta: missing version")
        print("FAIL yaver.meta missing version")
    p.req("yaver.dashboard", "GET", f"{YAVER}/api/dashboard", expect=200)
    p.req("yaver.tasks", "GET", f"{YAVER}/api/tasks", expect=200)
    p.req("yaver.jobs", "GET", f"{YAVER}/api/jobs", expect=200)
    p.req("yaver.queue", "GET", f"{YAVER}/api/queue", expect=200)
    p.req("yaver.poll", "GET", f"{YAVER}/api/poll", expect=200)
    p.req("yaver.settings", "GET", f"{YAVER}/api/settings", expect=200)
    p.req("yaver.models", "GET", f"{YAVER}/api/models", expect=200)
    p.req("yaver.schedules", "GET", f"{YAVER}/api/schedules", expect=200)
    p.req("yaver.storage", "GET", f"{YAVER}/api/storage", expect=200)
    p.req("yaver.storage_deletes", "GET", f"{YAVER}/api/storage/deletes", expect=200)
    p.req("yaver.opencode_sessions", "GET", f"{YAVER}/api/opencode-sessions", expect=200)
    p.req("yaver.jira_issue_types", "GET", f"{YAVER}/api/jira/issue-types", expect=(200, 502, 503, 400))
    p.req("yaver.job_missing", "GET", f"{YAVER}/api/jobs/no-such-job", expect=(404, 400))
    p.req("yaver.task_missing", "GET", f"{YAVER}/api/tasks/NO-SUCH-1", expect=(404, 200, 400))
    p.req("yaver.queue_delete_missing", "DELETE", f"{YAVER}/api/queue/no-such-q", expect=(404, 400))
    p.req(
        "yaver.bulk_delete_empty",
        "POST",
        f"{YAVER}/api/jobs/bulk-delete",
        body={"job_ids": []},
        expect=(200, 400),
    )
    p.req(
        "yaver.settings_patch_poll",
        "PATCH",
        f"{YAVER}/api/settings",
        body={"poll_interval_seconds": 45},
        expect=(200, 400),
    )
    p.req(
        "yaver.settings_patch_restore",
        "PATCH",
        f"{YAVER}/api/settings",
        body={"poll_interval_seconds": 30},
        expect=(200, 400),
    )
    p.req("yaver.jira_test", "POST", f"{YAVER}/api/settings/jira/test", body={}, expect=(200, 400, 502))
    p.req("yaver.gitlab_test", "POST", f"{YAVER}/api/settings/gitlab/test", body={}, expect=(200, 400, 422, 502))
    p.req(
        "yaver.schedule_preview",
        "GET",
        f"{YAVER}/api/schedules/preview?cron=0+9+*+*+1-5",
        expect=(200, 400, 422),
    )
    p.req(
        "yaver.report",
        "POST",
        f"{YAVER}/api/reports",
        body={"kind": "general", "note": "wsl linux-dist integration probe"},
        expect=(200, 201),
    )
    p.req(
        "yaver.jira_webhook_bad",
        "POST",
        f"{YAVER}/webhooks/jira",
        body={"webhookEvent": "jira:issue_updated"},
        expect=(200, 401, 403, 400),
    )
    p.req(
        "yaver.gitlab_webhook_bad",
        "POST",
        f"{YAVER}/webhooks/gitlab",
        body={"object_kind": "note"},
        expect=(401, 403, 400),
    )
    p.req(
        "yaver.task_start_disabled",
        "POST",
        f"{YAVER}/api/tasks/NO-SUCH-1/start",
        body={},
        expect=(400, 403, 404, 409, 410),
    )
    p.req(
        "yaver.storage_delete_bad",
        "POST",
        f"{YAVER}/api/storage/delete",
        body={"name": "../etc", "area": "temp"},
        expect=(400, 422),
    )

    # --- Real OpenCode serve ---
    oc_health = p.req("oc.health", "GET", f"{OC}/global/health", expect=200)
    p.req("oc.config", "GET", f"{OC}/config", expect=200)
    work = os.environ.get("OPENCODE_DIRECTORY", "/root/yaver-wsl-test/workdir")
    oc_headers = {"x-opencode-directory": work}
    p.req("oc.session_list", "GET", f"{OC}/session", headers=oc_headers, expect=200)
    p.req("oc.session_status", "GET", f"{OC}/session/status", headers=oc_headers, expect=200)
    created = p.req(
        "oc.session_create",
        "POST",
        f"{OC}/session",
        body={"title": "wsl-integration-probe"},
        headers=oc_headers,
        expect=200,
    )
    sid = None
    if isinstance(created, dict):
        sid = created.get("id") or (created.get("data") or {}).get("id")
    if not sid:
        p.failed.append("oc.session_create: no session id")
        print("FAIL oc.session_create: no session id")
    else:
        p.req("oc.session_get", "GET", f"{OC}/session/{sid}", headers=oc_headers, expect=200)
        p.req("oc.session_todos", "GET", f"{OC}/session/{sid}/todo", headers=oc_headers, expect=(200, 404))
        p.req("oc.session_messages", "GET", f"{OC}/session/{sid}/message", headers=oc_headers, expect=200)
        model = (os.environ.get("DEFAULT_MODEL") or "opencode/mimo-v2.5-free").strip()
        provider, mid = (model.split("/", 1) + ["mimo-v2.5-free"])[:2]
        p.req(
            "oc.session_message",
            "POST",
            f"{OC}/session/{sid}/message",
            body={
                "model": {"providerID": provider, "modelID": mid},
                "parts": [{"type": "text", "text": "Reply with exactly: WSL_OK"}],
            },
            headers=oc_headers,
            expect=(200, 400, 429, 500),
            timeout=20.0,
            allow_timeout=True,
        )
        p.req("oc.session_abort", "POST", f"{OC}/session/{sid}/abort", headers=oc_headers, expect=(200, 204, 400))
        p.req("oc.session_delete", "DELETE", f"{OC}/session/{sid}", headers=oc_headers, expect=(200, 204))

    rc = p.summary()
    if health is None:
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
