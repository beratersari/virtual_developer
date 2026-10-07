"""GitLab reviewer state after /review, /ask, and /yaver.

An empty /review approves. /ask marks the review reviewed and does not approve.
"""

from unittest.mock import MagicMock, patch

from src.processor import JobProcessor


def _proc():
    proc = object.__new__(JobProcessor)
    proc.state_manager = MagicMock()
    proc._model_for_issue = MagicMock(return_value="")
    proc._job_id_for_reply = MagicMock(return_value="")
    return proc


def _state():
    state = MagicMock()
    state.issue_key = "GL-1"
    state.metadata = {
        "gitlab_host": "gitlab.example.com",
        "gitlab_project_id": 14,
        "gitlab_mr_iid": 11,
        "target_branch": "develop",
    }
    return state


class _Resp:
    def __init__(self, status, body="{}"):
        self.status_code = status
        self.content = body.encode("utf-8")
        self.text = body

    def json(self):
        return {}


class _Client:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "json": json, "verify": self.kwargs.get("verify")})
        if url.endswith("/approve"):
            return _Resp(201, '{"ok":true}')
        return _Resp(204, "")


def test_publish_reviewer_state_posts_completed_without_verifying_tls(monkeypatch):
    from src.gitlab.client import GitlabClient

    created = []

    def factory(*args, **kwargs):
        client = _Client(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr("src.gitlab.client.httpx.Client", factory)
    ok = GitlabClient(host="gitlab.example.com", pat="glpat-test").publish_reviewer_state(
        project=14, mr_iid=11, state="reviewed"
    )
    assert ok is True
    assert created[0].kwargs.get("verify") is False
    assert created[0].posts[0]["url"].endswith(
        "/projects/14/merge_requests/11/draft_notes/bulk_publish"
    )
    assert created[0].posts[0]["json"] == {"reviewer_state": "reviewed"}


def test_approve_merge_request_posts_approve(monkeypatch):
    from src.gitlab.client import GitlabClient

    created = []

    def factory(*args, **kwargs):
        client = _Client(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr("src.gitlab.client.httpx.Client", factory)
    ok = GitlabClient(host="gitlab.example.com", pat="glpat-test").approve_merge_request(
        project=14, mr_iid=11
    )
    assert ok is True
    assert created[0].posts[0]["url"].endswith("/projects/14/merge_requests/11/approve")
    assert created[0].kwargs.get("verify") is False


def test_empty_findings_approve_and_still_post_the_note():
    proc = _proc()
    live = _state()
    proc.state_manager.get_state.return_value = live
    proc._post_gitlab_mr_reply = MagicMock(return_value=True)
    proc._comment_command = MagicMock(return_value="review")
    proc._workdir_for_issue = MagicMock(return_value=None)
    answer = (
        "### Özet\nSorun yok.\n\n"
        "```opencoderman-findings\n"
        '{"findings": []}\n'
        "```\n"
    )
    gl = MagicMock()
    gl.approve_merge_request.return_value = True
    with patch("src.gitlab.client.GitlabClient", return_value=gl), patch(
        "src.review.post.post_inline_findings"
    ) as inline:
        JobProcessor._deliver_review_comment(
            proc, live, MagicMock(), answer, azure=False
        )
    body = proc._post_gitlab_mr_reply.call_args[0][1]
    assert "Sorun yok." in body
    assert "opencoderman-findings" not in body
    inline.assert_called_once()
    assert inline.call_args.kwargs["findings"] == []
    gl.approve_merge_request.assert_called_once()
    gl.publish_reviewer_state.assert_not_called()


def test_empty_findings_mark_reviewed_when_approve_fails():
    proc = _proc()
    live = _state()
    proc.state_manager.get_state.return_value = live
    proc._post_gitlab_mr_reply = MagicMock(return_value=True)
    proc._comment_command = MagicMock(return_value="review")
    proc._workdir_for_issue = MagicMock(return_value=None)
    gl = MagicMock()
    gl.approve_merge_request.return_value = False
    with patch("src.gitlab.client.GitlabClient", return_value=gl), patch(
        "src.review.post.post_inline_findings", return_value=0
    ):
        JobProcessor._deliver_review_comment(
            proc,
            live,
            MagicMock(),
            "### Özet\nTemiz.\n```opencoderman-findings\n{\"findings\": []}\n```",
            azure=False,
        )
    gl.approve_merge_request.assert_called_once()
    gl.publish_reviewer_state.assert_called_once()
    assert gl.publish_reviewer_state.call_args.kwargs["state"] == "reviewed"


def test_review_with_findings_marks_reviewed_and_does_not_approve():
    proc = _proc()
    live = _state()
    proc.state_manager.get_state.return_value = live
    proc._post_gitlab_mr_reply = MagicMock(return_value=True)
    proc._comment_command = MagicMock(return_value="review")
    proc._workdir_for_issue = MagicMock(return_value=None)
    answer = (
        "### Özet\nBir sorun.\n"
        "```opencoderman-findings\n"
        '{"findings":[{"path":"a.py","start_line":4,"title":"kilit","body":"yarış"}]}\n'
        "```\n"
    )
    gl = MagicMock()
    with patch("src.gitlab.client.GitlabClient", return_value=gl), patch(
        "src.review.post.post_inline_findings", return_value=1
    ):
        JobProcessor._deliver_review_comment(
            proc, live, MagicMock(target_branch="develop"), answer, azure=False
        )
    gl.approve_merge_request.assert_not_called()
    gl.publish_reviewer_state.assert_called_once()
    assert gl.publish_reviewer_state.call_args.kwargs["state"] == "reviewed"


def test_ask_completes_the_review_without_approving():
    proc = _proc()
    live = _state()
    proc.state_manager.get_state.return_value = live
    proc._post_gitlab_mr_reply = MagicMock(return_value=True)
    proc._comment_command = MagicMock(return_value="ask")
    gl = MagicMock()
    with patch("src.gitlab.client.GitlabClient", return_value=gl), patch(
        "src.review.post.post_inline_findings"
    ) as inline:
        JobProcessor._deliver_review_comment(
            proc,
            live,
            MagicMock(),
            "### Özet\nTamam.\n```opencoderman-findings\n{\"findings\": []}\n```",
            azure=False,
        )
    body = proc._post_gitlab_mr_reply.call_args[0][1]
    assert "Tamam." in body
    inline.assert_not_called()
    gl.approve_merge_request.assert_not_called()
    gl.publish_reviewer_state.assert_called_once()
    assert gl.publish_reviewer_state.call_args.kwargs["state"] == "reviewed"


def test_azure_ask_does_not_finish_a_gitlab_review():
    proc = _proc()
    live = _state()
    proc.state_manager.get_state.return_value = live
    proc._post_azure_pr_reply = MagicMock(return_value=True)
    proc._comment_command = MagicMock(return_value="ask")
    gl = MagicMock()
    with patch("src.gitlab.client.GitlabClient", return_value=gl):
        JobProcessor._deliver_review_comment(
            proc,
            live,
            MagicMock(),
            "### Özet\nTamam.",
            azure=True,
        )
    proc._post_azure_pr_reply.assert_called_once()
    gl.approve_merge_request.assert_not_called()
    gl.publish_reviewer_state.assert_not_called()


def test_yaver_reply_sends_the_completed_review_event():
    proc = _proc()
    live = _state()
    proc._finish_gitlab_reviewer = MagicMock()
    gl = MagicMock()
    gl.post_mr_note.return_value = {"id": 9, "discussion_id": "d1"}
    with patch("src.gitlab.client.GitlabClient", return_value=gl):
        ok = JobProcessor._post_gitlab_mr_reply(
            proc, live, "*Yaver*\n\nPushed the branch.", finish_review=True
        )
    assert ok is True
    proc._finish_gitlab_reviewer.assert_called_once()
    assert proc._finish_gitlab_reviewer.call_args.kwargs["approve"] is False
