"""GitLab MR key fallbacks — same order as Azure PRs, Jira first."""

from __future__ import annotations

from src.azure.workitems import find_work_item_key_by_git
from src.gitlab.keys import resolve_mr_issue_key
from src.gitlab.webhook import decide_gitlab_note_webhook
from src.state.manager import JiraStateManager
from tests.test_gitlab_webhook import _mr_payload

_KEYS = ["KAN", "PROJ"]
_PATH = "acme/demo"
_REPO = "https://gitlab.example.com/acme/demo.git"
_REPO_BARE = "https://gitlab.example.com/acme/demo"
_SRC = "feature/login"
_TGT = "develop"


def _resolve(**kwargs):
    base = dict(
        project_path=_PATH,
        mr_iid=4,
        project_keys=_KEYS,
        repository_url=_REPO,
        source_branch=_SRC,
        target_branch=_TGT,
    )
    base.update(kwargs)
    return resolve_mr_issue_key(**base)


def _seed_work_item(
    tmp_path,
    key: str,
    *,
    repo: str = _REPO,
    source: str = _SRC,
    target: str = _TGT,
    in_metadata: bool = True,
    source_tag: str = "azure_workitem",
) -> JiraStateManager:
    sm = JiraStateManager(state_dir=tmp_path / "state")
    desc = (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        "Mode: build\n"
        "{params}"
    )
    sm.create_state(key, "Do the thing", desc)
    meta = {"source": source_tag}
    if in_metadata:
        meta.update(
            {
                "repository_url": repo,
                "source_branch": source,
                "target_branch": target,
            }
        )
    sm.update_state(key, metadata=meta)
    return sm


def test_1_jira_key_in_title():
    assert _resolve(mr_title="feat(KAN-12): add login") == "KAN-12"


def test_2_wit_key_in_title():
    assert _resolve(mr_title="feat(WIT-DEMO-42): follow-up") == "WIT-DEMO-42"


def test_3_jira_title_wins_over_wit_in_same_title():
    assert _resolve(mr_title="feat(KAN-12): also WIT-DEMO-42") == "KAN-12"


def test_4_closes_jira_key_in_description():
    assert (
        _resolve(mr_title="Add login", mr_description="Closes KAN-99") == "KAN-99"
    )


def test_5_wit_key_in_description():
    assert (
        _resolve(
            mr_title="Add login",
            mr_description="Implements WIT-DEMO-42 on this branch.",
        )
        == "WIT-DEMO-42"
    )


def test_6_closes_jira_wins_over_wit_in_description():
    assert (
        _resolve(
            mr_title="Add login",
            mr_description="Closes KAN-7\nAlso WIT-DEMO-42",
        )
        == "KAN-7"
    )


def test_7_local_work_item_by_metadata_git(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    assert _resolve(mr_title="Add login", state_manager=sm) == "WIT-DEMO-42"


def test_8_local_work_item_by_params_when_metadata_empty(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-7", in_metadata=False)
    assert _resolve(mr_title="Add login", state_manager=sm) == "WIT-DEMO-7"


def test_9_repo_normalized_dot_git(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", repo=_REPO)
    assert (
        _resolve(mr_title="Add login", repository_url=_REPO_BARE, state_manager=sm)
        == "WIT-DEMO-42"
    )


def test_10_source_mismatch_falls_back_to_gl(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", source="feature/other")
    assert _resolve(mr_title="Add login", state_manager=sm) == "GL-ACME-DEMO-4"


def test_11_target_mismatch_falls_back_to_gl(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", target="main")
    assert _resolve(mr_title="Add login", state_manager=sm) == "GL-ACME-DEMO-4"


def test_12_repo_mismatch_falls_back_to_gl(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", repo="https://other.example/r.git")
    assert _resolve(mr_title="Add login", state_manager=sm) == "GL-ACME-DEMO-4"


def test_13_two_matching_work_items_do_not_guess(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    sm.create_state("WIT-OTHER-9", "Other", "x")
    sm.update_state(
        "WIT-OTHER-9",
        metadata={
            "source": "azure_workitem",
            "repository_url": _REPO_BARE,
            "source_branch": _SRC,
            "target_branch": _TGT,
        },
    )
    assert find_work_item_key_by_git(_REPO, _SRC, _TGT, state_manager=sm) == ""
    assert _resolve(mr_title="Add login", state_manager=sm) == "GL-ACME-DEMO-4"


def test_14_jira_state_with_same_git_is_ignored(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-12", "Jira ticket", "x")
    sm.update_state(
        "KAN-12",
        metadata={
            "source": "jira",
            "repository_url": _REPO,
            "source_branch": _SRC,
            "target_branch": _TGT,
        },
    )
    assert find_work_item_key_by_git(_REPO, _SRC, _TGT, state_manager=sm) == ""
    assert _resolve(mr_title="Add login", state_manager=sm) == "GL-ACME-DEMO-4"


def test_15_title_wit_wins_over_git_match(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    assert _resolve(mr_title="feat(WIT-OTHER-1): x", state_manager=sm) == "WIT-OTHER-1"


def test_16_no_git_args_skips_local_match(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    assert (
        resolve_mr_issue_key(
            mr_title="Add login",
            project_path=_PATH,
            mr_iid=4,
            project_keys=_KEYS,
            state_manager=sm,
        )
        == "GL-ACME-DEMO-4"
    )


def test_17_legacy_bare_numeric_work_item_key_matches(tmp_path):
    sm = _seed_work_item(tmp_path, "42")
    assert _resolve(mr_title="Add login", state_manager=sm) == "42"


def _note_decision(payload):
    return decide_gitlab_note_webhook(
        payload,
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["berat_ai"],
        jira_project_keys=_KEYS,
    )


def test_hash_id_scoped_by_collection(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("WIT-DEMO-42", "Do", "x")
    sm.update_state(
        "WIT-DEMO-42",
        metadata={
            "source": "azure_workitem",
            "azure_work_item_id": 42,
            "azure_collection_url": "https://tfs.example.com/tfs/ColA",
        },
    )
    assert (
        _resolve(
            mr_title="Fixes #42",
            collection_url="https://tfs.example.com/tfs/ColA",
            state_manager=sm,
        )
        == "WIT-DEMO-42"
    )


def test_18_webhook_title_jira():
    d = _note_decision(
        _mr_payload(title="feat(KAN-12): add login", note="@berat_ai /yaver go")
    )
    assert d.accepted is True
    assert d.event.issue_key == "KAN-12"


def test_19_webhook_title_wit():
    d = _note_decision(
        _mr_payload(title="feat(WIT-DEMO-42): add login", note="@berat_ai /yaver go")
    )
    assert d.accepted is True
    assert d.event.issue_key == "WIT-DEMO-42"


def test_20_webhook_closes_and_gl_fallback():
    closes = _note_decision(
        _mr_payload(
            title="Add login",
            description="Closes KAN-99",
            note="@berat_ai /yaver go",
        )
    )
    assert closes.accepted is True
    assert closes.event.issue_key == "KAN-99"
    az = _note_decision(
        _mr_payload(title="Add login", note="@berat_ai /yaver go")
    )
    assert az.accepted is True
    assert az.event.issue_key == "GL-ACME-DEMO-4"


def test_21_webhook_git_match(tmp_path, monkeypatch):
    from src.config import settings

    sm = _seed_work_item(
        tmp_path,
        "WIT-DEMO-42",
        repo=_REPO,
        source=_SRC,
        target=_TGT,
    )
    monkeypatch.setattr(
        type(settings), "state_dir", property(lambda self: tmp_path / "state")
    )
    d = _note_decision(
        _mr_payload(title="Add login", note="@berat_ai /yaver go")
    )
    assert d.accepted is True
    assert d.event.issue_key == "WIT-DEMO-42"
    assert sm.get_state("WIT-DEMO-42") is not None
