"""Edit the agents shipped in ``opencoderman/agents``.

That folder is the copy in the release zip. Settings reads and writes only
those files. OpenCode and Claude do not load that folder. Sync copies each
agent into ``~/.opencode/agents`` and ``~/.claude/agents``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_NEW_AGENT = """---
description: Custom Yaver agent. Unattended — do not ask questions.
mode: primary
temperature: 0.2
permission:
  question: deny
---

You run unattended. Do not ask clarifying questions. Follow the job message.
"""


class AgentFileError(ValueError):
    pass


def agents_dir() -> Path:
    """Agents the operator edits. This is ``opencoderman/agents`` in the zip."""
    return Path(__file__).resolve().parent.parent / "opencoderman" / "agents"


def opencode_agents_dir() -> Path:
    return Path.home() / ".opencode" / "agents"


def claude_agents_dir() -> Path:
    profile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    root = Path(profile) if profile else Path.home()
    return root / ".claude" / "agents"


def validate_name(name: str) -> str:
    text = (name or "").strip()
    if not _NAME.match(text):
        raise AgentFileError(
            "Agent name must start with a letter or digit and use only "
            "letters, digits, hyphens, and underscores"
        )
    return text


def _path_for(name: str, root: Path) -> Path:
    path = (root / f"{name}.md").resolve()
    if path.parent != root.resolve():
        raise AgentFileError("Agent name is not a single file")
    return path


def _agent_files(root: Path) -> List[Path]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    files = []
    for entry in entries:
        if entry.suffix.lower() != ".md":
            continue
        if _NAME.match(entry.name[:-3]):
            files.append(entry)
    return files


def list_agents() -> List[str]:
    return sorted(path.name[:-3] for path in _agent_files(agents_dir()))


def read_agent(name: str) -> str:
    safe = validate_name(name)
    path = _path_for(safe, agents_dir())
    if path.is_file():
        return path.read_text(encoding="utf-8")
    raise AgentFileError(f"No agent file named {safe}")


def _existing_agent_stem(name: str) -> str:
    """Name already stored in ``opencoderman/agents``.

    One directory listing. Case-insensitive so ``Derman-Build`` is the same
    file as ``derman-build`` on Windows.
    """
    want = name.casefold()
    for entry in _agent_files(agents_dir()):
        stem = entry.name[:-3]
        if stem.casefold() == want:
            return stem
    return ""


def write_agent(name: str, text: str, *, create: bool = False) -> Path:
    safe = validate_name(name)
    body = text if text is not None else ""
    if not str(body).strip():
        raise AgentFileError("Agent text is empty")
    if create:
        existing = _existing_agent_stem(safe)
        if existing:
            raise AgentFileError(f"Agent {existing} already exists")
    root = agents_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _path_for(safe, root)
    if create:
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(str(body))
        except FileExistsError as exc:
            raise AgentFileError(f"Agent {safe} already exists") from exc
        return path
    path.write_text(str(body), encoding="utf-8")
    return path


def new_agent_template() -> str:
    return _NEW_AGENT


def _claude_agent_text(text: str, name: str) -> str:
    path = Path(__file__).resolve().parent.parent / "packaging" / "install_claude_agents.py"
    spec = importlib.util.spec_from_file_location("yaver_install_claude_agents", path)
    if spec is None or spec.loader is None:
        raise AgentFileError("Claude agent converter is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.to_claude_agent(text, name)


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _same_text(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False

    def norm(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    return norm(left) == norm(right)


def sync_status() -> Dict[str, Any]:
    """Catalog agents whose OpenCode or Claude copy does not match.

    Files that exist only in a home directory are left alone. Sync copies
    the catalog; it does not delete other agents the operator already had.
    """
    catalog = _agent_files(agents_dir())
    opencode = opencode_agents_dir()
    claude = claude_agents_dir()
    pending: List[str] = []
    for src in catalog:
        stem = src.name[:-3]
        text = _read_text(src)
        if text is None:
            pending.append(stem)
            continue
        if not _same_text(_read_text(opencode / src.name), text):
            pending.append(stem)
            continue
        if not _same_text(_read_text(claude / src.name), _claude_agent_text(text, stem)):
            pending.append(stem)
    pending.sort(key=str.casefold)
    return {"synced": not pending, "pending": pending}


def sync_agents() -> Dict[str, Any]:
    """Copy ``opencoderman/agents`` into the OpenCode and Claude homes."""
    files = _agent_files(agents_dir())
    if not files:
        raise AgentFileError("No agents to sync")
    opencode = opencode_agents_dir()
    claude = claude_agents_dir()
    opencode.mkdir(parents=True, exist_ok=True)
    claude.mkdir(parents=True, exist_ok=True)
    names: List[str] = []
    for src in files:
        stem = src.name[:-3]
        text = src.read_text(encoding="utf-8")
        shutil.copyfile(src, opencode / src.name)
        (claude / src.name).write_text(_claude_agent_text(text, stem), encoding="utf-8")
        names.append(stem)
    names.sort()
    return {"agents": names, "opencode": str(opencode), "claude": str(claude)}
