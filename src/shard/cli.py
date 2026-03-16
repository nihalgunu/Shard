"""CLI interface for Shard."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from shard.config import load_config, save_config
from shard.models import RunStatus, TaskStatus
from shard.orchestrator import Orchestrator
from shard.state import StateManager

console = Console()


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    if fmt == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                '{"time":"%(asctime)s","level":"%(levelname)s",'
                '"logger":"%(name)s","message":"%(message)s"}'
            )
        )
        logging.basicConfig(level=log_level, handlers=[handler])
    else:
        logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def get_repo_root() -> Path:
    """Find the git repository root."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    raise click.ClickException("Not inside a git repository")


@click.group()
@click.version_option(version="1.0.1", prog_name="shard")
def main() -> None:
    """Shard: A TDD-Driven, Parallelized AI Coding Orchestrator."""
    pass


@main.command()
@click.option("--prompt", "-p", help="Natural language prompt describing the task.")
@click.option("--prompt-file", "-f", type=click.Path(exists=True), help="Read prompt from file.")
@click.option("--agents", "-n", type=int, default=None, help="Max concurrent agents (default: 4).")
@click.option("--backend", type=click.Choice(["claude-code", "aider", "codex", "gemini", "cursor-cli", "custom"]),
              default=None, help="Agent backend to use.")
@click.option("--timeout", type=int, default=None, help="Global timeout in seconds.")
@click.option("--max-cost", type=float, default=None, help="Maximum cost in USD.")
def run(prompt: str | None, prompt_file: str | None, agents: int | None,
        backend: str | None, timeout: int | None,
        max_cost: float | None) -> None:
    """Execute a full pipeline run."""
    if not prompt and not prompt_file:
        raise click.ClickException("Either --prompt or --prompt-file is required")

    if prompt_file:
        with open(prompt_file) as f:
            prompt = f.read()

    assert prompt is not None

    repo_root = get_repo_root()
    config = load_config(repo_root)

    # Apply CLI overrides
    if agents is not None:
        config.max_agents = agents
    if backend is not None:
        from shard.models import AgentBackend
        config.agent_backend = AgentBackend(backend)
    if timeout is not None:
        config.global_timeout_s = timeout
    if max_cost is not None:
        config.max_cost_usd = max_cost

    setup_logging(config.log_level, config.log_format)

    orchestrator = Orchestrator(repo_root, config)
    console.print(f"[bold]Shard[/bold] starting run [cyan]{orchestrator.run_id}[/cyan]")

    try:
        graph = asyncio.run(orchestrator.run(prompt))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Run 'shard resume' to continue.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if graph.status == RunStatus.COMPLETED:
        console.print("[bold green]Pipeline completed successfully![/bold green]")
        sys.exit(0)
    else:
        console.print(f"[bold red]Pipeline finished with status: {graph.status.value}[/bold red]")
        sys.exit(1)


@main.command()
@click.option("--prompt", "-p", required=True, help="Natural language prompt.")
@click.option("--agents", "-n", type=int, default=None, help="Max concurrent agents.")
def plan(prompt: str, agents: int | None) -> None:
    """Run only Stage 1: produce the DAG and test scaffold without executing."""
    repo_root = get_repo_root()
    config = load_config(repo_root)
    if agents is not None:
        config.max_agents = agents

    setup_logging(config.log_level, config.log_format)

    orchestrator = Orchestrator(repo_root, config)

    try:
        graph = asyncio.run(orchestrator.run(prompt, plan_only=True))
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Display the DAG
    table = Table(title=f"Execution Plan: {graph.run_id}")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Depends On")
    table.add_column("Owned Files")
    table.add_column("Test Files")
    table.add_column("Priority", justify="right")

    for node in graph.nodes:
        table.add_row(
            node.task_id,
            node.title,
            ", ".join(node.depends_on) or "-",
            ", ".join(node.owned_files),
            ", ".join(node.test_files),
            str(node.priority),
        )

    console.print(table)
    console.print(f"\nDAG saved to .shard/graph.json")


@main.command()
@click.argument("run_id")
def resume(run_id: str) -> None:
    """Resume an interrupted run from the last checkpoint."""
    repo_root = get_repo_root()
    config = load_config(repo_root)
    setup_logging(config.log_level, config.log_format)

    orchestrator = Orchestrator(repo_root, config, run_id=run_id)
    console.print(f"[bold]Resuming run[/bold] [cyan]{run_id}[/cyan]")

    try:
        graph = asyncio.run(orchestrator.resume())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(130)
    except FileNotFoundError:
        console.print(f"[bold red]Error:[/bold red] Run '{run_id}' not found. Check .shard/ directory.")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if graph.status == RunStatus.COMPLETED:
        console.print("[bold green]Run completed successfully![/bold green]")
        sys.exit(0)
    else:
        console.print(f"[bold red]Run finished with status: {graph.status.value}[/bold red]")
        sys.exit(1)


@main.command()
@click.argument("run_id", required=False)
def status(run_id: str | None) -> None:
    """Show the current state of the execution graph."""
    repo_root = get_repo_root()
    state_dir = repo_root / ".shard"

    if not state_dir.exists():
        console.print("[yellow]No Shard state found in this repository.[/yellow]")
        return

    graph_path = state_dir / "graph.json"
    if not graph_path.exists():
        console.print("[yellow]No execution graph found.[/yellow]")
        return

    from shard.models import ExecutionGraph
    with open(graph_path) as f:
        data = json.load(f)
    graph = ExecutionGraph.from_dict(data)

    if run_id and graph.run_id != run_id:
        console.print(f"[yellow]Run {run_id} not found. Current run: {graph.run_id}[/yellow]")
        return

    # Status colors
    status_styles = {
        "COMPLETED": "green",
        "FAILED": "red",
        "RUNNING": "blue",
        "QUEUED": "yellow",
        "PENDING": "dim",
        "TIMED_OUT": "red",
        "HEALING": "magenta",
        "INTERRUPTED": "yellow",
    }

    console.print(f"\n[bold]Run:[/bold] {graph.run_id}")
    console.print(f"[bold]Status:[/bold] [{status_styles.get(graph.status.value, 'white')}]{graph.status.value}[/{status_styles.get(graph.status.value, 'white')}]")
    console.print(f"[bold]Target:[/bold] {graph.target_branch}")
    console.print(f"[bold]Retries:[/bold] {graph.global_retry_count}/{graph.config.max_retries_global}")
    console.print()

    table = Table(title="Tasks")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Retries", justify="right")

    for node in graph.nodes:
        style = status_styles.get(node.status.value, "white")
        duration = f"{node.duration_s:.1f}s" if node.duration_s else "-"
        table.add_row(
            node.task_id,
            node.title,
            f"[{style}]{node.status.value}[/{style}]",
            duration,
            f"{node.retry_count}/{graph.config.max_retries_per_task}",
        )

    console.print(table)


@main.command()
@click.argument("task_id")
def logs(task_id: str) -> None:
    """Tail the stdout/stderr logs for a specific agent."""
    repo_root = get_repo_root()
    state = StateManager(repo_root, "")
    stdout_path, stderr_path = state.get_log_paths(task_id)

    if stdout_path.exists():
        console.print(f"[bold]stdout ({stdout_path}):[/bold]")
        console.print(stdout_path.read_text())

    if stderr_path.exists():
        console.print(f"\n[bold]stderr ({stderr_path}):[/bold]")
        console.print(stderr_path.read_text())

    if not stdout_path.exists() and not stderr_path.exists():
        console.print(f"[yellow]No logs found for task {task_id}[/yellow]")


@main.command()
@click.argument("run_id")
def abort(run_id: str) -> None:
    """Terminate all running agents and mark the run as ABORTED."""
    repo_root = get_repo_root()
    config = load_config(repo_root)

    orchestrator = Orchestrator(repo_root, config, run_id=run_id)
    asyncio.run(orchestrator.abort())
    console.print(f"[bold yellow]Run {run_id} aborted.[/bold yellow]")
    console.print("Worktrees and branches preserved for manual inspection.")


@main.command()
@click.argument("run_id")
def clean(run_id: str) -> None:
    """Remove all worktrees, branches, and artifacts for a run."""
    repo_root = get_repo_root()
    config = load_config(repo_root)

    orchestrator = Orchestrator(repo_root, config, run_id=run_id)
    asyncio.run(orchestrator.clean())
    console.print(f"[bold green]Cleaned up run {run_id}[/bold green]")


@main.command("config")
def show_config() -> None:
    """Show or edit the shard.toml configuration."""
    repo_root = get_repo_root()
    config = load_config(repo_root)

    table = Table(title="Shard Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    for key, value in config.to_dict().items():
        table.add_row(key, str(value))

    console.print(table)

    config_path = repo_root / "shard.toml"
    if not config_path.exists():
        console.print(f"\n[dim]No shard.toml found. Using defaults. Run 'shard config --init' to create one.[/dim]")


if __name__ == "__main__":
    main()
