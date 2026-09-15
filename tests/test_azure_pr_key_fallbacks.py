"""Every Azure PR → ticket fallback, on real disk state (no MagicMock)."""

from __future__ import annotations

from src.azure.keys import (
    resolve_pr_issue_key,
    work_item_id_from_hash_mention,
    work_item_key_from_text,
)
from src.azure.webhook import decide_azure_comment_webhook
from src.azure.workitems import find_work_item_key_by_git
from src.state.manager import JiraStateManager
from tests.test_azure_webhook import _pr_comment_payload

_KEYS = ["KAN", "PROJ"]
_PATH = "DefaultCollection/Demo/app"
_REPO = "https://tfs.example.com/tfs/Col/Demo/_git/app.git"
_REPO_BARE = "https://tfs.example.com/tfs/Col/Demo/_git/app"
_SRC = "feature/login"
_TGT = "develop"


def _resolve(**kwargs):
    base = dict(
        project_path=_PATH,
        pr_id=9,
        project_keys=_KEYS,
        repository_url=_REPO,
        source_branch=_SRC,
        target_branch=_TGT,
    )
    base.update(kwargs)
    return resolve_pr_issue_key(**base)


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
    assert _resolve(pr_title="feat(KAN-12): add login") == "KAN-12"


def test_2_wit_key_in_title():
    assert _resolve(pr_title="feat(WIT-DEMO-42): follow-up") == "WIT-DEMO-42"


def test_3_jira_title_wins_over_wit_in_same_title():
    assert (
        _resolve(pr_title="feat(KAN-12): also WIT-DEMO-42") == "KAN-12"
    )


def test_4_closes_jira_key_in_description():
    assert (
        _resolve(pr_title="Add login", pr_description="Closes KAN-99")
        == "KAN-99"
    )


def test_5_wit_key_in_description():
    assert (
        _resolve(
            pr_title="Add login",
            pr_description="Implements WIT-DEMO-42 on this branch.",
        )
        == "WIT-DEMO-42"
    )


def test_6_closes_jira_wins_over_wit_in_description():
    assert (
        _resolve(
            pr_title="Add login",
            pr_description="Closes KAN-7\nAlso WIT-DEMO-42",
        )
        == "KAN-7"
    )


def test_7_local_work_item_by_metadata_git(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    assert (
        _resolve(pr_title="Add login", state_manager=sm) == "WIT-DEMO-42"
    )


def test_8_local_work_item_by_params_when_metadata_empty(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-7", in_metadata=False)
    assert _resolve(pr_title="Add login", state_manager=sm) == "WIT-DEMO-7"


def test_9_repo_normalized_dot_git_and_scheme(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", repo=_REPO)
    assert (
        _resolve(
            pr_title="Add login",
            repository_url=_REPO_BARE,
            state_manager=sm,
        )
        == "WIT-DEMO-42"
    )


def test_10_source_mismatch_falls_back_to_az(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", source="feature/other")
    assert _resolve(pr_title="Add login", state_manager=sm) == (
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


def test_11_target_mismatch_falls_back_to_az(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42", target="main")
    assert _resolve(pr_title="Add login", state_manager=sm) == (
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


def test_12_repo_mismatch_falls_back_to_az(tmp_path):
    sm = _seed_work_item(
        tmp_path, "WIT-DEMO-42", repo="https://other.example/r.git"
    )
    assert _resolve(pr_title="Add login", state_manager=sm) == (
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


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
    assert find_work_item_key_by_git(
        _REPO, _SRC, _TGT, state_manager=sm
    ) == ""
    assert _resolve(pr_title="Add login", state_manager=sm) == (
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


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
    assert find_work_item_key_by_git(
        _REPO, _SRC, _TGT, state_manager=sm
    ) == ""
    assert _resolve(pr_title="Add login", state_manager=sm) == (
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


def test_15_title_wit_wins_over_git_match(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    assert (
        _resolve(pr_title="feat(WIT-OTHER-1): x", state_manager=sm)
        == "WIT-OTHER-1"
    )


def test_16_no_git_args_skips_local_match(tmp_path):
    sm = _seed_work_item(tmp_path, "WIT-DEMO-42")
    assert (
        resolve_pr_issue_key(
            pr_title="Add login",
            project_path=_PATH,
            pr_id=9,
            project_keys=_KEYS,
            state_manager=sm,
        )
        == "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


def test_17_legacy_bare_numeric_work_item_key_matches(tmp_path):
    sm = _seed_work_item(tmp_path, "42")
    assert _resolve(pr_title="Add login", state_manager=sm) == "42"


def test_hash_mention_parses_azure_best_practice():
    assert work_item_id_from_hash_mention("fix(auth): reject empty tokens #42") == 42
    assert work_item_id_from_hash_mention("Fixes #42") == 42
    assert work_item_id_from_hash_mention("This fixed #42!") == 42
    assert work_item_id_from_hash_mention("https://tfs/x/_git/app/#/readme") is None
    assert work_item_id_from_hash_mention("KAN-12") is None


def test_hash_id_binds_work_item_in_same_collection(tmp_path):
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
            pr_title="fix: tokens #42",
            collection_url="https://tfs.example.com/tfs/ColA",
            state_manager=sm,
        )
        == "WIT-DEMO-42"
    )


def test_hash_id_picks_collection_when_two_ids_exist(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("WIT-ALPHA-42", "A", "x")
    sm.update_state(
        "WIT-ALPHA-42",
        metadata={
            "source": "azure_workitem",
            "azure_work_item_id": 42,
            "azure_collection_url": "https://tfs-a.example.com/tfs/ColA",
        },
    )
    sm.create_state("WIT-BETA-42", "B", "x")
    sm.update_state(
        "WIT-BETA-42",
        metadata={
            "source": "azure_workitem",
            "azure_work_item_id": 42,
            "azure_collection_url": "https://tfs-b.example.com/tfs/ColB",
        },
    )
    assert (
        _resolve(
            pr_title="Fixes #42",
            collection_url="https://tfs-b.example.com/tfs/ColB",
            state_manager=sm,
        )
        == "WIT-BETA-42"
    )
    assert (
        _resolve(
            pr_title="Fixes #42",
            collection_url="",
            state_manager=sm,
        )
        == "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )


def test_hash_id_in_description(tmp_path):
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
            pr_title="Add login",
            pr_description="Fixes #42",
            collection_url="https://tfs.example.com/tfs/ColA",
            state_manager=sm,
        )
        == "WIT-DEMO-42"
    )


def test_jira_title_wins_over_hash_id(tmp_path):
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
            pr_title="feat(KAN-12): also #42",
            collection_url="https://tfs.example.com/tfs/ColA",
            state_manager=sm,
        )
        == "KAN-12"
    )


def test_18_work_item_key_from_text_pattern():
    assert work_item_key_from_text("feat(WIT-DEMO-42): x") == "WIT-DEMO-42"
    assert work_item_key_from_text("see WIT-FOO-BAR-9 today") == "WIT-FOO-BAR-9"
    assert work_item_key_from_text("XWIT-DEMO-42") is None
    assert work_item_key_from_text("KAN-12") is None
    assert work_item_key_from_text("") is None


def _comment_decision(payload, monkeypatch):
    from src.azure.identity import reset_identity_cache

    reset_identity_cache()
    monkeypatch.setattr(
        "src.azure.identity.fetch_bot_identity", lambda **_k: None
    )
    return decide_azure_comment_webhook(
        payload,
        enabled=True,
        bot_mentions=["yaver"],
        jira_project_keys=_KEYS,
    )


def test_19_webhook_title_jira_key(monkeypatch):
    d = _comment_decision(
        _pr_comment_payload(title="feat(KAN-12): add login", note="@yaver /yaver go"),
        monkeypatch,
    )
    assert d.accepted is True
    assert d.event.issue_key == "KAN-12"


def test_20_webhook_title_wit_key(monkeypatch):
    d = _comment_decision(
        _pr_comment_payload(
            title="feat(WIT-DEMO-42): add login", note="@yaver /yaver go"
        ),
        monkeypatch,
    )
    assert d.accepted is True
    assert d.event.issue_key == "WIT-DEMO-42"


def test_21_webhook_closes_in_description(monkeypatch):
    d = _comment_decision(
        _pr_comment_payload(
            title="Add login",
            description="Closes KAN-99",
            note="@yaver /yaver go",
        ),
        monkeypatch,
    )
    assert d.accepted is True
    assert d.event.issue_key == "KAN-99"


def test_22_webhook_wit_in_description(monkeypatch):
    d = _comment_decision(
        _pr_comment_payload(
            title="Add login",
            description="Implements WIT-DEMO-42.",
            note="@yaver /yaver go",
        ),
        monkeypatch,
    )
    assert d.accepted is True
    assert d.event.issue_key == "WIT-DEMO-42"


def test_23_webhook_az_fallback(monkeypatch):
    d = _comment_decision(
        _pr_comment_payload(title="Add login", note="@yaver /yaver go"),
        monkeypatch,
    )
    assert d.accepted is True
    assert d.event.issue_key.startswith("AZ-")
    assert d.event.issue_key.endswith("-4")


def test_24_webhook_git_match_uses_pr_repo_source_target(tmp_path, monkeypatch):
    """Webhook passes remoteUrl + refs into resolve; one local WI matches."""
    from src.config import settings

    sm = _seed_work_item(
        tmp_path,
        "WIT-DEMO-42",
        repo="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo",
        source="feature/login",
        target="develop",
    )
    monkeypatch.setattr(type(settings), "state_dir", property(lambda self: tmp_path / "state"))
    d = _comment_decision(
        _pr_comment_payload(title="Add login", note="@yaver /yaver go", pr_id=4),
        monkeypatch,
    )
    assert d.accepted is True
    assert d.event.issue_key == "WIT-DEMO-42"
    assert sm.get_state("WIT-DEMO-42") is not None