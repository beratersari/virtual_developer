"""Main daemon for JIRA Virtual Developer."""

import asyncio
import platform
import signal
import sys
from typing import Optional

import uvicorn

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.jira.poller import JiraPoller
from src.logger import logger
from src.processor import JobProcessor
from src.state.manager import JiraStateManager

# Check if running on Windows
IS_WINDOWS = platform.system() == "Windows"


class JiraAgentDaemon:
    """Main daemon that runs the JIRA agent service (poller-only intake)."""

    def __init__(self):
        self.processor = JobProcessor()
        self.state_manager = JiraStateManager()
        self._running = False
        self._stopping = False
        self._poller: Optional[JiraPoller] = None
        self._dashboard_server: Optional[uvicorn.Server] = None

    async def start(self):
        """Start the daemon."""
        # Validate configuration
        settings.validate_or_raise()

        logger.info("Starting JIRA Virtual Developer daemon")
        logger.info(f"project_root={settings.project_root}")
        logger.info(f"jira_host={settings.jira_host}")
        logger.info(f"auto_start_plans={settings.auto_start_plans}")
        logger.info(f"poll_interval_seconds={settings.poll_interval_seconds}")

        # Disk PLANNING/EXECUTING after crash is not a live job — finalise first
        try:
            n = self.processor.recover_orphaned_in_flight()
            if n:
                logger.info(f"Startup recovery marked {n} orphaned job(s) as ERROR")
        except Exception as e:
            logger.exception(f"Startup orphan recovery failed: {e}", e)

        self._running = True
        self._stopping = False

        # Build dashboard app once so poller can attach for live settings
        self._dashboard_app = None
        if getattr(settings, "dashboard_enabled", True):
            self._dashboard_app = create_dashboard_app(
                processor=self.processor,
                state_manager=self.state_manager,
            )

        # Set up signal handlers (cross-platform)
        if IS_WINDOWS:
            # Windows: use signal.signal (synchronous handler)
            def signal_handler(sig, frame):
                asyncio.create_task(self.stop())

            signal.signal(signal.SIGINT, signal_handler)
            # SIGTERM may not be available on all Windows versions
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, signal_handler)
        else:
            # Unix/Linux/Mac: use add_signal_handler (async-aware)
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig, lambda: asyncio.create_task(self.stop())
                )

        tasks = []

        # Ops dashboard (REST + WebSocket)
        if getattr(settings, "dashboard_enabled", True):
            logger.info(
                f"Starting dashboard on "
                f"http://{settings.dashboard_host}:{settings.dashboard_port}"
            )
            tasks.append(asyncio.create_task(self._start_dashboard()))

        # Board/sprint poller is the sole issue intake path
        logger.info("Starting JIRA poller...")
        poller_task = asyncio.create_task(self._start_poller())
        tasks.append(poller_task)

        # Start monitoring active issues
        logger.info("Starting issue monitor...")
        monitor_task = asyncio.create_task(self._monitor_active_issues())
        tasks.append(monitor_task)

        logger.info("Daemon started. Press Ctrl+C to stop.")

        # Wait for all tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Stop the daemon gracefully: kill children, finalise in-flight, exit."""
        if self._stopping:
            return
        self._stopping = True
        logger.info("Stopping daemon...")
        self._running = False

        if self._poller:
            try:
                self._poller.stop()
            except Exception as e:
                logger.warning(f"Poller stop failed: {e}")

        if self._dashboard_server:
            self._dashboard_server.should_exit = True

        # Kill agent subprocesses and write CANCELLED before tearing down asyncio
        try:
            self.processor.shutdown_processing(reason="Daemon stopped (interrupt or shutdown)")
        except Exception as e:
            logger.exception(f"Processing shutdown failed: {e}", e)

        # Cancel remaining asyncio tasks (poller thread future, monitor, jobs)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Daemon stopped.")
        sys.exit(0)

    async def _start_dashboard(self):
        """Serve FastAPI dashboard (same process as poller/jobs)."""
        app = self._dashboard_app or create_dashboard_app(
            processor=self.processor,
            state_manager=self.state_manager,
        )
        self._dashboard_app = app
        config = uvicorn.Config(
            app,
            host=settings.dashboard_host,
            port=int(settings.dashboard_port),
            log_level="warning",
            loop="asyncio",
        )
        self._dashboard_server = uvicorn.Server(config)
        await self._dashboard_server.serve()

    async def _start_poller(self):
        """Start the JIRA poller."""
        self._poller = JiraPoller(board_id=settings.jira_board_id)
        # Link live poller for dashboard settings + cancel re-queue status markers
        self.processor._poller = self._poller
        app = getattr(self, "_dashboard_app", None)
        if app is not None:
            app.state.poller = self._poller

        # Run poller in executor since it's blocking
        loop = asyncio.get_event_loop()

        # Create async-safe handler that works from a different thread
        def async_handler(event):
            if not self._running or self._stopping:
                return
            asyncio.run_coroutine_threadsafe(
                self.processor.process_event(event),
                loop,
            )

        await loop.run_in_executor(
            None,
            self._poller.start,
            async_handler,
        )

    def _abort_stuck_issue(self, state, message: str) -> None:
        """Fail issue, notify Jira, and best-effort kill the agent process."""
        # Try to stop the live agent before marking ERROR so success path cannot
        # overwrite after the watchdog fires.
        try:
            runner = self.processor._runner_for(state.issue_key)
            if runner and state.current_task_id:
                runner.cancel_task(state.current_task_id)
        except Exception as e:
            logger.warning(f"Could not cancel agent for stuck {state.issue_key}: {e}")

        self.processor._fail_issue(
            state.issue_key,
            message,
            suggestion=(
                "Check session logs under .jira-agent/sessions/, then "
                "move the issue back to TO DO to re-queue."
            ),
        )

    async def _monitor_active_issues(self):
        """Watch for stuck in-flight issues and report them to Jira.

        Foreground workflows await the agent directly, so process-status polling
        is not used. Instead we enforce a wall-clock stuck timeout based on
        started_at + timeout_seconds * retries, then ERROR + Jira post_error.
        """
        from datetime import datetime

        from src.config import settings
        from src.state.models import TaskStatus

        in_flight = {
            TaskStatus.PLANNING,
            TaskStatus.EXECUTING,
        }

        while self._running:
            try:
                active_issues = self.state_manager.get_active_issues()

                for state in active_issues:
                    if state.status not in in_flight:
                        continue

                    timeout = state.timeout_seconds or settings.agent_task_timeout_seconds
                    # Treat falsy 0 as a real zero retries; only None falls back to settings
                    retries = (
                        state.max_retries
                        if state.max_retries is not None
                        else settings.agent_task_max_retries
                    )
                    # Allow full retry budget plus 50% headroom for backoff/overhead
                    limit_seconds = timeout * (retries + 1) * 1.5

                    # Missing started_at must not leave jobs stuck forever.
                    if not state.started_at:
                        logger.error(
                            f"Issue {state.issue_key} in-flight with no started_at; "
                            f"marking ERROR"
                        )
                        self._abort_stuck_issue(
                            state,
                            (
                                f"Job stuck in '{state.status.value}' with no start timestamp. "
                                f"Marking as error so it can be re-queued."
                            ),
                        )
                        continue

                    age = (datetime.now() - state.started_at).total_seconds()

                    if age <= limit_seconds:
                        continue

                    logger.error(
                        f"Issue {state.issue_key} stuck in {state.status.value} "
                        f"for {int(age)}s (limit {int(limit_seconds)}s)"
                    )
                    self._abort_stuck_issue(
                        state,
                        (
                            f"Job stuck in '{state.status.value}' for {int(age)}s "
                            f"(limit {int(limit_seconds)}s). The agent may have hung "
                            f"or the daemon may have lost the process."
                        ),
                    )

                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error monitoring issues: {e}")
                await asyncio.sleep(5)


def main():
    """Entry point for the daemon."""
    daemon = JiraAgentDaemon()
    asyncio.run(daemon.start())


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
