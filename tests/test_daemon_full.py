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
        s.jira_board_id = "1"
        s.poll_interval_seconds = 30

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
                        with patch("asyncio.gather", new_callable=AsyncMock) as gather:
                            gather.return_value = None
                            await daemon.start()
                            assert daemon._running is True


@pytest.mark.asyncio
async def test_stop_cancels_tasks():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = MagicMock()
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=0)

    with patch("sys.exit") as ex:
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                ex.assert_called_with(0)
    assert daemon._running is False
    daemon._poller.stop.assert_called()
    daemon.processor.shutdown_processing.assert_called_once()


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = MagicMock()
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=0)

    with patch("sys.exit"):
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                await daemon.stop()  # second call no-ops
    assert daemon.processor.shutdown_processing.call_count == 1

@pytest.mark.asyncio
async def test_start_poller_schedules_handler():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon.processor = MagicMock()
    daemon.processor.process_event = AsyncMock()

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
                call_args = loop.run_in_executor.call_args
                assert call_args is not None
                async_handler = call_args[0][2]
                async_handler({"webhookEvent": "x"})


def test_main_entry():
    from src import daemon as daemon_mod

    with patch.object(daemon_mod, "JiraAgentDaemon") as D:
        inst = MagicMock()
        inst.start = AsyncMock()
        D.return_value = inst
        with patch("asyncio.run") as ar:
            daemon_mod.main()
            ar.assert_called()
