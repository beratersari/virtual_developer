"""Mode: test routes to derman-test and the TEST_PROMPT stub."""

from src.orchestrator.prompt_builder import PromptBuilder
from src.state.session_bind_store import normalize_session_kind


def test_test_prompt_requires_agents_md_and_unit_tests_only():
    PromptBuilder.clear_prompt_file_cache()
    text = PromptBuilder.build_test_prompt(
        "KAN-9",
        "cover login",
        "{params}\nMode: test\n{params}",
        work_branch="feature/KAN-9",
    )
    assert "derman-test" in text
    assert "AGENTS.md" in text
    assert "unit tests only" in text.lower() or "Write **unit tests only**" in text
    assert "feature/KAN-9" in text
    assert "KAN-9" in text


def test_session_kind_testing_is_separate_from_build():
    assert normalize_session_kind("testing") == "test"
    assert normalize_session_kind("derman-test") == "test"
    assert normalize_session_kind("execution") == "build"
    assert normalize_session_kind("planning") == "plan"