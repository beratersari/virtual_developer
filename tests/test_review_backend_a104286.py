"""Proof tests for the a104286..HEAD backend review. Operator-safe outcomes only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_claude_backend import SESSION, _write_fake_claude


def _question_then_finish_script(path: Path) -> None:
    """First turn calls AskUserQuestion. Second turn finishes with no question."""
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                f"SESSION = '{SESSION}'",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': SESSION}), flush=True)",
                "turn = 0",
                "for line in sys.stdin:",
                "    line = line.strip()",
                "    if not line:",
                "        continue",
                "    turn += 1",
                "    if turn == 1:",
                "        print(json.dumps({",
                "            'type': 'assistant', 'session_id': SESSION,",
                "            'message': {'role': 'assistant', 'content': [",
                "                {'type': 'text', 'text': 'Which database should I use?'},",
                "                {'type': 'tool_use', 'name': 'AskUserQuestion', 'input': {}},",
                "            ]},",
                "        }), flush=True)",
                "        print(json.dumps({",
                "            'type': 'result', 'subtype': 'success', 'is_error': False,",
                "            'session_id': SESSION, 'result': 'Which database should I use?',",
                "        }), flush=True)",
                "        continue",
                "    print(json.dumps({",
                "        'type': 'assistant', 'session_id': SESSION,",
                "        'message': {'role': 'assistant', 'content': [",
                "            {'type': 'text', 'text': 'Finished the work.'},",
                "        ]},",
                "    }), flush=True)",
                "    print(json.dumps({",
                "        'type': 'result', 'subtype': 'success', 'is_error': False,",
                "        'session_id': SESSION, 'result': 'Finished the work.',",
                "        'total_cost_usd': 0.2,",
                "    }), flush=True)",
                "    break",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_nudge_success_is_not_poisoned_by_the_earlier_question(tmp_path, monkeypatch):
    """A finished second turn must not be failed because the first turn asked.

    The orchestrator does not deliver when the run is incomplete. Keeping
    AskUserQuestion from the pre-nudge turn marks a finished job as failed
    and drops the work.
    """
    from src.backends.base import AgentRunRequest
    from src.backends.claude import ClaudeBackend

    cli = _write_fake_claude(tmp_path)
    _question_then_finish_script(tmp_path / "fake_claude.py")
    monkeypatch.setattr(
        "src.backends.claude.resolve_claude_cli", lambda *_a, **_k: str(cli)
    )
    result = await ClaudeBackend().run(
        AgentRunRequest(
            prompt="implement the ticket",
            agent="derman-build",
            working_directory=tmp_path,
            timeout_seconds=30,
        )
    )
    assert result.session_id == SESSION
    assert "Finished the work." in (result.stdout or "")
    assert result.incomplete is False
    assert result.returncode == 0
    assert result.timed_out is False
