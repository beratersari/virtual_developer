"""Keep Windows-dist SPA freshness needles aligned with current dashboard source.

The Windows Distribution job greps the built ``web/dist`` bundle so a stale SPA
cannot ship. After #78 the Settings page no longer has a Codex API key field
(auth stays in each tool's own config). These tests fail locally if the
workflow needles and ``web/src`` drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-dist.yml"
WEB_SRC = ROOT / "web" / "src"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _required_needles() -> list[str]:
    text = _workflow_text()
    block = re.search(
        r'foreach \(\$needle in @\((.*?)\)\)',
        text,
        flags=re.S,
    )
    assert block, "Windows dist workflow is missing the SPA needle foreach"
    return re.findall(r'"([^"]+)"', block.group(1))


def _removed_needles() -> list[str]:
    text = _workflow_text()
    block = re.search(
        r'foreach \(\$gone in @\((.*?)\)\)',
        text,
        flags=re.S,
    )
    assert block, "Windows dist workflow is missing the removed-setting foreach"
    return re.findall(r'"([^"]+)"', block.group(1))


_SOURCE_FILES = (
    WEB_SRC / "pages" / "settings" / "SettingsPage.tsx",
    WEB_SRC / "ui" / "ModelField.tsx",
    WEB_SRC / "api" / "types.ts",
)


def _web_src_blob() -> str:
    missing = [str(p.relative_to(ROOT)) for p in _SOURCE_FILES if not p.is_file()]
    assert not missing, f"dashboard source missing: {missing}"
    return "\n".join(p.read_text(encoding="utf-8") for p in _SOURCE_FILES)


def test_windows_dist_ships_env_example():
    text = _workflow_text()
    assert '".env.example"' in text
    assert "include-hidden-files: true" in text


def test_spa_freshness_needles_exist_in_dashboard_source():
    blob = _web_src_blob()
    needles = _required_needles()
    assert "agent_backend" in needles
    assert "Default model for plan, build, test, and /yaver." in needles
    assert "Type any id Codex accepts" in needles
    assert "Delete unused clones after (days)" in needles
    assert "Codex API key" not in needles
    missing = [n for n in needles if n not in blob]
    assert missing == [], f"CI SPA needles missing from web/src: {missing}"


def test_windows_dist_requires_derman_reviewer_agent():
    text = _workflow_text()
    assert "opencoderman/agents/derman-reviewer.md" in text
    assert "opencoderman/agents/code-reviewer.md" not in text
    agent = ROOT / "opencoderman" / "agents" / "derman-reviewer.md"
    assert agent.is_file(), "Windows zip CI requires derman-reviewer.md in the tree"


def test_windows_dist_guards_opencode_exe_against_defender():
    """Runner AV has been eating opencode.exe between build and assert."""
    text = _workflow_text()
    assert "ExclusionProcess" in text
    assert "opencode.exe" in text
    assert "vendor/bin/opencode.exe" in text or r"vendor\bin\opencode.exe" in text
    assert "AV quarantine" in text or "Restored opencoderman opencode.exe" in text


def test_removed_settings_are_gone_from_dashboard_source():
    blob = _web_src_blob()
    gone = _removed_needles()
    assert "Codex API key" in gone
    assert "opencode_serve_max_compact_continues" in gone
    leftover = [n for n in gone if n in blob]
    assert leftover == [], f"Removed settings still in web/src: {leftover}"
