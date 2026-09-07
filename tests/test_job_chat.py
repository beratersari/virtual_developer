"""Job OpenCode chat history (SQLite messages + dashboard API)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.service import collect_job_chat
from src.opencode_sessions import extract_session_ids_from_text, list_session_chat
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager


def _chat_db(
    path: Path,
    *,
    session_id: str = "ses_chat1",
    title: str = "KAN-1: work",
    messages: list | None = None,
    base_time_ms: int | None = None,
) -> Path:
    """Create session + message + part tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            agent TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO session (
            id, title, directory, agent,
            time_created, time_updated, cost, tokens_input, tokens_output
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, title, "/tmp/clone", "atlas", 1, 2, 0.0, 0, 0),
    )
    import time as _time

    default_base = (
        int(base_time_ms)
        if base_time_ms is not None
        else int(_time.time() * 1000)
    )
    for i, row in enumerate(messages or []):
        if len(row) == 4:
            mid, msg, parts, t = row
        else:
            mid, msg, parts = row
            t = default_base + (i * 1000)
        con.execute(
            """
            INSERT INTO message (id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (mid, session_id, t, t, json.dumps(msg)),
        )
        for j, part in enumerate(parts):
            con.execute(
                """
                INSERT INTO part (
                    id, message_id, session_id, time_created, time_updated, data
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"prt_{i}_{j}", mid, session_id, t + j, t + j, json.dumps(part)),
            )
    con.commit()
    con.close()
    return path


def test_list_session_chat_joins_parts(tmp_path: Path):
    db = _chat_db(
        tmp_path / "chat.db",
        messages=[
            (
                "msg_u",
                {"role": "user", "time": {"created": 1_700_000_000_000}},
                [{"type": "text", "text": "Please implement KAN-1"}],
            ),
            (
                "msg_a",
                {"role": "assistant", "finish": "stop", "agent": "Atlas"},
                [
                    {"type": "reasoning", "text": "looking around"},
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "c1",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ls"},
                            "output": "a.txt\n",
                            "title": "ls",
                        },
                    },
                    {"type": "text", "text": "Done."},
                    {"type": "step-start"},
                ],
            ),
        ],
    )
    chat = list_session_chat("ses_chat1", db_path=db)
    assert chat["db_checked"] is True
    assert chat["error"] is None
    assert chat["title"] == "KAN-1: work"
    assert len(chat["messages"]) == 2
    user, asst = chat["messages"]
    assert user["role"] == "user"
    assert user["parts"][0]["text"] == "Please implement KAN-1"
    assert asst["role"] == "assistant"
    types = [p["type"] for p in asst["parts"]]
    assert types == ["reasoning", "tool", "text", "step-start"]
    tool = asst["parts"][1]
    assert tool["tool"] == "bash"
    assert tool["status"] == "completed"
    assert tool["output"] == "a.txt\n"
    assert "ls" in (tool.get("input") or {}).get("command", "")


def test_list_session_chat_thinking_alias_is_reasoning(tmp_path: Path):
    db = _chat_db(
        tmp_path / "think.db",
        messages=[
            (
                "msg_t",
                {"role": "assistant"},
                [{"type": "thinking", "text": "ponder"}],
            ),
        ],
    )
    chat = list_session_chat("ses_chat1", db_path=db)
    part = chat["messages"][0]["parts"][0]
    assert part["type"] == "reasoning"
    assert part["text"] == "ponder"


def test_list_session_chat_embedded_parts_without_part_table(tmp_path: Path):
    db = _chat_db(tmp_path / "emb.db", messages=[])
    con = sqlite3.connect(db)
    con.execute("DROP TABLE part")
    con.execute(
        """
        INSERT INTO message (id, session_id, time_created, time_updated, data)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "msg_e",
            "ses_chat1",
            10,
            10,
            json.dumps(
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "embedded ok"}],
                }
            ),
        ),
    )
    con.commit()
    con.close()
    chat = list_session_chat("ses_chat1", db_path=db)
    assert chat["messages"][0]["parts"][0]["text"] == "embedded ok"


def test_list_session_chat_missing_db(tmp_path: Path):
    chat = list_session_chat("ses_x", db_path=tmp_path / "nope.db")
    assert chat["messages"] == []
    assert chat["error"]


def test_list_session_chat_compaction_is_not_user_role(tmp_path: Path):
    """OpenCode stores compact as role=user; dashboard must not show You."""
    db = _chat_db(
        tmp_path / "compact.db",
        messages=[
            (
                "msg_u",
                {"role": "user"},
                [{"type": "text", "text": "Please implement KAN-1"}],
            ),
            (
                "msg_c",
                {"role": "user"},
                [{"type": "compaction", "auto": True}],
            ),
            (
                "msg_sum",
                {"role": "assistant", "agent": "compaction", "summary": True},
                [{"type": "text", "text": "## Compaction summary\nWork so far"}],
            ),
            (
                "msg_cont",
                {"role": "user"},
                [
                    {
                        "type": "text",
                        "text": "Continue the previous OpenCode session. The last turn stopped early (timeout or error).",
                    }
                ],
            ),
            (
                "msg_restore",
                {"role": "user"},
                [
                    {
                        "type": "text",
                        "text": "[restore checkpointed session agent configuration after compaction]\n<!-- OMO_INTERNAL_INITIATOR -->\n<!-- OMO_INTERNAL_NOREPLY -->",
                    }
                ],
            ),
            (
                "msg_oc_cont",
                {"role": "user"},
                [
                    {
                        "type": "text",
                        "text": (
                            "The previous request exceeded the provider's size "
                            "limit due to large media attachments. The "
                            "conversation was compacted and media files were "
                            "removed from context.\n\n"
                            "Continue if you have next steps, or stop and ask "
                            "for clarification if you are unsure how to proceed."
                        ),
                    }
                ],
            ),
            (
                "msg_a",
                {"role": "assistant", "finish": "stop", "agent": "Atlas"},
                [{"type": "text", "text": "Done."}],
            ),
        ],
    )
    chat = list_session_chat("ses_chat1", db_path=db)
    roles = [(m["role"], (m["parts"][0].get("text") or "")[:40]) for m in chat["messages"]]
    assert [r[0] for r in roles] == ["user", "compaction", "summary", "assistant"], roles
    summary = next(m for m in chat["messages"] if m["role"] == "summary")
    assert "Work so far" in (summary["parts"][0].get("text") or "")
    assert all(m["role"] != "user" or "Continue the previous" not in (m["parts"][0].get("text") or "") for m in chat["messages"])


def test_list_session_chat_keeps_first_search_mode_prompt(tmp_path: Path):
    """OMO wraps the first user turn; that is still the operator's message."""
    first = (
        "[search-mode] MAXIMIZE SEARCH EFFORT. Launch multiple background agents.\n\n"
        "[analyze-mode] ANALYSIS MODE.\n\n"
        "# Build mode\n\nYou run unattended inside a daemon.\n"
    )
    retry = (
        "[search-mode] MAXIMIZE SEARCH EFFORT.\n\n"
        "# Build mode\n\nRetry kit must be hidden.\n"
    )
    db = _chat_db(
        tmp_path / "first.db",
        messages=[
            ("msg_u1", {"role": "user"}, [{"type": "text", "text": first}]),
            (
                "msg_a1",
                {"role": "assistant", "finish": "stop"},
                [{"type": "text", "text": "Shall I continue with the remaining work?"}],
            ),
            ("msg_u2", {"role": "user"}, [{"type": "text", "text": retry}]),
            (
                "msg_a2",
                {"role": "assistant", "finish": "stop"},
                [{"type": "text", "text": "Working."}],
            ),
        ],
    )
    chat = list_session_chat("ses_chat1", db_path=db)
    users = [m for m in chat["messages"] if m["role"] == "user"]
    assert len(users) == 1, [m["role"] for m in chat["messages"]]
    body = users[0]["parts"][0].get("text") or ""
    assert body.startswith("# Build mode")
    assert "[search-mode]" not in body
    assert "Retry kit must be hidden" not in body
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "assistant"]


def test_list_session_chat_keeps_omo_tagged_task_on_resume(tmp_path: Path):
    """Continuing an older session must still show the original operator prompt.

    oh-my-openagent appends ``<!-- OMO_INTERNAL_INITIATOR -->`` to real TASK
    text. Matching that tag anywhere used to skip the first user turn.
    """
    first = (
        "1. TASK: Implement KAN-1 calculator add.\n\n"
        "2. EXPECTED OUTCOME: tests pass.\n\n"
        "<!-- OMO_INTERNAL_INITIATOR -->\n<!-- OMO_INTERNAL_NOREPLY -->"
    )
    db = _chat_db(
        tmp_path / "resume.db",
        messages=[
            ("msg_old", {"role": "user"}, [{"type": "text", "text": first}]),
            (
                "msg_a1",
                {"role": "assistant", "finish": "stop", "agent": "Atlas"},
                [{"type": "text", "text": "Working on add()."}],
            ),
            (
                "msg_c",
                {"role": "user"},
                [{"type": "compaction", "auto": True}],
            ),
            (
                "msg_sum",
                {"role": "assistant", "agent": "compaction", "summary": True},
                [{"type": "text", "text": "## Objective\n- Implement add() for KAN-1"}],
            ),
            (
                "msg_cont",
                {"role": "user"},
                [
                    {
                        "type": "text",
                        "text": (
                            "Continue after context compaction. Finish all remaining "
                            "todos and complete the original task."
                        ),
                    }
                ],
            ),
            (
                "msg_a2",
                {"role": "assistant", "finish": "stop", "agent": "Atlas"},
                [{"type": "text", "text": "add() done."}],
            ),
        ],
    )
    chat = list_session_chat("ses_chat1", db_path=db)
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["user", "assistant", "compaction", "summary", "assistant"], roles
    user = chat["messages"][0]
    body = user["parts"][0].get("text") or ""
    assert "Implement KAN-1" in body
    assert "OMO_INTERNAL" not in body
    assert all(
        "Continue after context" not in (m["parts"][0].get("text") or "")
        for m in chat["messages"]
        if m["role"] == "user"
    )
    assert chat["messages"][3]["role"] == "summary"
    assert "Implement add()" in (chat["messages"][3]["parts"][0].get("text") or "")


def test_collect_job_chat_discovers_session_by_working_directory(tmp_path: Path):
    clone = tmp_path / "clone"
    clone.mkdir()
    db = _chat_db(
        tmp_path / "wd.db",
        messages=[
            ("msg_u", {"role": "user"}, [{"type": "text", "text": "live"}]),
        ],
    )
    # Point the session row at this clone
    import sqlite3

    con = sqlite3.connect(db)
    con.execute(
        "UPDATE session SET directory=?, time_created=?, time_updated=?",
        (str(clone), 1_786_291_458_797, 1_786_291_458_797),
    )
    con.commit()
    con.close()
    job = {
        "job_id": "job_live",
        "issue_key": "KAN-7",
        "started_at": "2026-08-09T19:04:00",
        "working_directory": str(clone),
        "opencode_session_id": None,
        "opencode_session_ids": [],
        "retry_attempts": [],
        "session_log_paths": [],
    }
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        out = collect_job_chat(job)
    assert "ses_chat1" in out["session_ids"]
    assert out["messages"][0]["parts"][0]["text"] == "live"


def test_extract_session_ids_prefers_serve_created_line():
    text = (
        "[serve] health={'healthy': True}\n"
        "[serve] session created: ses_0140b26d4ffe3Xjzh7CZL40VyH\n"
        "[serve] turn=initial sending message…\n"
    )
    assert extract_session_ids_from_text(text) == ["ses_0140b26d4ffe3Xjzh7CZL40VyH"]


def test_collect_job_chat_discovers_session_from_serve_log(tmp_path: Path):
    """Output tab has the log; chat must parse ses_* from it during the run."""
    sid = "ses_chat1abcdef"
    db = _chat_db(
        tmp_path / "fromlog.db",
        session_id=sid,
        messages=[
            ("msg_u", {"role": "user"}, [{"type": "text", "text": "from log"}]),
        ],
    )
    log = tmp_path / "KAN-1_20260810_120000.log"
    log.write_text(
        f"[serve] session created: {sid}\n[serve] turn=initial sending message…\n",
        encoding="utf-8",
    )
    job = {
        "job_id": "job_log",
        "opencode_session_id": None,
        "opencode_session_ids": [],
        "retry_attempts": [],
        "session_log_path": str(log),
        "session_log_paths": [str(log)],
        "working_directory": None,
    }
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        out = collect_job_chat(job)
    assert out["session_ids"] == [sid]
    assert out["messages"][0]["parts"][0]["text"] == "from log"


def test_collect_job_chat_uses_job_session_ids(tmp_path: Path):
    db = _chat_db(
        tmp_path / "j.db",
        messages=[
            ("msg_u", {"role": "user"}, [{"type": "text", "text": "hi"}]),
        ],
    )
    job = {
        "job_id": "job_abc",
        "opencode_session_id": "ses_chat1",
        "opencode_session_ids": ["ses_chat1"],
        "retry_attempts": [],
        "session_log_paths": [],
    }
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        out = collect_job_chat(job)
    assert out["job_id"] == "job_abc"
    assert out["session_ids"] == ["ses_chat1"]
    assert len(out["messages"]) == 1
    assert out["sessions"][0]["message_count"] == 1


def test_api_job_chat(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _chat_db(
        tmp_path / "api.db",
        messages=[
            ("msg_u", {"role": "user"}, [{"type": "text", "text": "build it"}]),
        ],
    )
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    j = store.create_job(issue_key="CHAT-1", summary="s", description="d")
    store.update_job(j["job_id"], opencode_session_id="ses_chat1")
    sm.create_state("CHAT-1", "s", "d")

    with patch("src.dashboard.api.job_store", store), patch(
        "src.opencode_sessions._default_db_path", return_value=db
    ):
        app = create_dashboard_app(processor=None, state_manager=sm)
        client = TestClient(app)
        r = client.get(f"/api/jobs/{j['job_id']}/chat")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["job_id"] == j["job_id"]
        assert body["session_ids"] == ["ses_chat1"]
        assert body["messages"][0]["parts"][0]["text"] == "build it"
        assert client.get("/api/jobs/missing/chat").status_code == 404
        assert client.get("/api/jobs/legacy_x/chat").status_code == 404


def _iso_ms(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


_OMO_WRAP_A = (
    "[search-mode] MAXIMIZE SEARCH EFFORT.\n\n"
    "# Build mode\n\nFirst scheduled run: add() for KAN-9\n"
)
_OMO_WRAP_B = (
    "[search-mode] MAXIMIZE SEARCH EFFORT.\n\n"
    "# Build mode\n\nSecond run after cancel: add() for KAN-9\n"
)


def test_list_session_chat_hides_intra_job_retry_wrap(tmp_path: Path):
    t0 = 1_786_500_000_000
    db = _chat_db(
        tmp_path / "retry.db",
        messages=[
            ("u1", {"role": "user"}, [{"type": "text", "text": _OMO_WRAP_A}], t0),
            (
                "a1",
                {"role": "assistant"},
                [{"type": "text", "text": "working"}],
                t0 + 2_000,
            ),
            ("u2", {"role": "user"}, [{"type": "text", "text": _OMO_WRAP_A}], t0 + 4_000),
            (
                "a2",
                {"role": "assistant"},
                [{"type": "text", "text": "retrying"}],
                t0 + 5_000,
            ),
        ],
    )
    chat = list_session_chat("ses_chat1", db_path=db)
    users = [m for m in chat["messages"] if m["role"] == "user"]
    assert len(users) == 1
    assert "First scheduled run" in (users[0]["parts"][0].get("text") or "")


def test_collect_job_chat_continued_session_keeps_full_history(tmp_path: Path):
    """Continue after cancel: all prior prompts and model replies stay visible."""
    t0 = 1_786_510_000_000
    db = _chat_db(
        tmp_path / "requeue.db",
        messages=[
            ("u1", {"role": "user"}, [{"type": "text", "text": _OMO_WRAP_A}], t0 + 1_000),
            (
                "a1",
                {"role": "assistant", "agent": "Atlas"},
                [{"type": "text", "text": "cancelled mid-run"}],
                t0 + 60_000,
            ),
            (
                "u2",
                {"role": "user"},
                [{"type": "text", "text": _OMO_WRAP_B}],
                t0 + 8 * 60_000,
            ),
            (
                "a2",
                {"role": "assistant", "agent": "Atlas"},
                [{"type": "text", "text": "second run"}],
                t0 + 8 * 60_000 + 2_000,
            ),
        ],
    )
    job2 = {
        "job_id": "job_second",
        "status": "running",
        "started_at": _iso_ms(t0 + 8 * 60_000 - 500),
        "opencode_session_id": "ses_chat1",
        "opencode_session_ids": ["ses_chat1"],
        "description": "Second run after cancel: add() for KAN-9",
        "retry_attempts": [],
        "session_log_paths": [],
    }
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        second = collect_job_chat(job2)
    roles = [(m["role"], (m["parts"][0].get("text") or "")) for m in second["messages"]]
    users = [t for r, t in roles if r == "user"]
    assistants = [t for r, t in roles if r == "assistant"]
    assert len(users) == 2, roles
    assert "First scheduled run" in users[0]
    assert "Second run after cancel" in users[1]
    assert assistants == ["cancelled mid-run", "second run"]


def test_collect_job_chat_same_explanation_after_cancel(tmp_path: Path):
    """Same Jira text on requeue is still a new operator turn for the new job."""
    t0 = 1_786_520_000_000
    same = _OMO_WRAP_A
    db = _chat_db(
        tmp_path / "same.db",
        messages=[
            ("u1", {"role": "user"}, [{"type": "text", "text": same}], t0 + 1_000),
            (
                "a1",
                {"role": "assistant"},
                [{"type": "text", "text": "old"}],
                t0 + 2_000,
            ),
            ("u2", {"role": "user"}, [{"type": "text", "text": same}], t0 + 400_000),
            (
                "a2",
                {"role": "assistant"},
                [{"type": "text", "text": "new"}],
                t0 + 401_000,
            ),
        ],
    )
    job2 = {
        "job_id": "job_again",
        "status": "executing",
        "started_at": _iso_ms(t0 + 399_000),
        "opencode_session_id": "ses_chat1",
        "opencode_session_ids": ["ses_chat1"],
        "description": "First scheduled run: add() for KAN-9",
        "retry_attempts": [],
        "session_log_paths": [],
    }
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        out = collect_job_chat(job2)
    users = [m for m in out["messages"] if m["role"] == "user"]
    assistants = [m for m in out["messages"] if m["role"] == "assistant"]
    assert len(users) == 2, [m["role"] for m in out["messages"]]
    assert all("First scheduled run" in (u["parts"][0].get("text") or "") for u in users)
    assert [m["parts"][0].get("text") for m in assistants] == ["old", "new"]


def test_collect_job_chat_synthesizes_prompt_when_window_has_no_user(tmp_path: Path):
    t0 = 1_786_530_000_000
    db = _chat_db(
        tmp_path / "synth.db",
        messages=[
            (
                "cont",
                {"role": "user"},
                [
                    {
                        "type": "text",
                        "text": "Continue after context compaction. Finish all remaining todos.",
                    }
                ],
                t0 + 1_000,
            ),
            (
                "a1",
                {"role": "assistant"},
                [{"type": "text", "text": "resumed"}],
                t0 + 2_000,
            ),
        ],
    )
    prompt = tmp_path / "job.prompt.txt"
    prompt.write_text(
        "# Direct\n\n## Task\nRequeued after cancel: implement add\n\n# Instructions\n1. go\n",
        encoding="utf-8",
    )
    job = {
        "job_id": "job_synth",
        "status": "running",
        "started_at": _iso_ms(t0),
        "opencode_session_id": "ses_chat1",
        "opencode_session_ids": ["ses_chat1"],
        "description": "Requeued after cancel: implement add",
        "prompt_path": str(prompt),
        "retry_attempts": [],
        "session_log_paths": [],
    }
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        out = collect_job_chat(job)
    users = [m for m in out["messages"] if m["role"] == "user"]
    assert len(users) == 1
    assert "implement add" in (users[0]["parts"][0].get("text") or "").lower()
    assert users[0]["id"].endswith(":prompt")


def test_list_session_chat_restart_wraps_keeps_history_and_new_prompt(tmp_path: Path):
    t0 = 1_786_545_000_000
    db = _chat_db(
        tmp_path / "wraps.db",
        messages=[
            ("u1", {"role": "user"}, [{"type": "text", "text": _OMO_WRAP_A}], t0),
            (
                "a1",
                {"role": "assistant"},
                [{"type": "text", "text": "old answer"}],
                t0 + 1_000,
            ),
            ("c1", {"role": "user"}, [{"type": "compaction", "auto": True}], t0 + 2_000),
            (
                "s1",
                {"role": "assistant", "agent": "compaction", "summary": True},
                [{"type": "text", "text": "## Objective\n- old work"}],
                t0 + 3_000,
            ),
            ("u2", {"role": "user"}, [{"type": "text", "text": _OMO_WRAP_A}], t0 + 20_000),
            (
                "a2",
                {"role": "assistant"},
                [{"type": "text", "text": "new answer"}],
                t0 + 21_000,
            ),
            ("u3", {"role": "user"}, [{"type": "text", "text": _OMO_WRAP_A}], t0 + 22_000),
        ],
    )
    chat = list_session_chat(
        "ses_chat1", db_path=db, restart_wraps_at_ms=t0 + 15_000
    )
    users = [m for m in chat["messages"] if m["role"] == "user"]
    assistants = [m for m in chat["messages"] if m["role"] == "assistant"]
    summaries = [m for m in chat["messages"] if m["role"] == "summary"]
    assert len(users) == 2
    assert len(assistants) == 2
    assert assistants[0]["parts"][0]["text"] == "old answer"
    assert assistants[1]["parts"][0]["text"] == "new answer"
    assert summaries and "old work" in (summaries[0]["parts"][0].get("text") or "")


def test_list_session_chat_since_until_excludes_other_run(tmp_path: Path):
    t0 = 1_786_540_000_000
    db = _chat_db(
        tmp_path / "win.db",
        messages=[
            ("u1", {"role": "user"}, [{"type": "text", "text": "old prompt"}], t0),
            ("u2", {"role": "user"}, [{"type": "text", "text": "new prompt"}], t0 + 50_000),
        ],
    )
    early = list_session_chat(
        "ses_chat1", db_path=db, since_ms=t0 - 100, until_ms=t0 + 10_000
    )
    late = list_session_chat("ses_chat1", db_path=db, since_ms=t0 + 40_000)
    assert [m["parts"][0]["text"] for m in early["messages"]] == ["old prompt"]
    assert [m["parts"][0]["text"] for m in late["messages"]] == ["new prompt"]
