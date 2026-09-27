"""Codex thread id and Claude result parsing."""

from __future__ import annotations

import json


def test_codex_command_output_does_not_replace_thread_id():
    from src.backends.codex import parse_codex_thread_id

    text = "\n".join(
        [
            '{"type":"thread.started","thread_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}',
            '{"type":"item.completed","item":{"type":"command_execution","output":"session_id: 11111111-2222-3333-4444-555555555555"}}',
        ]
    )
    assert parse_codex_thread_id(text) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_claude_result_with_message_clears_the_question():
    from src.backends.claude import claude_reply_asks_question, parse_claude_output

    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Which database?"},
                            {"type": "tool_use", "name": "AskUserQuestion"},
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "Committed the fix.",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Committed the fix."}],
                    },
                }
            ),
        ]
    )
    parsed = parse_claude_output(raw)
    assert parsed["text"] == "Committed the fix."
    assert "AskUserQuestion" not in (parsed.get("tools") or [])
    assert claude_reply_asks_question(parsed) is False
