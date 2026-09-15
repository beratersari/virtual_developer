"""Turn Jira-wiki / light-markdown comment bodies into TFS work-item HTML.

Jira Server/DC comments stay wiki. GitLab and Azure PR threads stay
markdown. Only ``add_work_item_comment`` uses this.
"""

from __future__ import annotations

import html
import re
from typing import List

_CODE_BLOCK = re.compile(
    r"\{\{code(?::([a-zA-Z0-9_+-]+))?\}\}(.*)\{\{code\}\}",
    re.DOTALL | re.IGNORECASE,
)
_NOFORMAT = re.compile(
    r"\{\{noformat\}\}(.*?)\{\{noformat\}\}",
    re.DOTALL | re.IGNORECASE,
)
_MD_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n?(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^(h[1-6])\.\s+(.*)$", re.MULTILINE)
_HR = re.compile(r"^----+\s*$", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE = re.compile(r"`([^`\n]+)`")
_JIRA_BOLD = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_JIRA_ITALIC = re.compile(r"(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])")
_HTML_LEAD = re.compile(r"^\s*<(?:p|div|h[1-6]|ul|ol|pre|hr|strong)\b", re.I)


def looks_like_html(text: str) -> bool:
    return bool(_HTML_LEAD.search(text or ""))


def work_item_comment_html(text: str) -> str:
    """HTML for Azure Boards. Idempotent if the body is already HTML."""
    from src.brand import USAGE_MARKER

    raw = text or ""
    if not raw.strip():
        return ""
    keep_marker = USAGE_MARKER in raw
    if keep_marker:
        raw = raw.replace(USAGE_MARKER, "")
    if looks_like_html(raw):
        return (USAGE_MARKER + raw) if keep_marker else raw
    slots: List[str] = []

    def stash(chunk: str) -> str:
        slots.append(chunk)
        return f"\x00AZ{len(slots) - 1}AZ\x00"

    def keep_code(_m: re.Match[str], inner: str) -> str:
        body = html.escape(inner.strip("\n"))
        return stash(f"<pre><code>{body}</code></pre>")

    work = _CODE_BLOCK.sub(lambda m: keep_code(m, m.group(2) or ""), raw)
    work = _NOFORMAT.sub(
        lambda m: stash(f"<code>{html.escape((m.group(1) or '').strip())}</code>"),
        work,
    )
    work = _MD_FENCE.sub(lambda m: keep_code(m, m.group(1) or ""), work)
    work = html.escape(work)
    work = _MD_BOLD.sub(r"<strong>\1</strong>", work)
    work = _MD_CODE.sub(r"<code>\1</code>", work)
    work = _HEADING.sub(
        lambda m: f"<{m.group(1)}>{m.group(2)}</{m.group(1)}>", work
    )
    work = _HR.sub("<hr/>", work)
    work = _JIRA_BOLD.sub(r"<strong>\1</strong>", work)
    work = _JIRA_ITALIC.sub(r"<em>\1</em>", work)
    work = _lists_and_breaks(work)
    for i, chunk in enumerate(slots):
        work = work.replace(f"\x00AZ{i}AZ\x00", chunk)
    if keep_marker:
        return USAGE_MARKER + work
    return work


def _lists_and_breaks(text: str) -> str:
    lines = text.split("\n")
    out: List[str] = []
    in_ul = False
    para: List[str] = []

    def flush_para() -> None:
        if not para:
            return
        chunk = "<br/>".join(para)
        if chunk.strip():
            out.append(f"<p>{chunk}</p>")
        para.clear()

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for line in lines:
        bullet = re.match(r"^[\*•]\s+(.*)$", line)
        if bullet:
            flush_para()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{bullet.group(1)}</li>")
            continue
        if re.match(r"^<(?:h[1-6]|hr|pre|ul|ol)\b", line):
            flush_para()
            close_ul()
            out.append(line)
            continue
        close_ul()
        if line.strip() == "":
            flush_para()
        else:
            para.append(line)
    flush_para()
    close_ul()
    return "".join(out)


__all__ = ["looks_like_html", "work_item_comment_html"]
