"""Match a finding to an existing unresolved inline review thread."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from src.review.findings import Finding
from src.review.position import (
    AMIR_FINDING_MARK,
    CREASY_FINDING_MARK,
    YAVER_FINDING_MARK,
)

_FINDING_HEAD = re.compile(
    r"^\*\*("
    r"Critical|Major|Minor|Improvement|"
    r"Kritik|Önemli|Onemli|Küçük|Kucuk|İyileştirme|Iyilestirme"
    r")\*\*",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class ExistingThread:
    discussion_id: str
    path: str
    old_path: str
    start_line: int
    end_line: int
    side: str
    resolved: bool
    last_body: str = ""
    root_comment_id: int = 0


def is_finding_body(body: str) -> bool:
    text = body or ""
    plain = html.unescape(text)
    marks = (YAVER_FINDING_MARK, AMIR_FINDING_MARK, CREASY_FINDING_MARK)
    if any(mark in text or mark in plain for mark in marks):
        return True
    return bool(
        _FINDING_HEAD.search(text.lstrip()) or _FINDING_HEAD.search(plain.lstrip())
    )


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def _as_line(value: Any) -> int:
    try:
        return int(value) if value is not None and str(value).strip() != "" else 0
    except (TypeError, ValueError):
        return 0


def _range_from_position(pos: dict[str, Any]) -> Optional[tuple[str, int, int]]:
    line_range = pos.get("line_range") if isinstance(pos.get("line_range"), dict) else {}
    start = line_range.get("start") if isinstance(line_range.get("start"), dict) else {}
    end = line_range.get("end") if isinstance(line_range.get("end"), dict) else {}
    start_new = _as_line(start.get("new_line")) or _as_line(pos.get("new_line"))
    end_new = _as_line(end.get("new_line")) or start_new
    start_old = _as_line(start.get("old_line")) or _as_line(pos.get("old_line"))
    end_old = _as_line(end.get("old_line")) or start_old
    if start_new:
        lo, hi = start_new, end_new or start_new
        if lo > hi:
            lo, hi = hi, lo
        return "new", lo, hi
    if start_old:
        lo, hi = start_old, end_old or start_old
        if lo > hi:
            lo, hi = hi, lo
        return "old", lo, hi
    return None


def parse_finding_thread(raw: dict[str, Any]) -> Optional[ExistingThread]:
    if not isinstance(raw, dict) or raw.get("individual_note"):
        return None
    notes = raw.get("notes") or []
    if not notes or not isinstance(notes[0], dict):
        return None
    first = notes[0]
    if first.get("system"):
        return None
    pos = first.get("position")
    if not isinstance(pos, dict):
        return None
    if not is_finding_body(str(first.get("body") or "")):
        return None
    span = _range_from_position(pos)
    if span is None:
        return None
    side, start, end = span
    discussion_id = str(raw.get("id") or "").strip()
    if not discussion_id:
        return None
    return ExistingThread(
        discussion_id=discussion_id,
        path=_norm_path(str(pos.get("new_path") or "")),
        old_path=_norm_path(str(pos.get("old_path") or "")),
        start_line=start,
        end_line=end,
        side=side,
        resolved=bool(first.get("resolved") or raw.get("resolved")),
        last_body=_last_finding_body(notes),
        root_comment_id=_as_line(first.get("id")),
    )


def _last_finding_body(notes: list) -> str:
    last = ""
    for note in notes:
        if not isinstance(note, dict) or note.get("system"):
            continue
        body = str(note.get("body") or "")
        if is_finding_body(body):
            last = body
    return last


def parse_finding_threads(raw: Iterable[Any]) -> list[ExistingThread]:
    out: list[ExistingThread] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        thread = parse_finding_thread(item)
        if thread is not None:
            out.append(thread)
    return out


def _paths_match(finding_path: str, thread: ExistingThread) -> bool:
    want = _norm_path(finding_path)
    if not want:
        return False
    return want in {thread.path, thread.old_path}


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def match_finding_thread(
    finding: Finding,
    threads: Iterable[ExistingThread],
    used: set[str],
) -> Optional[ExistingThread]:
    want_side = finding.side if finding.side in {"new", "old"} else "new"
    start = finding.start_line
    end = finding.end_line or finding.start_line
    if start > end:
        start, end = end, start
    hits: list[ExistingThread] = []
    for thread in threads:
        if thread.discussion_id in used or thread.resolved:
            continue
        if thread.side != want_side:
            continue
        if not _paths_match(finding.path, thread):
            continue
        if _overlaps(start, end, thread.start_line, thread.end_line):
            hits.append(thread)
    if not hits:
        return None
    return min(hits, key=lambda t: (t.end_line - t.start_line, t.start_line, t.discussion_id))
