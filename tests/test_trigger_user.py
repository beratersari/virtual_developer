"""One comma-separated trigger list per provider."""

from __future__ import annotations

from src.config import Settings, format_trigger_users
from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update, build_settings_view


def test_format_trigger_users_strips_at_for_every_provider():
    assert format_trigger_users("@berat_ai, @yaver") == "berat_ai, yaver"
    assert format_trigger_users("Beratersari, @jira ai bot") == "Beratersari, jira ai bot"
    assert format_trigger_users("  @yaver ,, yaver  ") == "yaver"


def test_jira_trigger_user_comma_list_wins_over_leftover():
    s = Settings(
        jira_trigger_user="Beratersari, Jira AI Bot",
        trigger_assignee_names="old-assignee",
        trigger_mentions="@old",
    )
    assert s.jira_trigger_user_list == ["beratersari", "jira ai bot"]
    assert s.trigger_assignee_names_list == ["beratersari", "jira ai bot"]
    assert s.trigger_mentions_list == ["@beratersari", "@jira ai bot"]
    assert s.resolved_jira_trigger_user() == "Beratersari, Jira AI Bot"
    assert "@" not in s.resolved_jira_trigger_user()


def test_jira_leftover_assignee_names_still_load():
    s = Settings(jira_trigger_user="", trigger_assignee_names="devbot,jiraai")
    assert s.jira_trigger_user_list == ["devbot", "jiraai"]


def test_gitlab_trigger_user_comma_list_wins_over_leftover():
    s = Settings(
        gitlab_trigger_user="@berat_ai, DevBot",
        gitlab_bot_mentions="@old",
        gitlab_bot_usernames="legacy",
    )
    assert s.gitlab_trigger_user_list == ["berat_ai", "devbot"]
    assert s.gitlab_bot_mentions_list == ["berat_ai", "devbot"]
    assert s.gitlab_bot_usernames_list == ["berat_ai", "devbot"]
    assert s.resolved_gitlab_trigger_user() == "berat_ai, DevBot"


def test_gitlab_leftover_mentions_still_load():
    s = Settings(gitlab_trigger_user="", gitlab_bot_mentions="@BotOne, bot-two")
    assert s.gitlab_trigger_user_list == ["botone", "bot-two"]


def test_azure_trigger_user_comma_list_wins_over_leftover():
    s = Settings(
        azure_trigger_user="@yaver, @devbot",
        azure_bot_mentions="@old",
    )
    assert s.azure_trigger_user_list == ["yaver", "devbot"]
    assert s.azure_bot_mentions_list == ["yaver", "devbot"]
    assert s.resolved_azure_trigger_user() == "yaver, devbot"


def test_azure_leftover_mentions_still_load():
    s = Settings(azure_trigger_user="", azure_bot_mentions="@yaver, devbot")
    assert s.azure_trigger_user_list == ["yaver", "devbot"]


def test_settings_view_and_patch_use_new_keys(tmp_path, monkeypatch):
    s = Settings(
        jira_trigger_user="",
        gitlab_trigger_user="",
        azure_trigger_user="",
        trigger_assignee_names="old-jira",
        gitlab_bot_mentions="@old-gl",
        azure_bot_mentions="@old-az",
    )
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: tmp_path / "runtime_settings.json",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None)
    monkeypatch.setattr("src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None)

    shown = build_settings_view()
    assert shown.jira_trigger_user == "old-jira"
    assert shown.gitlab_trigger_user == "old-gl"
    assert shown.azure_trigger_user == "old-az"

    view = apply_settings_update(
        SettingsUpdate(
            jira_trigger_user="Alice, Bob",
            gitlab_trigger_user="gl_bot, other",
            azure_trigger_user="az_bot, tfs_bot",
        )
    )
    assert view.jira_trigger_user == "Alice, Bob"
    assert view.trigger_assignee_names == "Alice, Bob"
    assert view.gitlab_trigger_user == "gl_bot, other"
    assert view.gitlab_bot_mentions == "gl_bot, other"
    assert view.azure_trigger_user == "az_bot, tfs_bot"
    assert view.azure_bot_mentions == "az_bot, tfs_bot"
    assert s.jira_trigger_user_list == ["alice", "bob"]
    assert s.gitlab_trigger_user_list == ["gl_bot", "other"]
    assert s.azure_trigger_user_list == ["az_bot", "tfs_bot"]


def test_leftover_settings_patch_still_writes_new_fields(tmp_path, monkeypatch):
    from src.config import settings as live

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: tmp_path / "runtime_settings.json",
    )
    monkeypatch.setattr("src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None)
    monkeypatch.setattr("src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None)
    monkeypatch.setattr(live, "jira_trigger_user", "")
    monkeypatch.setattr(live, "gitlab_trigger_user", "")
    monkeypatch.setattr(live, "azure_trigger_user", "")
    view = apply_settings_update(
        SettingsUpdate(
            trigger_assignee_names="Beratersari",
            gitlab_bot_mentions="new_bot",
            azure_bot_mentions="@yaver",
        )
    )
    assert view.jira_trigger_user == "Beratersari"
    assert view.gitlab_trigger_user == "new_bot"
    assert view.azure_trigger_user == "yaver"
    assert live.jira_trigger_user == "Beratersari"
    assert live.gitlab_trigger_user == "new_bot"
    assert live.azure_trigger_user == "yaver"
