"""Inline finding threads: GitLab discussions and Azure file threads."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.review.diffmap import parse_unified_diff
from src.review.findings import Finding
from src.review.post import post_inline_findings
from src.review.position import build_position_variants

_DIFF = """diff --git a/src/buf.cpp b/src/buf.cpp
--- a/src/buf.cpp
+++ b/src/buf.cpp
@@ -4,3 +4,4 @@
 int n = 0;
+strcpy(dest, src);
 return n;
"""


def _finding(**kwargs) -> Finding:
    data = dict(
        path="src/buf.cpp",
        start_line=5,
        end_line=5,
        side="new",
        severity="critical",
        title="overflow",
        body="unbounded",
    )
    data.update(kwargs)
    return Finding(**data)


def test_diffmap_resolves_added_line():
    dm = parse_unified_diff(_DIFF)
    hit = dm.find("src/buf.cpp")
    assert hit is not None
    old, new, kind = hit.resolve_new(5)
    assert new == 5
    assert kind == "added"
    assert old is None


def test_gitlab_position_has_new_line():
    dm = parse_unified_diff(_DIFF)
    variants = build_position_variants(
        _finding(),
        dm,
        base_sha="aaa",
        start_sha="aaa",
        head_sha="bbb",
    )
    assert variants
    assert variants[0]["new_path"] == "src/buf.cpp"
    assert variants[0]["new_line"] == 5
    assert variants[0]["head_sha"] == "bbb"


def test_post_inline_gitlab_opens_discussion(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    finding = _finding()
    gl = MagicMock()
    gl.get_mr_diff_refs.return_value = ("aaa", "aaa", "bbb")
    gl.list_mr_discussions.return_value = []
    gl.post_mr_discussion.return_value = {"id": "d1"}
    with patch("src.gitlab.client.GitlabClient", return_value=gl), patch(
        "src.review.post.merge_base", return_value="aaa"
    ), patch("src.review.post.head_sha", return_value="bbb"), patch(
        "src.review.post.unified_diff", return_value=_DIFF
    ):
        n = post_inline_findings(
            findings=[finding],
            workdir=tmp_path,
            target_branch="develop",
            azure=False,
            meta={
                "gitlab_host": "gitlab.example.com",
                "gitlab_project": "g/r",
                "gitlab_mr_iid": 12,
            },
        )
    assert n == 1
    gl.post_mr_discussion.assert_called_once()
    kwargs = gl.post_mr_discussion.call_args.kwargs
    assert kwargs["mr_iid"] == 12
    assert "yaver-finding" in kwargs["body"]
    assert kwargs["position"]["new_line"] == 5


def test_post_inline_azure_opens_file_thread(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    finding = _finding()
    az = MagicMock()
    az.pr_iteration_span.return_value = (1, 2)
    az.list_pr_threads.return_value = []
    az.post_pr_file_thread.return_value = {"id": 9}
    with patch("src.azure.client.AzureDevOpsClient", return_value=az), patch(
        "src.review.post.merge_base", return_value="aaa"
    ), patch("src.review.post.head_sha", return_value="bbb"), patch(
        "src.review.post.unified_diff", return_value=_DIFF
    ):
        n = post_inline_findings(
            findings=[finding],
            workdir=tmp_path,
            target_branch="main",
            azure=True,
            meta={
                "azure_host": "tfs.example.com",
                "azure_project": "P",
                "azure_repository": "R",
                "azure_pr_id": 7,
            },
        )
    assert n == 1
    az.post_pr_file_thread.assert_called_once()
    ctx = az.post_pr_file_thread.call_args.kwargs["thread_context"]
    assert ctx["filePath"] == "/src/buf.cpp"
    assert ctx["rightFileStart"]["line"] == 5


def test_post_inline_skips_without_clone(tmp_path: Path):
    n = post_inline_findings(
        findings=[_finding()],
        workdir=tmp_path / "missing",
        target_branch="develop",
        azure=False,
        meta={"gitlab_host": "h", "gitlab_project": "g/r", "gitlab_mr_iid": 1},
    )
    assert n == 0


def _post_inline(tmp_path: Path, *, azure: bool, finding=None, **client_patches):
    (tmp_path / ".git").mkdir()
    finding = finding or _finding()
    patches = [
        patch("src.review.post.merge_base", return_value="aaa"),
        patch("src.review.post.head_sha", return_value="bbb"),
        patch("src.review.post.unified_diff", return_value=_DIFF),
    ]
    if azure:
        az = MagicMock()
        az.pr_iteration_span.return_value = (1, 2)
        az.list_pr_threads.return_value = []
        az.post_pr_file_thread.return_value = {"id": 9}
        for name, value in client_patches.items():
            setattr(az, name, value)
        patches.append(patch("src.azure.client.AzureDevOpsClient", return_value=az))
        client = az
        meta = {
            "azure_host": "tfs.example.com",
            "azure_project": "P",
            "azure_repository": "R",
            "azure_pr_id": 7,
        }
    else:
        gl = MagicMock()
        gl.get_mr_diff_refs.return_value = ("aaa", "aaa", "bbb")
        gl.list_mr_discussions.return_value = []
        gl.post_mr_discussion.return_value = {"id": "d1"}
        for name, value in client_patches.items():
            setattr(gl, name, value)
        patches.append(patch("src.gitlab.client.GitlabClient", return_value=gl))
        client = gl
        meta = {
            "gitlab_host": "gitlab.example.com",
            "gitlab_project": "g/r",
            "gitlab_mr_iid": 12,
        }
    with patches[0], patches[1], patches[2], patches[3]:
        n = post_inline_findings(
            findings=[finding],
            workdir=tmp_path,
            target_branch="develop",
            azure=azure,
            meta=meta,
        )
    return n, client


def test_post_inline_gitlab_skips_finding_without_position(tmp_path: Path):
    n, gl = _post_inline(
        tmp_path,
        azure=False,
        finding=_finding(path="src/missing.cpp"),
    )
    assert n == 0
    gl.post_mr_discussion.assert_not_called()


def test_post_inline_gitlab_continues_when_discussion_post_fails(tmp_path: Path):
    n, gl = _post_inline(
        tmp_path,
        azure=False,
        post_mr_discussion=MagicMock(return_value=None),
    )
    assert n == 0
    gl.post_mr_discussion.assert_called()


def test_post_inline_azure_skips_finding_without_position(tmp_path: Path):
    n, az = _post_inline(
        tmp_path,
        azure=True,
        finding=_finding(path="src/missing.cpp"),
    )
    assert n == 0
    az.post_pr_file_thread.assert_not_called()


def test_post_inline_azure_continues_when_thread_post_fails(tmp_path: Path):
    n, az = _post_inline(
        tmp_path,
        azure=True,
        post_pr_file_thread=MagicMock(return_value=None),
    )
    assert n == 0
    az.post_pr_file_thread.assert_called()


def test_deliver_review_ask_does_not_post_findings():
    from src.processor import JobProcessor

    proc = object.__new__(JobProcessor)
    state = MagicMock()
    state.issue_key = "KAN-1"
    state.metadata = {}
    live = MagicMock()
    live.metadata = {}
    proc.state_manager = MagicMock()
    proc.state_manager.get_state.return_value = live
    proc._post_gitlab_mr_reply = MagicMock()
    proc._post_azure_pr_reply = MagicMock()
    proc._comment_command = MagicMock(return_value="ask")
    proc._workdir_for_issue = MagicMock(return_value=None)
    with patch("src.review.post.post_inline_findings") as post:
        JobProcessor._deliver_review_comment(
            proc,
            state,
            MagicMock(command="ask"),
            "### Özet\nok\n```opencoderman-findings\n"
            '{"findings":[{"path":"a.py","start_line":1,"title":"t","body":"b"}]}\n```',
            azure=False,
        )
    proc._post_gitlab_mr_reply.assert_called_once()
    post.assert_not_called()
    body = proc._post_gitlab_mr_reply.call_args[0][1]
    assert "opencoderman-findings" not in body


def test_deliver_review_survives_findings_post_error():
    from src.processor import JobProcessor

    proc = object.__new__(JobProcessor)
    state = MagicMock()
    state.issue_key = "KAN-1"
    state.metadata = {}
    live = MagicMock()
    live.metadata = {"target_branch": "develop"}
    proc.state_manager = MagicMock()
    proc.state_manager.get_state.return_value = live
    proc._post_gitlab_mr_reply = MagicMock()
    proc._post_azure_pr_reply = MagicMock()
    proc._comment_command = MagicMock(return_value="review")
    proc._workdir_for_issue = MagicMock(return_value=None)
    with patch(
        "src.review.post.post_inline_findings",
        side_effect=TypeError(
            "Logger.warning() takes 2 positional arguments but 4 were given"
        ),
    ):
        JobProcessor._deliver_review_comment(
            proc,
            state,
            MagicMock(command="review", target_branch="develop"),
            "overview\n```opencoderman-findings\n"
            '{"findings":[{"path":"a.py","start_line":1,"title":"t","body":"b"}]}\n```',
            azure=False,
        )
    proc._post_gitlab_mr_reply.assert_called_once()
    proc.state_manager.update_state.assert_not_called()
