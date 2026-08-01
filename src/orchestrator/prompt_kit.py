"""Parse and load the unified agent prompt kit (agent/AGENT_PROMPT.md)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from src.logger import logger

# Section headers: ## §role.direct  or  ## §policy.commit
_SECTION_RE = re.compile(r"^## §([a-z0-9_.]+)\s*$", re.MULTILINE)

KNOWN_SECTIONS = (
    "policy.commit",
    "role.planning",
    "role.execution",
    "role.direct",
    "role.oracle",
)

# Built-in fallbacks if kit file is missing (keep short — same spirit as kit).
_DEFAULT_SECTIONS: Dict[str, str] = {
    "policy.commit": (
        "Work on branch `feature/{ISSUE_KEY}` (create if needed).\n"
        "If you change files, commit yourself. Do not push or open an MR. "
        "Do not commit secrets.\n\n"
        "Subject: `[{ISSUE_KEY}] <type>: <short description>`\n"
        "Types: feat, fix, refactor, docs, test, perf, ci, build, revert, chore\n"
        'Example: `git commit -m "[{ISSUE_KEY}] fix: short description"`'
    ),
    "role.planning": (
        "You are Prometheus. Create a work plan for this Jira issue: "
        "clarify requirements, research the codebase, write a checkbox plan "
        "with files, approach, testing, and effort. Planning only — do not "
        "implement product code."
    ),
    "role.execution": (
        "You are Atlas. Execute the plan: read it, delegate with the task tool "
        "(categories visual-engineering / deep / quick; oracle / explore), "
        "verify, tick checkboxes, commit per git policy."
    ),
    "role.direct": (
        "You are Sisyphus. Analyze, implement with minimal focused changes, "
        "verify when practical, commit if you changed files, report summary "
        "and commit hash. Follow existing style; do not break tests."
    ),
    "role.oracle": (
        "You are Oracle. Answer with: direct answer, rationale, alternatives, "
        "trade-offs, implementation hints. Concise and practical."
    ),
}


def parse_prompt_kit(text: str) -> Dict[str, str]:
    """Split kit markdown into `{section_id: body}` by `## §id` headers."""
    sections: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text or ""))
    for i, match in enumerate(matches):
        key = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        # Drop horizontal rules that only separate sections
        if body.startswith("---"):
            body = body.lstrip("-").strip()
        if body.endswith("---"):
            body = body[: body.rfind("---")].rstrip().rstrip("-").strip()
        sections[key] = body
    return sections


def substitute_issue_key(text: str, issue_key: str) -> str:
    """Replace `{ISSUE_KEY}` placeholders (literal brace form only)."""
    if not text:
        return ""
    key = (issue_key or "ISSUE").strip() or "ISSUE"
    return text.replace("{ISSUE_KEY}", key)


def _resolve_kit_path(configured: Optional[Path] = None) -> Optional[Path]:
    """Return first existing kit path candidate."""
    cwd = Path.cwd()
    candidates = []
    if configured is not None:
        p = Path(configured)
        candidates.append(p if p.is_absolute() else cwd / p)
    candidates.append(cwd / "agent" / "AGENT_PROMPT.md")
    for path in candidates:
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=8)
def _load_kit_cached(path_str: str, mtime_ns: int) -> Dict[str, str]:
    """Cache parsed kit by path + mtime so edits reload without process restart."""
    del mtime_ns  # part of cache key only
    text = Path(path_str).read_text(encoding="utf-8")
    sections = parse_prompt_kit(text)
    logger.debug(
        f"Loaded prompt kit from {path_str}: {len(sections)} section(s) "
        f"({', '.join(sorted(sections)) or 'none'})"
    )
    return sections


def clear_prompt_kit_cache() -> None:
    """Drop parsed-kit cache (tests / after file replace)."""
    _load_kit_cached.cache_clear()


def load_prompt_sections(
    kit_path: Optional[Path] = None,
    *,
    refresh: bool = False,
) -> Dict[str, str]:
    """Load kit sections from disk, falling back to built-in defaults.

    Returns a complete map for all ``KNOWN_SECTIONS`` (file overrides defaults).
    """
    if refresh:
        clear_prompt_kit_cache()

    resolved = _resolve_kit_path(kit_path)
    file_sections: Dict[str, str] = {}
    if resolved is not None:
        try:
            stat = resolved.stat()
            file_sections = dict(
                _load_kit_cached(str(resolved.resolve()), stat.st_mtime_ns)
            )
        except OSError as e:
            logger.warning(f"Could not read prompt kit {resolved}: {e}")

    out = dict(_DEFAULT_SECTIONS)
    out.update(file_sections)
    return out


def get_section(
    section_id: str,
    *,
    kit_path: Optional[Path] = None,
    issue_key: Optional[str] = None,
    refresh: bool = False,
) -> str:
    """Return one section body; optionally substitute ``{ISSUE_KEY}``."""
    sections = load_prompt_sections(kit_path, refresh=refresh)
    body = sections.get(section_id) or _DEFAULT_SECTIONS.get(section_id) or ""
    if issue_key is not None:
        return substitute_issue_key(body, issue_key)
    return body
