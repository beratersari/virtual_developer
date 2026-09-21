"""Review-thread context for GitLab MR and Azure PR comments.

A webhook often has the operator note (``@yaver /yaver …``) but not the
selected code. GitLab DiffNotes and Azure file threads send *position*
(file + lines), not the snippet. We turn that into a prompt section and,
after clone, fill the snippet from the work tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

_MAX_SNIPPET_LINES = 80
_MAX_SNIPPET_CHARS = 8000
_MAX_THREAD_COMMENTS = 12
_MAX_COMMENT_CHARS = 2000


def extract_review_context(
    raw: Optional[Dict[str, Any]],
    *,
    current_body: str = "",
) -> Dict[str, Any]:
    """Best-effort file/line/thread context from a GitLab or Azure payload."""
    if not isinstance(raw, dict):
        return {}
    if _looks_azure(raw):
        ctx = _extract_azure(raw, current_body=current_body)
    else:
        ctx = _extract_gitlab(raw, current_body=current_body)
    return _trim_empty(ctx)


def apply_gitlab_note_position(
    ctx: Optional[Dict[str, Any]], note: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Fill file/lines from a GitLab Discussions/Notes API note.

    The Note Hook often has only ``line_code`` (one endpoint). The API
    ``position.line_range`` is the selected span — it wins when present.
    """
    out = dict(ctx or {})
    extra = extract_review_context(
        {"object_attributes": note or {}},
        current_body="",
    )
    for key in (
        "file_path",
        "old_path",
        "start_line",
        "end_line",
        "side",
        "commit_sha",
        "kind",
    ):
        if extra.get(key):
            out[key] = extra[key]
    return _trim_empty(out)


def attach_workdir_snippet(
    ctx: Optional[Dict[str, Any]], workdir: Optional[str]
) -> Dict[str, Any]:
    """Read the selected line range from the checkout into ``snippet``."""
    out = dict(ctx or {})
    if out.get("snippet"):
        return out
    root = Path(str(workdir or "").strip())
    rel = str(out.get("file_path") or "").strip().lstrip("/").replace("\\", "/")
    start = _as_int(out.get("start_line"))
    if not rel or not start or not root.is_dir():
        return out
    end = _as_int(out.get("end_line")) or start
    if end < start:
        start, end = end, start
    if end - start + 1 > _MAX_SNIPPET_LINES:
        end = start + _MAX_SNIPPET_LINES - 1
    path = root / rel
    if not path.is_file():
        out["snippet_error"] = f"file not in checkout: {rel}"
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        out["snippet_error"] = str(e)
        return out
    lo = max(1, start)
    hi = min(len(lines), end)
    if lo > len(lines):
        out["snippet_error"] = f"line {start} past end of {rel} ({len(lines)} lines)"
        return out
    chunk = "\n".join(lines[lo - 1 : hi])
    if len(chunk) > _MAX_SNIPPET_CHARS:
        chunk = chunk[:_MAX_SNIPPET_CHARS] + "\n…[truncated]…"
    out["snippet"] = chunk
    out["snippet_start"] = lo
    out["snippet_end"] = hi
    return out


def format_review_context(ctx: Optional[Dict[str, Any]]) -> str:
    """Markdown for the agent prompt. Empty when there is nothing to add."""
    if not isinstance(ctx, dict) or not ctx:
        return ""
    parts: List[str] = []
    loc = _format_location(ctx)
    if loc:
        parts.append("## Review location\n\n" + loc)
    thread = _format_thread(ctx)
    if thread:
        parts.append("## Review thread\n\n" + thread)
    return "\n\n".join(parts)


def _format_location(ctx: Dict[str, Any]) -> str:
    path = str(ctx.get("file_path") or "").strip()
    start = _as_int(ctx.get("start_line"))
    end = _as_int(ctx.get("end_line")) or start
    if not path and not start:
        return ""
    lines = [
        "This comment is on a **selected range** in the checkout. "
        "That range is often missing from the operator note — use it.",
        "",
    ]
    if path:
        lines.append(f"* File: `{path}`")
    if start:
        span = f"{start}–{end}" if end and end != start else str(start)
        side = str(ctx.get("side") or "").strip()
        extra = f" ({side} side)" if side else ""
        lines.append(f"* Lines: {span}{extra}")
    if ctx.get("old_path") and ctx.get("old_path") != path:
        lines.append(f"* Old path: `{ctx.get('old_path')}`")
    if ctx.get("commit_sha"):
        lines.append(f"* Commit: `{ctx.get('commit_sha')}`")
    if ctx.get("kind"):
        lines.append(f"* Kind: {ctx.get('kind')}")
    snippet = str(ctx.get("snippet") or "").rstrip()
    if snippet and path and start:
        fence = f"{start}:{end or start}:{path}"
        lines.extend(["", f"```{fence}", snippet, "```"])
    elif ctx.get("snippet_error"):
        lines.extend(
            [
                "",
                f"Could not read the range from the clone: {ctx.get('snippet_error')}. "
                "Open the file at the lines above.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "The webhook did not include the source text. Open the file at "
                "those lines in the work tree.",
            ]
        )
    return "\n".join(lines)


def _format_thread(ctx: Dict[str, Any]) -> str:
    comments = ctx.get("thread_comments") or []
    if not isinstance(comments, list) or not comments:
        return ""
    blocks = []
    for row in comments[:_MAX_THREAD_COMMENTS]:
        if not isinstance(row, dict):
            continue
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        if len(body) > _MAX_COMMENT_CHARS:
            body = body[:_MAX_COMMENT_CHARS] + "\n…[truncated]…"
        who = str(row.get("author") or "someone").strip() or "someone"
        blocks.append(f"**{who}:**\n\n{body}")
    if not blocks:
        return ""
    return (
        "Earlier comments on this review thread (context only; do what "
        "**Prompt** says):\n\n" + "\n\n---\n\n".join(blocks)
    )


def _looks_azure(raw: Dict[str, Any]) -> bool:
    if raw.get("eventType") or raw.get("event_type"):
        return True
    if isinstance(raw.get("resource"), dict):
        return True
    return False


def _extract_gitlab(raw: Dict[str, Any], *, current_body: str) -> Dict[str, Any]:
    attrs = _as_dict(raw.get("object_attributes"))
    pos = _coerce_position(
        attrs.get("position")
        or attrs.get("original_position")
        or attrs.get("change_position")
        or raw.get("position")
    )
    note_type = str(attrs.get("type") or attrs.get("noteable_type") or "").strip()
    st_diff = _as_dict(attrs.get("st_diff") or attrs.get("stDiff"))
    ctx: Dict[str, Any] = {"forge": "gitlab"}
    if str(note_type).lower() == "diffnote" or pos or st_diff or attrs.get("line_code"):
        ctx["kind"] = "diff"
    path = str(
        _first_str(
            pos.get("new_path"),
            pos.get("newPath"),
            pos.get("old_path"),
            pos.get("oldPath"),
            st_diff.get("new_path"),
            st_diff.get("old_path"),
        )
    )
    old_path = str(_first_str(pos.get("old_path"), pos.get("oldPath")))
    if path:
        ctx["file_path"] = path
    if old_path and old_path != path:
        ctx["old_path"] = old_path
    start, end, side = _gitlab_lines(pos, attrs)
    if start:
        ctx["start_line"] = start
        ctx["end_line"] = end or start
        ctx["side"] = side
    sha = str(
        _first_str(
            pos.get("head_sha"),
            pos.get("headSha"),
            attrs.get("commit_id"),
            attrs.get("commitId"),
        )
    )
    if sha:
        ctx["commit_sha"] = sha
    comments = _gitlab_thread_comments(raw, current_body=current_body)
    if comments:
        ctx["thread_comments"] = comments
    return ctx


def _coerce_position(raw: Any) -> Dict[str, Any]:
    """GitLab webhooks send position as a dict, JSON string, or camelCase."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        import json

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _first_str(*vals: Any) -> str:
    for val in vals:
        text = str(val or "").strip()
        if text:
            return text
    return ""


def _gitlab_lines(
    pos: Dict[str, Any], attrs: Optional[Dict[str, Any]] = None
) -> tuple[Optional[int], Optional[int], str]:
    rng = _as_dict(pos.get("line_range") or pos.get("lineRange"))
    start_row = _as_dict(rng.get("start"))
    end_row = _as_dict(rng.get("end"))
    start_side = str(start_row.get("type") or "").strip().lower()
    start = _gitlab_row_line(start_row, start_side)
    end = _gitlab_row_line(end_row, str(end_row.get("type") or start_side).strip().lower())
    if not start:
        start = (
            _as_int(pos.get("new_line") or pos.get("newLine"))
            or _as_int(pos.get("old_line") or pos.get("oldLine"))
            or _line_from_code(
                str(pos.get("line_code") or pos.get("lineCode") or ""),
                prefer="new",
            )
        )
        if attrs and not start:
            start = _line_from_code(str(attrs.get("line_code") or ""), prefer="new")
    if not end:
        end = start
    if (
        start_row.get("new_line")
        or start_row.get("newLine")
        or pos.get("new_line")
        or pos.get("newLine")
        or start_side == "new"
    ):
        side = "new"
    elif (
        start_row.get("old_line")
        or start_row.get("oldLine")
        or pos.get("old_line")
        or pos.get("oldLine")
        or start_side == "old"
    ):
        side = "old"
    else:
        side = ""
    return start, end, side


def _gitlab_row_line(row: Dict[str, Any], side: str) -> Optional[int]:
    prefer = "old" if side == "old" else "new"
    n = _as_int(row.get("new_line") or row.get("newLine"))
    o = _as_int(row.get("old_line") or row.get("oldLine"))
    if prefer == "old":
        return o or n or _line_from_code(
            str(row.get("line_code") or row.get("lineCode") or ""), prefer="old"
        )
    return n or o or _line_from_code(
        str(row.get("line_code") or row.get("lineCode") or ""), prefer="new"
    )


def _line_from_code(code: str, *, prefer: str) -> Optional[int]:
    """Parse GitLab ``<sha1>_<old>_<new>`` line_code into a 1-based line."""
    text = (code or "").strip()
    if "_" not in text:
        return None
    parts = text.rsplit("_", 2)
    if len(parts) != 3:
        return None
    try:
        old_n = int(parts[1])
        new_n = int(parts[2])
    except ValueError:
        return None
    if prefer == "old" and old_n > 0:
        return old_n
    if prefer == "new" and new_n > 0:
        return new_n
    return new_n or old_n or None


def _gitlab_thread_comments(
    raw: Dict[str, Any], *, current_body: str
) -> List[Dict[str, str]]:
    """Notes already on the payload (rare). Parent-only payloads handled here."""
    out: List[Dict[str, str]] = []
    for key in ("notes", "discussion_notes"):
        rows = raw.get(key)
        if isinstance(rows, list):
            for note in rows:
                item = _comment_item(note, body_keys=("body", "note", "content"))
                if item and not _same_text(item.get("body"), current_body):
                    out.append(item)
    parent = (
        _as_dict(raw.get("object_attributes")).get("in_reply_to")
        or raw.get("in_reply_to")
    )
    item = _comment_item(parent, body_keys=("body", "note", "content"))
    if item and not _same_text(item.get("body"), current_body):
        if all(c.get("body") != item.get("body") for c in out):
            out.insert(0, item)
    return out


def _extract_azure(raw: Dict[str, Any], *, current_body: str) -> Dict[str, Any]:
    resource = _as_dict(raw.get("resource"))
    comment = _as_dict(resource.get("comment") or raw.get("comment"))
    thread = _first_dict(
        resource.get("thread"),
        resource.get("pullRequestThread"),
        comment.get("thread"),
    )
    tctx = _first_dict(
        thread.get("threadContext"),
        resource.get("threadContext"),
        comment.get("threadContext"),
        _as_dict(resource.get("pullRequestThreadContext")),
    )
    ctx: Dict[str, Any] = {"forge": "azure"}
    path = str(tctx.get("filePath") or tctx.get("file_path") or "").strip()
    if path:
        ctx["file_path"] = path.lstrip("/")
        ctx["kind"] = "file"
    start, end, side = _azure_lines(tctx)
    if start:
        ctx["start_line"] = start
        ctx["end_line"] = end or start
        ctx["side"] = side
        ctx.setdefault("kind", "file")
    comments = _azure_thread_comments(thread, comment, current_body=current_body)
    if comments:
        ctx["thread_comments"] = comments
    return ctx


def _azure_lines(tctx: Dict[str, Any]) -> tuple[Optional[int], Optional[int], str]:
    right_s = _as_dict(tctx.get("rightFileStart") or tctx.get("right_file_start"))
    right_e = _as_dict(tctx.get("rightFileEnd") or tctx.get("right_file_end"))
    left_s = _as_dict(tctx.get("leftFileStart") or tctx.get("left_file_start"))
    left_e = _as_dict(tctx.get("leftFileEnd") or tctx.get("left_file_end"))
    if right_s or right_e:
        start = _as_int(right_s.get("line")) or _as_int(right_e.get("line"))
        end = _as_int(right_e.get("line")) or start
        return start, end, "right"
    if left_s or left_e:
        start = _as_int(left_s.get("line")) or _as_int(left_e.get("line"))
        end = _as_int(left_e.get("line")) or start
        return start, end, "left"
    return None, None, ""


def _azure_thread_comments(
    thread: Dict[str, Any],
    comment: Dict[str, Any],
    *,
    current_body: str,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    rows = thread.get("comments") if isinstance(thread.get("comments"), list) else []
    for row in rows:
        item = _comment_item(
            row, body_keys=("content", "comments", "body", "text")
        )
        if item and not _same_text(item.get("body"), current_body):
            out.append(item)
    parent = comment.get("parentComment") or comment.get("parent_comment")
    item = _comment_item(
        parent, body_keys=("content", "comments", "body", "text")
    )
    if item and not _same_text(item.get("body"), current_body):
        if all(c.get("body") != item.get("body") for c in out):
            out.insert(0, item)
    return out


def _comment_item(raw: Any, *, body_keys: tuple[str, ...]) -> Optional[Dict[str, str]]:
    if isinstance(raw, str) and raw.strip():
        return {"author": "someone", "body": raw.strip()}
    if not isinstance(raw, dict):
        return None
    body = ""
    for key in body_keys:
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            body = val.strip()
            break
    if not body:
        return None
    author = ""
    user = raw.get("author") or raw.get("user") or {}
    if isinstance(user, dict):
        author = str(
            user.get("displayName")
            or user.get("name")
            or user.get("username")
            or user.get("uniqueName")
            or ""
        ).strip()
    if not author:
        author = str(raw.get("author_name") or raw.get("authorName") or "").strip()
    return {"author": author or "someone", "body": body}


def _same_text(a: Any, b: Any) -> bool:
    return str(a or "").strip() == str(b or "").strip() and bool(str(a or "").strip())


def _first_dict(*vals: Any) -> Dict[str, Any]:
    for val in vals:
        d = _as_dict(val)
        if d:
            return d
    return {}


def _as_dict(val: Any) -> Dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _as_int(val: Any) -> Optional[int]:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _trim_empty(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in ctx.items() if v not in (None, "", [], {})}
