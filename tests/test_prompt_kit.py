"""Unit tests for prompt kit parser and loaders."""

from pathlib import Path

from src.orchestrator.prompt_kit import (
    clear_prompt_kit_cache,
    get_section,
    load_prompt_sections,
    parse_prompt_kit,
    substitute_issue_key,
)


def test_parse_ignores_preamble():
    text = "# Kit\n\nIntro text.\n\n## §role.oracle\nBe wise.\n"
    s = parse_prompt_kit(text)
    assert list(s.keys()) == ["role.oracle"]
    assert s["role.oracle"] == "Be wise."


def test_load_sections_from_tmp_kit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    kit = tmp_path / "agent" / "AGENT_PROMPT.md"
    kit.parent.mkdir(parents=True)
    kit.write_text(
        "## §policy.commit\nBranch feature/{ISSUE_KEY}\n\n"
        "## §role.direct\nCUSTOM DIRECT ROLE\n",
        encoding="utf-8",
    )
    sections = load_prompt_sections(refresh=True)
    assert "CUSTOM DIRECT ROLE" in sections["role.direct"]
    assert "feature/{ISSUE_KEY}" in sections["policy.commit"]
    # Unspecified sections still get built-in defaults
    assert sections["role.oracle"]


def test_get_section_substitutes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    kit = tmp_path / "agent" / "AGENT_PROMPT.md"
    kit.parent.mkdir(parents=True)
    kit.write_text("## §policy.commit\n[{ISSUE_KEY}] fix: x\n", encoding="utf-8")
    body = get_section(
        "policy.commit",
        kit_path=Path("agent/AGENT_PROMPT.md"),
        issue_key="ZZ-1",
        refresh=True,
    )
    assert body == "[ZZ-1] fix: x"


def test_defaults_when_no_kit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    sections = load_prompt_sections(refresh=True)
    assert "Sisyphus" in sections["role.direct"] or "implement" in sections["role.direct"].lower()
    assert "{ISSUE_KEY}" in sections["policy.commit"]


def test_substitute_empty_key():
    assert "[ISSUE]" in substitute_issue_key("[{ISSUE_KEY}]", "")
