"""CLI interface for Shard."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from shard.config import load_config, save_config
from shard.models import RunStatus, TaskStatus
from shard.orchestrator import Orchestrator
from shard.state import StateManager

console = Console()

BANNER = """[bold cyan]
   _____ _                   _
  / ____| |                 | |
 | (___ | |__   __ _ _ __ __| |
  \\___ \\| '_ \\ / _` | '__/ _` |
  ____) | | | | (_| | | | (_| |
 |_____/|_| |_|\\__,_|_|  \\__,_|[/bold cyan]
[dim]  TDD-Driven Parallel AI Orchestrator[/dim]
"""


def print_banner() -> None:
    """Print the Shard banner."""
    console.print(BANNER)


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


def print_error(message: str, suggestion: str | None = None) -> None:
    """Print an error in a styled panel."""
    content = f"[bold red]{message}[/bold red]"
    if suggestion:
        content += f"\n\n[dim]{suggestion}[/dim]"
    console.print(Panel(content, title="[red]Error[/red]", border_style="red"))


def print_success(message: str) -> None:
    """Print a success message in a styled panel."""
    console.print(Panel(f"[bold green]{message}[/bold green]", border_style="green"))


def build_dag_tree(graph) -> Tree:
    """Build a Rich Tree visualization of the execution DAG."""
    tree = Tree(
        f"[bold cyan]Execution Plan[/bold cyan] [dim]({graph.run_id})[/dim]",
        guide_style="dim"
    )

    # Build dependency map
    task_nodes = {node.task_id: node for node in graph.nodes}

    # Find root tasks (no dependencies)
    roots = [n for n in graph.nodes if not n.depends_on]

    def add_node(parent_tree: Tree, node) -> None:
        # Status indicator
        status_icons = {
            "PENDING": "[dim]○[/dim]",
            "QUEUED": "[yellow]◐[/yellow]",
            "RUNNING": "[blue]◑[/blue]",
            "COMPLETED": "[green]●[/green]",
            "FAILED": "[red]✗[/red]",
            "TIMED_OUT": "[red]⏱[/red]",
        }
        icon = status_icons.get(node.status.value, "○")

        # Build node label
        deps_str = ""
        if node.depends_on:
            deps_str = f" [dim](← {', '.join(node.depends_on)})[/dim]"

        label = f"{icon} [bold]{node.task_id}[/bold]: {node.title}{deps_str}"
        branch = parent_tree.add(label)

        # Add files
        for f in node.owned_files:
            branch.add(f"[dim]📄 {f}[/dim]")
        for t in node.test_files:
            branch.add(f"[dim]🧪 {t}[/dim]")

    # Add all nodes (flat structure with dependency indicators)
    for node in graph.nodes:
        add_node(tree, node)

    return tree


class RichGroup(click.Group):
    """Custom Click group that shows a banner."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        console.print(BANNER)
        console.print("[bold]Usage:[/bold] shard [OPTIONS] COMMAND [ARGS]...\n")
        console.print("[bold]Commands:[/bold]")

        commands = [
            ("run", "Execute a full pipeline run", "green"),
            ("plan", "Generate DAG without executing", "cyan"),
            ("status", "Show current execution state", "blue"),
            ("resume", "Resume an interrupted run", "yellow"),
            ("logs", "View agent logs for a task", "magenta"),
            ("abort", "Terminate all running agents", "red"),
            ("clean", "Remove artifacts for a run", "dim"),
            ("config", "Show configuration", "dim"),
        ]

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="cyan", width=12)
        table.add_column()

        for cmd, desc, style in commands:
            table.add_row(cmd, f"[{style}]{desc}[/{style}]")

        console.print(table)
        console.print("\n[dim]Run 'shard COMMAND --help' for more info on a command.[/dim]")


@click.group(cls=RichGroup)
@click.version_option(version="1.0.2", prog_name="shard")
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
        print_error(
            "Missing prompt",
            "Provide a prompt with --prompt or --prompt-file"
        )
        sys.exit(1)

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

    print_banner()
    orchestrator = Orchestrator(repo_root, config)

    console.print(f"[bold]Run ID:[/bold] [cyan]{orchestrator.run_id}[/cyan]")
    console.print(f"[bold]Backend:[/bold] {config.agent_backend.value}")
    console.print(f"[bold]Max Agents:[/bold] {config.max_agents}")
    console.print()

    try:
        graph = asyncio.run(orchestrator.run(prompt))
    except KeyboardInterrupt:
        console.print()
        console.print(Panel(
            f"[yellow]Interrupted by user[/yellow]\n\n"
            f"Resume with: [bold]shard resume {orchestrator.run_id}[/bold]",
            title="[yellow]Paused[/yellow]",
            border_style="yellow"
        ))
        sys.exit(130)
    except Exception as e:
        print_error(str(e))
        sys.exit(1)

    if graph.status == RunStatus.COMPLETED:
        # Show summary
        completed = sum(1 for n in graph.nodes if n.status == TaskStatus.COMPLETED)
        total_duration = sum(n.duration_s or 0 for n in graph.nodes)

        console.print()
        print_success(
            f"Pipeline completed!\n\n"
            f"[dim]Tasks:[/dim] {completed}/{len(graph.nodes)}\n"
            f"[dim]Duration:[/dim] {total_duration:.1f}s"
        )
        sys.exit(0)
    else:
        print_error(f"Pipeline finished with status: {graph.status.value}")
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
    print_banner()

    orchestrator = Orchestrator(repo_root, config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        progress.add_task("Planning execution DAG...", total=None)
        try:
            graph = asyncio.run(orchestrator.run(prompt, plan_only=True))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)

    # Display the DAG as a tree
    console.print()
    tree = build_dag_tree(graph)
    console.print(tree)

    # Summary
    console.print()
    console.print(Panel(
        f"[bold]{len(graph.nodes)}[/bold] tasks planned\n"
        f"[dim]DAG saved to[/dim] .shard/graph.json",
        title="[cyan]Plan Complete[/cyan]",
        border_style="cyan"
    ))


@main.command()
@click.argument("run_id")
def resume(run_id: str) -> None:
    """Resume an interrupted run from the last checkpoint."""
    repo_root = get_repo_root()
    config = load_config(repo_root)
    setup_logging(config.log_level, config.log_format)

    print_banner()
    orchestrator = Orchestrator(repo_root, config, run_id=run_id)
    console.print(f"[bold]Resuming:[/bold] [cyan]{run_id}[/cyan]\n")

    try:
        graph = asyncio.run(orchestrator.resume())
    except KeyboardInterrupt:
        console.print()
        console.print(Panel(
            "[yellow]Interrupted by user[/yellow]",
            border_style="yellow"
        ))
        sys.exit(130)
    except FileNotFoundError:
        print_error(
            f"Run '{run_id}' not found",
            "Check .shard/ directory for available runs"
        )
        sys.exit(1)
    except Exception as e:
        print_error(str(e))
        sys.exit(1)

    if graph.status == RunStatus.COMPLETED:
        print_success("Run completed successfully!")
        sys.exit(0)
    else:
        print_error(f"Run finished with status: {graph.status.value}")
        sys.exit(1)


@main.command()
@click.argument("run_id", required=False)
def status(run_id: str | None) -> None:
    """Show the current state of the execution graph."""
    repo_root = get_repo_root()
    state_dir = repo_root / ".shard"

    if not state_dir.exists():
        print_error("No Shard state found", "Run 'shard run' to start a pipeline")
        return

    graph_path = state_dir / "graph.json"
    if not graph_path.exists():
        print_error("No execution graph found")
        return

    from shard.models import ExecutionGraph
    with open(graph_path) as f:
        data = json.load(f)
    graph = ExecutionGraph.from_dict(data)

    if run_id and graph.run_id != run_id:
        print_error(f"Run {run_id} not found", f"Current run: {graph.run_id}")
        return

    # Status colors and icons
    status_config = {
        "PLANNING": ("blue", "📝"),
        "PARTITIONING": ("cyan", "🔀"),
        "DISPATCHING": ("yellow", "🚀"),
        "MERGING": ("magenta", "🔗"),
        "TESTING": ("blue", "🧪"),
        "HEALING": ("magenta", "🩹"),
        "COMPLETED": ("green", "✓"),
        "FAILED": ("red", "✗"),
        "INTERRUPTED": ("yellow", "⏸"),
        "ABORTED": ("red", "⛔"),
    }

    color, icon = status_config.get(graph.status.value, ("white", "?"))

    # Header panel
    header = Text()
    header.append(f"{icon} ", style=color)
    header.append(graph.status.value, style=f"bold {color}")

    console.print(Panel(
        f"[bold]Run:[/bold] [cyan]{graph.run_id}[/cyan]\n"
        f"[bold]Status:[/bold] [{color}]{icon} {graph.status.value}[/{color}]\n"
        f"[bold]Target:[/bold] {graph.target_branch}\n"
        f"[bold]Retries:[/bold] {graph.global_retry_count}/{graph.config.max_retries_global}",
        title="[bold]Shard Status[/bold]",
        border_style="cyan"
    ))

    console.print()

    # Task table
    table = Table(title="Tasks", show_lines=True)
    table.add_column("Task", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Status", justify="center")
    table.add_column("Duration", justify="right")
    table.add_column("Retries", justify="center")

    task_status_icons = {
        "PENDING": "[dim]○ PENDING[/dim]",
        "QUEUED": "[yellow]◐ QUEUED[/yellow]",
        "RUNNING": "[blue]◑ RUNNING[/blue]",
        "COMPLETED": "[green]● DONE[/green]",
        "FAILED": "[red]✗ FAILED[/red]",
        "TIMED_OUT": "[red]⏱ TIMEOUT[/red]",
        "HEALING": "[magenta]🩹 HEALING[/magenta]",
        "INTERRUPTED": "[yellow]⏸ PAUSED[/yellow]",
    }

    for node in graph.nodes:
        status_display = task_status_icons.get(node.status.value, node.status.value)
        duration = f"{node.duration_s:.1f}s" if node.duration_s else "-"
        table.add_row(
            node.task_id,
            node.title,
            status_display,
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

    found = False

    if stdout_path.exists():
        found = True
        content = stdout_path.read_text()
        console.print(Panel(
            Syntax(content, "text", theme="monokai", line_numbers=True) if content else "[dim]Empty[/dim]",
            title=f"[green]stdout[/green] [dim]({stdout_path})[/dim]",
            border_style="green"
        ))

    if stderr_path.exists():
        found = True
        content = stderr_path.read_text()
        if content:
            console.print(Panel(
                Syntax(content, "text", theme="monokai", line_numbers=True),
                title=f"[red]stderr[/red] [dim]({stderr_path})[/dim]",
                border_style="red"
            ))

    if not found:
        print_error(f"No logs found for task {task_id}")


@main.command()
@click.argument("run_id")
def abort(run_id: str) -> None:
    """Terminate all running agents and mark the run as ABORTED."""
    repo_root = get_repo_root()
    config = load_config(repo_root)

    orchestrator = Orchestrator(repo_root, config, run_id=run_id)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        progress.add_task("Aborting run...", total=None)
        asyncio.run(orchestrator.abort())

    console.print(Panel(
        f"Run [bold]{run_id}[/bold] aborted\n\n"
        "[dim]Worktrees and branches preserved for inspection[/dim]",
        title="[yellow]Aborted[/yellow]",
        border_style="yellow"
    ))


@main.command()
@click.argument("run_id")
def clean(run_id: str) -> None:
    """Remove all worktrees, branches, and artifacts for a run."""
    repo_root = get_repo_root()
    config = load_config(repo_root)

    orchestrator = Orchestrator(repo_root, config, run_id=run_id)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        progress.add_task("Cleaning up artifacts...", total=None)
        asyncio.run(orchestrator.clean())

    print_success(f"Cleaned up run {run_id}")


@main.command("config")
@click.option("--init", is_flag=True, help="Create a default shard.toml file.")
def show_config(init: bool) -> None:
    """Show or initialize the shard.toml configuration."""
    repo_root = get_repo_root()
    config_path = repo_root / "shard.toml"

    if init:
        if config_path.exists():
            print_error("shard.toml already exists")
            return
        config = load_config(repo_root)
        save_config(repo_root, config)
        print_success(f"Created {config_path}")
        return

    config = load_config(repo_root)

    table = Table(title="[bold]Shard Configuration[/bold]", show_lines=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    # Group settings
    groups = {
        "Agent": ["agent_backend", "agent_binary", "max_agents", "stagger_delay_s"],
        "Timeouts": ["per_task_timeout_s", "global_timeout_s", "output_stall_s", "commit_stall_s"],
        "Retries": ["max_retries_per_task", "max_retries_global"],
        "Cost": ["max_cost_usd", "warn_cost_usd"],
        "Git": ["worktree_dir", "branch_prefix", "auto_cleanup"],
        "Test": ["test_runner", "test_args", "test_json_report"],
        "Logging": ["log_level", "log_format", "archive_agent_logs"],
    }

    config_dict = config.to_dict()

    for group_name, keys in groups.items():
        table.add_row(f"[bold]{group_name}[/bold]", "", end_section=False)
        for key in keys:
            if key in config_dict:
                value = config_dict[key]
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                table.add_row(f"  {key}", str(value))

    console.print(table)

    if not config_path.exists():
        console.print()
        console.print("[dim]No shard.toml found. Using defaults.[/dim]")
        console.print("[dim]Run 'shard config --init' to create one.[/dim]")


if __name__ == "__main__":
    main()
