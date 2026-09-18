"""Turn a finding + local diff map into GitLab discussion positions."""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from src.review.diffmap import DiffMap, FileDiff
from src.review.findings import Finding

CREASY_FINDING_MARK = "<!-- creasy-finding -->"
AMIR_FINDING_MARK = "<!-- amir-mini-finding -->"
YAVER_FINDING_MARK = "<!-- yaver-finding -->"
_SEVERITY_LABEL = {
    "critical": "Kritik",
    "major": "Önemli",
    "minor": "Küçük",
    "improvement": "İyileştirme",
}


def line_code(path: str, old_line: Optional[int], new_line: Optional[int]) -> str:
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
    return f"{digest}_{old_line or 0}_{new_line or 0}"


def format_discussion(finding: Finding) -> str:
    severity = _SEVERITY_LABEL.get(
        (finding.severity or "").lower(), (finding.severity or "").capitalize()
    )
    title = finding.title.strip()
    head = f"**{severity}** · {title}" if title else f"**{severity}**"
    body = finding.body.strip()
    text = f"{head}\n\n{body}" if body else head
    return f"{YAVER_FINDING_MARK}\n{AMIR_FINDING_MARK}\n{CREASY_FINDING_MARK}\n{text}"


def build_position_variants(
    finding: Finding,
    diffmap: DiffMap,
    *,
    base_sha: str,
    start_sha: str,
    head_sha: str,
) -> list[dict[str, Any]]:
    file_diff = diffmap.find(finding.path)
    if file_diff is None or not base_sha or not head_sha:
        return []
    start = _resolve(file_diff, finding.side, finding.start_line)
    end = _resolve(file_diff, finding.side, finding.end_line)
    if start is None or end is None:
        return []
    old_path = file_diff.old_path or file_diff.new_path or finding.path
    new_path = file_diff.new_path or file_diff.old_path or finding.path
    code_path = new_path if finding.side == "new" else old_path
    start_sha = start_sha or base_sha

    primary = _position(
        old_path=old_path,
        new_path=new_path,
        code_path=code_path,
        start=start,
        end=end,
        base_sha=base_sha,
        start_sha=start_sha,
        head_sha=head_sha,
        with_range=finding.end_line != finding.start_line,
    )
    variants = [primary]
    if finding.end_line != finding.start_line:
        variants.append(
            _position(
                old_path=old_path,
                new_path=new_path,
                code_path=code_path,
                start=start,
                end=start,
                base_sha=base_sha,
                start_sha=start_sha,
                head_sha=head_sha,
                with_range=False,
            )
        )
    alt = dict(primary)
    if start[2] == "added":
        alt.pop("old_line", None)
        alt["new_line"] = start[1]
    elif start[2] == "deleted":
        alt.pop("new_line", None)
        alt["old_line"] = start[0]
    elif start[0] and start[1] and ("old_line" not in primary or "new_line" not in primary):
        alt["old_line"] = start[0]
        alt["new_line"] = start[1]
    if alt != primary and alt not in variants:
        variants.append(alt)
    return variants


def _resolve(
    file_diff: FileDiff, side: str, line: int
) -> Optional[tuple[Optional[int], Optional[int], str]]:
    if side == "old":
        return file_diff.resolve_old(line)
    return file_diff.resolve_new(line)


def _position(
    *,
    old_path: str,
    new_path: str,
    code_path: str,
    start: tuple[Optional[int], Optional[int], str],
    end: tuple[Optional[int], Optional[int], str],
    base_sha: str,
    start_sha: str,
    head_sha: str,
    with_range: bool,
) -> dict[str, Any]:
    old_line, new_line, kind = start
    payload: dict[str, Any] = {
        "position_type": "text",
        "base_sha": base_sha,
        "start_sha": start_sha,
        "head_sha": head_sha,
        "old_path": old_path,
        "new_path": new_path,
    }
    if kind == "deleted":
        payload["old_line"] = old_line
    elif kind == "added":
        payload["new_line"] = new_line
    else:
        if old_line:
            payload["old_line"] = old_line
        if new_line:
            payload["new_line"] = new_line
        if "old_line" not in payload and "new_line" not in payload:
            payload["new_line"] = start[1] or end[1]
    if with_range:
        start_type = "old" if kind == "deleted" else "new"
        end_type = "old" if end[2] == "deleted" else "new"
        payload["line_range"] = {
            "start": _range_end(code_path, start, start_type),
            "end": _range_end(code_path, end, end_type),
        }
    return payload


def _range_end(
    path: str,
    hit: tuple[Optional[int], Optional[int], str],
    side: str,
) -> dict[str, Any]:
    old_line, new_line, _kind = hit
    item: dict[str, Any] = {
        "line_code": line_code(path, old_line, new_line),
        "type": side,
    }
    if old_line:
        item["old_line"] = old_line
    if new_line:
        item["new_line"] = new_line
    return item
