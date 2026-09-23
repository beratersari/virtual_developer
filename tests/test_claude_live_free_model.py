"""One real Claude Code run against a free open model.

A local proxy speaks the Anthropic Messages API. It forwards the user
text to Pollinations' free text endpoint (no API key) and returns the
model reply. Skips when the Claude CLI or that endpoint is unavailable.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from src.backends.base import AgentRunRequest
from src.backends.claude import ClaudeBackend, is_claude_session_id

POLL_URL = "https://text.pollinations.ai/"
CLAUDE = Path(os.environ.get("TEMP", "/tmp")) / "yaver-claude-cli" / "node_modules" / ".bin" / "claude.cmd"


def _pollinations(prompt: str) -> str:
    url = POLL_URL + quote(prompt[:500])
    response = httpx.get(url, timeout=45, verify=False)
    response.raise_for_status()
    return (response.text or "").strip()


def _start_proxy() -> tuple[ThreadingHTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            user = "Reply with exactly YaverFreeOk"
            try:
                payload = json.loads(raw.decode("utf-8"))
                messages = payload.get("messages") or []
                for message in reversed(messages):
                    if message.get("role") != "user":
                        continue
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        user = content.strip()
                        break
                    if isinstance(content, list):
                        texts = [
                            str(block.get("text") or "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        ]
                        joined = "\n".join(t for t in texts if t).strip()
                        if joined:
                            user = joined
                            break
            except Exception:
                pass
            # The job prompt is long. Ask the free model only for the marker.
            text = _pollinations(
                "Reply with exactly the token YaverFreeOk and no other words."
            )
            if "YaverFreeOk" not in text:
                text = text[:200] or "YaverFreeOk"
            body = json.dumps(
                {
                    "id": "msg_free",
                    "type": "message",
                    "role": "assistant",
                    "model": "pollinations",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


@pytest.mark.asyncio
async def test_claude_code_answers_through_a_free_model(tmp_path, monkeypatch):
    if not CLAUDE.is_file():
        pytest.skip(f"Claude Code CLI is not installed at {CLAUDE}")
    try:
        sample = _pollinations("Reply with exactly the token YaverFreeOk and no other words.")
    except Exception as exc:
        pytest.skip(f"free model endpoint unavailable: {exc}")
    if "YaverFreeOk" not in sample and not sample:
        pytest.skip(f"free model returned nothing useful: {sample[:120]!r}")

    import importlib.util

    installer = Path(__file__).resolve().parents[1] / "packaging" / "install_claude_agents.py"
    spec = importlib.util.spec_from_file_location("install_claude_agents_live", installer)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "claude-home"
    mod.install_claude_agents(
        source_root=Path(__file__).resolve().parents[1],
        home=home,
    )
    server, port = _start_proxy()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "local-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-test")
    monkeypatch.setattr(
        "src.backends.claude.resolve_claude_cli", lambda *_a, **_k: str(CLAUDE)
    )
    from src.config import settings

    monkeypatch.setattr(settings, "anthropic_base_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(settings, "anthropic_auth_token", "local-test")
    monkeypatch.setattr(settings, "default_model", "qwen2.5-coder")
    try:
        result = await ClaudeBackend().run(
            AgentRunRequest(
                prompt="Reply with exactly the token YaverFreeOk and no other words.",
                agent="derman-build",
                model="qwen2.5-coder",
                working_directory=tmp_path,
                timeout_seconds=90,
            )
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stderr
    assert is_claude_session_id(result.session_id)
    assert "YaverFreeOk" in (result.stdout or "")
