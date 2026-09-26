"""Read and write OpenCode agent markdown in the user OpenCode home.

Edits go to ``~/.opencode/agents``. Packaged ``opencoderman/agents`` is only
a fallback when the home copy is missing, so a Settings save does not change
the git submodule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

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
    return Path.home() / ".opencode" / "agents"


def _packaged_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "opencoderman" / "agents"


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


def list_agents() -> List[str]:
    names = set()
    for root in (agents_dir(), _packaged_dir()):
        if not root.is_dir():
            continue
        for path in root.glob("*.md"):
            if path.is_file() and _NAME.match(path.stem):
                names.add(path.stem)
    return sorted(names)


def read_agent(name: str) -> str:
    safe = validate_name(name)
    home = _path_for(safe, agents_dir())
    if home.is_file():
        return home.read_text(encoding="utf-8")
    packaged = _path_for(safe, _packaged_dir())
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    raise AgentFileError(f"No agent file named {safe}")


def write_agent(name: str, text: str, *, create: bool = False) -> Path:
    safe = validate_name(name)
    body = text if text is not None else ""
    if not str(body).strip():
        raise AgentFileError("Agent text is empty")
    root = agents_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _path_for(safe, root)
    if create and path.is_file():
        raise AgentFileError(f"Agent {safe} already exists")
    path.write_text(str(body), encoding="utf-8")
    return path


def new_agent_template() -> str:
    return _NEW_AGENT
