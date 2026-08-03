"""Cloud vs on-prem transition and To Do detection."""

from unittest.mock import MagicMock, patch

import pytest

from src.jira.client import JiraClient
from src.jira.poller import JiraPoller


def test_is_todo_turkish_and_category():
    assert JiraPoller._is_todo_status(
        {"status": {"name": "Yapılacaklar", "statusCategory": {"key": "new"}}}
    )
    assert JiraPoller._is_todo_status(
        {"status": {"name": "To Do", "statusCategory": {"key": "new"}}}
    )
    assert not JiraPoller._is_todo_status(
        {"status": {"name": "Devam Ediyor", "statusCategory": {"key": "indeterminate"}}}
    )


def _client_with_transitions(host: str, transitions: list) -> JiraClient:
    with patch("src.jira.client.httpx.Client"):
        with patch("src.jira.client.settings") as s:
            s.jira_host = host
            s.jira_api_token = "t"
            c = JiraClient(host=host, api_token="t")
    c.get_transitions = MagicMock(return_value=transitions)
    c.do_transition = MagicMock(return_value=True)
    return c


def test_onprem_only_matches_in_progress_name():
    c = _client_with_transitions(
        "https://jira.onprem.local",
        [
            {"id": "1", "name": "Start Progress / In Progress", "to": {"name": "In Progress"}},
            {"id": "2", "name": "Devam Ediyor", "to": {"name": "Devam Ediyor"}},
        ],
    )
    assert c.is_cloud is False
    assert c.transition_to_in_progress("X-1") is True
    c.do_transition.assert_called_once_with("X-1", "1")


def test_cloud_matches_devam_ediyor():
    c = _client_with_transitions(
        "https://x.atlassian.net",
        [
            {
                "id": "11",
                "name": "Yapılacaklar",
                "to": {"name": "Yapılacaklar", "statusCategory": {"key": "new"}},
            },
            {
                "id": "21",
                "name": "Devam Ediyor",
                "to": {"name": "Devam Ediyor", "statusCategory": {"key": "indeterminate"}},
            },
            {
                "id": "31",
                "name": "In Review",
                "to": {"name": "İNCELEMEDE", "statusCategory": {"key": "indeterminate"}},
            },
        ],
    )
    assert c.is_cloud is True
    assert c.transition_to_in_progress("KAN-1") is True
    c.do_transition.assert_called_once_with("KAN-1", "21")


def test_cloud_skips_review_uses_indeterminate():
    c = _client_with_transitions(
        "https://x.atlassian.net",
        [
            {
                "id": "31",
                "name": "In Review",
                "to": {"name": "Review", "statusCategory": {"key": "indeterminate"}},
            },
            {
                "id": "21",
                "name": "Start Work",
                "to": {"name": "Doing", "statusCategory": {"key": "indeterminate"}},
            },
        ],
    )
    assert c.transition_to_in_progress("K-1") is True
    c.do_transition.assert_called_once_with("K-1", "21")
