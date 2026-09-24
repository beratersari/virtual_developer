"""Copy derman agents into the Claude Code home as Claude agent files.

OpenCode frontmatter (mode, temperature, permission) is replaced with
Claude's name / description / tools header. The instruction body is kept.
Skills are copied as-is (each folder already has SKILL.md).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

AGENT_NAMES = (
    "derman-build.md",
    "derman-plan.md",
    "derman-test.md",
    "derman-reviewer.md",
)


def claude_home() -> Path:
    profile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if profile:
        return Path(profile) / ".claude"
    return Path.home() / ".claude"


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


def _find_source(root: Path) -> tuple[Path, Path]:
    for rel in ("opencoderman", "opencode_configs", ""):
        base = root / rel if rel else root
        agents = base / "agents"
        skills = base / "skills"
        if agents.is_dir() and skills.is_dir():
            return agents, skills
    raise FileNotFoundError(
        f"Could not find opencoderman/agents and skills under {root}"
    )


def install_claude_agents(*, source_root: Path, home: Path | None = None) -> Path:
    agents, skills = _find_source(source_root)
    dest = home or claude_home()
    agent_dir = dest / "agents"
    skill_dir = dest / "skills"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for filename in AGENT_NAMES:
        src = agents / filename
        if not src.is_file():
            raise FileNotFoundError(f"required agent missing: {src}")
        name = filename[: -len(".md")]
        text = to_claude_agent(src.read_text(encoding="utf-8"), name)
        (agent_dir / filename).write_text(text, encoding="utf-8")
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    shutil.copytree(skills, skill_dir)
    return dest


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source = Path.cwd()
    home: Path | None = None
    i = 0
    while i < len(args):
        if args[i] == "--source" and i + 1 < len(args):
            source = Path(args[i + 1])
            i += 2
            continue
        if args[i] == "--claude-home" and i + 1 < len(args):
            home = Path(args[i + 1])
            i += 2
            continue
        i += 1
    dest = install_claude_agents(source_root=source, home=home)
    print(f"[OK] claude agents -> {dest / 'agents'}")
    print(f"[OK] claude skills -> {dest / 'skills'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
