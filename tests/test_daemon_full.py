"""Coverage for JiraAgentDaemon."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_start_and_stop_unix():
    from src.daemon import JiraAgentDaemon

    with patch("src.daemon.settings") as s:
        s.validate_or_raise = MagicMock()
        s.project_root = "/tmp"
        s.jira_host = "http://j"
        s.auto_start_plans = False
        s.jira_board_id = "1"
        s.webhook_port = 3000
        s.enable_webhook = False

        daemon = JiraAgentDaemon()
        daemon.processor = MagicMock()
        daemon.state_manager = MagicMock()
        daemon.state_manager.get_active_issues.return_value = []

        async def fake_poller():
            await asyncio.sleep(0.01)

        async def fake_monitor():
            daemon._running = False
            await asyncio.sleep(0.01)

        with patch.object(daemon, "_start_poller", side_effect=fake_poller):
            with patch.object(daemon, "_monitor_active_issues", side_effect=fake_monitor):
                with patch("src.daemon.IS_WINDOWS", False):
                    with patch("asyncio.get_event_loop") as gel:
                        loop = MagicMock()
                        gel.return_value = loop
                        # start will gather tasks - monitor stops running
                        # Actually _running set True then gather waits forever
                        # Better approach: patch gather
                        with patch("asyncio.gather", new_callable=AsyncMock) as gather:
                            gather.return_value = None
                            await daemon.start()
                            assert daemon._running is True


@pytest.mark.asyncio
async def test_stop_cancels_tasks():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._webhook_server = MagicMock()
    daemon._poller = MagicMock()

    with patch("sys.exit") as ex:
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                ex.assert_called_with(0)
    assert daemon._running is False
    daemon._webhook_server.should_exit = True
    daemon._poller.stop.assert_called()


@pytest.mark.asyncio
async def test_start_webhook_and_poller():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon.processor = MagicMock()
    daemon.processor.process_event = AsyncMock()

    with patch("src.daemon.create_webhook_app", return_value=MagicMock()):
        with patch("src.daemon.uvicorn.Server") as Srv:
            srv = MagicMock()
            srv.serve = AsyncMock()
            Srv.return_value = srv
            with patch("src.daemon.settings") as s:
                s.webhook_port = 3000
                await daemon._start_webhook()
            assert daemon._webhook_server is srv

    with patch("src.daemon.JiraPoller") as Poller:
        poller = MagicMock()
        Poller.return_value = poller
        with patch("src.daemon.settings") as s:
            s.jira_board_id = "1"
            with patch("asyncio.get_event_loop") as gel:
                loop = MagicMock()
                loop.run_in_executor = AsyncMock(return_value=None)
                gel.return_value = loop
                await daemon._start_poller()
                # invoke async_handler
                handler = poller.start.call_args[0][0] if poller.start.call_args else None
                # start is called as run_in_executor(None, self._poller.start, async_handler)
                # so args are on executor call
                call_args = loop.run_in_executor.call_args
                assert call_args is not None
                async_handler = call_args[0][2]
                async_handler({"webhookEvent": "x"})


def test_webhook_callbacks_create_tasks():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon.processor = MagicMock()
    daemon.processor.process_event = AsyncMock()

    with patch("asyncio.create_task") as ct:
        daemon._on_issue_created({"a": 1})
        daemon._on_issue_updated({"b": 2})
        daemon._on_comment_added({"c": 3})
        assert ct.call_count == 3


def test_main_entry():
    from src import daemon as daemon_mod

    with patch.object(daemon_mod, "JiraAgentDaemon") as D:
        inst = MagicMock()
        inst.start = AsyncMock()
        D.return_value = inst
        with patch("asyncio.run") as ar:
            daemon_mod.main()
            ar.assert_called()
