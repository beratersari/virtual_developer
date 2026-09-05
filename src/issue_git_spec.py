"""Parse repository URL and branches from a Jira issue ``{params}`` block.

Put git settings **between** matching ``{params}`` markers (anywhere in the
description). Example::

    {params}
    Repository: https://gitlab.example.com/group/repo.git
    Source branch: feature/PROJ-123
    Target branch: develop
    {params}

GitLab MR semantics (source → target):

* **Target branch** — must already exist on the remote. The merge request
  merges **into** this branch.
* **Source branch** — the work branch (MR *source*). May differ from the Jira
  issue key. If it **exists on the remote**, it is checked out and used. If
  it does **not** exist, it is created **from target**. When source equals
  target, or source is a primary base (``main`` / ``master`` / ``develop`` /
  …), the agent uses ``feature/{ISSUE_KEY}`` as the work branch instead.
* Commit subjects always use the **Jira issue key**, not the source branch
  name.

Only text inside the first ``{params}`` … ``{params}`` pair is scanned
(so free-form acceptance criteria cannot confuse the parser).

Aliases inside the block:
* Repository / Repo / GitLab / Project URL
* Source branch / Work branch
* Target branch / MR target / Merge into / Base branch
* Model / LLM (optional OpenCode model id; empty = settings default)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

# First {params} ... {params} block (case-insensitive tag; body may span lines)
_PARAMS_BLOCK = re.compile(
    r"(?is)\{params\}\s*(.*?)\s*\{params\}"
)

# Jira wiki: [label|url] or [url|url|smart-card]
_JIRA_LINK = re.compile(
    r"\[([^\]|\n]+)\|([^\]|\n]+)(?:\|[^\]\n]+)?\]"
)
# Cloud auto-link of an issue key: [KAN-7] or [KAN-7|https://…/browse/KAN-7]
_JIRA_ISSUE_KEY = re.compile(
    r"\[([A-Za-z][A-Za-z0-9]+-\d+)(?:\|[^\]\n]+)?\]"
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")

_REPO_KEY = r"(?:repository|repo|gitlab(?:\s*url)?|project(?:\s*url)?)"
# Work / MR source branch (not the integration base)
_SOURCE_KEY = r"(?:source\s*branch|work\s*branch)"
# MR destination / base that must exist — includes "base branch" alias
_TARGET_KEY = r"(?:target\s*branch|mr\s*target|merge\s*into|merge\s*target|base\s*branch)"
# Mode / workflow decision inside {params}
_MODE_KEY = r"(?:mode|workflow(?:\s*mode)?)"
_MODEL_KEY = r"(?:model|llm|opencode\s*model|default\s*model)"
_BACKEND_KEY = r"(?:backend|agent\s*backend|worker)"
_ANY_KEY = rf"(?:{_REPO_KEY}|{_SOURCE_KEY}|{_TARGET_KEY}|{_MODE_KEY}|{_MODEL_KEY}|{_BACKEND_KEY})"

_REPO_FIELD = re.compile(
    rf"(?is)(?:^|[\n\r])\s*{_REPO_KEY}\s*:\s*(.*?)(?=\s*{_ANY_KEY}\s*:|\Z)"
)
_SOURCE_FIELD = re.compile(
    rf"(?is)(?:^|[\n\r]|[\s])\s*{_SOURCE_KEY}\s*:\s*(\S+)"
)
_TARGET_FIELD = re.compile(
    rf"(?is)(?:^|[\n\r]|[\s])\s*{_TARGET_KEY}\s*:\s*(\S+)"
)
_MODE_FIELD = re.compile(
    rf"(?is)(?:^|[\n\r]|[\s])\s*{_MODE_KEY}\s*:\s*(\S+)"
)
_MODEL_FIELD = re.compile(
    rf"(?is)(?:^|[\n\r]|[\s])\s*{_MODEL_KEY}\s*:\s*(\S+)"
)
_BACKEND_FIELD = re.compile(
    rf"(?is)(?:^|[\n\r]|[\s])\s*{_BACKEND_KEY}\s*:\s*(\S+)"
)

_URL_TOKEN = re.compile(
    r"(?i)\b((?:https?://|git@)[^\s\[\]<>\"']+)"
)

TEMPLATE_HELP = """\
{params}
Repository: https://gitlab.example.com/group/your-repo.git
Source branch: feature/PROJ-123
Target branch: develop
Mode: plan
Model: opencode/hy3-free
Backend: opencode
{params}

Mode is optional (default ``build``):
* plan  — generate a plan, append it to the Jira description (no GitLab push)
* build — implement / execute (push branch + open merge request)
Model and Backend are optional (default from .env / dashboard Settings).
"""

# Valid mode tokens (aliases → canonical)
_MODE_ALIASES = {
    "plan": "plan",
    "planning": "plan",
    "prometheus": "plan",
    "build": "build",
    "execute": "build",
    "execution": "build",
    "atlas": "build",
    "implement": "build",
}


@dataclass(frozen=True)
class IssueGitSpec:
    """Git target resolved from a Jira issue."""

    repository_url: str
    source_branch: str
    target_branch: str
    mode: Optional[str] = None  # "plan" | "build" when present
    model: Optional[str] = None  # model id; empty = settings default
    backend: Optional[str] = None  # opencode | codex; empty = settings default


class IssueGitConfigError(Exception):
    """Issue text does not satisfy the repository/branch template."""

    def __init__(self, user_message: str):
        self.user_message = user_message
        super().__init__(user_message)


def _strip_wiki_field_bold(text: str) -> str:
    """Turn Jira ``*Model:* foo`` / ``*Backend:* bar`` into ``Model: foo``."""
    return re.sub(r"(?im)^([ \t]*)\*([^*\n]+)\*\s*", r"\1\2 ", text or "")


def _expand_links(text: str) -> str:
    """Turn Jira/markdown links into bare URLs / keys for field parsing.

    Issue-key wiki (``[KAN-7]`` / ``[KAN-7|browse-url]``) must become the
    key, not the browse URL — otherwise ``Source branch: feature/[KAN-7]``
    is rejected as an invalid git ref.
    """

    def jira_repl(m: re.Match) -> str:
        left, right = m.group(1).strip(), m.group(2).strip()
        if _looks_like_git_url(right) or right.startswith("http"):
            return right
        if _looks_like_git_url(left) or left.startswith("http"):
            return left
        return right

    text = _JIRA_ISSUE_KEY.sub(r"\1", text)
    text = _JIRA_LINK.sub(jira_repl, text)
    text = _MD_LINK.sub(lambda m: m.group(2), text)
    return text


def _normalize_repo_url(raw: str) -> str:
    url = (raw or "").strip().strip("<>").strip("`").strip()
    url = url.rstrip(").,;\"'")
    if url and not _looks_like_git_url(url):
        m = _URL_TOKEN.search(url)
        if m:
            url = m.group(1).rstrip(").,;\"'")
    return url


def _normalize_backend_id(raw: str) -> str:
    """opencode | codex. Empty if unset."""
    from src.backends.base import normalize_backend_name

    return normalize_backend_name(raw)


def _normalize_model_id(raw: str) -> str:
    """OpenCode model id (provider/name). Empty if unset or junk."""
    mid = (raw or "").strip().strip("`").rstrip(".,;")
    if not mid or len(mid) > 200:
        return ""
    if any(ch.isspace() for ch in mid):
        return ""
    return mid


def _upsert_params_field(
    description: str,
    *,
    key_re: str,
    label: str,
    value: str,
    present: Optional[re.Pattern[str]],
) -> str:
    """Set or replace one ``Key:`` line inside the first ``{params}`` block."""
    text = description or ""
    if not value:
        return text
    m = _PARAMS_BLOCK.search(text)
    if not m:
        return text
    inner = _strip_wiki_field_bold(m.group(1))
    inner2, n = re.subn(
        rf"(?im)^([ \t]*\*?[ \t]*(?:{key_re})[ \t]*\*?\s*:\s*)\S+",
        rf"\g<1>{value}",
        inner,
        count=1,
    )
    if n == 0 and present is not None and present.search(inner):
        inner2, n = re.subn(
            rf"(?is)((?:{key_re})\s*:\s*)\S+",
            rf"\g<1>{value}",
            inner,
            count=1,
        )
    if n == 0:
        inner2 = inner.rstrip() + f"\n{label}: {value}\n"
    return text[: m.start(1)] + inner2 + text[m.end(1) :]


def upsert_params_model(description: str, model: str) -> str:
    """Set or replace ``Model:`` inside the first ``{params}`` block."""
    return _upsert_params_field(
        description,
        key_re=_MODEL_KEY,
        label="Model",
        value=_normalize_model_id(model),
        present=_MODEL_FIELD,
    )


def upsert_params_backend(description: str, backend: str) -> str:
    """Set or replace ``Backend:`` inside the first ``{params}`` block."""
    return _upsert_params_field(
        description,
        key_re=_BACKEND_KEY,
        label="Backend",
        value=_normalize_backend_id(backend),
        present=_BACKEND_FIELD,
    )


def _normalize_branch(raw: str) -> str:
    branch = (raw or "").strip().strip("`").strip()
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    # Jira visual editor wraps issue keys: feature/[KAN-7] or feature/[KAN-7|url]
    branch = _JIRA_ISSUE_KEY.sub(r"\1", branch)
    return branch


def _looks_like_git_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    if lower.startswith("git@"):
        return ":" in url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "ssh"):
        return False
    if not parsed.netloc:
        return False
    path = (parsed.path or "").strip("/")
    return bool(path)


def _looks_like_branch(name: str) -> bool:
    if not name or len(name) > 255:
        return False
    if any(c in name for c in (" ", "\t", "\\")):
        return False
    if ".." in name:
        return False
    if name.startswith("/") or name.endswith("/"):
        return False
    # Leading '-' is a git option (e.g. --mirror), not a ref
    if name.startswith("-"):
        return False
    return bool(re.match(r"^[A-Za-z0-9._/\-]+$", name))


def _extract_params_block(text: str) -> Optional[str]:
    """Return body between first ``{params}`` … ``{params}``, or None."""
    if not text:
        return None
    m = _PARAMS_BLOCK.search(text)
    if not m:
        return None
    return (m.group(1) or "").strip()


def strip_params_block(text: str) -> str:
    """Remove all ``{params}`` … ``{params}`` blocks from issue text.

    Used when building agent prompts so git template metadata is not sent to
    the model. Parsing for clone/push still uses the raw description.
    """
    if not text:
        return ""
    cleaned = _PARAMS_BLOCK.sub("", text)
    # Collapse leftover blank runs from block removal
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _params_block_text(summary: str = "", description: str = "") -> Optional[str]:
    """Return expanded body of the first ``{params}`` block, or None."""
    raw = f"{summary or ''}\n{description or ''}"
    expanded = _expand_links(raw)
    block = _extract_params_block(expanded)
    if block is None:
        block = _extract_params_block(raw)
        if block is not None:
            block = _expand_links(block)
    else:
        block = _expand_links(block)
    return block


def peek_issue_git_fields(summary: str = "", description: str = "") -> Dict[str, str]:
    """Best-effort ``{params}`` field peek for dashboard prefills.

    Unlike ``parse_issue_git_spec`` this never fails: missing or invalid
    fields are empty strings so the operator can complete them in the picker.
    """
    empty = {
        "repository_url": "",
        "source_branch": "",
        "target_branch": "",
        "mode": "",
        "model": "",
        "backend": "",
    }
    spec, _err = parse_issue_git_spec(summary, description)
    if spec is not None:
        return {
            "repository_url": spec.repository_url or "",
            "source_branch": spec.source_branch or "",
            "target_branch": spec.target_branch or "",
            "mode": spec.mode or "",
            "model": spec.model or "",
            "backend": spec.backend or "",
        }
    block = _params_block_text(summary, description)
    if not block:
        return empty
    text = _strip_wiki_field_bold(block)
    repo = _extract_repo(text)
    source_m = _SOURCE_FIELD.search(text)
    target_m = _TARGET_FIELD.search(text)
    mode_m = _MODE_FIELD.search(text)
    model_m = _MODEL_FIELD.search(text)
    backend_m = _BACKEND_FIELD.search(text)
    source = _normalize_branch(source_m.group(1)) if source_m else ""
    target = _normalize_branch(target_m.group(1)) if target_m else ""
    mode_raw = (
        (mode_m.group(1) or "").strip().lower().strip("`").rstrip(".,;:") if mode_m else ""
    )
    mode = _MODE_ALIASES.get(mode_raw, "")
    return {
        "repository_url": repo if _looks_like_git_url(repo) else "",
        "source_branch": source if _looks_like_branch(source) else "",
        "target_branch": target if _looks_like_branch(target) else "",
        "mode": mode,
        "model": _normalize_model_id(model_m.group(1)) if model_m else "",
        "backend": _normalize_backend_id(backend_m.group(1)) if backend_m else "",
    }


def parse_issue_mode(summary: str = "", description: str = "") -> Optional[str]:
    """Return canonical mode (``plan`` / ``build``) from ``{params}``, or None.

    Looks for ``Mode:`` / ``Workflow:`` inside the params block only.
    When a ``{params}`` block exists but Mode is omitted, defaults to ``build``.
    No params block → ``None`` (router keeps its no-template heuristics).
    """
    block = _params_block_text(summary, description)
    if not block:
        return None
    m = _MODE_FIELD.search(block)
    if not m:
        return "build"
    token = (m.group(1) or "").strip().lower().strip("`").strip()
    # Drop trailing punctuation
    token = token.rstrip(".,;:")
    return _MODE_ALIASES.get(token)


def _extract_repo(text: str) -> str:
    """Repository URL from field value and/or following lines inside params."""
    m = _REPO_FIELD.search(text)
    if not m:
        # Fallback: first URL-looking token inside the block
        um = _URL_TOKEN.search(text)
        if um:
            cand = _normalize_repo_url(um.group(1))
            if _looks_like_git_url(cand):
                return cand
        return ""
    blob = (m.group(1) or "").strip()
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        url = _normalize_repo_url(line)
        if _looks_like_git_url(url):
            return url
        um = _URL_TOKEN.search(line)
        if um:
            cand = _normalize_repo_url(um.group(1))
            if _looks_like_git_url(cand):
                return cand
    um = _URL_TOKEN.search(blob)
    if um:
        cand = _normalize_repo_url(um.group(1))
        if _looks_like_git_url(cand):
            return cand
    return _normalize_repo_url(blob)


def parse_issue_git_spec(
    summary: str = "",
    description: str = "",
) -> Tuple[Optional[IssueGitSpec], Optional[str]]:
    """Extract repository, source branch, and target branch from ``{params}``.

    Target branch is optional; when omitted it defaults to the source branch
    (agent will still use ``feature/{KEY}`` as the work branch when they match).
    Mode is optional (default ``build``). Model and Backend are optional
    (empty = dashboard / .env defaults).

    Returns ``(spec, None)`` on success, or ``(None, user_error_message)`` on failure.
    """
    raw = f"{summary or ''}\n{description or ''}"
    # Expand wiki/markdown links in the whole issue first (params may wrap them)
    expanded = _expand_links(raw)
    block = _extract_params_block(expanded)
    # Also try unexpanded raw (escaped braces / wiki quirks)
    if block is None:
        block = _extract_params_block(raw)
        if block is not None:
            block = _expand_links(block)
    else:
        block = _expand_links(block)

    if block is None:
        return None, (
            "*Yaver* could not start: no ``{params}`` block found on the issue.\n\n"
            "Wrap the git settings between ``{params}`` markers in the *description* "
            "(or summary), then move the issue back to *To Do*:\n\n"
            "{code}\n"
            f"{TEMPLATE_HELP.strip()}\n"
            "{code}"
        )

    text = _strip_wiki_field_bold(block)
    repo = _extract_repo(text)
    source_m = _SOURCE_FIELD.search(text)
    target_m = _TARGET_FIELD.search(text)
    mode_m = _MODE_FIELD.search(text)

    source = _normalize_branch(source_m.group(1)) if source_m else ""
    target = _normalize_branch(target_m.group(1)) if target_m else ""
    mode_raw = (mode_m.group(1) or "").strip().lower().strip("`").rstrip(".,;:") if mode_m else ""
    # Mode is optional — default build (Model / Backend already optional)
    mode = _MODE_ALIASES.get(mode_raw) if mode_raw else "build"
    model_m = _MODEL_FIELD.search(text)
    model = _normalize_model_id(model_m.group(1)) if model_m else ""
    backend_m = _BACKEND_FIELD.search(text)
    backend = _normalize_backend_id(backend_m.group(1)) if backend_m else ""

    missing = []
    if not repo:
        missing.append(
            "Repository (e.g. `Repository: https://gitlab.example.com/group/repo.git`)"
        )
    if not source:
        missing.append(
            "Source branch (e.g. `Source branch: feature/PROJ-123` "
            "or `Source branch: develop` to auto-use feature/{KEY})"
        )
    if mode_raw and mode_raw not in _MODE_ALIASES:
        missing.append(
            f"Mode (got `{mode_raw}`; must be `plan` or `build`, or omit for build)"
        )

    if missing:
        return None, (
            "*Yaver* could not start: the issue description format is incomplete.\n\n"
            f"*Missing / invalid:* {', '.join(missing)}.\n\n"
            "Add a ``{params}`` block to the *description* with *all* of these fields, "
            "then move the issue back to *To Do*:\n\n"
            "{code}\n"
            f"{TEMPLATE_HELP.strip()}\n"
            "{code}"
        )

    if not target:
        target = source

    if not _looks_like_git_url(repo):
        return None, (
            "*Yaver* could not start: the repository URL looks invalid.\n\n"
            f"Parsed value: `{repo}`\n\n"
            "Use a full HTTPS (or SSH) GitLab URL inside ``{params}``, for example:\n"
            "`Repository: https://gitlab.example.com/group/repo.git`"
        )

    if not _looks_like_branch(source):
        return None, (
            "*Yaver* could not start: the source branch name looks invalid.\n\n"
            f"Parsed value: `{source}`\n\n"
            "Example: `Source branch: feature/PROJ-123`"
        )

    if not _looks_like_branch(target):
        return None, (
            "*Yaver* could not start: the target branch name looks invalid.\n\n"
            f"Parsed value: `{target}`\n\n"
            "Example: `Target branch: develop` (must already exist on GitLab)"
        )

    return (
        IssueGitSpec(
            repository_url=repo,
            source_branch=source,
            target_branch=target,
            mode=mode,
            model=model or None,
            backend=backend or None,
        ),
        None,
    )


def require_issue_git_spec(summary: str = "", description: str = "") -> IssueGitSpec:
    """Like ``parse_issue_git_spec`` but raises ``IssueGitConfigError``."""
    spec, err = parse_issue_git_spec(summary, description)
    if err or spec is None:
        raise IssueGitConfigError(err or "Invalid git template on issue.")
    return spec
