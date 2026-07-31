"""Full branch coverage for WorkflowRouter."""

from unittest.mock import patch

from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType


def test_route_comment_returns_comment_response():
    wt = WorkflowRouter.route_issue("X-1", "sum", "desc", comment="@DevBot hi")
    assert wt == WorkflowType.COMMENT_RESPONSE


def test_route_oracle_keywords():
    wt = WorkflowRouter.route_issue("X-1", "how to design this", "should we use pattern")
    assert wt == WorkflowType.ORACLE_CONSULT


def test_route_planning_high_complexity():
    # multiple planning keywords + long description + file ext
    desc = "implement create build refactor " + ("x" * 1100) + " file.py"
    wt = WorkflowRouter.route_issue("X-1", "feature epic", desc)
    assert wt == WorkflowType.PLANNING


def test_route_direct_low_complexity():
    wt = WorkflowRouter.route_issue("X-1", "fix typo", "small change")
    assert wt == WorkflowType.DIRECT_EXECUTION


def test_complexity_score_caps_at_five():
    text = " ".join(WorkflowRouter.PLANNING_KEYWORDS) + " " + (".py " * 5) + ("z" * 1100)
    score = WorkflowRouter._calculate_complexity("implement create build", text)
    assert score == 5


def test_complexity_mid_length():
    # only >500 not >1000
    desc = "a" * 600
    score = WorkflowRouter._calculate_complexity("nothing special", desc)
    assert score == 1


def test_should_auto_start_direct_and_comment():
    assert WorkflowRouter.should_auto_start(WorkflowType.DIRECT_EXECUTION) is True
    assert WorkflowRouter.should_auto_start(WorkflowType.COMMENT_RESPONSE) is True


def test_should_auto_start_planning_follows_setting():
    with patch("src.orchestrator.workflow_router.settings") as s:
        s.auto_start_plans = False
        assert WorkflowRouter.should_auto_start(WorkflowType.PLANNING) is False
        s.auto_start_plans = True
        assert WorkflowRouter.should_auto_start(WorkflowType.PLANNING) is True
        assert WorkflowRouter.should_auto_start(WorkflowType.ORACLE_CONSULT) is True


def test_get_agent_for_workflow_all_types():
    with patch("src.orchestrator.workflow_router.settings") as s:
        s.planning_agent = "prometheus"
        s.default_agent = "sisyphus"
        assert WorkflowRouter.get_agent_for_workflow(WorkflowType.PLANNING) == "prometheus"
        assert WorkflowRouter.get_agent_for_workflow(WorkflowType.DIRECT_EXECUTION) == "sisyphus"
        assert WorkflowRouter.get_agent_for_workflow(WorkflowType.COMMENT_RESPONSE) == "sisyphus"
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
