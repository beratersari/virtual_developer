"""Claude Code backend: argv, question nudge, and a fake CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src.backends.base import BACKEND_CLAUDE, normalize_backend_name
from src.backends.claude import (
    build_claude_argv,
    claude_reply_asks_question,
    parse_claude_output,
)
from src.backends.registry import get_agent_backend


SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_install_claude_agents_rewrites_frontmatter(tmp_path):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "packaging" / "install_claude_agents.py"
    spec = importlib.util.spec_from_file_location("install_claude_agents", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = Path(__file__).resolve().parents[1]
    install_claude_agents = mod.install_claude_agents
    dest = install_claude_agents(source_root=root, home=tmp_path / "claude")
    text = (dest / "agents" / "derman-build.md").read_text(encoding="utf-8")
    header, _, body = text.split("---", 2)
    assert "name: derman-build" in header or "name: derman-build" in text.split("---")[1]
    assert "AskUserQuestion" in text
    assert "mode: primary" not in text.split("---")[1]
    assert "strictly unattended" in body.lower()
    assert (dest / "skills" / "python" / "SKILL.md").is_file()


def test_normalize_claude_backend_name():
    assert normalize_backend_name("claude") == BACKEND_CLAUDE
    assert normalize_backend_name("claude-code") == BACKEND_CLAUDE
    assert get_agent_backend("claude").name == BACKEND_CLAUDE


def test_build_claude_argv_is_unattended_and_has_no_secret():
    argv = build_claude_argv(
        cli="claude",
        prompt="do the work",
        model="qwen2.5-coder",
        agent="derman-build",
    )
    assert argv[0] == "claude"
    assert "--print" in argv
    assert "bypassPermissions" in argv
    assert "AskUserQuestion" in argv
    assert "--agent" in argv
    assert "derman-build" in argv
    assert "--model" in argv
    assert "qwen2.5-coder" in argv
    assert argv[-1].startswith("UNATTENDED JOB:")
    assert "do the work" in argv[-1]
    assert "sk-" not in " ".join(argv)


def test_build_claude_argv_resumes_a_uuid_only():
    argv = build_claude_argv(
        cli="claude",
        prompt="continue",
        resume_id=SESSION,
        agent="build",
    )
    assert "--resume" in argv
    assert SESSION in argv
    assert "derman-build" in argv
    skipped = build_claude_argv(
        cli="claude",
        prompt="continue",
        resume_id="ses_abc",
    )
    assert "--resume" not in skipped


def test_session_log_drops_cli_diagnostic_on_success():
    from src.backends.claude import claude_session_log_text

    body = claude_session_log_text(
        "YaverFreeOk\nHere’s a quick check for you.",
        '[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}',
        returncode=0,
    )
    assert body == "YaverFreeOk\nHere’s a quick check for you."
    assert "query_source" not in body


def test_parse_claude_output_shapes():
    """Real CLI shapes: pretty JSON, stream-json, and the KAN-537 mix."""
    from src.backends.claude import claude_session_log_text, parse_claude_output

    live = (
        "YaverFreeOk\nHere’s a quick check for you.\n"
        '[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}'
    )
    parsed = parse_claude_output(live)
    assert parsed["text"] == "YaverFreeOk\nHere’s a quick check for you."
    assert "query_source" not in parsed["text"]

    nested = (
        '[claude-code:unrecognized_model] '
        '{"model":"openai-fast","query_source":"sdk","extra":{"nested":true}}'
    )
    assert "query_source" not in parse_claude_output("Hello\n" + nested)["text"]

    pretty = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "From pretty JSON.",
            "session_id": SESSION,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
        indent=2,
    )
    pretty_parsed = parse_claude_output(pretty)
    assert pretty_parsed["text"] == "From pretty JSON."
    assert pretty_parsed["session_id"] == SESSION

    raw_stdout = json.dumps(
        {
            "type": "result",
            "result": "YaverFreeOk\nHere’s a quick check for you.",
            "session_id": SESSION,
        }
    )
    stored = claude_session_log_text(
        raw_stdout,
        '[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}',
        returncode=0,
    )
    assert stored == "YaverFreeOk\nHere’s a quick check for you."

    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": SESSION,
                    "model": "openai-fast",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Working."},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                        ],
                    },
                    "session_id": SESSION,
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "Working.",
                    "session_id": SESSION,
                    "total_cost_usd": 0,
                }
            ),
        ]
    )
    stream_parsed = parse_claude_output(stream)
    assert stream_parsed["text"] == "Working."
    assert "Bash" in stream_parsed["tools"]
    assert "total_cost_usd" not in stream_parsed["text"]

    blocks = parse_claude_output(
        json.dumps(
            {
                "type": "result",
                "result": [{"type": "text", "text": "From blocks."}],
                "session_id": SESSION,
            }
        )
    )
    assert blocks["text"] == "From blocks."

    failed = parse_claude_output(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "",
                "error": "model not found",
                "session_id": SESSION,
            }
        )
    )
    assert failed["text"] == "model not found"
    assert failed["is_error"] is True

    own = parse_claude_output('Use this config:\n{"ok": true}')
    assert own["text"] == 'Use this config:\n{"ok": true}'

    same_line = parse_claude_output(
        'Hello [claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"} there'
    )
    assert same_line["text"] == "Hello there"

    kept = claude_session_log_text("partial", "connection refused", returncode=1)
    assert kept == "partial\nconnection refused"
    dropped = claude_session_log_text(
        "partial",
        '[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}',
        returncode=1,
    )
    assert dropped == "partial"


def test_job_backend_params_beat_a_uuid_session():
    from src.dashboard.service import _resolve_job_backend

    claude_job = {
        "backend": "",
        "opencode_session_id": SESSION,
        "description": "{params}\nBackend: claude\n{params}",
    }
    assert _resolve_job_backend(claude_job) == "claude"
    assert (
        _resolve_job_backend({"opencode_session_id": SESSION}) == "codex"
    )


def test_parse_claude_json_and_question_tool():
    raw = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "Which database should I use?",
            "session_id": SESSION,
        }
    )
    parsed = parse_claude_output(raw)
    assert parsed["session_id"] == SESSION
    assert claude_reply_asks_question(parsed) is True
    asked = {
        "text": "done",
        "tools": ["AskUserQuestion"],
    }
    assert claude_reply_asks_question(asked) is True
    assert claude_reply_asks_question({"text": "Committed the fix.", "tools": []}) is False


def _write_fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "\n".join(
            [
                "import json, sys",
                "args = sys.argv[1:]",
                "prompt = args[-1] if args else ''",
                "resume = '--resume' in args",
                "if resume or 'no human in this' in prompt.lower():",
                "    text = 'Finished the work.'",
                "else:",
                "    text = 'Which database should I use?'",
                "print(json.dumps({",
                "    'type': 'result',",
                "    'subtype': 'success',",
                "    'is_error': False,",
                f"    'session_id': '{SESSION}',",
                "    'result': text,",
                "}))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        cmd = tmp_path / "claude.cmd"
        cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
        return cmd
    launcher = tmp_path / "claude"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


@pytest.mark.asyncio
async def test_claude_backend_nudges_once_then_finishes(tmp_path, monkeypatch):
    from src.backends.base import AgentRunRequest
    from src.backends.claude import ClaudeBackend

    cli = _write_fake_claude(tmp_path)
    monkeypatch.setattr(
        "src.backends.claude.resolve_claude_cli", lambda *_a, **_k: str(cli)
    )

    backend = ClaudeBackend()
    result = await backend.run(
        AgentRunRequest(
            prompt="implement the ticket",
            agent="derman-build",
            model="qwen2.5-coder",
            working_directory=tmp_path,
            timeout_seconds=30,
        )
    )
    assert result.backend == BACKEND_CLAUDE
    assert result.session_id == SESSION
    assert result.incomplete is False
    assert result.returncode == 0
    assert "Finished the work." in result.stdout
    assert result.extra.get("unattended_nudge") is True
