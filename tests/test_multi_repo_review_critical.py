"""Critical multi-repo paths: per-repository branches must survive dispatch and follow-up.

These tests state the safe outcome. A failure is a daily-use break, not a
style nit. They do not clone or call Jira.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.config import settings
from src.git_manager import GitManager
from tests.conftest import make_issue_event

API = "https://gitlab.com/acme/api.git"
WEB = "https://gitlab.com/acme/web.git"
WORKER = "https://gitlab.com/acme/worker.git"


def _description(*rows: tuple[str, str, str]) -> str:
    lines = ["{code}", "{params}"]
    for url, source, target in rows:
        lines.extend(
            [
                f"Repository: {url}",
                f"Source branch: {source}",
                f"Target branch: {target}",
            ]
        )
    lines.extend(["Mode: build", "{params}", "{code}"])
    return "\n".join(lines) + "\n"


def _schedule_event(key: str, refs: list[dict]) -> dict:
    rows = [
        (row["url"], row["source_branch"], row["target_branch"]) for row in refs
    ]
    event = make_issue_event(
        key=key,
        summary=key,
        description=_description(*rows),
        event_type="jira:issue_created",
    )
    event["scheduled_job"] = True
    event["schedule_id"] = "sched-test"
    event["repository_urls"] = [row["url"] for row in refs]
    event["repository_refs"] = refs
    return event


def _spec(specs: list[dict], needle: str) -> dict:
    hits = [row for row in specs if needle in row["url"]]
    assert len(hits) == 1, specs
    return hits[0]


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "clones"))
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _stored_refs(processor, event: dict) -> dict:
    """Run schedule intake far enough to persist repository metadata."""

    async def _stop(_state):
        return None

    async def _run():
        with patch.object(processor, "_start_execution_workflow", side_effect=_stop):
            with patch.object(processor, "_start_planning_workflow", side_effect=_stop):
                with patch.object(
                    processor, "_mark_jira_in_progress", return_value=True
                ):
                    await processor._handle_issue_created(event)

    asyncio.run(_run())
    state = processor.state_manager.get_state(event["issue"]["key"])
    assert state is not None
    return dict(state.metadata or {})


def test_followup_on_a_later_repository_keeps_the_first_repository(
    processor, tmp_path, monkeypatch
):
    """A comment on the second MR must not retarget the first repository.

    Dashboard dispatch sends repository_urls and repository_refs. The first
    repository keeps feature/KAN-30 cut from main. The second keeps hotfix
    cut from develop. Following up the second merge request still clones
    the first repository on feature/KAN-30 from main, in the same multi folder.
    """
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "clones"))
    refs = [
        {
            "url": API,
            "source_branch": "feature/KAN-30",
            "target_branch": "main",
            "source_branch_mode": "issue_key",
        },
        {
            "url": WEB,
            "source_branch": "hotfix",
            "target_branch": "develop",
            "source_branch_mode": "custom",
        },
    ]
    event = _schedule_event("KAN-30", refs)
    meta = _stored_refs(processor, event)
    stored = list(meta.get("repository_refs") or [])
    assert stored and stored[0]["source_branch"] == "feature/KAN-30", stored
    assert stored[0]["target_branch"] == "main", stored
    assert stored[1]["source_branch"] == "hotfix", stored
    assert stored[1]["target_branch"] == "develop", stored

    original = GitManager(
        issue_key=None,
        remote_url=API,
        source_branch="feature/KAN-30",
        target_branch="main",
        repository_urls=list(meta.get("repository_urls") or []),
        repository_refs=stored,
    )
    follow = GitManager(
        issue_key=None,
        remote_url=WEB,
        source_branch="hotfix",
        target_branch="develop",
        keep_source_work_branch=True,
        repository_urls=list(meta.get("repository_urls") or []),
        repository_refs=stored,
    )
    api = _spec(follow._repository_specs(), "/api")
    web = _spec(follow._repository_specs(), "/web")
    assert (api["source_branch"], api["target_branch"]) == (
        "feature/KAN-30",
        "main",
    ), api
    assert (web["source_branch"], web["target_branch"]) == ("hotfix", "develop"), web
    assert (
        GitManager.resolve_work_branch_name(
            "KAN-30",
            api["source_branch"],
            api["target_branch"],
            keep_source=True,
        )
        == "feature/KAN-30"
    )
    assert original._multi_repo_root().name == follow._multi_repo_root().name


def test_followup_on_the_middle_repository_of_three_keeps_every_branch(
    processor, tmp_path, monkeypatch
):
    """Three repositories, three branch pairs. The middle MR is the follow-up."""
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "clones"))
    refs = [
        {"url": API, "source_branch": "feature/KAN-31", "target_branch": "main"},
        {"url": WEB, "source_branch": "integration", "target_branch": "main"},
        {"url": WORKER, "source_branch": "chore/deps", "target_branch": "release/1.2"},
    ]
    meta = _stored_refs(processor, _schedule_event("KAN-31", refs))
    stored = list(meta.get("repository_refs") or [])
    follow = GitManager(
        issue_key=None,
        remote_url=WEB,
        source_branch="integration",
        target_branch="main",
        keep_source_work_branch=True,
        repository_urls=list(meta.get("repository_urls") or []),
        repository_refs=stored,
    )
    specs = follow._repository_specs()
    assert (_spec(specs, "/api")["source_branch"], _spec(specs, "/api")["target_branch"]) == (
        "feature/KAN-31",
        "main",
    )
    assert (_spec(specs, "/web")["source_branch"], _spec(specs, "/web")["target_branch"]) == (
        "integration",
        "main",
    )
    assert (
        _spec(specs, "/worker")["source_branch"],
        _spec(specs, "/worker")["target_branch"],
    ) == ("chore/deps", "release/1.2")


def test_first_run_checks_out_each_repository_on_its_own_branch(
    processor, tmp_path, monkeypatch
):
    """The ticket description supplies the parent branch on the first run.

    Empty stored fields must not move the first repository onto the second
    repository's branch while that parent branch is still the first one.
    """
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "clones"))
    refs = [
        {"url": API, "source_branch": "feature/KAN-35", "target_branch": "main"},
        {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
    ]
    meta = _stored_refs(processor, _schedule_event("KAN-35", refs))
    first_run = GitManager(
        issue_key=None,
        remote_url=API,
        source_branch="feature/KAN-35",
        target_branch="main",
        repository_urls=list(meta.get("repository_urls") or []),
        repository_refs=list(meta.get("repository_refs") or []),
    )
    specs = first_run._repository_specs()
    assert (_spec(specs, "/api")["source_branch"], _spec(specs, "/api")["target_branch"]) == (
        "feature/KAN-35",
        "main",
    )
    assert (_spec(specs, "/web")["source_branch"], _spec(specs, "/web")["target_branch"]) == (
        "hotfix",
        "develop",
    )


def test_followup_reuses_the_workspace_from_the_first_run(
    processor, tmp_path, monkeypatch
):
    """The follow-up clone must be the same multi_ folder as the first run."""
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "clones"))
    refs = [
        {"url": API, "source_branch": "feature/KAN-36", "target_branch": "main"},
        {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
    ]
    meta = _stored_refs(processor, _schedule_event("KAN-36", refs))
    stored = list(meta.get("repository_refs") or [])
    urls = list(meta.get("repository_urls") or [])
    original = GitManager(
        issue_key=None,
        remote_url=API,
        source_branch="feature/KAN-36",
        target_branch="main",
        repository_urls=urls,
        repository_refs=stored,
    )
    follow = GitManager(
        issue_key=None,
        remote_url=WEB,
        source_branch="hotfix",
        target_branch="develop",
        keep_source_work_branch=True,
        repository_urls=urls,
        repository_refs=stored,
    )
    assert original._multi_repo_root().name == follow._multi_repo_root().name


def test_board_poll_keeps_each_repository_branch_for_a_later_followup(processor):
    """A To Do rework has no repository_urls list. The description is the source."""
    event = make_issue_event(
        key="KAN-32",
        summary="KAN-32",
        description=_description(
            (API, "feature/KAN-32", "main"),
            (WEB, "hotfix", "develop"),
        ),
    )
    saved = processor._repository_urls_from_event(event)
    follow = GitManager(
        issue_key=None,
        remote_url=WEB,
        source_branch="hotfix",
        target_branch="develop",
        keep_source_work_branch=True,
        repository_urls=list(saved.get("repository_urls") or []),
        repository_refs=list(saved.get("repository_refs") or []),
    )
    api = _spec(follow._repository_specs(), "/api")
    assert (api["source_branch"], api["target_branch"]) == ("feature/KAN-32", "main")


def test_same_branch_on_every_repository_survives_a_second_repo_followup(processor):
    """The common case: every repository uses feature/KEY into main."""
    refs = [
        {"url": API, "source_branch": "feature/KAN-33", "target_branch": "main"},
        {"url": WEB, "source_branch": "feature/KAN-33", "target_branch": "main"},
    ]
    meta = _stored_refs(processor, _schedule_event("KAN-33", refs))
    follow = GitManager(
        issue_key=None,
        remote_url=WEB,
        source_branch="feature/KAN-33",
        target_branch="main",
        keep_source_work_branch=True,
        repository_urls=list(meta.get("repository_urls") or []),
        repository_refs=list(meta.get("repository_refs") or []),
    )
    for row in follow._repository_specs():
        assert (row["source_branch"], row["target_branch"]) == (
            "feature/KAN-33",
            "main",
        ), row


def test_two_deliveries_keep_the_multi_folder_until_both_reviews_finish(
    processor, tmp_path, monkeypatch
):
    """Merging the first repository's MR must leave the shared folder in place."""
    from src.dashboard.temp_storage import multi_workspace_ready_to_delete

    monkeypatch.setattr(
        "src.dashboard.temp_storage._lookup_review_state", lambda _url: "opened"
    )
    processor.state_manager.create_state("KAN-34", "pair", "d")
    job = processor.job_store.create_job(issue_key="KAN-34", summary="pair")
    assert job is not None
    processor._active_jobs["KAN-34"] = job["job_id"]
    processor.job_store.update_job(
        job["job_id"], working_directory=str(tmp_path / "multi_abc")
    )
    state = processor.state_manager.get_state("KAN-34")
    assert state is not None
    api_mr = "https://gitlab.com/acme/api/-/merge_requests/4"
    web_mr = "https://gitlab.com/acme/web/-/merge_requests/9"
    processor._record_git_delivery(
        state,
        feature_branch="feature/KAN-34",
        merge_request_url=api_mr,
        repository_url=API,
        target_branch="main",
    )
    processor._record_git_delivery(
        state,
        feature_branch="hotfix",
        merge_request_url=web_mr,
        repository_url=WEB,
        target_branch="develop",
    )
    stored = processor.job_store.get_job(job["job_id"]) or {}
    urls = [row.get("repository_url") for row in stored.get("deliveries") or []]
    assert API in urls and WEB in urls, stored.get("deliveries")
    assert multi_workspace_ready_to_delete("multi_abc", known_done_url=api_mr) is False


def test_review_remote_matches_clone_url_with_or_without_git_suffix():
    from src.dashboard.temp_storage import _remote_matches_review

    review = "https://gitlab.com/acme/api/-/merge_requests/4"
    assert _remote_matches_review("https://gitlab.com/acme/api.git", review)
    assert _remote_matches_review("https://oauth2:secret@gitlab.com/acme/api", review)
    assert not _remote_matches_review("https://gitlab.com/acme/web.git", review)


def test_single_repo_session_does_not_reuse_the_multi_repo_chat():
    from src.state.session_bind_store import bind_id_for, multi_session_scope

    scope = multi_session_scope(
        [API, WEB],
        [
            {"url": API, "source_branch": "feature/KAN-30", "target_branch": "main"},
            {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
        ],
    )
    assert scope.startswith("multi:")
    multi = bind_id_for(
        API, "feature/KAN-30", "main", kind="build", scope=scope, backend="opencode"
    )
    single = bind_id_for(
        API, "feature/KAN-30", "main", kind="build", scope="", backend="opencode"
    )
    assert multi != single


class _Child:
    def __init__(self, url: str, branch: str, target: str, root):
        self.remote_url = url
        self.source_branch = branch
        self.target_branch = target
        self.work_branch = branch
        self.temp_dir = root
        self.repo_checkouts = []

    def ensure_feature_branch(self, issue_key=None):
        return self.work_branch


class _Runner:
    def __init__(self):
        self.prompts: list[str] = []

    async def run_agent_with_retry(self, task, **_kwargs):
        self.prompts.append(task.prompt)
        return {"returncode": 0, "aborted": True, "stdout": "stopped", "stderr": ""}


def _parent_with_children(tmp_path, children: list[_Child]) -> GitManager:
    parent = GitManager(
        issue_key=None,
        remote_url=children[0].remote_url,
        source_branch=children[0].source_branch,
        target_branch=children[0].target_branch,
        keep_source_work_branch=True,
    )
    parent.repo_checkouts = children
    parent.temp_dir = tmp_path
    parent.get_working_directory = lambda: tmp_path  # type: ignore[method-assign]
    return parent


async def _run_until_prompt(processor, starter, event, parent, runner) -> None:
    def fake_init(*_args, **_kwargs):
        processor._contexts[event.issue_key] = {"git": parent, "runner": runner}
        return parent

    with patch.object(processor, "_init_git_manager", side_effect=fake_init):
        await starter(processor.state_manager.get_state(event.issue_key), event)


@pytest.mark.asyncio
async def test_gitlab_followup_on_the_first_mr_tells_the_agent_that_branch(
    processor, tmp_path
):
    """The prompt for api's MR must say feature/KAN-40, not the last repo's branch."""
    from src.gitlab.webhook import GitlabMrNoteEvent

    processor.state_manager.create_state(
        "KAN-40",
        "KAN-40",
        _description((API, "feature/KAN-40", "main"), (WEB, "hotfix", "develop")),
    )
    parent = _parent_with_children(
        tmp_path,
        [
            _Child(API, "feature/KAN-40", "main", tmp_path),
            _Child(WEB, "hotfix", "develop", tmp_path),
        ],
    )
    runner = _Runner()
    event = GitlabMrNoteEvent(
        issue_key="KAN-40",
        note_id="1",
        note_body="@berat_ai /yaver add a health endpoint",
        prompt="add a health endpoint",
        author_username="alice",
        author_name="Alice",
        project_id=3,
        project_path="acme/api",
        repository_url=API,
        host="gitlab.com",
        mr_iid=4,
        mr_title="feat(KAN-40): api",
        mr_description="",
        source_branch="feature/KAN-40",
        target_branch="main",
        mr_url="https://gitlab.com/acme/api/-/merge_requests/4",
    )
    await _run_until_prompt(
        processor, processor._start_gitlab_mr_workflow, event, parent, runner
    )
    assert runner.prompts, "GitLab follow-up returned before building the prompt"
    prompt = runner.prompts[-1]
    told = [line.strip() for line in prompt.splitlines() if "Work branch" in line]
    state = processor.state_manager.get_state("KAN-40")
    recorded = (state.metadata or {}).get("feature_branch")
    assert "* Work branch: `feature/KAN-40`" in prompt, (told, recorded)
    assert "* Work branch: `hotfix`" not in prompt, (told, recorded)
    assert recorded == "feature/KAN-40"


@pytest.mark.asyncio
async def test_azure_followup_on_the_first_pr_tells_the_agent_that_branch(
    processor, tmp_path
):
    """Same rule on an Azure PR comment: the work branch is that PR's source."""
    from src.azure.webhook import AzurePrCommentEvent

    processor.state_manager.create_state("KAN-41", "KAN-41", "build")
    parent = _parent_with_children(
        tmp_path,
        [
            _Child(API, "feature/KAN-41", "main", tmp_path),
            _Child(WEB, "hotfix", "develop", tmp_path),
        ],
    )
    runner = _Runner()
    event = AzurePrCommentEvent(
        issue_key="KAN-41",
        comment_id="7",
        comment_body="@yaver /yaver add a health endpoint",
        prompt="add a health endpoint",
        author_username="alice",
        author_name="Alice",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        project="Acme",
        repository_id="api-id",
        repository_name="api",
        project_path="Acme/api",
        repository_url=API,
        host="tfs.example.com",
        pr_id=15,
        pr_title="feat(KAN-41): api",
        pr_description="",
        source_branch="feature/KAN-41",
        target_branch="main",
        pr_url="https://tfs.example.com/tfs/DefaultCollection/Acme/_git/api/pullrequest/15",
    )
    await _run_until_prompt(
        processor, processor._start_azure_pr_workflow, event, parent, runner
    )
    assert runner.prompts, "Azure follow-up returned before building the prompt"
    prompt = runner.prompts[-1]
    told = [line.strip() for line in prompt.splitlines() if "Work branch" in line]
    state = processor.state_manager.get_state("KAN-41")
    recorded = (state.metadata or {}).get("feature_branch")
    assert "* Work branch: `feature/KAN-41`" in prompt, (told, recorded)
    assert "* Work branch: `hotfix`" not in prompt, (told, recorded)
    assert recorded == "feature/KAN-41"


def test_empty_primary_keeps_the_branches_on_the_real_row():
    """Dispatch calls normalize with a blank primary. The dict row wins the blanks."""
    from src.dashboard.repo_sets import normalize_repository_refs

    rows = normalize_repository_refs(
        API,
        "",
        "",
        [
            {
                "url": API,
                "source_branch": "feature/KAN-30",
                "target_branch": "main",
            },
            {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
        ],
    )
    assert rows[0]["source_branch"] == "feature/KAN-30"
    assert rows[0]["target_branch"] == "main"
    assert (rows[1]["source_branch"], rows[1]["target_branch"]) == (
        "hotfix",
        "develop",
    )


def test_a_branch_already_set_on_the_primary_is_not_replaced():
    """Scheduler passes the real primary branches. A duplicate dict must not overwrite them."""
    from src.dashboard.repo_sets import normalize_repository_refs

    rows = normalize_repository_refs(
        API,
        "feature/KAN-30",
        "main",
        [
            {"url": API, "source_branch": "hotfix", "target_branch": "develop"},
            {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
        ],
    )
    assert rows[0]["source_branch"] == "feature/KAN-30"
    assert rows[0]["target_branch"] == "main"
    assert (rows[1]["source_branch"], rows[1]["target_branch"]) == (
        "hotfix",
        "develop",
    )


def test_wiped_first_row_does_not_take_the_followup_branch():
    """A stored blank row must not be prepared on the other repository's branch."""
    wiped = [
        {"url": API, "source_branch": "", "target_branch": ""},
        {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
    ]
    follow = GitManager(
        issue_key=None,
        remote_url=WEB,
        source_branch="hotfix",
        target_branch="develop",
        keep_source_work_branch=True,
        repository_refs=wiped,
    )
    api = _spec(follow._repository_specs(), "/api")
    web = _spec(follow._repository_specs(), "/web")
    assert api["source_branch"] == ""
    assert api["target_branch"] == ""
    assert (web["source_branch"], web["target_branch"]) == ("hotfix", "develop")


def test_missing_branches_are_filled_from_the_described_repository():
    from src.dashboard.repo_sets import merge_missing_ref_branches

    stored = [
        {"url": API, "source_branch": "", "target_branch": ""},
        {"url": WEB, "source_branch": "hotfix", "target_branch": ""},
    ]
    rows, changed = merge_missing_ref_branches(
        stored,
        (
            (API, "feature/KAN-42", "main"),
            (WEB, "other", "develop"),
        ),
    )
    assert changed is True
    assert rows[0]["source_branch"] == "feature/KAN-42"
    assert rows[0]["target_branch"] == "main"
    assert rows[1]["source_branch"] == "hotfix"
    assert rows[1]["target_branch"] == "develop"
    _again, changed_again = merge_missing_ref_branches(rows, ((API, "nope", "nope"),))
    assert changed_again is False
    assert rows[0]["source_branch"] == "feature/KAN-42"


def _capture_git_init(processor, issue_key: str, **git_kwargs):
    captured: dict = {}
    claimed: list = []

    def fake_git(**kwargs):
        captured.update(kwargs)
        git = MagicMock()
        git.work_branch = "hotfix"
        git.repo_checkouts = []
        git.get_working_directory.return_value = "."
        return git

    def claim(_key, url, branch):
        claimed.append((url, branch))
        return True

    with patch("src.processor.GitManager", side_effect=fake_git) as mocked:
        mocked._is_primary_base = GitManager._is_primary_base
        with patch("src.processor.AgentRunner", return_value=MagicMock()):
            with patch.object(processor, "_claim_source_branch", side_effect=claim):
                processor._init_git_manager(issue_key, **git_kwargs)
    return captured, claimed


def test_followup_repairs_a_wiped_row_from_the_description(processor):
    """The next follow-up restores branches an older dispatch saved blank."""
    processor.state_manager.create_state(
        "KAN-42",
        "KAN-42",
        _description((API, "feature/KAN-42", "main"), (WEB, "hotfix", "develop")),
    )
    processor.state_manager.update_state(
        "KAN-42",
        metadata={
            "repository_urls": [API, WEB],
            "repository_refs": [
                {"url": API, "source_branch": "", "target_branch": ""},
                {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
            ],
        },
    )
    captured, claimed = _capture_git_init(
        processor,
        "KAN-42",
        repository_url=WEB,
        source_branch="hotfix",
        target_branch="develop",
        keep_source_work_branch=True,
    )
    refs = list(captured["repository_refs"])
    api = _spec(refs, "/api")
    assert (api["source_branch"], api["target_branch"]) == ("feature/KAN-42", "main")
    stored = processor.state_manager.get_state("KAN-42").metadata["repository_refs"]
    assert stored[0]["source_branch"] == "feature/KAN-42"
    assert stored[0]["target_branch"] == "main"
    api_claims = [branch for url, branch in claimed if "/api" in url]
    assert api_claims == ["feature/KAN-42"]


def test_wiped_row_without_a_description_is_not_locked_as_the_followup_branch(
    processor,
):
    processor.state_manager.create_state("KAN-44", "KAN-44", "no repository block")
    processor.state_manager.update_state(
        "KAN-44",
        metadata={
            "repository_urls": [API, WEB],
            "repository_refs": [
                {"url": API, "source_branch": "", "target_branch": ""},
                {"url": WEB, "source_branch": "hotfix", "target_branch": "develop"},
            ],
        },
    )
    captured, claimed = _capture_git_init(
        processor,
        "KAN-44",
        repository_url=WEB,
        source_branch="hotfix",
        target_branch="develop",
        keep_source_work_branch=True,
    )
    api = _spec(list(captured["repository_refs"]), "/api")
    assert api["source_branch"] == ""
    assert [branch for url, branch in claimed if "/api" in url] == []
    assert [branch for url, branch in claimed if "/web" in url] == ["hotfix"]


@pytest.mark.asyncio
async def test_gitlab_followup_on_the_middle_mr_names_that_branch(
    processor, tmp_path
):
    """Three clones. The prompt names the reviewed repository, not the last one."""
    from src.gitlab.webhook import GitlabMrNoteEvent

    processor.state_manager.create_state("KAN-43", "KAN-43", "build")
    parent = _parent_with_children(
        tmp_path,
        [
            _Child(API, "feature/KAN-43", "main", tmp_path),
            _Child(WEB, "integration", "main", tmp_path),
            _Child(WORKER, "chore/deps", "release/1.2", tmp_path),
        ],
    )
    runner = _Runner()
    event = GitlabMrNoteEvent(
        issue_key="KAN-43",
        note_id="2",
        note_body="@berat_ai /yaver add a health endpoint",
        prompt="add a health endpoint",
        author_username="alice",
        author_name="Alice",
        project_id=4,
        project_path="acme/web",
        repository_url=WEB,
        host="gitlab.com",
        mr_iid=9,
        mr_title="feat(KAN-43): web",
        mr_description="",
        source_branch="integration",
        target_branch="main",
        mr_url="https://gitlab.com/acme/web/-/merge_requests/9",
    )
    await _run_until_prompt(
        processor, processor._start_gitlab_mr_workflow, event, parent, runner
    )
    prompt = runner.prompts[-1]
    assert "* Work branch: `integration`" in prompt
    assert "* Work branch: `chore/deps`" not in prompt
    state = processor.state_manager.get_state("KAN-43")
    assert (state.metadata or {}).get("feature_branch") == "integration"
