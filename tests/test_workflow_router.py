"""Full branch coverage for WorkflowRouter (mode-based routing)."""

from unittest.mock import patch

from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType


def _params(mode: str) -> str:
    return (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: feature/X-1\n"
        "Target branch: develop\n"
        f"Mode: {mode}\n"
        "{params}"
    )


def test_route_oracle_keywords():
    # Pure consult — no implementation verbs, no Mode
    wt = WorkflowRouter.route_issue("X-1", "how to structure this", "should we use pattern")
    assert wt == WorkflowType.ORACLE_CONSULT


def test_route_implement_not_oracle():
    """Implementation work must not be stolen by oracle phrases like 'how to'."""
    wt = WorkflowRouter.route_issue("X-1", "how to implement auth", "add OAuth login")
    assert wt != WorkflowType.ORACLE_CONSULT


def test_route_mode_plan():
    wt = WorkflowRouter.route_issue("X-1", "feature", _params("plan"))
    assert wt == WorkflowType.PLANNING


def test_route_mode_build():
    wt = WorkflowRouter.route_issue("X-1", "fix typo", _params("build"))
    assert wt == WorkflowType.EXECUTION


def test_route_mode_aliases():
    assert WorkflowRouter.route_issue("X-1", "s", _params("planning")) == WorkflowType.PLANNING
    assert WorkflowRouter.route_issue("X-1", "s", _params("execute")) == WorkflowType.EXECUTION


def test_route_missing_mode_still_routes_template_checked_later():
    """Routing does not fail on missing Mode; git template parse does."""
    wt, err = WorkflowRouter.route_issue_with_reason("X-1", "fix typo", "small change")
    assert wt == WorkflowType.PLANNING
    assert err is None


def test_should_auto_start_execution():
    assert WorkflowRouter.should_auto_start(WorkflowType.EXECUTION) is True


def test_should_auto_start_planning_never():
    """Plans never auto-start; operators use Mode: build + To Do."""
    assert WorkflowRouter.should_auto_start(WorkflowType.PLANNING) is False
    assert WorkflowRouter.should_auto_start(WorkflowType.ORACLE_CONSULT) is False


def test_get_agent_for_workflow_all_types():
    with patch("src.orchestrator.workflow_router.settings") as s:
        s.planning_agent = "prometheus"
        s.orchestrator_agent = "atlas"
        s.default_agent = "sisyphus"
        assert WorkflowRouter.get_agent_for_workflow(WorkflowType.PLANNING) == "prometheus"
        assert WorkflowRouter.get_agent_for_workflow(WorkflowType.EXECUTION) == "atlas"
        assert WorkflowRouter.get_agent_for_workflow(WorkflowType.ORACLE_CONSULT) == "oracle"


def test_extract_mention_command_found():
    with patch("src.orchestrator.workflow_router.settings") as s:
        s.trigger_mentions_list = ["@DevBot", "@AI"]
        cmd = WorkflowRouter.extract_mention_command("hey @DevBot /status please")
        assert cmd is not None
        assert "/status" in cmd.lower() or "status" in cmd.lower()


def test_extract_mention_command_not_found():
    with patch("src.orchestrator.workflow_router.settings") as s:
        s.trigger_mentions_list = ["@DevBot"]
        assert WorkflowRouter.extract_mention_command("no bot here") is None


def test_no_comment_workflow_type():
    assert not hasattr(WorkflowType, "COMMENT_RESPONSE")
    assert not hasattr(WorkflowType, "DIRECT_EXECUTION")
    assert {w.value for w in WorkflowType} == {"planning", "execution", "oracle"}
