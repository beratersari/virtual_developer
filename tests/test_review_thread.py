"""Selected-code / review-thread context for GitLab and Azure comments."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from src.orchestrator.prompt_builder import PromptBuilder
from src.review_thread import (
    apply_gitlab_note_position,
    attach_workdir_snippet,
    extract_review_context,
    format_review_context,
)


def test_gitlab_diffnote_position_becomes_review_location():
    raw = {
        "object_kind": "note",
        "object_attributes": {
            "type": "DiffNote",
            "note": "@yaver /yaver rename this helper",
            "position": {
                "new_path": "src/auth.py",
                "old_path": "src/auth.py",
                "new_line": 12,
                "line_range": {
                    "start": {"new_line": 10, "old_line": None},
                    "end": {"new_line": 15, "old_line": None},
                },
                "head_sha": "abc123",
            },
        },
    }
    ctx = extract_review_context(raw, current_body="@yaver /yaver rename this helper")
    assert ctx["file_path"] == "src/auth.py"
    assert ctx["start_line"] == 10
    assert ctx["end_line"] == 15
    assert ctx["side"] == "new"
    assert ctx["kind"] == "diff"
    text = format_review_context(ctx)
    assert "## Review location" in text
    assert "`src/auth.py`" in text
    assert "10–15" in text


def test_gitlab_deleted_line_uses_old_side():
    raw = {
        "object_attributes": {
            "type": "DiffNote",
            "position": {
                "old_path": "gone.py",
                "new_path": "gone.py",
                "old_line": 4,
            },
        }
    }
    ctx = extract_review_context(raw)
    assert ctx["start_line"] == 4
    assert ctx["side"] == "old"


def test_azure_thread_context_and_parent_comment():
    raw = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {
                "id": 2,
                "content": "@yaver /yaver fix the race",
                "parentComment": {
                    "content": "This lock looks racy.",
                    "author": {"displayName": "Bob"},
                },
            },
            "thread": {
                "id": 8,
                "threadContext": {
                    "filePath": "/src/lock.c",
                    "rightFileStart": {"line": 40, "offset": 1},
                    "rightFileEnd": {"line": 48, "offset": 8},
                },
                "comments": [
                    {
                        "content": "This lock looks racy.",
                        "author": {"displayName": "Bob"},
                    },
                    {
                        "content": "@yaver /yaver fix the race",
                        "author": {"displayName": "Alice"},
                    },
                ],
            },
        },
    }
    ctx = extract_review_context(raw, current_body="@yaver /yaver fix the race")
    assert ctx["file_path"] == "src/lock.c"
    assert ctx["start_line"] == 40
    assert ctx["end_line"] == 48
    assert ctx["side"] == "right"
    bodies = [c["body"] for c in ctx["thread_comments"]]
    assert "This lock looks racy." in bodies
    assert "@yaver /yaver fix the race" not in bodies


def test_azure_left_side_deleted_lines():
    raw = {
        "resource": {
            "thread": {
                "threadContext": {
                    "filePath": "old.c",
                    "leftFileStart": {"line": 2},
                    "leftFileEnd": {"line": 3},
                }
            }
        }
    }
    ctx = extract_review_context(raw)
    assert ctx["side"] == "left"
    assert ctx["start_line"] == 2
    assert ctx["end_line"] == 3


def test_snippet_from_workdir(tmp_path):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "mod.py").write_text("a\nb\nSELECTED\nd\n", encoding="utf-8")
    ctx = attach_workdir_snippet(
        {"file_path": "pkg/mod.py", "start_line": 3, "end_line": 3},
        str(tmp_path),
    )
    assert ctx["snippet"] == "SELECTED"
    text = format_review_context(ctx)
    assert "```3:3:pkg/mod.py" in text
    assert "SELECTED" in text


def test_snippet_missing_file_records_error(tmp_path):
    ctx = attach_workdir_snippet(
        {"file_path": "nope.py", "start_line": 1},
        str(tmp_path),
    )
    assert "snippet_error" in ctx
    assert "nope.py" in format_review_context(ctx)


def test_overview_comment_has_no_review_location():
    raw = {
        "object_attributes": {
            "type": "DiscussionNote",
            "note": "@yaver /yaver look at the MR",
        }
    }
    ctx = extract_review_context(raw, current_body="@yaver /yaver look at the MR")
    assert "file_path" not in ctx
    assert format_review_context(ctx) == ""


def test_gitlab_line_code_only_range_becomes_review_location():
    """GitLab range threads often send line_code without new_line on start/end."""
    raw = {
        "object_kind": "note",
        "object_attributes": {
            "type": "DiffNote",
            "note": "@yaver /yaver extract this helper",
            "line_code": "588440f66559714280628a4f9799f0c4eb880a4a_0_18",
            "position": {
                "new_path": "src/auth.py",
                "old_path": "src/auth.py",
                "new_line": None,
                "old_line": None,
                "line_range": {
                    "start": {
                        "line_code": "588440f66559714280628a4f9799f0c4eb880a4a_0_10",
                        "type": "new",
                        "new_line": None,
                        "old_line": None,
                    },
                    "end": {
                        "line_code": "588440f66559714280628a4f9799f0c4eb880a4a_0_18",
                        "type": "new",
                        "new_line": None,
                        "old_line": None,
                    },
                },
            },
        },
    }
    ctx = extract_review_context(raw, current_body="@yaver /yaver extract this helper")
    assert ctx["file_path"] == "src/auth.py"
    assert ctx["start_line"] == 10
    assert ctx["end_line"] == 18
    text = format_review_context(ctx)
    assert "10–18" in text
    assert "`src/auth.py`" in text


def test_gitlab_json_string_position_and_camel_case():
    raw = {
        "object_attributes": {
            "type": "DiffNote",
            "position": (
                '{"newPath":"lib/util.ts","newLine":22,'
                '"lineRange":{"start":{"newLine":20},"end":{"newLine":22}}}'
            ),
        }
    }
    ctx = extract_review_context(raw)
    assert ctx["file_path"] == "lib/util.ts"
    assert ctx["start_line"] == 20
    assert ctx["end_line"] == 22


def test_gitlab_st_diff_and_top_level_line_code():
    raw = {
        "object_attributes": {
            "type": "DiffNote",
            "line_code": "bec9703f7a456cd2b4ab5fb3220ae016e3e394e3_0_7",
            "st_diff": {"new_path": "six.py", "old_path": "six.py"},
        }
    }
    ctx = extract_review_context(raw)
    assert ctx["file_path"] == "six.py"
    assert ctx["start_line"] == 7


def test_gitlab_prompt_includes_line_code_range():
    PromptBuilder.clear_prompt_file_cache()
    p = PromptBuilder.build_gitlab_comment_prompt(
        issue_key="KAN-9",
        mr_title="Login",
        mr_url="https://gitlab.example.com/g/r/-/merge_requests/1",
        source_branch="feature/x",
        target_branch="develop",
        author="alice",
        comment="@yaver /yaver extract this helper",
        raw={
            "object_attributes": {
                "type": "DiffNote",
                "position": {
                    "new_path": "src/auth.py",
                    "line_range": {
                        "start": {
                            "line_code": "abc_0_10",
                            "type": "new",
                        },
                        "end": {
                            "line_code": "abc_0_14",
                            "type": "new",
                        },
                    },
                },
            }
        },
    )
    assert "## Review location" in p
    assert "`src/auth.py`" in p
    assert "10–14" in p
    assert "extract this helper" in p


def test_gitlab_prompt_includes_selected_range():
    PromptBuilder.clear_prompt_file_cache()
    p = PromptBuilder.build_gitlab_comment_prompt(
        issue_key="KAN-9",
        mr_title="Login",
        mr_url="https://gitlab.example.com/g/r/-/merge_requests/1",
        source_branch="feature/x",
        target_branch="develop",
        author="alice",
        comment="@yaver /yaver rename this",
        raw={
            "object_attributes": {
                "type": "DiffNote",
                "position": {
                    "new_path": "src/auth.py",
                    "new_line": 12,
                    "line_range": {
                        "start": {"new_line": 10},
                        "end": {"new_line": 14},
                    },
                },
            }
        },
    )
    assert "## Review location" in p
    assert "`src/auth.py`" in p
    assert "10–14" in p
    assert "## Prompt" in p
    assert "rename this" in p


def test_azure_prompt_includes_thread_and_range():
    PromptBuilder.clear_prompt_file_cache()
    p = PromptBuilder.build_azure_comment_prompt(
        issue_key="KAN-9",
        pr_title="Lock",
        pr_url="https://tfs.example.com/pr/1",
        source_branch="feature/x",
        target_branch="develop",
        author="alice",
        comment="fix the race",
        raw={
            "eventType": "ms.vss-code.git-pullrequest-comment-event",
            "resource": {
                "comment": {
                    "content": "@yaver /yaver fix the race",
                    "parentComment": {"content": "This lock is racy."},
                },
                "thread": {
                    "threadContext": {
                        "filePath": "/src/lock.c",
                        "rightFileStart": {"line": 40},
                        "rightFileEnd": {"line": 42},
                    }
                },
            },
        },
    )
    assert "## Review location" in p
    assert "`src/lock.c`" in p
    assert "40–42" in p
    assert "This lock is racy." in p


def test_apply_gitlab_note_position_replaces_line_code_with_range():
    """Webhook line_code is the end line; API line_range is the selection."""
    ctx = extract_review_context(
        {
            "object_attributes": {
                "type": "DiffNote",
                "line_code": "abc_0_18",
                "st_diff": {"new_path": "src/auth.py", "old_path": "src/auth.py"},
            }
        }
    )
    assert ctx["start_line"] == 18
    assert ctx["end_line"] == 18
    filled = apply_gitlab_note_position(
        ctx,
        {
            "id": 77,
            "type": "DiffNote",
            "position": {
                "new_path": "src/auth.py",
                "new_line": 18,
                "line_range": {
                    "start": {"new_line": 10, "type": "new"},
                    "end": {"new_line": 18, "type": "new"},
                },
            },
        },
    )
    assert filled["file_path"] == "src/auth.py"
    assert filled["start_line"] == 10
    assert filled["end_line"] == 18


def test_gitlab_review_needs_position_for_official_diff_webhook():
    from src.gitlab.webhook import GitlabMrNoteEvent
    from src.processor import JobProcessor

    event = GitlabMrNoteEvent(
        issue_key="GL-ACME-DEMO-4",
        note_id="77",
        note_body="@yaver /yaver extract this helper",
        prompt="extract this helper",
        author_username="alice",
        author_name="Alice",
        project_id=1,
        project_path="acme/demo",
        repository_url="https://gitlab.example.com/acme/demo.git",
        host="gitlab.example.com",
        mr_iid=4,
        mr_title="Login",
        mr_description="",
        source_branch="feature/x",
        target_branch="develop",
        mr_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        raw={
            "object_attributes": {
                "id": 77,
                "note": "@yaver /yaver extract this helper",
                "noteable_type": "MergeRequest",
                "line_code": "abc_0_18",
                "st_diff": {"new_path": "src/auth.py", "old_path": "src/auth.py"},
            }
        },
    )
    ctx = extract_review_context(event.raw, current_body=event.prompt)
    assert ctx["file_path"] == "src/auth.py"
    assert ctx["start_line"] == 18
    assert JobProcessor._gitlab_review_needs_position(event, ctx) is True
    overview = GitlabMrNoteEvent(
        issue_key="GL-ACME-DEMO-4",
        note_id="78",
        note_body="@yaver /yaver look at the MR",
        prompt="look at the MR",
        author_username="alice",
        author_name="Alice",
        project_id=1,
        project_path="acme/demo",
        repository_url="https://gitlab.example.com/acme/demo.git",
        host="gitlab.example.com",
        mr_iid=4,
        mr_title="Login",
        mr_description="",
        source_branch="feature/x",
        target_branch="develop",
        mr_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        raw={
            "object_attributes": {
                "id": 78,
                "type": "DiscussionNote",
                "note": "@yaver /yaver look at the MR",
                "noteable_type": "MergeRequest",
            }
        },
    )
    empty = extract_review_context(overview.raw, current_body=overview.prompt)
    assert JobProcessor._gitlab_review_needs_position(overview, empty) is False


def test_gitlab_selected_range_from_notes_api_lands_in_prompt(tmp_path):
    """Note Hook has line_code+st_diff only; GET /notes supplies line_range."""
    from src.gitlab.client import GitlabClient
    from src.gitlab.webhook import GitlabMrNoteEvent
    from src.processor import JobProcessor

    hits: list[str] = []
    note_body = {
        "id": 77,
        "type": "DiffNote",
        "body": "@yaver /yaver extract this helper",
        "position": {
            "new_path": "src/auth.py",
            "old_path": "src/auth.py",
            "new_line": 14,
            "line_range": {
                "start": {
                    "line_code": "abc_0_10",
                    "type": "new",
                    "new_line": 10,
                },
                "end": {
                    "line_code": "abc_0_14",
                    "type": "new",
                    "new_line": 14,
                },
            },
            "head_sha": "deadbeef",
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            hits.append(path)
            if path.endswith("/merge_requests/4/notes/77"):
                payload = json.dumps(note_body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                sock = socket.create_connection(httpd.server_address, timeout=0.2)
                sock.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("GitLab notes test server did not start")
        host = f"127.0.0.1:{httpd.server_address[1]}"
        src = tmp_path / "src"
        src.mkdir()
        (src / "auth.py").write_text(
            "\n".join(f"line-{i}" for i in range(1, 21)) + "\n",
            encoding="utf-8",
        )
        event = GitlabMrNoteEvent(
            issue_key="KAN-9",
            note_id="77",
            note_body="@yaver /yaver extract this helper",
            prompt="extract this helper",
            author_username="alice",
            author_name="Alice",
            project_id=1,
            project_path="acme/demo",
            repository_url=f"http://{host}/acme/demo.git",
            host=host,
            mr_iid=4,
            mr_title="Login",
            mr_description="",
            source_branch="feature/x",
            target_branch="develop",
            mr_url=f"http://{host}/acme/demo/-/merge_requests/4",
            raw={
                "object_kind": "note",
                "object_attributes": {
                    "id": 77,
                    "note": "@yaver /yaver extract this helper",
                    "noteable_type": "MergeRequest",
                    "line_code": "abc_0_14",
                    "st_diff": {
                        "new_path": "src/auth.py",
                        "old_path": "src/auth.py",
                    },
                },
            },
        )
        note = GitlabClient(host=host).get_mr_discussion_note(
            project=1, mr_iid=4, note_id="77"
        )
        assert note is not None
        assert note["position"]["line_range"]["start"]["new_line"] == 10
        proc = JobProcessor()
        ctx = proc._review_context_for_event(event, str(tmp_path))
        assert ctx["file_path"] == "src/auth.py"
        assert ctx["start_line"] == 10
        assert ctx["end_line"] == 14
        assert "line-10" in ctx["snippet"]
        assert "line-14" in ctx["snippet"]
        PromptBuilder.clear_prompt_file_cache()
        prompt = PromptBuilder.build_gitlab_comment_prompt(
            issue_key="KAN-9",
            mr_title="Login",
            mr_url=event.mr_url,
            source_branch="feature/x",
            target_branch="develop",
            author="alice",
            comment="extract this helper",
            raw=event.raw,
            review_context=ctx,
        )
        assert "## Review location" in prompt
        assert "`src/auth.py`" in prompt
        assert "10–14" in prompt
        assert "line-10" in prompt
        assert "extract this helper" in prompt
        assert any(path.endswith("/merge_requests/4/notes/77") for path in hits)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_gitlab_notes_without_range_falls_back_to_discussion_http():
    from src.gitlab.client import GitlabClient

    note_id = 77
    discussion = {
        "id": "disc-range",
        "notes": [
            {
                "id": note_id,
                "type": "DiffNote",
                "position": {
                    "new_path": "pkg/mod.py",
                    "line_range": {
                        "start": {"new_line": 3, "type": "new"},
                        "end": {"new_line": 7, "type": "new"},
                    },
                },
            }
        ],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path.endswith("/merge_requests/4/notes/77"):
                payload = json.dumps(
                    {
                        "id": note_id,
                        "type": "DiffNote",
                        "position": {
                            "new_path": "pkg/mod.py",
                            "new_line": 7,
                        },
                    }
                ).encode("utf-8")
            elif path.endswith("/merge_requests/4/discussions"):
                payload = json.dumps([discussion]).encode("utf-8")
            elif path.endswith("/merge_requests/4/discussions/disc-range"):
                payload = json.dumps(discussion).encode("utf-8")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                sock = socket.create_connection(httpd.server_address, timeout=0.2)
                sock.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("GitLab discussion fallback server did not start")
        host = f"127.0.0.1:{httpd.server_address[1]}"
        note = GitlabClient(host=host).get_mr_discussion_note(
            project=1, mr_iid=4, note_id="77"
        )
        assert note is not None
        rng = note["position"]["line_range"]
        assert rng["start"]["new_line"] == 3
        assert rng["end"]["new_line"] == 7
    finally:
        httpd.shutdown()
        httpd.server_close()
