"""Full branch coverage for PromptBuilder."""

from src.orchestrator.prompt_builder import PromptBuilder


def test_prometheus_without_acceptance():
    p = PromptBuilder.build_prometheus_prompt("A-1", "sum", "desc")
    assert "A-1" in p and "sum" in p and "desc" in p
    assert "Acceptance Criteria" not in p


def test_prometheus_with_acceptance():
    p = PromptBuilder.build_prometheus_prompt("A-1", "s", "d", acceptance_criteria="must pass")
    assert "Acceptance Criteria" in p
    assert "must pass" in p


def test_atlas_without_learnings():
    p = PromptBuilder.build_atlas_prompt("A-1", "/plans/A-1.md")
    assert "A-1" in p and "/plans/A-1.md" in p
    assert "Previous Learnings" not in p


def test_atlas_with_learnings():
    p = PromptBuilder.build_atlas_prompt("A-1", "p.md", previous_learnings=["l1", "l2"])
    assert "Previous Learnings" in p
    assert "l1" in p and "l2" in p


def test_sisyphus_no_context():
    p = PromptBuilder.build_sisyphus_prompt("A-1", "do thing")
    assert "do thing" in p
    assert "Context" not in p


def test_sisyphus_with_files_and_patterns():
    p = PromptBuilder.build_sisyphus_prompt(
        "A-1",
        "do",
        context={"files": ["a.py", "b.py"], "patterns": ["singleton"]},
    )
    assert "a.py" in p and "b.py" in p and "singleton" in p


def test_sisyphus_context_empty_keys():
    # empty dict is falsy — no Context section; non-empty without files/patterns still adds header
    p_empty = PromptBuilder.build_sisyphus_prompt("A-1", "do", context={})
    assert "Context" not in p_empty
    p = PromptBuilder.build_sisyphus_prompt("A-1", "do", context={"other": 1})
    assert "Context" in p


def test_comment_response_with_and_without_state():
    p1 = PromptBuilder.build_comment_response_prompt("A-1", "hi")
    assert "Current Work State" not in p1
    p2 = PromptBuilder.build_comment_response_prompt("A-1", "hi", current_state="executing")
    assert "executing" in p2


def test_code_review_prompt():
    p = PromptBuilder.build_code_review_prompt("A-1", "s", "d", "model-x")
    assert "model-x" in p and "A-1" in p


def test_oracle_with_and_without_files():
    p1 = PromptBuilder.build_oracle_consult_prompt("why?")
    assert "Context Files" not in p1
    p2 = PromptBuilder.build_oracle_consult_prompt("why?", context_files=["a.py"])
    assert "a.py" in p2
