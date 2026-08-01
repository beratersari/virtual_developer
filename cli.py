#!/usr/bin/env python3
"""CLI for JIRA Virtual Developer."""

import os
import sys
from pathlib import Path
from typing import Optional

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import click
from rich.console import Console
from rich.table import Table

from src import __version__
from src.config import settings
from src.daemon import JiraAgentDaemon
from src.state.manager import JiraStateManager

console = Console()


def validate_config():
    """Validate configuration and exit with helpful message if not configured."""
    if not settings.is_configured():
        console.print("[red]Error: JIRA configuration missing![/red]")
        console.print("\n[yellow]Please configure your JIRA credentials:[/yellow]")
        console.print("1. Copy the example file: [green]cp .env.example .env[/green]")
        console.print("2. Edit [green].env[/green] and add your JIRA credentials:")
        console.print("   - JIRA_HOST=https://your-jira.example.com")
        console.print("   - JIRA_API_TOKEN=your-api-token")
        console.print("\nGet your API token from: https://id.atlassian.com/manage-profile/security/api-tokens")
        sys.exit(1)


@click.group()
@click.version_option(version=__version__)
def cli():
    """JIRA Virtual Developer - AI Agent Integration for JIRA."""
    pass


@cli.command()
def start():
    """Start the JIRA agent daemon."""
    validate_config()
    daemon = JiraAgentDaemon()
    try:
        import asyncio
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")


@cli.command()
@click.argument("issue_key")
@click.option("--agent", "-a", default=None, help="Override agent (sisyphus, prometheus, atlas)")
@click.option("--dry-run", is_flag=True, help="Show what would be done without running")
def process(issue_key: str, agent: Optional[str], dry_run: bool):
    """Process a specific JIRA issue manually.
    
    Examples:
        python cli.py process SIM-1008
        python cli.py process SIM-1008 --agent sisyphus
        python cli.py process SIM-1008 --dry-run
    """
    validate_config()
    from src.processor import JobProcessor
    from src.jira.client import JiraClient
    
    console.print(f"[blue]Processing issue: {issue_key}[/blue]")
    
    with JiraClient() as client:
        issue = client.get_issue(issue_key)
        if not issue:
            console.print(f"[red]Issue {issue_key} not found[/red]")
            console.print("\n[yellow]Tips:[/yellow]")
            console.print("  - Check if the issue key is correct")
            console.print("  - Verify your API token has permissions")
            console.print(f"  - JIRA_HOST: {settings.jira_host}")
            return
        
        fields = issue.get("fields", {})
        console.print(f"[green]✓ Found issue:[/green]")
        console.print(f"  Summary: {fields.get('summary', 'N/A')}")
        console.print(f"  Status: {fields.get('status', {}).get('name', 'N/A')}")
        console.print(f"  Assignee: {fields.get('assignee', {}).get('displayName', 'Unassigned')}")
        console.print(f"  Labels: {', '.join(fields.get('labels', [])) or 'None'}")
        
        if dry_run:
            console.print("\n[yellow]Dry run - not processing[/yellow]")
            return
        
        event = {
            "webhookEvent": "jira:issue_created",
            "issue": issue,
        }
        
        processor = JobProcessor()
        import asyncio
        asyncio.run(processor.process_event(event))


def format_progress_bar(percentage: int, width: int = 10) -> str:
    """Create a visual progress bar."""
    filled = int((percentage / 100) * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}] {percentage}%"


@cli.command()
def status():
    """Show status of active issues."""
    manager = JiraStateManager()
    active = manager.get_active_issues()
    
    if not active:
        console.print("[yellow]No active issues[/yellow]")
        return
    
    table = Table(title="Active Issues")
    table.add_column("Issue Key", style="cyan")
    table.add_column("Summary", style="white")
    table.add_column("Status", style="green")
    table.add_column("Progress", style="blue")
    table.add_column("Started", style="dim")
    
    for state in active:
        progress_display = format_progress_bar(state.progress_percentage)
        table.add_row(
            state.issue_key,
            state.issue_summary[:40] + "..." if len(state.issue_summary) > 40 else state.issue_summary,
            state.status.value,
            progress_display,
            state.started_at.strftime("%Y-%m-%d %H:%M") if state.started_at else "N/A",
        )
    
    console.print(table)
    console.print(f"\n[dim]Total: {len(active)} active issue(s)[/dim]")
    console.print("[dim]Run 'python cli.py show <issue_key>' for details[/dim]")


@cli.command()
@click.argument("issue_key")
def show(issue_key: str):
    """Show details for a specific issue."""
    manager = JiraStateManager()
    state = manager.get_state(issue_key)
    
    if not state:
        console.print(f"[red]No state found for {issue_key}[/red]")
        return
    
    console.print(f"\n[bold cyan]{issue_key}[/bold cyan]")
    console.print(f"Summary: {state.issue_summary}")
    console.print(f"Status: {state.status.value}")
    console.print(f"Progress: {format_progress_bar(state.progress_percentage)}")
    console.print(f"Started: {state.started_at}")
    
    if state.completed_at:
        console.print(f"Completed: {state.completed_at}")
        if state.execution_duration_seconds:
            duration = state.execution_duration_seconds
            if duration > 0:
                console.print(f"Duration: {duration:.1f} seconds")
    
    console.print(f"Task ID: {state.current_task_id}")
    console.print(f"Plan Path: {state.plan_path}")
    
    # Cost info
    if state.estimated_cost and state.estimated_cost > 0:
        console.print(f"\n[bold]💰 Cost Information:[/bold]")
        console.print(f"  Input tokens:  {state.token_usage_input:,}")
        console.print(f"  Output tokens: {state.token_usage_output:,}")
        console.print(f"  Total tokens:  {state.token_usage_input + state.token_usage_output:,}")
        console.print(f"  Est. cost:     ${state.estimated_cost:.4f}")
    
    if state.error_message:
        console.print(f"\n[bold red]Error:[/bold red]")
        console.print(state.error_message[:500])


@cli.command()
@click.argument("issue_key")
def cancel(issue_key: str):
    """Cancel processing for an issue."""
    from src.state.models import TaskStatus
    
    manager = JiraStateManager()
    state = manager.get_state(issue_key)
    
    if not state:
        console.print(f"[red]No state found for {issue_key}[/red]")
        return
    
    # Cancel running task
    if state.current_task_id:
        from src.orchestrator.agent_runner import AgentRunner
        runner = AgentRunner()
        runner.cancel_task(state.current_task_id)
    
    # Update state
    manager.update_state(issue_key, status=TaskStatus.CANCELLED)
    console.print(f"[green]Cancelled {issue_key}[/green]")


@cli.command()
def costs():
    """Show cost summary across all issues."""
    from pathlib import Path
    import json
    
    state_dir = Path("state")
    if not state_dir.exists():
        console.print("[yellow]No state directory found[/yellow]")
        return
    
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    total_duration = 0.0
    issue_count = 0
    
    for state_file in state_dir.glob("*.json"):
        try:
            with open(state_file) as f:
                data = json.load(f)
                total_input_tokens += data.get("token_usage_input", 0)
                total_output_tokens += data.get("token_usage_output", 0)
                total_cost += data.get("estimated_cost", 0.0)
                total_duration += data.get("execution_duration_seconds", 0.0)
                issue_count += 1
        except (json.JSONDecodeError, KeyError):
            continue
    
    if issue_count == 0:
        console.print("[yellow]No completed issues with cost data found[/yellow]")
        return
    
    table = Table(title="💰 Cost Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    
    table.add_row("Total Issues", str(issue_count))
    table.add_row("Total Duration", f"{total_duration:.1f}s ({total_duration/60:.1f}m)")
    table.add_row("Input Tokens", f"{total_input_tokens:,}")
    table.add_row("Output Tokens", f"{total_output_tokens:,}")
    table.add_row("Total Tokens", f"{total_input_tokens + total_output_tokens:,}")
    table.add_row("Estimated Cost", f"${total_cost:.4f}")
    table.add_row("Avg Cost/Issue", f"${total_cost/issue_count:.4f}" if issue_count > 0 else "$0.00")
    
    console.print(table)


@cli.command()
def config():
    """Show current configuration."""
    table = Table(title="Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")
    
    table.add_row("JIRA Host", settings.jira_host)
    table.add_row("Projects", ", ".join(settings.jira_projects_list))
    table.add_row("Default Agent", settings.default_agent)
    table.add_row("Planning Agent", settings.planning_agent)
    table.add_row("Orchestrator Agent", settings.orchestrator_agent)
    table.add_row("Auto-start Plans", str(settings.auto_start_plans))
    table.add_row("Max Concurrent Jobs", str(settings.max_concurrent_jobs))
    table.add_row("Poll Interval (s)", str(settings.poll_interval_seconds))
    table.add_row("Board ID", settings.jira_board_id or "(not set)")
    
    console.print(table)


@cli.command()
def init():
    """Initialize the project structure."""
    dirs = [
        settings.state_dir,
        settings.project_root / ".jira-agent" / "sessions",
        settings.full_plans_dir,
        Path("logs"),
    ]
    
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]Created:[/green] {d}")
    
    # Create .env if it doesn't exist
    env_file = Path(".env")
    if not env_file.exists():
        example = Path(".env.example")
        if example.exists():
            import shutil
            shutil.copy(example, env_file)
            console.print(f"[green]Created:[/green] {env_file} (from example)")
    
    console.print("\n[bold green]Initialization complete![/bold green]")
    console.print("\nNext steps:")
    console.print("1. Edit .env with your JIRA credentials")
    console.print("2. Run: python cli.py start")


@cli.command()
@click.option("--project", "-p", default="sample_project", help="Project directory to work on")
@click.option("--title", "-t", required=True, help="Issue title/summary")
@click.option("--description", "-d", required=True, help="Issue description")
@click.option("--agent", "-a", default="sisyphus", help="Agent to use (sisyphus, prometheus, atlas, oracle)")
@click.option("--category", "-c", help="Category for task (quick, deep, visual-engineering, etc.)")
@click.option("--plan-only", is_flag=True, help="Only create a plan (Prometheus), don't execute")
@click.option("--dry-run", is_flag=True, help="Show what would be done without running agent")
@click.option("--model", "-m", default=None, help="Override DEFAULT_MODEL for this run")
@click.option("--timeout", default=None, type=int, help="Agent timeout seconds (default from settings)")
def test_issue(
    project: str,
    title: str,
    description: str,
    agent: str,
    category: Optional[str],
    plan_only: bool,
    dry_run: bool,
    model: Optional[str],
    timeout: Optional[int],
):
    """Test the agent with a simulated issue (no JIRA required).
    
    Examples:
        # Fix bugs in sample calculator
        python cli.py test-issue \\
            --title "Fix calculator bugs" \\
            --description "Fix all bugs in calculator/calc.py"
        
        # Plan a new feature
        python cli.py test-issue \\
            --title "Add logging" \\
            --description "Add logging to all calculator methods" \\
            --plan-only
        
        # Use specific category
        python cli.py test-issue \\
            --title "Update UI" \\
            --description "Make it look better" \\
            --category visual-engineering
    """
    import asyncio
    from datetime import datetime
    from src.orchestrator.agent_runner import AgentRunner, AgentTask
    from src.orchestrator.prompt_builder import PromptBuilder
    from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
    from src.state.manager import JiraStateManager
    from src.state.models import JiraAgentState, TaskStatus
    
    # Generate a fake issue key
    issue_key = f"TEST-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    console.print(f"\n[bold cyan]Testing Agent - {issue_key}[/bold cyan]")
    console.print(f"Project: {project}")
    console.print(f"Title: {title}")
    console.print(f"Description: {description}")
    console.print(f"Agent: {agent}")
    if category:
        console.print(f"Category: {category}")
    console.print()
    
    if dry_run:
        console.print("[yellow]Dry run - not executing agent[/yellow]")
        return
    
    # Create state
    state_manager = JiraStateManager()
    state = state_manager.create_state(
        issue_key=issue_key,
        issue_summary=title,
        description=description,
        triggered_by="test-command",
    )
    
    # Determine workflow
    workflow = WorkflowRouter.route_issue(issue_key, title, description)
    console.print(f"Detected workflow: [green]{workflow.value}[/green]\n")
    
    async def run_agent():
        project_path = Path(project).resolve()
        if not project_path.exists():
            console.print(f"[red]Project path does not exist: {project_path}[/red]")
            raise SystemExit(1)
        runner = AgentRunner(working_directory=project_path)
        
        if plan_only or workflow == WorkflowType.PLANNING:
            # Planning workflow
            console.print("[blue]Starting planning workflow...[/blue]")
            state.status = TaskStatus.PLANNING
            state_manager.set_state(state)
            
            prompt = PromptBuilder.build_prometheus_prompt(
                issue_key=issue_key,
                summary=title,
                description=description,
            )
            task = AgentTask(
                description=f"Plan: {title}",
                prompt=prompt,
                agent="prometheus",
                issue_key=issue_key,
                model=model,
            )
        elif agent == "oracle":
            # Oracle consultation
            console.print("[blue]Starting Oracle consultation...[/blue]")
            prompt = PromptBuilder.build_oracle_consult_prompt(
                question=description,
            )
            task = AgentTask(
                description=f"Consult: {title}",
                prompt=prompt,
                agent="oracle",
                issue_key=issue_key,
                model=model,
            )
        else:
            # Direct execution
            console.print("[blue]Starting direct execution...[/blue]")
            state.status = TaskStatus.EXECUTING
            state_manager.set_state(state)
            
            prompt = PromptBuilder.build_sisyphus_prompt(
                issue_key=issue_key,
                task_description=description,
            )
            task = AgentTask(
                description=f"Execute: {title}",
                prompt=prompt,
                agent=agent,
                category=category or settings.execution_category,
                issue_key=issue_key,
                model=model,
            )
        
        effective_timeout = timeout if timeout is not None else settings.agent_task_timeout_seconds
        if model:
            console.print(f"[dim]Model override: {model}[/dim]")
        console.print(f"[dim]Timeout: {effective_timeout}s[/dim]")

        # Run the agent
        result = await runner.run_agent(
            task,
            on_output=lambda stream, line: console.print(f"[{stream}] {line}"),
            timeout_seconds=effective_timeout,
        )
        
        # Show results
        console.print("\n" + "=" * 60)
        if result["returncode"] == 0:
            console.print("[bold green]✓ Agent completed successfully[/bold green]")
            state.status = TaskStatus.COMPLETED
            state.progress_percentage = 100
        else:
            console.print("[bold red]✗ Agent failed[/bold red]")
            state.status = TaskStatus.ERROR
            state.error_message = result["stderr"][:500]
        
        state.completed_at = datetime.now()
        state_manager.set_state(state)
        
        console.print(f"\nReturn code: {result['returncode']}")
        console.print(f"Session log: {result.get('session_file', 'N/A')}")
        
        if result["stdout"]:
            console.print("\n[bold]Output:[/bold]")
            console.print(result["stdout"][:2000])  # Limit output
        
        if result["stderr"] and result["returncode"] != 0:
            console.print("\n[bold red]Errors:[/bold red]")
            console.print(result["stderr"][:1000])
    
    asyncio.run(run_agent())
    
    console.print(f"\n[dim]State saved in: {settings.state_dir}[/dim]")


@cli.group()
def simulate():
    """Simulated JIRA server commands for testing."""
    pass


@simulate.command()
@click.option("--port", "-p", default=7001, help="Server port")
def start_server(port: int):
    """Start the simulated JIRA server (in-memory REST; no push to daemon)."""
    import subprocess
    import sys

    console.print(f"[bold green]Starting Simulated JIRA Server on port {port}...[/bold green]")
    console.print("[dim]In-memory issue store only — daemon intake is board poller[/dim]")
    console.print()

    try:
        subprocess.run([sys.executable, "simulated_jira_server.py", str(port)])
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped[/yellow]")


@simulate.command()
@click.option("--summary", "-s", required=True, help="Issue summary/title")
@click.option("--description", "-d", required=True, help="Issue description")
@click.option("--assignee", "-a", default="DevBot", help="Assignee username")
@click.option("--labels", "-l", default="ai-assist", help="Comma-separated labels")
@click.option("--issue-key", "-k", help="Manual issue key to use (e.g., KAN-9683)")
@click.option("--server", default="http://localhost:7001", help="Simulated JIRA server URL")
@click.option("--no-comment", is_flag=True, help="Skip adding comments to real Jira")
def create_issue(summary: str, description: str, assignee: str, labels: str, issue_key: Optional[str], server: str, no_comment: bool):
    """Create a new issue in the simulated JIRA (in-memory store only).

    Use --issue-key to specify a real Jira issue key (must exist in real Jira).
    To run agent work, use ``cli.py process`` or put the issue on a polled board.

    Examples:
        python cli.py simulate create-issue -s "Title" -d "Desc"
        python cli.py simulate create-issue -s "Title" -d "Desc" -k KAN-9683
        python cli.py simulate create-issue -s "Title" -d "Desc" -k KAN-9683 --no-comment
    """
    from src.jira.simulated_client import SimulatedJiraClient

    labels_list = [l.strip() for l in labels.split(",") if l.strip()]

    with SimulatedJiraClient(base_url=server) as client:
        issue = client.create_issue(
            summary=summary,
            description=description,
            assignee=assignee,
            labels=labels_list,
            key=issue_key,
        )

        if issue:
            console.print(f"[bold green]✓ Created issue: {issue['key']}[/bold green]")
            console.print(f"Summary: {issue['summary']}")
            console.print(f"Status: {issue['status']}")
            console.print(f"Assignee: {issue['assignee']}")
            if issue_key:
                console.print(f"[yellow]⚠ Using manual key: {issue_key}[/yellow]")
                console.print(f"[dim]Comments will be posted to real Jira issue {issue_key}[/dim]")
                if no_comment:
                    console.print(f"[dim]Comments disabled with --no-comment[/dim]")
            console.print()
            console.print("[dim]Issue stored in simulated server only — use poller or cli.py process[/dim]")
        else:
            console.print("[bold red]✗ Failed to create issue[/bold red]")


@simulate.command()
@click.option("--server", default="http://localhost:7001", help="Simulated JIRA server URL")
def list_issues(server: str):
    """List all issues in the simulated JIRA."""
    from src.jira.simulated_client import SimulatedJiraClient
    from rich.table import Table
    
    with SimulatedJiraClient(base_url=server) as client:
        issues = client.list_issues()
        
        if not issues:
            console.print("[yellow]No issues found[/yellow]")
            return
        
        table = Table(title="Simulated JIRA Issues")
        table.add_column("Key", style="cyan")
        table.add_column("Summary", style="white")
        table.add_column("Status", style="green")
        table.add_column("Assignee", style="blue")
        table.add_column("Created", style="dim")
        
        for issue in issues:
            table.add_row(
                issue['key'],
                issue['summary'][:40] + "..." if len(issue['summary']) > 40 else issue['summary'],
                issue['status'],
                issue['assignee'] or "Unassigned",
                issue['created'][:10] if issue['created'] else "N/A",
            )
        
        console.print(table)
        console.print(f"\nTotal: {len(issues)} issues")


@simulate.command()
@click.argument("key")
@click.option("--server", default="http://localhost:7001", help="Simulated JIRA server URL")
def show_issue(key: str, server: str):
    """Show details for a specific issue."""
    from src.jira.simulated_client import SimulatedJiraClient
    
    with SimulatedJiraClient(base_url=server) as client:
        issue = client.get_issue(key)
        
        if not issue:
            console.print(f"[red]Issue {key} not found[/red]")
            return
        
        console.print(f"\n[bold cyan]{issue['key']}[/bold cyan]")
        console.print(f"Summary: {issue['summary']}")
        console.print(f"Status: {issue['status']}")
        console.print(f"Type: {issue['issue_type']}")
        console.print(f"Priority: {issue['priority']}")
        console.print(f"Assignee: {issue['assignee'] or 'Unassigned'}")
        console.print(f"Reporter: {issue['reporter']}")
        console.print(f"Labels: {', '.join(issue['labels']) or 'None'}")
        console.print(f"Created: {issue['created']}")
        console.print(f"Updated: {issue['updated']}")
        console.print(f"\n[bold]Description:[/bold]")
        console.print(issue['description'])
        
        if issue['comments']:
            console.print(f"\n[bold]Comments ({len(issue['comments'])}):[/bold]")
            for comment in issue['comments']:
                console.print(f"\n[dim]{comment['author']} - {comment['created']}[/dim]")
                console.print(comment['body'][:200] + "..." if len(comment['body']) > 200 else comment['body'])


if __name__ == "__main__":
    cli()
