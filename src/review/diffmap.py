"""Map HEAD / old line numbers onto a unified diff."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_PATHS = re.compile(r"^diff --git a/(.*) b/(.*)$")


@dataclass
class FileDiff:
    old_path: str = ""
    new_path: str = ""
    added: bool = False
    deleted: bool = False
    new_to_old: dict[int, Optional[int]] = field(default_factory=dict)
    old_to_new: dict[int, Optional[int]] = field(default_factory=dict)
    hunks: list[tuple[int, int, int, int]] = field(default_factory=list)

    def resolve_new(self, line: int) -> tuple[Optional[int], Optional[int], str]:
        if line in self.new_to_old:
            old = self.new_to_old[line]
            return old, line, ("added" if old is None else "context")
        inferred = self._infer_old(line)
        if self.added:
            return None, line, "added"
        if inferred is None:
            return None, line, "unknown"
        return inferred, line, "context"

    def resolve_old(self, line: int) -> tuple[Optional[int], Optional[int], str]:
        if line in self.old_to_new:
            new = self.old_to_new[line]
            return line, new, ("deleted" if new is None else "context")
        inferred = self._infer_new(line)
        if self.deleted:
            return line, None, "deleted"
        if inferred is None:
            return line, None, "unknown"
        return line, inferred, "context"

    def _infer_old(self, new_line: int) -> Optional[int]:
        prev_new_end = 1
        prev_old_end = 1
        for old_start, old_count, new_start, new_count in self.hunks:
            if new_line < new_start:
                return prev_old_end + (new_line - prev_new_end)
            if new_start <= new_line < new_start + max(new_count, 0):
                return None
            prev_new_end = new_start + new_count
            prev_old_end = old_start + old_count
        return prev_old_end + (new_line - prev_new_end)

    def _infer_new(self, old_line: int) -> Optional[int]:
        prev_new_end = 1
        prev_old_end = 1
        for old_start, old_count, new_start, new_count in self.hunks:
            if old_line < old_start:
                return prev_new_end + (old_line - prev_old_end)
            if old_start <= old_line < old_start + max(old_count, 0):
                return None
            prev_new_end = new_start + new_count
            prev_old_end = old_start + old_count
        return prev_new_end + (old_line - prev_old_end)


@dataclass
class DiffMap:
    files: list[FileDiff] = field(default_factory=list)

    def find(self, path: str) -> Optional[FileDiff]:
        key = _norm(path)
        if not key:
            return None
        for item in self.files:
            if _norm(item.new_path) == key or _norm(item.old_path) == key:
                return item
        return None


def parse_unified_diff(text: str) -> DiffMap:
    files: list[FileDiff] = []
    current = FileDiff()
    have_file = False
    in_hunk = False
    old_line = 0
    new_line = 0

    def flush() -> None:
        nonlocal have_file
        if have_file:
            files.append(current)
        have_file = False

    for raw in (text or "").replace("\r\n", "\n").splitlines():
        if raw.startswith("diff --git "):
            flush()
            current = FileDiff()
            have_file = True
            in_hunk = False
            match = _GIT_PATHS.match(raw)
            if match:
                current.old_path = match.group(1)
                current.new_path = match.group(2)
            continue
        if raw.startswith("rename from "):
            current.old_path = raw[12:].strip()
            continue
        if raw.startswith("rename to "):
            current.new_path = raw[10:].strip()
            continue
        if raw.startswith("new file mode"):
            current.added = True
            continue
        if raw.startswith("deleted file mode"):
            current.deleted = True
            continue
        if not in_hunk and raw.startswith("--- "):
            path = _strip_prefix(raw[4:])
            if path == "/dev/null":
                current.added = True
            else:
                current.old_path = path
            continue
        if not in_hunk and raw.startswith("+++ "):
            path = _strip_prefix(raw[4:])
            if path == "/dev/null":
                current.deleted = True
                if not current.new_path:
                    current.new_path = current.old_path
            else:
                current.new_path = path
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            in_hunk = True
            old_start = int(hunk.group(1))
            old_count = int(hunk.group(2) or "1")
            new_start = int(hunk.group(3))
            new_count = int(hunk.group(4) or "1")
            current.hunks.append((old_start, old_count, new_start, new_count))
            old_line = old_start
            new_line = new_start
            continue
        if not in_hunk:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            current.new_to_old[new_line] = None
            new_line += 1
        elif raw.startswith("-"):
            current.old_to_new[old_line] = None
            old_line += 1
        else:
            current.new_to_old[new_line] = old_line
            current.old_to_new[old_line] = new_line
            old_line += 1
            new_line += 1
    flush()
    return DiffMap(files=files)


def _strip_prefix(value: str) -> str:
    path = value.split("\t", 1)[0].strip()
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def strip_dot_slash(path: str) -> str:
    """Drop a leading ``./`` prefix without eating a dotfile name."""
    norm = (path or "").replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _norm(path: str) -> str:
    return strip_dot_slash(path).lstrip("/")
