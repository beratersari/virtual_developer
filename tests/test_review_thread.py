"""Selected-code / review-thread context for GitLab and Azure comments."""

from __future__ import annotations

from src.orchestrator.prompt_builder import PromptBuilder
from src.review_thread import (
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
