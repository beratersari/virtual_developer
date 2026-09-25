"""Parse and strip the agent's structured findings JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

MAX_FINDINGS = 30
_SIDES = frozenset({"new", "old"})
_SEVERITIES = frozenset({"critical", "major", "minor", "improvement"})
_FINDINGS_TAG = "opencoderman-findings"
_FENCE = re.compile(
    r"^[ \t]*(`{3,}|~{3,})(opencoderman-findings|json)[ \t]*\r?\n(.*?)\r?\n[ \t]*\1[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_GROUP = re.compile(
    r"^###\s+(Critical|Major|Minor|Improvement|Kritik|Önemli|Onemli|Küçük|Kucuk|İyileştirme|Iyilestirme)\s*$",
    re.I,
)
_TITLE = re.compile(
    r"^####\s+\d+\.\s+`([^`:]+)(?::(\d+)(?:-(\d+))?)?[^`]*`\s+[—–-]\s+(.+?)\s*$"
)
_WHY = re.compile(r"^\*\*(Why it is an issue and where|Sorun)\*\*\s*$", re.I)
_FIX = re.compile(r"^\*\*(Suggested fix|Öneri)\*\*\s*$", re.I)
_CODE = re.compile(r"^\*\*(Code|Kod)\*\*\s*$", re.I)
_SEVERITY_ALIAS = {
    "kritik": "critical",
    "önemli": "major",
    "onemli": "major",
    "küçük": "minor",
    "kucuk": "minor",
    "iyileştirme": "improvement",
    "iyilestirme": "improvement",
}


@dataclass(frozen=True)
class Finding:
    path: str
    start_line: int
    end_line: int
    side: str
    severity: str
    title: str
    body: str


def split_findings(text: str) -> tuple[str, list[Finding]]:
    """Return (markdown without the machine block, parsed findings)."""
    source = (text or "").replace("\r\n", "\n")
    matches = list(_FENCE.finditer(source))
    if not matches:
        markdown = source.strip()
        return markdown, extract_markdown_findings(markdown)[:MAX_FINDINGS]

    chosen: Optional[re.Match[str]] = None
    findings: list[Finding] = []
    for match in reversed(matches):
        parsed = _parse_block(match.group(3), require_tag=(match.group(2) == "json"))
        if parsed is None:
            continue
        chosen = match
        findings = parsed
        break

    if chosen is None:
        last = next((m for m in reversed(matches) if m.group(2) == _FINDINGS_TAG), None)
        if last is None:
            markdown = source.strip()
            return markdown, extract_markdown_findings(markdown)[:MAX_FINDINGS]
        markdown = (source[: last.start()] + source[last.end() :]).strip()
        return markdown, extract_markdown_findings(markdown)[:MAX_FINDINGS]

    markdown = source
    for match in reversed(matches):
        tag = match.group(2)
        if tag == _FINDINGS_TAG or match is chosen:
            markdown = markdown[: match.start()] + markdown[match.end() :]
    markdown = markdown.strip()
    if not findings:
        findings = extract_markdown_findings(markdown)
    return markdown, findings[:MAX_FINDINGS]


def extract_markdown_findings(text: str) -> list[Finding]:
    """Recover findings from ``#### N. `path:lines` — title`` headings."""
    severity = "major"
    pending: Optional[dict[str, Any]] = None
    out: list[Finding] = []

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        finding = _coerce(pending)
        if finding is not None:
            out.append(finding)
        pending = None

    why_lines: list[str] = []
    fix_lines: list[str] = []
    section = ""
    for raw in (text or "").replace("\r\n", "\n").splitlines():
        stripped = raw.strip()
        group = _GROUP.match(stripped)
        if group:
            flush()
            severity = _SEVERITY_ALIAS.get(group.group(1).lower(), group.group(1).lower())
            section = ""
            continue
        title = _TITLE.match(stripped)
        if title:
            flush()
            start = int(title.group(2) or "0")
            end = int(title.group(3) or start or "0")
            pending = {
                "path": title.group(1),
                "start_line": start or None,
                "end_line": end or start or None,
                "side": "new",
                "severity": severity,
                "title": title.group(4).strip(),
                "body": "",
            }
            why_lines = []
            fix_lines = []
            section = ""
            continue
        if pending is None:
            continue
        if _WHY.match(stripped):
            section = "why"
            continue
        if _FIX.match(stripped):
            section = "fix"
            continue
        if _CODE.match(stripped) or stripped.startswith("```"):
            if section in {"why", "fix"}:
                continue
            section = "skip"
            continue
        if section == "skip":
            continue
        if section == "why" and stripped:
            why_lines.append(stripped)
        elif section == "fix" and stripped:
            fix_lines.append(stripped)
        if pending is not None:
            parts = why_lines[:4]
            if fix_lines:
                parts.append("Suggested fix: " + " ".join(fix_lines[:3]))
            pending["body"] = " ".join(parts)
    flush()
    return out


def _parse_block(raw: str, *, require_tag: bool) -> Optional[list[Finding]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("findings")
    if not isinstance(items, list):
        return None
    if require_tag and set(data.keys()) - {"findings", "version"}:
        return None
    out: list[Finding] = []
    for item in items:
        finding = _coerce(item)
        if finding is not None:
            out.append(finding)
    return out


def _coerce(item: Any) -> Optional[Finding]:
    if not isinstance(item, dict):
        return None
    from src.review.diffmap import strip_dot_slash

    path = strip_dot_slash(str(item.get("path") or "")).lstrip("/")
    if not path:
        return None
    start = _as_line(item.get("start_line"), item.get("line"))
    if start is None:
        return None
    end = _as_line(item.get("end_line"), start)
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    side = str(item.get("side") or "new").strip().lower()
    if side not in _SIDES:
        side = "new"
    severity = str(item.get("severity") or "major").strip().lower()
    if severity not in _SEVERITIES:
        severity = "major"
    title = str(item.get("title") or "").strip()
    body = str(item.get("body") or item.get("message") or "").strip()
    if not title and not body:
        return None
    return Finding(
        path=path,
        start_line=start,
        end_line=end,
        side=side,
        severity=severity,
        title=title,
        body=body,
    )


def _as_line(value: Any, default: Any = None) -> Optional[int]:
    if value is None:
        value = default
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line >= 1 else None
