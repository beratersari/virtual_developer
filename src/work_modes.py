"""Issue modes and the OpenCode agent each one runs.

Built-in plan, build, and test keep their delivery rules. A custom mode
picks one of those behaviors and its own agent. ``Mode: <name>`` in
``{params}`` selects the row.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from src.config import settings

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_BEHAVIORS = {"plan": "planning", "build": "execution", "test": "testing"}
_BUILTIN = ("plan", "build", "test")
_DEFAULT_AGENTS = {
    "plan": "derman-plan",
    "build": "derman-build",
    "test": "derman-test",
}


def _agent_setting(mode: str) -> str:
    if mode == "plan":
        raw = getattr(settings, "default_plan_agent", "") or ""
    elif mode == "test":
        raw = getattr(settings, "default_test_agent", "") or ""
    else:
        raw = getattr(settings, "default_agent", "") or ""
    text = str(raw).strip()
    return text or _DEFAULT_AGENTS[mode]


def _parse_saved(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        behavior = str(item.get("behavior") or "").strip().lower()
        agent = str(item.get("agent") or "").strip()
        if not _NAME.match(name) or name in seen or behavior not in _BEHAVIORS:
            continue
        if not agent:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "behavior": behavior,
                "agent": agent,
                "builtin": name in _BUILTIN,
            }
        )
    return out


def all_modes() -> List[Dict[str, Any]]:
    saved = {row["name"]: row for row in _parse_saved(getattr(settings, "work_modes", "") or "")}
    modes: List[Dict[str, Any]] = []
    for name in _BUILTIN:
        row = saved.get(name)
        modes.append(
            {
                "name": name,
                "behavior": (row or {}).get("behavior") or name,
                "agent": (row or {}).get("agent") or _agent_setting(name),
                "builtin": True,
            }
        )
    for row in saved.values():
        if row["name"] not in _BUILTIN:
            modes.append(row)
    return modes


def lookup(name: Optional[str]) -> Optional[Dict[str, Any]]:
    token = str(name or "").strip().lower()
    if not token:
        return None
    for row in all_modes():
        if row["name"] == token:
            return row
    return None


def workflow_value(behavior: str) -> str:
    return _BEHAVIORS.get(str(behavior or "").strip().lower(), "")


def apply_saved_modes(rows: List[Any]) -> Dict[str, Any]:
    """Store every mode, including a changed delivery rule for plan/build/test.

    Agent names for those three also update ``default_plan_agent``,
    ``default_agent``, and ``default_test_agent``.
    """
    stored: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip().lower()
            behavior = str(item.get("behavior") or "").strip().lower()
            agent = str(item.get("agent") or "").strip()
        else:
            name = str(getattr(item, "name", "") or "").strip().lower()
            behavior = str(getattr(item, "behavior", "") or "").strip().lower()
            agent = str(getattr(item, "agent", "") or "").strip()
        if name in _BUILTIN and behavior not in _BEHAVIORS:
            behavior = name
        if not _NAME.match(name) or name in seen or behavior not in _BEHAVIORS:
            continue
        if name in _BUILTIN and not agent:
            agent = _DEFAULT_AGENTS[name]
        if not agent:
            continue
        seen.add(name)
        if name == "plan":
            settings.default_plan_agent = agent
        elif name == "test":
            settings.default_test_agent = agent
        elif name == "build":
            settings.default_agent = agent
        stored.append(
            {
                "name": name,
                "behavior": behavior,
                "agent": agent,
                "builtin": name in _BUILTIN,
            }
        )
    settings.work_modes = json.dumps(stored)
    return {
        "work_modes": settings.work_modes,
        "default_agent": settings.default_agent,
        "default_plan_agent": settings.default_plan_agent,
        "default_test_agent": settings.default_test_agent,
    }
