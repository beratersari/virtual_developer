"""Job OpenCode chat history (SQLite messages + dashboard API)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.service import collect_job_chat
from src.opencode_sessions import list_session_chat
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager


def _chat_db(
    path: Path,
    *,
    session_id: str = "ses_chat1",
    title: str = "KAN-1: work",
    messages: list[tuple[str, dict, list[dict]]] | None = None,
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
    for i, (mid, msg, parts) in enumerate(messages or []):
        t = 1000 + i
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
