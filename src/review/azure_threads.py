"""Map findings onto Azure PR threads."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from src.review.diffmap import DiffMap
from src.review.findings import Finding
from src.review.threads import ExistingThread, is_finding_body


def azure_thread_context(finding: Finding, diffmap: DiffMap) -> Optional[dict[str, Any]]:
    file_diff = diffmap.find(finding.path)
    if file_diff is None:
        return None
    if finding.side == "old":
        hit = file_diff.resolve_old(finding.start_line)
        end = file_diff.resolve_old(finding.end_line or finding.start_line)
        if hit is None or end is None:
            return None
        start_line = finding.start_line
        end_line = finding.end_line or finding.start_line
        if start_line > end_line:
            start_line, end_line = end_line, start_line
        path = _file_path(file_diff.old_path or finding.path)
        return {
            "filePath": path,
            "leftFileStart": {"line": start_line, "offset": 1},
            "leftFileEnd": {"line": end_line, "offset": 1},
        }
    hit = file_diff.resolve_new(finding.start_line)
    end = file_diff.resolve_new(finding.end_line or finding.start_line)
    if hit is None or end is None:
        return None
    start_line = finding.start_line
    end_line = finding.end_line or finding.start_line
    if start_line > end_line:
        start_line, end_line = end_line, start_line
    path = _file_path(file_diff.new_path or finding.path)
    return {
        "filePath": path,
        "rightFileStart": {"line": start_line, "offset": 1},
        "rightFileEnd": {"line": end_line, "offset": 1},
    }


def parse_azure_threads(raw: Iterable[Any]) -> list[ExistingThread]:
    out: list[ExistingThread] = []
    for item in raw or []:
        thread = parse_azure_thread(item)
        if thread is not None:
            out.append(thread)
    return out


def parse_azure_thread(raw: dict[str, Any]) -> Optional[ExistingThread]:
    if not isinstance(raw, dict):
        return None
    comments = raw.get("comments") or raw.get("notes") or []
    if not comments or not isinstance(comments[0], dict):
        return None
    first = comments[0]
    body = str(first.get("content") or first.get("body") or "")
    if not is_finding_body(body):
        return None
    ctx = raw.get("threadContext") if isinstance(raw.get("threadContext"), dict) else {}
    path = _norm_path(str(ctx.get("filePath") or ""))
    if not path:
        return None
    right = ctx.get("rightFileStart") if isinstance(ctx.get("rightFileStart"), dict) else {}
    left = ctx.get("leftFileStart") if isinstance(ctx.get("leftFileStart"), dict) else {}
    right_end = ctx.get("rightFileEnd") if isinstance(ctx.get("rightFileEnd"), dict) else {}
    left_end = ctx.get("leftFileEnd") if isinstance(ctx.get("leftFileEnd"), dict) else {}
    if right.get("line"):
        side = "new"
        start = _as_line(right.get("line"))
        end = _as_line(right_end.get("line")) or start
    elif left.get("line"):
        side = "old"
        start = _as_line(left.get("line"))
        end = _as_line(left_end.get("line")) or start
    else:
        return None
    if start <= 0:
        return None
    if end < start:
        start, end = end, start
    discussion_id = str(raw.get("id") or "").strip()
    if not discussion_id:
        return None
    status = str(raw.get("status") or "").strip().lower()
    resolved = status in {"fixed", "wontfix", "closed", "bydesign"}
    return ExistingThread(
        discussion_id=discussion_id,
        path=path,
        old_path=path,
        start_line=start,
        end_line=end,
        side=side,
        resolved=resolved,
        last_body=_last_finding_body(comments),
        root_comment_id=_as_line(first.get("id")),
    )


def _last_finding_body(comments: list) -> str:
    last = ""
    for note in comments:
        if not isinstance(note, dict):
            continue
        body = str(note.get("content") or note.get("body") or "")
        if is_finding_body(body):
            last = body
    return last


def _file_path(path: str) -> str:
    norm = (path or "").replace("\\", "/").lstrip("./")
    return "/" + norm if norm and not norm.startswith("/") else norm


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def _as_line(value: Any) -> int:
    try:
        return int(value) if value is not None and str(value).strip() != "" else 0
    except (TypeError, ValueError):
        return 0
