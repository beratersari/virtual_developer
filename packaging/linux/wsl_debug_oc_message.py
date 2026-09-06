#!/usr/bin/env python3
import json
import urllib.error
import urllib.request

base = "http://127.0.0.1:14097"

DIR = "/root/yaver-wsl-test/workdir"


def req(method, url, body=None):
    data = None
    headers = {"x-opencode-directory": DIR}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read()
            print("STATUS", resp.status, url)
            print(raw[:2000].decode("utf-8", "replace"))
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        print("STATUS", e.code, url)
        print(raw[:2000].decode("utf-8", "replace"))
        return {}

cfg = req("GET", base + "/config")
print("model", cfg.get("model"))
print("cfg_keys", list(cfg)[:30])
created = req("POST", base + "/session", {"title": "probe2"})
sid = created.get("id")
print("sid", sid)
if sid:
    req(
        "POST",
        f"{base}/session/{sid}/message",
        {
            "model": {"providerID": "opencode", "modelID": "ling-3.0-flash-fin-free"},
            "parts": [{"type": "text", "text": "Reply with exactly WSL_OK"}],
        },
    )
    req("DELETE", f"{base}/session/{sid}")
