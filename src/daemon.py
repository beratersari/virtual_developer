"""Main daemon for JIRA Virtual Developer."""

import asyncio
import platform
import signal
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.jira.poller import JiraPoller
from src.logger import logger
from src.processor import JobProcessor

# Check if running on Windows
IS_WINDOWS = platform.system() == "Windows"


class JiraAgentDaemon:
    """Main daemon that runs the JIRA agent service (poller-only intake)."""

    def __init__(self):
        self.processor = JobProcessor()
        # One process-wide state manager (processor owns it; dashboard + poller share)
        self.state_manager = self.processor.state_manager
        self._running = False
        self._stopping = False
        self._poller: Optional[JiraPoller] = None
        self._dashboard_server: Optional[uvicorn.Server] = None
        # Main asyncio loop used by poller thread → process_event handoff
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self):
        """Start the daemon."""
        # Validate configuration
        settings.validate_or_raise()

        # Capture the running loop once so poller workers never call
        # run_coroutine_threadsafe on a closed/stale loop reference.
        self._main_loop = asyncio.get_running_loop()

        from src.paths import agent_data_dir, ensure_agent_data_dir

        logger.info("Starting Yaver daemon")
        logger.info(f"project_root={settings.project_root}")
        logger.info(f"data_dir={agent_data_dir()}")
        logger.info(f"temp_dir_base={settings.temp_dir_base}")
        ensure_agent_data_dir(migrate=True)
        try:
            Path(settings.temp_dir_base).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create TEMP_DIR_BASE {settings.temp_dir_base}: {e}")
        logger.info(f"jira_host={settings.jira_host}")
        logger.info(f"poll_interval_seconds={settings.poll_interval_seconds}")
        intake = (
            settings.jira_intake_mode_normalized
            if hasattr(settings, "jira_intake_mode_normalized")
            else (settings.jira_intake_mode or "poll")
        )
        logger.info(f"jira_intake_mode={intake}")

        # Disk PENDING/PLANNING/EXECUTING after crash is not a live job — finalise first
        try:
            n = self.processor.recover_orphaned_in_flight()
            if n:
                logger.info(f"Startup recovery marked {n} orphaned job(s) as ERROR")
        except Exception as e:
            logger.exception(f"Startup orphan recovery failed: {e}", e)

        # Schedules left in "dispatching" after crash never become due again
        try:
            from src.scheduler.service import recover_stuck_schedules

            n = recover_stuck_schedules(max_age_seconds=0.0)
            if n:
                logger.info(
                    f"Startup recovery re-opened {n} stuck schedule(s) "
                    f"(dispatching → scheduled)"
                )
        except Exception as e:
            logger.exception(f"Startup schedule recovery failed: {e}", e)

        try:
            n = self.processor.queue_store.recover_stuck_running()
            if n:
                logger.info(f"Startup recovery re-queued {n} orphaned queue item(s)")
        except Exception as e:
            logger.exception(f"Startup queue recovery failed: {e}", e)

        # Crash recovery leaves durable rows in ``queued``. Start them now —
        # otherwise poller sees "already queued" and never dispatches.
        try:
            started = await self.processor.dispatch_queue()
            if started:
                logger.info(f"Startup queue dispatch started {started} item(s)")
        except Exception as e:
            logger.exception(f"Startup queue dispatch failed: {e}", e)

        # Age-based temp clone sweep (default: delete dirs older than 24h)
        try:
            from src.git_manager import purge_stale_temp_dirs, session_bound_workspace_paths

            purged = purge_stale_temp_dirs(
                protect_paths=session_bound_workspace_paths()
            )
            if purged:
                logger.info(f"Startup temp purge removed {purged} stale clone(s)")
        except Exception as e:
            logger.exception(f"Startup temp purge failed: {e}", e)

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
            host = (settings.dashboard_host or "0.0.0.0").strip()
            loopback = host in ("127.0.0.1", "localhost", "::1")
            if not loopback and not getattr(settings, "dashboard_allow_remote", True):
                logger.warning(
                    f"dashboard_host={host!r} is not loopback and "
                    f"DASHBOARD_ALLOW_REMOTE is false — binding 127.0.0.1 "
                    f"(dashboard has no auth)"
                )
                settings.dashboard_host = "127.0.0.1"
                host = "127.0.0.1"
            port = int(settings.dashboard_port)
            logger.info(f"Starting dashboard on http://{host}:{port}")
            if host in ("0.0.0.0", "::"):
                logger.info(
                    f"Dashboard reachable at http://127.0.0.1:{port}/ "
                    f"(and this machine's LAN IP on port {port})"
                )
            tasks.append(asyncio.create_task(self._start_dashboard()))

        # Board/sprint poller (idle when jira_intake_mode=webhook)
        logger.info("Starting JIRA poller...")
        poller_task = asyncio.create_task(self._start_poller())
        tasks.append(poller_task)

        # Start monitoring active issues
        logger.info("Starting issue monitor...")
        monitor_task = asyncio.create_task(self._monitor_active_issues())
        tasks.append(monitor_task)

        # Fire local scheduled jobs when due (Jira issue already created at schedule time)
        logger.info("Starting schedule dispatcher...")
        schedule_task = asyncio.create_task(self._run_schedule_dispatcher())
        tasks.append(schedule_task)

        # Periodic purge of temp clones older than temp_cleanup_max_age_days
        logger.info("Starting temp cleanup sweeper...")
        cleanup_task = asyncio.create_task(self._run_temp_cleanup_sweeper())
        tasks.append(cleanup_task)

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
        app.state.loop = self._main_loop or asyncio.get_running_loop()
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
        self._poller = JiraPoller(
            board_id=settings.jira_board_id,
            state_manager=self.state_manager,
        )
        # Link live poller for dashboard settings + cancel re-queue status markers
        self.processor._poller = self._poller
        self._poller._processor = self.processor
        try:
            n = self.processor.seed_poller_requeue_markers()
            if n:
                logger.info(f"Seeded poller status trackers for {n} requeue-eligible issue(s)")
        except Exception as e:
            logger.warning(f"Could not seed poller requeue markers: {e}")
        app = getattr(self, "_dashboard_app", None)
        if app is not None:
            app.state.poller = self._poller

        # Prefer the loop captured at start(); fall back to running loop here
        loop = self._main_loop or asyncio.get_running_loop()
        self._main_loop = loop

        # Create async-safe handler that works from a different thread
        def async_handler(event):
            if not self._running or self._stopping:
                self._record_dropped_accept(
                    event, "Daemon is stopping or not running."
                )
                return
            issue_key = (event.get("issue") or {}).get("key") or "unknown"
            main = self._main_loop
            if main is None:
                logger.error(
                    f"Cannot schedule process_event for {issue_key}: "
                    f"no asyncio loop captured (restart daemon)"
                )
                self._record_dropped_accept(
                    event, "No asyncio loop was captured."
                )
                return
            try:
                if main.is_closed():
                    logger.error(
                        f"Cannot schedule process_event for {issue_key}: "
                        f"asyncio event loop is closed "
                        f"(issue may sit In Progress without a job — restart daemon)"
                    )
                    self._record_dropped_accept(
                        event, "The asyncio event loop is closed."
                    )
                    return
            except Exception:
                pass
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self.processor.enqueue_jira_event(event),
                    main,
                )
            except RuntimeError as e:
                logger.error(
                    f"Failed to schedule process_event for {issue_key}: {e} "
                    f"(issue may sit In Progress without a job — restart daemon)"
                )
                self._record_dropped_accept(event, str(e))
                return

            def _on_done(f: "asyncio.Future") -> None:
                try:
                    result = f.result()
                except Exception as exc:
                    logger.exception(
                        f"process_event failed for {issue_key}: {exc}",
                        exc,
                    )
                    self._record_dropped_accept(event, str(exc))
                    return
                if isinstance(result, dict) and result.get("ok") is False:
                    self._record_dropped_accept(
                        event,
                        str(result.get("reason") or "enqueue rejected"),
                    )

            try:
                fut.add_done_callback(_on_done)
            except Exception:
                pass

        await loop.run_in_executor(
            None,
            self._poller.start,
            async_handler,
        )

    def _record_dropped_accept(self, event: dict, reason: str) -> None:
        """Jira already left To Do; persist ERROR + comment so it is not silent."""
        issue = (event or {}).get("issue") or {}
        key = (issue.get("key") or "").strip()
        if not key or key.lower() == "unknown":
            return
        fields = issue.get("fields") or {}
        summary = fields.get("summary") or key
        proc = getattr(self, "processor", None)
        if proc is None or not hasattr(proc, "record_dropped_accept"):
            logger.error(
                f"{key}: accepted but no worker and no processor to record ERROR"
            )
            return
        try:
            proc.record_dropped_accept(key, str(summary or key), reason=reason)
        except Exception as e:
            logger.warning(f"{key}: dropped-accept notify failed: {e}")

    def _abort_stuck_issue(self, state, message: str) -> None:
        """Fail issue, notify Jira, kill agent children, and release live context."""
        issue_key = state.issue_key
        try:
            self.processor._kill_children_for_issue(issue_key)
        except Exception as e:
            logger.warning(f"Could not cancel agent for stuck {issue_key}: {e}")

        self.processor._fail_issue(
            issue_key,
            message,
            suggestion=(
                "Check session logs under .jira-agent/sessions/, then "
                "move the issue back to TO DO to re-queue."
            ),
        )
        try:
            self.processor._release_context(issue_key, success=False)
        except Exception as e:
            logger.warning(f"Could not release context for stuck {issue_key}: {e}")

    async def _run_schedule_dispatcher(self):
        """Poll local schedule store and dispatch due jobs via process_event."""
        from src.scheduler.service import dispatch_due_schedules

        # Slightly faster than board poll so near-term schedules fire promptly
        interval = 15
        while self._running:
            try:
                result = await dispatch_due_schedules(processor=self.processor)
                if result.get("claimed"):
                    logger.info(
                        f"Schedule dispatch: due={result.get('due')} "
                        f"claimed={result.get('claimed')} "
                        f"launched={result.get('launched')}"
                    )
            except Exception as e:
                logger.exception(f"Schedule dispatcher tick failed: {e}", e)
            await asyncio.sleep(interval)

    async def _run_temp_cleanup_sweeper(self):
        """Delete temp clone directories older than configured max age (default 24h)."""
        from src.git_manager import purge_stale_temp_dirs

        # Hourly is enough; startup already runs one sweep
        interval = 3600
        while self._running:
            try:
                policy = (settings.temp_cleanup_policy or "age").strip().lower()
                # Always allow age purge when policy is age; also run when always
                # is set so long-lived dirs still get collected
                if policy in {"age", "always"}:
                    protect = set()
                    try:
                        for ctx in (self.processor._contexts or {}).values():
                            git = ctx.get("git") if isinstance(ctx, dict) else None
                            td = getattr(git, "temp_dir", None) if git is not None else None
                            if td:
                                protect.add(td)
                    except Exception:
                        protect = set()
                    try:
                        from src.git_manager import session_bound_workspace_paths

                        protect.update(session_bound_workspace_paths())
                    except Exception:
                        pass
                    n = purge_stale_temp_dirs(protect_paths=protect)
                    if n:
                        logger.info(f"Periodic temp purge removed {n} stale clone(s)")
            except Exception as e:
                logger.exception(f"Temp cleanup sweeper failed: {e}", e)
            await asyncio.sleep(interval)

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
                    meta = state.metadata or {}
                    extra = 0
                    try:
                        extra = max(
                            int(meta.get("max_incomplete_retries") or 0),
                            int(meta.get("max_compact_continues") or 0),
                        )
                    except (TypeError, ValueError):
                        extra = 0
                    # Allow full retry + compact-continue budget plus 50% headroom
                    from src.config import compute_stuck_limit_seconds

                    limit_seconds = compute_stuck_limit_seconds(
                        timeout, retries, extra_attempts=extra
                    )
                    # Clone phase (live context, no agent task id yet) uses git budget
                    live = False
                    try:
                        live = self.processor._is_live_processing(state.issue_key)
                    except Exception:
                        live = False
                    if live and not getattr(state, "current_task_id", None):
                        clone_budget = int(
                            getattr(settings, "git_clone_timeout_seconds", 1800) or 1800
                        )
                        limit_seconds = max(limit_seconds, clone_budget + 60)

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
