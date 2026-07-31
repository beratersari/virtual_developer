"""Main daemon for JIRA Virtual Developer."""

import asyncio
import platform
import signal
import sys
from typing import Optional

import uvicorn

from src.config import settings
from src.jira.poller import JiraPoller
from src.jira.webhook_server import create_webhook_app
from src.logger import logger
from src.processor import JobProcessor
from src.state.manager import JiraStateManager

# Check if running on Windows
IS_WINDOWS = platform.system() == "Windows"


class JiraAgentDaemon:
    """Main daemon that runs the JIRA agent service."""
    
    def __init__(self):
        self.processor = JobProcessor()
        self.state_manager = JiraStateManager()
        self._running = False
        self._webhook_server: Optional[uvicorn.Server] = None
        self._poller: Optional[JiraPoller] = None
    
    async def start(self):
        """Start the daemon."""
        # Validate configuration
        settings.validate_or_raise()
        
        logger.info("=" * 60)
        logger.info("JIRA Virtual Developer Daemon")
        logger.info("=" * 60)
        logger.info(f"Project Root: {settings.project_root}")
        logger.info(f"JIRA Host: {settings.jira_host}")
        logger.info(f"Auto-start Plans: {settings.auto_start_plans}")
        logger.info("=" * 60)
        
        self._running = True
        
        # Set up signal handlers (cross-platform)
        if IS_WINDOWS:
            # Windows: use signal.signal (synchronous handler)
            def signal_handler(sig, frame):
                asyncio.create_task(self.stop())
            
            signal.signal(signal.SIGINT, signal_handler)
            # SIGTERM may not be available on all Windows versions
            if hasattr(signal, 'SIGTERM'):
                signal.signal(signal.SIGTERM, signal_handler)
        else:
            # Unix/Linux/Mac: use add_signal_handler (async-aware)
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig, lambda: asyncio.create_task(self.stop())
                )
        
        tasks = []
        
        # Start webhook server if enabled
        #if settings.enable_webhook:
        #    print("\n📡 Starting webhook server...")
        #    webhook_task = asyncio.create_task(self._start_webhook())
        #    tasks.append(webhook_task)
        
        # Start poller if enabled
        # if settings.enable_polling: TO DO
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
        """Stop the daemon gracefully."""
        logger.info("Stopping daemon...")
        self._running = False
        
        if self._webhook_server:
            self._webhook_server.should_exit = True
        
        if self._poller:
            self._poller.stop()
        
        # Cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Daemon stopped.")
        sys.exit(0)
    
    async def _start_webhook(self):
        """Start the webhook server."""
        app = create_webhook_app(
            on_issue_created=self._on_issue_created,
            on_issue_updated=self._on_issue_updated,
            on_comment_added=self._on_comment_added,
        )
        
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=settings.webhook_port,
            log_level="info",
        )
        self._webhook_server = uvicorn.Server(config)
        await self._webhook_server.serve()
    
    async def _start_poller(self):
        """Start the JIRA poller."""
        self._poller = JiraPoller(board_id=settings.jira_board_id)
        
        # Run poller in executor since it's blocking
        loop = asyncio.get_event_loop()
        
        # Create async-safe handler that works from a different thread
        def async_handler(event):
            asyncio.run_coroutine_threadsafe(
                self.processor.process_event(event),
                loop
            )
        
        await loop.run_in_executor(
            None,
            self._poller.start,
            async_handler,
        )
    
    async def _monitor_active_issues(self):
        while self._running:
            try:
                active_issues = self.state_manager.get_active_issues()
                
                for state in active_issues:
                    if state.current_task_id and self.processor.agent_runner:
                        status = await self.processor.agent_runner.check_task_status(
                            state.current_task_id
                        )
                        
                        if status and status["status"] in ("completed", "error"):
                            logger.info(f"Task {state.current_task_id} {status['status']}")
                            
                            output = self.processor.agent_runner.read_session_output(
                                state.current_task_id
                            )
                            
                            if status["status"] == "completed":
                                from src.state.models import TaskStatus
                                self.state_manager.update_state(
                                    state.issue_key,
                                    status=TaskStatus.COMPLETED,
                                    progress_percentage=100,
                                )
                            else:
                                from src.state.models import TaskStatus
                                self.state_manager.update_state(
                                    state.issue_key,
                                    status=TaskStatus.ERROR,
                                    error_message=output[:500],
                                )
                
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"Error monitoring issues: {e}")
                await asyncio.sleep(5)
    
    def _on_issue_created(self, event: dict):
        """Handle issue created event."""
        asyncio.create_task(self.processor.process_event(event))
    
    def _on_issue_updated(self, event: dict):
        """Handle issue updated event."""
        asyncio.create_task(self.processor.process_event(event))
    
    def _on_comment_added(self, event: dict):
        """Handle comment added event."""
        asyncio.create_task(self.processor.process_event(event))


def main():
    """Entry point for the daemon."""
    daemon = JiraAgentDaemon()
    asyncio.run(daemon.start())


if __name__ == "__main__":
    main()
