"""In-process tests for simulated_gitlab_server (CE notes API + hook helper)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import simulated_gitlab_server as sim


@pytest.fixture
def sim_client():
    sim.reset_demo_data()
    sim.DAEMON_WEBHOOK = "http://127.0.0.1:8080/webhooks/gitlab"
    sim.WEBHOOK_SECRET = "secret"
    return sim.app.test_client()


def test_sim_lists_seed_project(sim_client):
    r = sim_client.get("/api/v4/projects")
    assert r.status_code == 200
    rows = r.get_json()
    assert rows[0]["path_with_namespace"] == "acme/demo"


def test_sim_post_note_api(sim_client):
    r = sim_client.post(
        "/api/v4/projects/1/merge_requests/1/notes",
        json={"body": "*Virtual Developer*\n\nhello"},
    )
    assert r.status_code == 201
    note = r.get_json()
    assert "hello" in note["body"]
    listed = sim_client.get("/api/v4/projects/1/merge_requests/1/notes").get_json()
    assert any(n["id"] == note["id"] for n in listed)


def test_sim_comment_fires_note_hook(sim_client):
    captured = {}

    class FakeResp:
        status_code = 200
        text = '{"ok": true}'

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

    with patch.object(sim.httpx, "Client", FakeClient):
        r = sim_client.post(
            "/simulate/comment",
            json={
                "project_id": 1,
                "mr_iid": 1,
                "body": "@berat_ai explain login",
                "username": "alice",
            },
        )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert captured["url"].endswith("/webhooks/gitlab")
    assert captured["headers"]["X-Gitlab-Event"] == "Note Hook"
    assert captured["headers"]["X-Gitlab-Token"] == "secret"
    assert captured["json"]["object_kind"] == "note"
    assert captured["json"]["object_attributes"]["noteable_type"] == "MergeRequest"
    assert "@berat_ai" in captured["json"]["object_attributes"]["note"]
    assert captured["json"]["merge_request"]["source_branch"] == "feature/login"
