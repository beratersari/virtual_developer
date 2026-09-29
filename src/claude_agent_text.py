"""Claude agent text derived from an OpenCode agent file.

OpenCode frontmatter (mode, temperature, permission) is replaced with
Claude's name / description / tools header. The instruction body is kept.
"""

from __future__ import annotations


def _split_frontmatter(text: str) -> str:
    raw = text.lstrip("\ufeff")
    if not raw.startswith("---"):
        return raw.strip() + "\n"
    end = raw.find("\n---", 3)
    if end < 0:
        return raw.strip() + "\n"
    body = raw[end + 4 :]
    return body.strip() + "\n"


def to_claude_agent(text: str, name: str) -> str:
    """Return a Claude agent file for ``name`` using the OpenCode body."""
    description = (
        f"Unattended Yaver agent {name}. Never asks questions. "
        "Does not git push."
    )
    body = _split_frontmatter(text)
    if name == "derman-reviewer":
        tools = "Read, Grep, Glob"
        denied = "AskUserQuestion, Edit, Write, Bash"
    else:
        tools = "Read, Edit, Write, Grep, Glob, Bash"
        denied = "AskUserQuestion"
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tools: {tools}\n"
        f"disallowedTools: {denied}\n"
        "---\n\n"
        + body
    )
